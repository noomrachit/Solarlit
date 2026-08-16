"""
ระบบถ่ายทอดเสียงทางเดียว (one-way live audio relay)
ห้องหลัก (source) -> ห้องย่อยหลายห้อง (targets) แบบเรียลไทม์

สถาปัตยกรรม: บอท 1 + N ตัวในโปรเซสเดียวกัน
- listener_bot : เข้าห้องหลัก ดักจับเสียงทุกคนที่พูด (ใช้ discord-ext-voice-recv)
- speaker_bots : บอทพูด N ตัว แต่ละตัวเข้าห้องย่อย 1 ห้อง เล่นเสียงที่ mix แล้ว
                 (จำนวนห้องย่อยพร้อมกันสูงสุด = จำนวนบอทพูดที่ตั้งค่าไว้)

Environment variables ที่ต้องตั้ง:
  LISTENER_BOT_TOKEN     = token บอทฟัง (หัวหน้า)
  SPEAKER_BOT_TOKEN_1    = token บอทพูดตัวที่ 1 (ลูกน้อง)
  SPEAKER_BOT_TOKEN_2    = token บอทพูดตัวที่ 2 (ลูกน้อง 1)
  SPEAKER_BOT_TOKEN_3    = token บอทพูดตัวที่ 3 (ลูกน้อง 2)
  ... เพิ่มได้เรื่อยๆ ตามจำนวนห้องฟังสูงสุดที่ต้องการรองรับพร้อมกัน

ข้อจำกัดที่ทราบอยู่แล้ว (ไม่ใช่บั๊ก แต่เป็นข้อจำกัดของสถาปัตยกรรมนี้):
- หน่วงเวลาประมาณ 0.3-0.8 วินาที (รับ -> mix -> เข้ารหัส -> ส่ง -> เล่น)
- เป็นเสียงทางเดียวเท่านั้น ห้องย่อยพูดกลับห้องหลักไม่ได้
- ต้อง invite บอททุกตัวเข้าเซิร์ฟเวอร์เดียวกัน (คนละ token คนละแอป)
- ต้องมี libopus ติดตั้งในระบบ (ดู nixpacks.toml)
- จำนวนห้องย่อยที่กระจายพร้อมกันได้ ถูกจำกัดด้วยจำนวนบอทพูดที่ตั้งค่าไว้เท่านั้น
"""

import os
import asyncio
import logging
import struct
import time
from collections import defaultdict
from contextlib import AsyncExitStack
from typing import Union

import numpy as np

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import voice_recv
from dotenv import load_dotenv
from aiohttp import web

import access as billing_access

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("voice-relay")

LISTENER_TOKEN = os.getenv("LISTENER_BOT_TOKEN")

SPEAKER_TOKENS = []
_i = 1
while True:
    _t = os.getenv(f"SPEAKER_BOT_TOKEN_{_i}")
    if not _t:
        break
    SPEAKER_TOKENS.append(_t)
    _i += 1

if not LISTENER_TOKEN:
    raise RuntimeError("ต้องตั้งค่า LISTENER_BOT_TOKEN")
if not SPEAKER_TOKENS:
    raise RuntimeError("ต้องตั้งค่าอย่างน้อย SPEAKER_BOT_TOKEN_1 หนึ่งตัว")

log.info(f"พบบอทพูดทั้งหมด {len(SPEAKER_TOKENS)} ตัว (รองรับห้องฟังพร้อมกันได้สูงสุด {len(SPEAKER_TOKENS)} ห้อง)")


def _patch_voice_recv_resilience():
    """
    แพตช์ไลบรารี discord-ext-voice-recv ให้ทนต่อ error 'corrupted stream' จาก opus decode
    ปกติเกิดเวลามีคนพูดพร้อมกันหลายคน/แพ็กเก็ตขาดหาย/decoder เพิ่งถูกสร้างสำหรับคนพูดใหม่
    ถ้าไม่แพตช์ error ตัวเดียวจะทำให้ thread รับเสียงทั้งหมดตาย ฟังเสียงต่อไม่ได้เลยทั้ง session
    หลังแพตช์: ข้าม packet ที่ decode ไม่ได้ทิ้งไปเฉยๆ (เสียงสะดุดแป๊บเดียว) แทนที่จะล่มทั้งระบบ
    """
    try:
        from discord.ext.voice_recv import opus as vr_opus
    except Exception as e:
        log.warning(f"ไม่พบโมดูล voice_recv.opus สำหรับแพตช์ (ข้ามได้ ไม่ critical): {e}")
        vr_opus = None

    if vr_opus is not None:
        decoder_cls = getattr(vr_opus, "PacketDecoder", None)
        if decoder_cls is not None and hasattr(decoder_cls, "_process_packet"):
            original_process = decoder_cls._process_packet

            def _safe_process_packet(self, packet, *args, **kwargs):
                try:
                    return original_process(self, packet, *args, **kwargs)
                except Exception as e:
                    log.warning(f"ข้าม packet เสียงที่ decode ไม่ได้ (ไม่ล่มทั้งระบบ): {e}")
                    return None

            decoder_cls._process_packet = _safe_process_packet
            log.info("แพตช์ PacketDecoder._process_packet สำเร็จ")
        else:
            log.warning("ไม่พบ PacketDecoder._process_packet สำหรับแพตช์ (โครงสร้างไลบรารีอาจเปลี่ยน)")

    try:
        from discord.ext.voice_recv import router as vr_router
    except Exception as e:
        log.warning(f"ไม่พบโมดูล voice_recv.router สำหรับแพตช์ (ข้ามได้ ไม่ critical): {e}")
        vr_router = None

    if vr_router is not None:
        router_cls = getattr(vr_router, "PacketRouter", None)
        if router_cls is not None and hasattr(router_cls, "_do_run"):
            original_do_run = router_cls._do_run

            def _safe_do_run(self, *args, **kwargs):
                try:
                    return original_do_run(self, *args, **kwargs)
                except Exception as e:
                    log.warning(f"ข้าม error ใน packet router loop (ไม่ล่มทั้ง thread): {e}")
                    return None

            router_cls._do_run = _safe_do_run
            log.info("แพตช์ PacketRouter._do_run สำเร็จ")
        else:
            log.warning("ไม่พบ PacketRouter._do_run สำหรับแพตช์ (โครงสร้างไลบรารีอาจเปลี่ยน)")


_patch_voice_recv_resilience()

FRAME_BYTES = 3840  # เฟรมเสียง 20ms ที่ 48kHz, 16-bit, stereo (มาตรฐานของ Discord voice)

relay_active = False
# คิวเสียงแยกตามบอทพูดแต่ละตัว (index ตรงกับ speaker_bots)
speaker_queues: list = [asyncio.Queue(maxsize=50) for _ in SPEAKER_TOKENS]
# index ของบอทพูดที่กำลังใช้งานอยู่ (เชื่อมต่อห้องย่อยอยู่)
active_speaker_indices: set = set()
# เก็บว่า index ไหนกำลังเล่นในช่องไหน (ไว้โชว์ /relay status)
speaker_channel_map: dict = {}

# ── บอทตัวที่ 1: ฟังเสียงในห้องหลัก ──
intents_listener = discord.Intents.default()
intents_listener.voice_states = True
intents_listener.guilds = True
listener_bot = commands.Bot(command_prefix="!", intents=intents_listener)
tree = listener_bot.tree


@tree.check
async def global_billing_check(interaction: discord.Interaction) -> bool:
    """เช็คสิทธิ์สมาชิกก่อนทุกคำสั่ง /relay (ยกเว้นเซิร์ฟเวอร์ที่อยู่ใน EXEMPT_GUILD_IDS)"""
    if not interaction.guild:
        return True
    allowed, reason = await billing_access.check_guild_access(interaction.guild.id)
    if not allowed:
        try:
            await interaction.response.send_message(reason, ephemeral=True)
        except Exception:
            pass
        return False
    return True


# ── บอทพูด N ตัว ──
speaker_bots: list = []
for _ in SPEAKER_TOKENS:
    intents_speaker = discord.Intents.default()
    intents_speaker.voice_states = True
    intents_speaker.guilds = True
    speaker_bots.append(commands.Bot(command_prefix="!", intents=intents_speaker))


# ─────────────────────────────────────────────
# ตัวผสมเสียง (Mixer): รวมเสียงทุกคนที่พูดพร้อมกันในห้องหลัก
# ให้กลายเป็นเฟรมเดียว ก่อนกระจายไปทุกบอทพูดที่กำลังทำงานอยู่
# ─────────────────────────────────────────────
class Mixer:
    def __init__(self):
        self.buffers: dict = defaultdict(bytearray)

    def feed(self, user_id: int, pcm: bytes):
        self.buffers[user_id].extend(pcm)

    def pop_frame(self) -> bytes:
        mixed = None
        for uid, buf in list(self.buffers.items()):
            if len(buf) >= FRAME_BYTES:
                chunk = bytes(buf[:FRAME_BYTES])
                del buf[:FRAME_BYTES]
                # ใช้ numpy แทน struct/list loop เดิม เร็วกว่ามาก ลดโอกาสจังหวะเฟรมเพี้ยนจน CPU ตามไม่ทัน
                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.int32)
                mixed = samples if mixed is None else mixed + samples
            if len(buf) == 0:
                del self.buffers[uid]
        if mixed is None:
            return b"\x00" * FRAME_BYTES
        # clip กันเสียงล้น (hard clipping) ตอนมีคนพูดพร้อมกันหลายคนเสียงดังรวมกัน
        mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
        return mixed.tobytes()


mixer = Mixer()


class RelaySink(voice_recv.AudioSink):
    """
    รับเสียง PCM ที่ decode แล้วจากทุกคนในห้องหลัก แล้วป้อนเข้า mixer
    มีระบบ "warm-up mute" — ตอนมีคนเริ่มพูดใหม่ (decoder ตัวใหม่ถูกสร้าง)
    ช่วงแรกๆ มักเจอ error decode เสียงพัง (corrupted stream) เพราะ decoder ยังไม่เสถียร
    เลยเงียบเสียงคนนั้นไปก่อนสั้นๆ (WARMUP_SECONDS) รอให้ decoder นิ่งก่อนค่อยปล่อยเสียงเข้าระบบจริง
    ผลคือผู้ฟังได้ยิน "เงียบสั้นๆ" แทน "เสียงแตก" ตอนมีคนเริ่มพูด
    """

    WARMUP_SECONDS = 0.25
    SILENCE_RESET_SECONDS = 1.5  # เงียบเกินนี้ = ถือว่าเริ่มพูดใหม่ (decoder ตัวใหม่ถูกสร้างอีกรอบ)

    def __init__(self):
        self._first_seen: dict = {}   # user_id -> เวลาที่เริ่มเห็น packet แรกของ "รอบพูด" นี้
        self._last_seen: dict = {}    # user_id -> เวลาที่เห็น packet ล่าสุด (ไว้ตรวจจับช่วงเงียบ)

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        if user is None or user.bot:
            return

        now = time.monotonic()
        last_seen = self._last_seen.get(user.id)
        first_seen = self._first_seen.get(user.id)

        # เงียบไปนานเกินไป (หรือพูดครั้งแรก) = เริ่มรอบ warm-up ใหม่
        if first_seen is None or last_seen is None or (now - last_seen) > self.SILENCE_RESET_SECONDS:
            self._first_seen[user.id] = now
            self._last_seen[user.id] = now
            return  # เฟรมแรกของรอบใหม่ ข้ามไปเลย ไม่ป้อนเข้า mixer

        self._last_seen[user.id] = now

        if now - first_seen < self.WARMUP_SECONDS:
            return  # ยังอยู่ในช่วง warm-up เงียบไว้ก่อน กัน corrupted stream หลุดออกไปเป็นเสียงแตก

        mixer.feed(user.id, data.pcm)

    def cleanup(self):
        self._first_seen.clear()
        self._last_seen.clear()


class QueueAudioSource(discord.AudioSource):
    """ดึงเฟรมที่ mix แล้วจาก queue เฉพาะของบอทพูดตัวนั้นๆ"""

    def __init__(self, queue: asyncio.Queue):
        self.queue = queue

    def read(self) -> bytes:
        try:
            return self.queue.get_nowait()
        except asyncio.QueueEmpty:
            return b"\x00" * FRAME_BYTES

    def is_opus(self) -> bool:
        return False


async def mixer_pump():
    """
    วน mix เฟรมทุก 20ms แล้วกระจาย (broadcast) เข้า queue ของทุกบอทพูดที่กำลังทำงานอยู่
    ใช้ timer แบบอิงเวลาสัมบูรณ์ (next_tick) แทนการ sleep(0.02) ตรงๆ
    เพราะการ sleep ตรงๆ จะสะสมความคลาดเคลื่อน (drift) ไปเรื่อยๆ เมื่อมี jitter จาก CPU/GC
    ทำให้จังหวะเฟรมเสียงค่อยๆ เพี้ยนไปจนได้ยินเป็นเสียงแตก/สะดุด
    """
    loop = asyncio.get_running_loop()
    FRAME_INTERVAL = 0.02
    next_tick = loop.time()

    while True:
        next_tick += FRAME_INTERVAL
        delay = next_tick - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            # ตกจังหวะไปมาก (เช่นเครื่องช้าตอนนั้น) รีเซ็ต baseline กันสะสม drift ยาวๆ ต่อเนื่อง
            next_tick = loop.time()

        if not relay_active or not active_speaker_indices:
            continue
        frame = mixer.pop_frame()
        for idx in list(active_speaker_indices):
            q = speaker_queues[idx]
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # ทิ้งเฟรมเก่าสุด กันดีเลย์สะสม
                except asyncio.QueueEmpty:
                    pass
                q.put_nowait(frame)


def has_relay_perms():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        # interaction.user ที่ Discord ส่งมาตอนกดคำสั่งในเซิร์ฟเวอร์ เป็น Member ที่มีสิทธิ์ครบอยู่แล้ว
        # ไม่ต้องพึ่ง guild.get_member() ซึ่งต้องมี Members Intent + cache ถึงจะเจอ
        member = interaction.user
        if not isinstance(member, discord.Member):
            member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            return False
        perms = member.guild_permissions
        return perms.manage_channels or perms.administrator
    return app_commands.check(predicate)


# ─────────────────────────────────────────────
# ระบบผูกบอทกับ "ห้อง" ให้เข้า/ออกอัตโนมัติตามความเคลื่อนไหวของห้อง
# เข้าเมื่อมีคนแรกเข้าห้อง (ห้องว่าง -> มีคน) / ออกเมื่อห้องว่าง (คนสุดท้ายออก)
# ไม่ผูกกับคนใดคนหนึ่งอีกต่อไป
# ─────────────────────────────────────────────
listener_binding: dict = {"channel_id": None}
speaker_bindings: dict = {}  # index -> {"channel_id": ...}


def _count_humans(channel) -> int:
    """นับจำนวนคนที่ไม่ใช่บอทในห้องเสียง/สเตจ"""
    if channel is None:
        return 0
    return len([m for m in channel.members if not m.bot])


async def start_listening(channel: Union[discord.VoiceChannel, discord.StageChannel]):
    """เริ่มให้บอทฟังเข้าห้องหลักและดักจับเสียง (ใช้ได้ทั้งเรียกเองผ่านคำสั่ง และเรียกอัตโนมัติ)"""
    global relay_active
    if relay_active:
        return
    listener_vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
    listener_vc.listen(RelaySink())
    relay_active = True
    log.info(f"[Listener] เข้าห้อง {channel.name} แล้ว")


async def stop_listening():
    """ให้บอทฟังออกจากห้องหลัก"""
    global relay_active
    if not relay_active:
        return
    relay_active = False
    for guild in listener_bot.guilds:
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)
    log.info("[Listener] ออกจากห้องแล้ว")


async def start_speaking(index: int, channel: discord.VoiceChannel):
    """เริ่มให้บอทพูดตัวที่ index เข้าห้องย่อยและเล่นเสียง"""
    if index in active_speaker_indices:
        return

    # เช็คโควต้าจำนวนบอทตามแพ็กเกจของเซิร์ฟเวอร์นี้ ก่อนเปิดบอทพูดตัวใหม่
    # นับรวมบอทฟัง (ถ้ากำลังทำงานอยู่) + บอทพูดที่ใช้อยู่แล้ว
    limit = await billing_access.get_relay_bot_limit(channel.guild.id)
    current_bots_in_use = (1 if relay_active else 0) + len(active_speaker_indices)
    if current_bots_in_use + 1 > limit:
        log.warning(
            f"[Speaker {index + 1}] ปฏิเสธการเข้าห้อง {channel.name}: "
            f"เกินโควต้าแพ็กเกจของเซิร์ฟเวอร์ (ใช้อยู่ {current_bots_in_use}/{limit} บอท) "
            f"— อัปเกรดแพ็กเกจเพื่อเพิ่มจำนวนห้องที่ถ่ายทอดพร้อมกันได้"
        )
        return

    speaker_bot = speaker_bots[index]
    target_guild = speaker_bot.get_guild(channel.guild.id)
    target_channel = target_guild.get_channel(channel.id) if target_guild else None
    if target_channel is None:
        log.error(f"[Speaker {index + 1}] มองไม่เห็นห้อง {channel.name} (ยัง invite บอทเข้าเซิร์ฟเวอร์หรือยัง?)")
        return
    vc = await target_channel.connect()
    vc.play(QueueAudioSource(speaker_queues[index]))
    active_speaker_indices.add(index)
    speaker_channel_map[index] = channel.id
    log.info(f"[Speaker {index + 1}] เข้าห้อง {channel.name} แล้ว")


async def stop_speaking(index: int):
    """ให้บอทพูดตัวที่ index ออกจากห้องย่อย"""
    if index not in active_speaker_indices:
        return
    speaker_bot = speaker_bots[index]
    for guild in speaker_bot.guilds:
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)
    active_speaker_indices.discard(index)
    speaker_channel_map.pop(index, None)
    while not speaker_queues[index].empty():
        try:
            speaker_queues[index].get_nowait()
        except asyncio.QueueEmpty:
            break
    log.info(f"[Speaker {index + 1}] ออกจากห้องแล้ว")


# ─────────────────────────────────────────────
# Slash Commands (ลงทะเบียนบนบอทฟัง/หัวหน้าตัวเดียว)
# ─────────────────────────────────────────────

relay_group = app_commands.Group(name="relay", description="ถ่ายทอดเสียงสดจากห้องหลักไปห้องย่อยหลายห้อง (ทางเดียว)")


@relay_group.command(name="start", description="เริ่มฟังเสียงจากห้องหลัก (ยังไม่กระจายไปไหนจนกว่าจะ /relay addtarget)")
@has_relay_perms()
@app_commands.describe(source="ห้องหลัก (Voice หรือ Stage Channel — แนะนำ Stage Channel เพราะไม่ติดปัญหาเข้ารหัส DAVE)")
async def relay_start(interaction: discord.Interaction, source: Union[discord.VoiceChannel, discord.StageChannel]):
    await interaction.response.defer(ephemeral=True)

    if relay_active:
        return await interaction.followup.send("⚠️ กำลังถ่ายทอดอยู่แล้ว ใช้ `/relay stop` ก่อนเริ่มใหม่", ephemeral=True)

    try:
        await start_listening(source)
    except Exception as e:
        return await interaction.followup.send(f"❌ บอทฟังเชื่อมต่อห้องหลักไม่สำเร็จ: {e}", ephemeral=True)

    await interaction.followup.send(
        f"🎧 เริ่มฟังห้อง {source.mention} แล้ว\n"
        f"ใช้ `/relay addtarget` เพื่อเพิ่มห้องย่อยที่จะกระจายเสียงไป (รองรับสูงสุด {len(SPEAKER_TOKENS)} ห้องพร้อมกัน)",
        ephemeral=True
    )


@relay_group.command(name="addtarget", description="เพิ่มห้องย่อยที่จะกระจายเสียงไป (ใช้บอทพูดตัวถัดไปที่ว่าง)")
@has_relay_perms()
@app_commands.describe(channel="ห้องย่อยที่จะเล่นเสียงถ่ายทอด")
async def relay_addtarget(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if not relay_active:
        return await interaction.response.send_message("❌ ยังไม่ได้ `/relay start` เริ่มฟังห้องหลักก่อน", ephemeral=True)

    free_index = None
    for i in range(len(speaker_bots)):
        if i not in active_speaker_indices:
            free_index = i
            break

    if free_index is None:
        return await interaction.response.send_message(
            f"❌ บอทพูดไม่พอ (มีทั้งหมด {len(speaker_bots)} ตัว ใช้ครบทุกตัวแล้ว) "
            f"ใช้ `/relay removetarget` เพื่อเลิกใช้ห้องเดิมก่อน หรือเพิ่ม token บอทพูดตัวใหม่",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    try:
        await start_speaking(free_index, channel)
    except Exception as e:
        return await interaction.followup.send(f"❌ บอทพูดตัวที่ {free_index + 1} เชื่อมต่อห้องไม่สำเร็จ: {e}", ephemeral=True)

    if free_index not in active_speaker_indices:
        limit = await billing_access.get_relay_bot_limit(interaction.guild.id)
        current_bots_in_use = (1 if relay_active else 0) + len(active_speaker_indices)
        if current_bots_in_use >= limit:
            return await interaction.followup.send(
                f"❌ ใช้บอทครบตามโควต้าแพ็กเกจแล้ว ({current_bots_in_use}/{limit} บอท) "
                f"อัปเกรดแพ็กเกจเพื่อถ่ายทอดได้หลายห้องขึ้นได้ที่เว็บไซต์",
                ephemeral=True
            )
        return await interaction.followup.send(
            f"❌ บอทพูดตัวที่ {free_index + 1} ยังไม่ได้ invite เข้าเซิร์ฟเวอร์นี้ (หรือมองไม่เห็นห้องนี้)",
            ephemeral=True
        )

    await interaction.followup.send(
        f"🔊 เพิ่ม {channel.mention} เป็นห้องฟังแล้ว (บอทพูดตัวที่ {free_index + 1}/{len(speaker_bots)})",
        ephemeral=True
    )


@relay_group.command(name="removetarget", description="เลิกกระจายเสียงไปห้องที่ระบุ")
@has_relay_perms()
@app_commands.describe(channel="ห้องย่อยที่ต้องการเลิกกระจายเสียงไป")
async def relay_removetarget(interaction: discord.Interaction, channel: discord.VoiceChannel):
    target_index = None
    for idx, ch_id in speaker_channel_map.items():
        if ch_id == channel.id:
            target_index = idx
            break

    if target_index is None:
        return await interaction.response.send_message("ห้องนี้ไม่ได้อยู่ในรายการกระจายเสียงอยู่", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    await stop_speaking(target_index)
    await interaction.followup.send(f"🔇 เลิกกระจายเสียงไป {channel.mention} แล้ว", ephemeral=True)


@relay_group.command(name="stop", description="หยุดถ่ายทอดเสียงทั้งหมด (ทุกห้อง)")
@has_relay_perms()
async def relay_stop(interaction: discord.Interaction):
    if not relay_active:
        return await interaction.response.send_message("ตอนนี้ไม่มีการถ่ายทอดเสียงทำงานอยู่", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    await stop_listening()
    for idx in list(active_speaker_indices):
        await stop_speaking(idx)

    await interaction.followup.send("🛑 หยุดถ่ายทอดเสียงทั้งหมดแล้ว", ephemeral=True)


@relay_group.command(name="bindlistener", description="ผูกบอทฟังให้เข้า/ออกห้องหลักอัตโนมัติตามความเคลื่อนไหวของห้อง")
@has_relay_perms()
@app_commands.describe(channel="ห้องหลักที่จะผูกไว้ (Voice หรือ Stage Channel) — เข้าเมื่อมีคนเข้าห้อง ออกเมื่อห้องว่าง")
async def relay_bindlistener(interaction: discord.Interaction, channel: Union[discord.VoiceChannel, discord.StageChannel]):
    listener_binding["channel_id"] = channel.id
    await interaction.response.send_message(
        f"🔗 ผูกบอทฟังกับห้อง {channel.mention} แล้ว\n"
        f"ต่อไปนี้: มีคนเข้าห้องนี้ (คนแรก) → บอทฟังตามเข้าอัตโนมัติ / ห้องว่าง (คนสุดท้ายออก) → บอทฟังตามออกอัตโนมัติ",
        ephemeral=True
    )


@relay_group.command(name="bindspeaker", description="ผูกบอทพูดตัวที่ระบุให้เข้า/ออกห้องย่อยอัตโนมัติตามความเคลื่อนไหวของห้อง")
@has_relay_perms()
@app_commands.describe(
    index=f"หมายเลขบอทพูด (1-{len(SPEAKER_TOKENS)})",
    channel="ห้องย่อยที่จะผูกไว้ — เข้าเมื่อมีคนเข้าห้อง ออกเมื่อห้องว่าง"
)
async def relay_bindspeaker(interaction: discord.Interaction, index: int, channel: discord.VoiceChannel):
    if index < 1 or index > len(speaker_bots):
        return await interaction.response.send_message(f"❌ หมายเลขบอทพูดต้องอยู่ระหว่าง 1-{len(speaker_bots)}", ephemeral=True)

    idx0 = index - 1
    speaker_bindings[idx0] = {"channel_id": channel.id}
    await interaction.response.send_message(
        f"🔗 ผูกบอทพูดตัวที่ {index} กับห้อง {channel.mention} แล้ว\n"
        f"ต่อไปนี้: มีคนเข้าห้องนี้ (คนแรก) → บอทพูดตัวที่ {index} ตามเข้าอัตโนมัติ / ห้องว่าง → บอทตามออกอัตโนมัติ",
        ephemeral=True
    )


@relay_group.command(name="unbind", description="ยกเลิกการผูกอัตโนมัติทั้งหมด (บอทจะไม่ตามเข้า-ออกห้องไหนอีก)")
@has_relay_perms()
async def relay_unbind(interaction: discord.Interaction):
    listener_binding["channel_id"] = None
    speaker_bindings.clear()
    await interaction.response.send_message("🔓 ยกเลิกการผูกอัตโนมัติทั้งหมดแล้ว (ยังใช้คำสั่งแบบ manual ได้ปกติ)", ephemeral=True)


@relay_group.command(name="status", description="เช็คสถานะการถ่ายทอดเสียงตอนนี้")
async def relay_status(interaction: discord.Interaction):
    lines = []

    if listener_binding["channel_id"]:
        ch = interaction.guild.get_channel(listener_binding["channel_id"])
        lines.append(f"🔗 บอทฟัง ผูกกับห้อง {ch.mention if ch else '?'} (เข้า-ออกตามคนในห้อง)")

    for idx, binding in speaker_bindings.items():
        ch = interaction.guild.get_channel(binding["channel_id"])
        lines.append(f"🔗 บอทพูดตัวที่ {idx + 1} ผูกกับห้อง {ch.mention if ch else '?'} (เข้า-ออกตามคนในห้อง)")

    if lines:
        lines.append("")

    if not relay_active:
        lines.append("🔴 ไม่ได้ถ่ายทอดอยู่ตอนนี้")
        return await interaction.response.send_message("\n".join(lines), ephemeral=True)

    lines.append("🟢 กำลังฟังห้องหลักอยู่")
    lines.append(f"บอทพูดที่ใช้งาน: {len(active_speaker_indices)}/{len(speaker_bots)} ตัว")
    for idx, ch_id in speaker_channel_map.items():
        ch = interaction.guild.get_channel(ch_id)
        lines.append(f"  • บอทพูดตัวที่ {idx + 1} → {ch.mention if ch else f'`{ch_id}`'}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


tree.add_command(relay_group)


@tree.error
async def on_relay_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้ (ต้องมีสิทธิ์ **Manage Channels** หรือ **Administrator** ในเซิร์ฟเวอร์นี้)"
    else:
        log.exception(f"Unhandled command error: {error}")
        msg = f"❌ เกิดข้อผิดพลาด: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# Health check
async def health_handler(request):
    return web.Response(text="OK", status=200)


async def start_health_server():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("BOT_HEALTH_PORT", 8200))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Health server running on port {port}")


@listener_bot.event
async def on_ready():
    log.info(f"[Listener] Logged in as {listener_bot.user}")
    try:
        synced = await tree.sync()
        log.info(f"[Listener] Synced {len(synced)} commands")
    except Exception as e:
        log.error(f"[Listener] Sync failed: {e}")
    asyncio.create_task(mixer_pump())
    asyncio.create_task(start_health_server())


@listener_bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """บอทฟัง: เข้าเมื่อมีคนแรกเข้าห้องที่ผูกไว้ / ออกเมื่อห้องว่าง (ตาม /relay bindlistener)"""
    if member.bot:
        return
    bound_channel_id = listener_binding["channel_id"]
    if bound_channel_id is None:
        return

    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None
    if before_id == after_id:
        return

    # มีคนออกจากห้องที่ผูกไว้ -> เช็คว่าห้องว่างหรือยัง
    if before_id == bound_channel_id and before.channel is not None:
        if _count_humans(before.channel) == 0:
            await stop_listening()

    # มีคนเข้าห้องที่ผูกไว้ -> ถ้ายังไม่ได้ฟังอยู่ ให้เริ่มฟัง (คนแรกเข้า)
    if after_id == bound_channel_id and after.channel is not None:
        if not relay_active:
            try:
                await start_listening(after.channel)
            except Exception as e:
                log.error(f"[Listener] Auto-join ล้มเหลว: {e}")


def make_speaker_ready_handler(index: int):
    async def on_ready():
        log.info(f"[Speaker {index + 1}] Logged in as {speaker_bots[index].user}")
    return on_ready


def make_speaker_voice_handler(index: int):
    async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """บอทพูดตัวที่ index: เข้าเมื่อมีคนแรกเข้าห้อง / ออกเมื่อห้องว่าง (ตาม /relay bindspeaker)"""
        if member.bot:
            return
        binding = speaker_bindings.get(index)
        if not binding:
            return
        bound_channel_id = binding["channel_id"]

        before_id = before.channel.id if before.channel else None
        after_id = after.channel.id if after.channel else None
        if before_id == after_id:
            return

        if before_id == bound_channel_id and before.channel is not None:
            if _count_humans(before.channel) == 0:
                await stop_speaking(index)

        if after_id == bound_channel_id and after.channel is not None:
            if index not in active_speaker_indices:
                try:
                    await start_speaking(index, after.channel)
                except Exception as e:
                    log.error(f"[Speaker {index + 1}] Auto-join ล้มเหลว: {e}")

    return on_voice_state_update


for i, sbot in enumerate(speaker_bots):
    sbot.event(make_speaker_ready_handler(i))
    sbot.event(make_speaker_voice_handler(i))


async def _start_bot_safe(bot: commands.Bot, token: str, label: str):
    """
    login บอทแต่ละตัวแบบแยกอิสระ ถ้าตัวไหน token ผิด/login ไม่ผ่าน
    จะ log error ไว้แล้วปล่อยให้บอทตัวอื่นทำงานต่อได้ตามปกติ ไม่ให้ทั้งระบบล่มไปด้วย
    """
    try:
        await bot.start(token)
    except discord.LoginFailure as e:
        log.error(f"[{label}] Login ไม่ผ่าน (token ผิด/หมดอายุ): {e}")
    except Exception as e:
        log.error(f"[{label}] เกิดข้อผิดพลาดไม่คาดคิด: {e}")


async def main():
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(listener_bot)
        for sbot in speaker_bots:
            await stack.enter_async_context(sbot)

        tasks = [_start_bot_safe(listener_bot, LISTENER_TOKEN, "Listener")]
        for i, (token, sbot) in enumerate(zip(SPEAKER_TOKENS, speaker_bots)):
            tasks.append(_start_bot_safe(sbot, token, f"Speaker {i + 1}"))
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
