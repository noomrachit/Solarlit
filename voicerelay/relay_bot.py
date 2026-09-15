"""
ระบบถ่ายทอดเสียงทางเดียว (one-way live audio relay)
ห้องหลัก (source) -> ห้องย่อยหลายห้อง (targets) แบบเรียลไทม์ — รองรับ "หัวหน้า" (listener bot) ได้พร้อมกันหลายตัว

สถาปัตยกรรม:
- RelayUnit  : ห่อ state ที่เคยเป็น global เดี่ยวๆ (relay_active, mixer, role_filter, bindings ฯลฯ)
               ให้เป็นของแต่ละ "หัวหน้า" (listener bot) ตัวใดตัวหนึ่งโดยเฉพาะ — แต่ละตัวมี
               discord.Bot + command tree (`/relay ...`) เป็นของตัวเอง (คนละแอปดิสคอร์ด)
- SpeakerPool: บอทพูดทั้งหมด (SPEAKER_BOT_TOKEN_1..N) เป็นทรัพยากรกลางที่ "แชร์" ระหว่างทุก RelayUnit
               แต่ละตัวถูกจอง (owner) ให้ unit ใดหนึ่งได้ครั้งละ unit เดียวเท่านั้น
               (เพราะบอท 1 ตัวเข้าห้องเสียงได้ทีละ 1 ห้องอยู่แล้ว เป็นข้อจำกัดของ Discord เอง)
- mixer_pump : วน loop เดียว ไล่ทุก unit ที่ active อยู่ ผสมเสียงแยกกันคนละ mixer แล้วป้อนเข้า
               queue เฉพาะของบอทพูดที่ unit นั้น "เป็นเจ้าของ" อยู่ตอนนั้น

Environment variables:
  LISTENER_BOT_TOKEN     = token หัวหน้าตัวที่ 1 (บังคับต้องมี)
  LISTENER_BOT_TOKEN_2   = token หัวหน้าตัวที่ 2 (ไม่ใส่ = รันแค่หัวหน้าตัวเดียว เหมือนสถาปัตยกรรมเดิม)
  SPEAKER_BOT_TOKEN_1..N = บอทพูด (พูลกลาง แชร์กันทุกหัวหน้า)

/relay bindspeaker กันชนข้าม unit แล้ว — pool.try_bind()/release_bind() ปฏิเสธถ้าหัวหน้าอีกตัวผูกเลข
เดียวกันไว้กับห้องอื่นอยู่ก่อน (บอกชื่อหัวหน้าที่ถืออยู่ในข้อความ error ด้วย) ต้อง unbind ตัวเดิมก่อนถึงจะ
ผูกใหม่ข้าม unit ได้ — ดู SpeakerPool.try_bind/release_bind และ relay_bindspeaker ด้านล่าง

ข้อจำกัดที่ทราบอยู่แล้ว:
- หน่วงเวลาประมาณ 0.3-0.8 วินาที (รับ -> mix -> เข้ารหัส -> ส่ง -> เล่น)
- เป็นเสียงทางเดียวเท่านั้น ห้องย่อยพูดกลับห้องหลักไม่ได้
- ต้อง invite บอททุกตัวเข้าเซิร์ฟเวอร์เดียวกัน (คนละ token คนละแอป)
- ต้องมี libopus ติดตั้งในระบบ (ดู nixpacks.toml)
- จำนวนห้องย่อยที่กระจายพร้อมกันได้ ถูกจำกัดด้วยจำนวนบอทพูดที่ตั้งค่าไว้เท่านั้น (แชร์ข้ามหัวหน้าทุกตัว)
- โควต้าต่อเซิร์ฟเวอร์ (_quota_ok) นับเฉพาะบอทของ "หัวหน้าตัวนั้นๆ" เอง ไม่รวมของหัวหน้าอีกตัวในเซิร์ฟเวอร์
  เดียวกัน (เหมือนพฤติกรรมเดิมตอนมีหัวหน้าตัวเดียว) — ตอนนี้ billing_access ยังเป็น stub คืน limit=99 เสมอ
  ไม่กระทบอะไรจริง จนกว่าจะมีระบบ tier จริงมาแทนที่

/relay setrole <role> จำกัดให้กระจายเสียงเฉพาะคนที่มีบทบาทนี้ในห้องหลัก — คนอื่นยังพูดคุยในห้องหลัก
ได้ตามปกติ (ไม่ได้ถูกตัดไมค์/เตะออก) แค่เสียงของเขาจะไม่ถูกป้อนเข้า mixer จึงไม่ถูกส่งต่อไปห้องย่อย
ใช้ /relay clearrole เพื่อยกเลิกและกลับไปกระจายเสียงทุกคนตามเดิม
"""

import os
import asyncio
import logging
import time
from collections import defaultdict
from contextlib import AsyncExitStack
from typing import Optional, Union

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
LISTENER_TOKEN_2 = os.getenv("LISTENER_BOT_TOKEN_2")  # ไม่ใส่ = รันแค่หัวหน้าตัวเดียว เหมือนเดิม

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

log.info(f"พบบอทพูดทั้งหมด {len(SPEAKER_TOKENS)} ตัว (พูลกลาง แชร์กันทุกหัวหน้า)")
log.info(f"หัวหน้าที่จะรัน: 1 ตัว" + (" + ตัวที่ 2" if LISTENER_TOKEN_2 else " (ไม่ได้ตั้งค่า LISTENER_BOT_TOKEN_2)"))


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


class Mixer:
    """ตัวผสมเสียง (Mixer): รวมเสียงทุกคนที่พูดพร้อมกันในห้องหลักให้กลายเป็นเฟรมเดียว — 1 instance ต่อ 1 RelayUnit"""

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


class RelaySink(voice_recv.AudioSink):
    """
    รับเสียง PCM ที่ decode แล้วจากทุกคนในห้องหลัก แล้วป้อนเข้า mixer ของ unit ตัวเอง (ส่งเข้า constructor
    แทนการอ้าง global ตรงๆ — ทำให้แต่ละ RelayUnit มี mixer/role_filter แยกกันคนละชุด)
    ตอนมีคนเริ่มพูดใหม่หลังเงียบไปนาน (decoder ตัวใหม่ถูกสร้าง) แพ็กเก็ตแรกสุดมักเป็นขยะ/ไม่สมบูรณ์
    เลยข้ามแพ็กเก็ตแรกไปเฉยๆ ก่อนเริ่มป้อนเข้า mixer จริง (แค่ 1 แพ็กเก็ต ~20ms ไม่ใช่ mute ยาว)
    """

    SILENCE_RESET_SECONDS = 1.5  # เงียบเกินนี้ = ถือว่าเริ่มพูดใหม่ (decoder ตัวใหม่ถูกสร้างอีกรอบ)

    def __init__(self, mixer: Mixer, role_filter: dict):
        self._mixer = mixer
        self._role_filter = role_filter
        self._first_seen: dict = {}   # user_id -> เวลาที่เริ่มเห็น packet แรกของ "รอบพูด" นี้
        self._last_seen: dict = {}    # user_id -> เวลาที่เห็น packet ล่าสุด (ไว้ตรวจจับช่วงเงียบ)

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        if user is None or user.bot:
            return

        role_id = self._role_filter["role_id"]
        if role_id is not None and role_id not in {r.id for r in getattr(user, "roles", ())}:
            return  # ไม่มีบทบาทที่กำหนด ไม่กระจายเสียงคนนี้ไปห้องย่อย (ยังพูดในห้องหลักได้ปกติ ไม่ได้ตัดไมค์)

        now = time.monotonic()
        last_seen = self._last_seen.get(user.id)
        first_seen = self._first_seen.get(user.id)

        # เงียบไปนานเกินไป (หรือพูดครั้งแรก) = เริ่มรอบ warm-up ใหม่
        if first_seen is None or last_seen is None or (now - last_seen) > self.SILENCE_RESET_SECONDS:
            self._first_seen[user.id] = now
            self._last_seen[user.id] = now
            return  # เฟรมแรกของรอบใหม่ ข้ามไปเลย ไม่ป้อนเข้า mixer

        self._last_seen[user.id] = now
        self._mixer.feed(user.id, data.pcm)

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


def _count_humans(channel) -> int:
    """นับจำนวนคนที่ไม่ใช่บอทในห้องเสียง/สเตจ"""
    if channel is None:
        return 0
    return len([m for m in channel.members if not m.bot])


class SpeakerPool:
    """
    บอทพูดทั้งหมด (แชร์กันทุก RelayUnit) — จองได้ครั้งละ 1 unit ต่อ 1 index เท่านั้น
    (เพราะบอท 1 ตัวเข้าได้แค่ 1 ห้องเสียงพร้อมกันอยู่แล้ว เป็นข้อจำกัดของ Discord เอง ไม่ใช่ของโค้ดนี้)
    """

    def __init__(self, speaker_bots: list):
        self.speaker_bots = speaker_bots
        self.queues: list = [asyncio.Queue(maxsize=50) for _ in speaker_bots]
        self.owner: dict = {}              # index -> RelayUnit ที่กำลังใช้ index นี้ "สด" อยู่ตอนนี้ (connect อยู่จริง)
        self.channel_map: dict = {}        # index -> channel_id (ไว้โชว์ /relay status)
        self.bind_owner: dict = {}         # index -> RelayUnit ที่ "จอง static bind" ไว้ (/relay bindspeaker)
                                            # แยกจาก owner เพราะ bind ไว้ล่วงหน้าได้โดยยังไม่ connect จริง
                                            # (รอคนเข้าห้องก่อนค่อย auto-join) — ดู try_bind()/release_bind()

    def free_index(self) -> Optional[int]:
        for i in range(len(self.speaker_bots)):
            if i not in self.owner:
                return i
        return None

    def claim(self, index: int, unit) -> bool:
        """จอง index ให้ unit — คืน False ถ้ามีคนอื่นจองอยู่ก่อนแล้ว (กันแย่งกันตอน race)"""
        if index in self.owner:
            return False
        self.owner[index] = unit
        return True

    def release(self, index: int):
        self.owner.pop(index, None)
        self.channel_map.pop(index, None)
        q = self.queues[index]
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break

    def indices_for(self, unit) -> list:
        return [i for i, owner in self.owner.items() if owner is unit]

    def total_in_use(self) -> int:
        return len(self.owner)

    # ── static bind (/relay bindspeaker) — กันหัวหน้า 2 ตัวผูกบอทพูดตัวเดียวกันซ้อนกัน ──

    def try_bind(self, index: int, unit) -> Optional[object]:
        """
        จอง static bind ให้ unit — คืน None ถ้าสำเร็จ (หรือ unit เดิมผูกซ้ำ/เปลี่ยนห้องของตัวเอง)
        คืน "เจ้าของเดิม" (RelayUnit อีกตัว) ถ้ามีคนอื่นผูก index นี้ไว้ก่อนแล้ว — ผู้เรียกเอาไปโชว์ error ได้
        """
        current = self.bind_owner.get(index)
        if current is not None and current is not unit:
            return current
        self.bind_owner[index] = unit
        return None

    def release_bind(self, index: int, unit) -> bool:
        """คืน static bind — คืน False เฉยๆ ถ้า index นี้ไม่ได้เป็นของ unit นี้อยู่ (กันเผลอไปเคลียร์ของหัวหน้าตัวอื่น)"""
        if self.bind_owner.get(index) is not unit:
            return False
        del self.bind_owner[index]
        return True


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


class RelayUnit:
    """
    หนึ่ง "หัวหน้า" (listener bot) พร้อม state ที่เดิมเป็น global เดี่ยว — ตอนนี้แยกเป็นของตัวเองแต่ละตัว
    ทุก unit ยืมบอทพูดจาก SpeakerPool กลางตัวเดียวกัน (ดูคำเตือนเรื่อง bindspeaker ที่หัวไฟล์)
    """

    def __init__(self, name: str, token: str, pool: SpeakerPool):
        self.name = name
        self.token = token
        self.pool = pool

        self.relay_active = False
        self.mixer = Mixer()
        # ถ้าตั้งค่าไว้ (ไม่ใช่ None) บอทฟังจะกระจายเสียงเฉพาะคนที่มีบทบาทนี้เท่านั้น
        # คนอื่นในห้องหลักยังพูดได้ปกติ ไม่ได้ถูกตัดไมค์ แค่เสียงของเขาจะไม่ถูกป้อนเข้า mixer
        # จึงไม่ถูกส่งต่อไปห้องย่อย (ดู RelaySink.write และ /relay setrole)
        self.role_filter: dict = {"role_id": None}
        # ระบบผูกบอทกับ "ห้อง" ให้เข้า/ออกอัตโนมัติตามความเคลื่อนไหวของห้อง
        # เข้าเมื่อมีคนแรกเข้าห้อง (ห้องว่าง -> มีคน) / ออกเมื่อห้องว่าง (คนสุดท้ายออก)
        self.listener_binding: dict = {"channel_id": None}
        self.speaker_bindings: dict = {}  # index -> {"channel_id": ...}
        self.guild_id: Optional[int] = None  # guild ที่กำลังฟังอยู่ตอนนี้ (ไว้คิดโควต้า)
        self.last_speaking_error: Optional[str] = None  # "quota" | "not_invited" | None — อ่านหลังเรียก start_speaking*

        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self.tree = self.bot.tree
        # app_commands.CommandTree ไม่มี decorator @tree.check แบบ commands.Bot — ต้องตั้งผ่าน
        # interaction_check ตรงๆ แทน (assign ฟังก์ชันเข้า instance attribute เพื่อ override
        # CommandTree.interaction_check ที่ปกติ return True เฉยๆ)
        self.tree.interaction_check = global_billing_check

        self._build_commands()
        self._register_events()

    # ── connect/disconnect ──

    async def start_listening(self, channel: Union[discord.VoiceChannel, discord.StageChannel]):
        """เริ่มให้บอทฟังเข้าห้องหลักและดักจับเสียง (ใช้ได้ทั้งเรียกเองผ่านคำสั่ง และเรียกอัตโนมัติ)"""
        if self.relay_active:
            return
        listener_vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        listener_vc.listen(RelaySink(self.mixer, self.role_filter))
        self.relay_active = True
        self.guild_id = channel.guild.id
        log.info(f"[{self.name}] เข้าห้อง {channel.name} แล้ว")

    async def stop_listening(self):
        """ให้บอทฟังออกจากห้องหลัก"""
        if not self.relay_active:
            return
        self.relay_active = False
        for guild in self.bot.guilds:
            if guild.voice_client:
                await guild.voice_client.disconnect(force=True)
        log.info(f"[{self.name}] ออกจากห้องแล้ว")

    async def _quota_ok(self, guild_id: int) -> bool:
        """
        เช็คโควต้าจำนวนบอทตามแพ็กเกจของเซิร์ฟเวอร์นี้ ก่อนเปิดบอทพูดตัวใหม่
        (ตอนนี้ billing_access ยังเป็น stub คืน limit=99 เสมอ เก็บ plumbing นี้ไว้รอวันมีระบบ tier จริง)
        นับเฉพาะบอทฟัง+บอทพูดของ "unit นี้" เท่านั้น (เหมือนพฤติกรรมเดิมตอนมี unit เดียว) ไม่รวมของหัวหน้าตัวอื่น
        """
        limit = await billing_access.get_relay_bot_limit(guild_id)
        current_bots_in_use = (1 if self.relay_active else 0) + len(self.pool.indices_for(self))
        if current_bots_in_use + 1 > limit:
            log.warning(
                f"[{self.name}] ปฏิเสธการเข้าห้อง: เกินโควต้าแพ็กเกจของเซิร์ฟเวอร์ "
                f"(ใช้อยู่ {current_bots_in_use}/{limit} บอท) — อัปเกรดแพ็กเกจเพื่อเพิ่มจำนวนห้องที่ถ่ายทอดพร้อมกันได้"
            )
            self.last_speaking_error = "quota"
            return False
        return True

    async def start_speaking(self, channel: discord.VoiceChannel) -> Optional[int]:
        """จองบอทพูดตัวที่ว่างจากพูลกลาง (ตัวไหนก็ได้) แล้วเข้าห้องย่อย — ใช้กับ /relay addtarget (manual)"""
        self.last_speaking_error = None
        index = self.pool.free_index()
        if index is None:
            return None
        if not await self._quota_ok(channel.guild.id):
            return None
        if not self.pool.claim(index, self):
            return None
        if not await self._connect_speaker(index, channel):
            return None
        return index

    async def start_speaking_at(self, index: int, channel: discord.VoiceChannel) -> bool:
        """
        จองบอทพูด "ตัวที่ระบุเจาะจง" แล้วเข้าห้องย่อย — ใช้กับ auto-join จาก /relay bindspeaker เท่านั้น
        (ต่างจาก start_speaking ตรงที่ห้ามสลับไปใช้ตัวอื่นแทน เพราะผู้ใช้ตั้งใจผูกเลขนี้ไว้กับห้องนี้)
        คืน False เฉยๆ ถ้า index นี้มีคนอื่นถือ "สด" อยู่ก่อนแล้ว (ไม่ raise, ให้ผู้เรียก log เอง)
        """
        self.last_speaking_error = None
        if not await self._quota_ok(channel.guild.id):
            return False
        if not self.pool.claim(index, self):
            return False
        return await self._connect_speaker(index, channel)

    async def _connect_speaker(self, index: int, channel: discord.VoiceChannel) -> bool:
        """ส่วนเชื่อมต่อจริงที่ใช้ร่วมกันของ start_speaking/start_speaking_at — สมมติว่า claim(index, self) ผ่านแล้ว"""
        speaker_bot = self.pool.speaker_bots[index]
        target_guild = speaker_bot.get_guild(channel.guild.id)
        target_channel = target_guild.get_channel(channel.id) if target_guild else None
        if target_channel is None:
            self.pool.release(index)
            log.error(f"[{self.name}/Speaker {index + 1}] มองไม่เห็นห้อง {channel.name} (ยัง invite บอทเข้าเซิร์ฟเวอร์หรือยัง?)")
            self.last_speaking_error = "not_invited"
            return False

        vc = await target_channel.connect()
        vc.play(QueueAudioSource(self.pool.queues[index]))
        self.pool.channel_map[index] = channel.id
        log.info(f"[{self.name}/Speaker {index + 1}] เข้าห้อง {channel.name} แล้ว")
        return True

    async def stop_speaking(self, index: int):
        """ให้บอทพูดตัวที่ index ออกจากห้องย่อย"""
        if self.pool.owner.get(index) is not self:
            return
        speaker_bot = self.pool.speaker_bots[index]
        for guild in speaker_bot.guilds:
            if guild.voice_client:
                await guild.voice_client.disconnect(force=True)
        self.pool.release(index)
        log.info(f"[{self.name}/Speaker {index + 1}] ออกจากห้องแล้ว")

    # ── slash commands (ลงทะเบียนบน tree ของ unit นี้เอง — คนละแอปดิสคอร์ดกับ unit อื่น) ──

    def _build_commands(self):
        unit = self  # ชื่อสั้นให้ closure อ่านง่าย
        pool = self.pool

        relay_group = app_commands.Group(
            name="relay", description=f"ถ่ายทอดเสียงสดจากห้องหลักไปห้องย่อยหลายห้อง (ทางเดียว) — {unit.name}"
        )

        @relay_group.command(name="start", description="เริ่มฟังเสียงจากห้องหลัก (ยังไม่กระจายไปไหนจนกว่าจะ /relay addtarget)")
        @has_relay_perms()
        @app_commands.describe(source="ห้องหลัก (Voice หรือ Stage Channel — แนะนำ Stage Channel เพราะไม่ติดปัญหาเข้ารหัส DAVE)")
        async def relay_start(interaction: discord.Interaction, source: Union[discord.VoiceChannel, discord.StageChannel]):
            await interaction.response.defer(ephemeral=True)

            if unit.relay_active:
                return await interaction.followup.send("⚠️ กำลังถ่ายทอดอยู่แล้ว ใช้ `/relay stop` ก่อนเริ่มใหม่", ephemeral=True)

            try:
                await unit.start_listening(source)
            except Exception as e:
                return await interaction.followup.send(f"❌ {unit.name} เชื่อมต่อห้องหลักไม่สำเร็จ: {e}", ephemeral=True)

            await interaction.followup.send(
                f"🎧 {unit.name} เริ่มฟังห้อง {source.mention} แล้ว\n"
                f"ใช้ `/relay addtarget` เพื่อเพิ่มห้องย่อยที่จะกระจายเสียงไป "
                f"(บอทพูดว่างตอนนี้ {len(pool.speaker_bots) - pool.total_in_use()}/{len(pool.speaker_bots)} ตัว ทั้งระบบ)",
                ephemeral=True
            )

        @relay_group.command(name="addtarget", description="เพิ่มห้องย่อยที่จะกระจายเสียงไป (ใช้บอทพูดตัวถัดไปที่ว่างจากพูลกลาง)")
        @has_relay_perms()
        @app_commands.describe(channel="ห้องย่อยที่จะเล่นเสียงถ่ายทอด")
        async def relay_addtarget(interaction: discord.Interaction, channel: discord.VoiceChannel):
            if not unit.relay_active:
                return await interaction.response.send_message("❌ ยังไม่ได้ `/relay start` เริ่มฟังห้องหลักก่อน", ephemeral=True)

            await interaction.response.defer(ephemeral=True)
            index = await unit.start_speaking(channel)
            if index is None:
                if unit.last_speaking_error == "quota":
                    return await interaction.followup.send(
                        "❌ ใช้บอทครบตามโควต้าแพ็กเกจแล้ว อัปเกรดแพ็กเกจเพื่อถ่ายทอดได้หลายห้องขึ้นได้ที่เว็บไซต์",
                        ephemeral=True
                    )
                if unit.last_speaking_error == "not_invited":
                    return await interaction.followup.send(
                        "❌ บอทพูดตัวที่เลือกยังไม่ได้ invite เข้าเซิร์ฟเวอร์นี้ (หรือมองไม่เห็นห้องนี้)",
                        ephemeral=True
                    )
                free = len(pool.speaker_bots) - pool.total_in_use()
                return await interaction.followup.send(
                    f"❌ บอทพูดว่างไม่พอ (เหลือว่าง {free}/{len(pool.speaker_bots)} ตัวทั้งระบบ — "
                    f"ตัวอื่นอาจถูกหัวหน้าตัวอื่นใช้อยู่) ใช้ `/relay removetarget` เพื่อคืนตัวที่ไม่ใช้ก่อน",
                    ephemeral=True
                )

            await interaction.followup.send(
                f"🔊 เพิ่ม {channel.mention} เป็นห้องฟังแล้ว (บอทพูดตัวที่ {index + 1}/{len(pool.speaker_bots)})",
                ephemeral=True
            )

        @relay_group.command(name="removetarget", description="เลิกกระจายเสียงไปห้องที่ระบุ")
        @has_relay_perms()
        @app_commands.describe(channel="ห้องย่อยที่ต้องการเลิกกระจายเสียงไป")
        async def relay_removetarget(interaction: discord.Interaction, channel: discord.VoiceChannel):
            target_index = None
            for idx in pool.indices_for(unit):
                if pool.channel_map.get(idx) == channel.id:
                    target_index = idx
                    break

            if target_index is None:
                return await interaction.response.send_message("ห้องนี้ไม่ได้อยู่ในรายการกระจายเสียงของหัวหน้าตัวนี้", ephemeral=True)

            await interaction.response.defer(ephemeral=True)
            await unit.stop_speaking(target_index)
            await interaction.followup.send(f"🔇 เลิกกระจายเสียงไป {channel.mention} แล้ว", ephemeral=True)

        @relay_group.command(name="stop", description="หยุดถ่ายทอดเสียงทั้งหมด (ทุกห้องของหัวหน้าตัวนี้)")
        @has_relay_perms()
        async def relay_stop(interaction: discord.Interaction):
            if not unit.relay_active:
                return await interaction.response.send_message("ตอนนี้ไม่มีการถ่ายทอดเสียงทำงานอยู่", ephemeral=True)

            await interaction.response.defer(ephemeral=True)
            await unit.stop_listening()
            for idx in list(pool.indices_for(unit)):
                await unit.stop_speaking(idx)

            await interaction.followup.send("🛑 หยุดถ่ายทอดเสียงทั้งหมดแล้ว", ephemeral=True)

        @relay_group.command(
            name="setrole",
            description="กระจายเสียงเฉพาะคนที่มีบทบาทนี้ในห้องหลัก (คนอื่นพูดในห้องได้ปกติ แค่ไม่ถูกส่งไปห้องย่อย)"
        )
        @has_relay_perms()
        @app_commands.describe(role="บทบาทที่อนุญาตให้กระจายเสียงไปห้องย่อย")
        async def relay_setrole(interaction: discord.Interaction, role: discord.Role):
            unit.role_filter["role_id"] = role.id
            await interaction.response.send_message(
                f"🎙️ ตั้งค่าแล้ว: กระจายเสียงเฉพาะคนที่มีบทบาท {role.mention} เท่านั้น\n"
                f"คนที่ไม่มีบทบาทนี้ยังพูดในห้องหลักได้ตามปกติ (ไม่ได้ปิดไมค์ใคร) แค่เสียงจะไม่ถูกส่งไปห้องย่อย",
                ephemeral=True
            )

        @relay_group.command(name="clearrole", description="ยกเลิกการกรองบทบาท กลับไปกระจายเสียงทุกคนในห้องหลักเหมือนเดิม")
        @has_relay_perms()
        async def relay_clearrole(interaction: discord.Interaction):
            unit.role_filter["role_id"] = None
            await interaction.response.send_message("🔓 ยกเลิกการกรองบทบาทแล้ว กระจายเสียงทุกคนในห้องหลักตามปกติ", ephemeral=True)

        @relay_group.command(name="bindlistener", description="ผูกบอทฟังให้เข้า/ออกห้องหลักอัตโนมัติตามความเคลื่อนไหวของห้อง")
        @has_relay_perms()
        @app_commands.describe(channel="ห้องหลักที่จะผูกไว้ (Voice หรือ Stage Channel) — เข้าเมื่อมีคนเข้าห้อง ออกเมื่อห้องว่าง")
        async def relay_bindlistener(interaction: discord.Interaction, channel: Union[discord.VoiceChannel, discord.StageChannel]):
            unit.listener_binding["channel_id"] = channel.id
            await interaction.response.send_message(
                f"🔗 ผูก {unit.name} กับห้อง {channel.mention} แล้ว\n"
                f"ต่อไปนี้: มีคนเข้าห้องนี้ (คนแรก) → {unit.name} ตามเข้าอัตโนมัติ / ห้องว่าง (คนสุดท้ายออก) → ตามออกอัตโนมัติ",
                ephemeral=True
            )

        @relay_group.command(
            name="bindspeaker",
            description="ผูกบอทพูดตัวที่ระบุให้เข้า/ออกห้องย่อยอัตโนมัติตามความเคลื่อนไหวของห้อง"
        )
        @has_relay_perms()
        @app_commands.describe(
            index=f"หมายเลขบอทพูด (1-{len(pool.speaker_bots)}) — เลขเดียวกันผูกซ้ำจากหัวหน้าอีกตัวไม่ได้ ต้อง unbind ตัวเดิมก่อน",
            channel="ห้องย่อยที่จะผูกไว้ — เข้าเมื่อมีคนเข้าห้อง ออกเมื่อห้องว่าง"
        )
        async def relay_bindspeaker(interaction: discord.Interaction, index: int, channel: discord.VoiceChannel):
            if index < 1 or index > len(pool.speaker_bots):
                return await interaction.response.send_message(
                    f"❌ หมายเลขบอทพูดต้องอยู่ระหว่าง 1-{len(pool.speaker_bots)}", ephemeral=True
                )
            idx0 = index - 1

            other = pool.try_bind(idx0, unit)
            if other is not None:
                return await interaction.response.send_message(
                    f"❌ บอทพูดตัวที่ {index} ถูก **{other.name}** ผูกไว้กับห้องอื่นอยู่แล้ว "
                    f"ให้ {other.name} สั่ง `/relay unbind` เลิกผูกตัวนี้ก่อน (บอทพูดตัวเดียวเข้าได้ทีละห้องเสียง "
                    f"จะให้ 2 หัวหน้าผูกเลขเดียวกันคนละห้องพร้อมกันไม่ได้)",
                    ephemeral=True
                )

            unit.speaker_bindings[idx0] = {"channel_id": channel.id}
            await interaction.response.send_message(
                f"🔗 ผูกบอทพูดตัวที่ {index} กับห้อง {channel.mention} แล้ว ({unit.name})\n"
                f"ต่อไปนี้: มีคนเข้าห้องนี้ (คนแรก) → บอทพูดตัวที่ {index} ตามเข้าอัตโนมัติ / ห้องว่าง → บอทตามออกอัตโนมัติ",
                ephemeral=True
            )

        @relay_group.command(name="unbind", description="ยกเลิกการผูกอัตโนมัติทั้งหมดของหัวหน้าตัวนี้ (บอทจะไม่ตามเข้า-ออกห้องไหนอีก)")
        @has_relay_perms()
        async def relay_unbind(interaction: discord.Interaction):
            unit.listener_binding["channel_id"] = None
            for idx0 in list(unit.speaker_bindings.keys()):
                pool.release_bind(idx0, unit)
            unit.speaker_bindings.clear()
            await interaction.response.send_message("🔓 ยกเลิกการผูกอัตโนมัติทั้งหมดแล้ว (ยังใช้คำสั่งแบบ manual ได้ปกติ)", ephemeral=True)

        @relay_group.command(name="status", description="เช็คสถานะการถ่ายทอดเสียงของหัวหน้าตัวนี้ตอนนี้")
        async def relay_status(interaction: discord.Interaction):
            lines = [f"— {unit.name} —"]

            if unit.listener_binding["channel_id"]:
                ch = interaction.guild.get_channel(unit.listener_binding["channel_id"])
                lines.append(f"🔗 บอทฟัง ผูกกับห้อง {ch.mention if ch else '?'} (เข้า-ออกตามคนในห้อง)")

            for idx0, binding in unit.speaker_bindings.items():
                ch = interaction.guild.get_channel(binding["channel_id"])
                lines.append(f"🔗 บอทพูดตัวที่ {idx0 + 1} ผูกกับห้อง {ch.mention if ch else '?'} (เข้า-ออกตามคนในห้อง)")

            if unit.role_filter["role_id"]:
                role = interaction.guild.get_role(unit.role_filter["role_id"])
                lines.append(f"🎙️ กรองบทบาท: กระจายเสียงเฉพาะ {role.mention if role else '?'}")

            if lines[1:]:
                lines.append("")

            if not unit.relay_active:
                lines.append("🔴 ไม่ได้ถ่ายทอดอยู่ตอนนี้")
                return await interaction.response.send_message("\n".join(lines), ephemeral=True)

            my_indices = pool.indices_for(unit)
            lines.append("🟢 กำลังฟังห้องหลักอยู่")
            lines.append(f"บอทพูดที่ใช้งาน: {len(my_indices)}/{len(pool.speaker_bots)} ตัว (รวมทั้งระบบใช้อยู่ {pool.total_in_use()}/{len(pool.speaker_bots)})")
            for idx in my_indices:
                ch = interaction.guild.get_channel(pool.channel_map.get(idx))
                lines.append(f"  • บอทพูดตัวที่ {idx + 1} → {ch.mention if ch else f'`{pool.channel_map.get(idx)}`'}")

            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        self.tree.add_command(relay_group)

        @self.tree.error
        async def on_relay_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            if isinstance(error, app_commands.CheckFailure):
                msg = "❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้ (ต้องมีสิทธิ์ **Manage Channels** หรือ **Administrator** ในเซิร์ฟเวอร์นี้)"
            else:
                log.exception(f"[{unit.name}] Unhandled command error: {error}")
                msg = f"❌ เกิดข้อผิดพลาด: {error}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

    # ── events ──

    def _register_events(self):
        unit = self

        @self.bot.event
        async def on_ready():
            log.info(f"[{unit.name}] Logged in as {unit.bot.user}")
            try:
                synced = await unit.tree.sync()
                log.info(f"[{unit.name}] Synced {len(synced)} commands")
            except Exception as e:
                log.error(f"[{unit.name}] Sync failed: {e}")

        @self.bot.event
        async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
            """บอทฟัง: เข้าเมื่อมีคนแรกเข้าห้องที่ผูกไว้ / ออกเมื่อห้องว่าง (ตาม /relay bindlistener)"""
            if member.bot:
                return
            bound_channel_id = unit.listener_binding["channel_id"]
            if bound_channel_id is None:
                return

            before_id = before.channel.id if before.channel else None
            after_id = after.channel.id if after.channel else None
            if before_id == after_id:
                return

            # มีคนออกจากห้องที่ผูกไว้ -> เช็คว่าห้องว่างหรือยัง
            if before_id == bound_channel_id and before.channel is not None:
                if _count_humans(before.channel) == 0:
                    await unit.stop_listening()

            # มีคนเข้าห้องที่ผูกไว้ -> ถ้ายังไม่ได้ฟังอยู่ ให้เริ่มฟัง (คนแรกเข้า)
            if after_id == bound_channel_id and after.channel is not None:
                if not unit.relay_active:
                    try:
                        await unit.start_listening(after.channel)
                    except Exception as e:
                        log.error(f"[{unit.name}] Auto-join ล้มเหลว: {e}")


def make_speaker_ready_handler(index: int, pool: SpeakerPool):
    async def on_ready():
        log.info(f"[Speaker {index + 1}] Logged in as {pool.speaker_bots[index].user}")
    return on_ready


def make_speaker_voice_handler(index: int, pool: SpeakerPool):
    """
    บอทพูดตัวที่ index: เข้า/ออกห้องอัตโนมัติตาม /relay bindspeaker
    เจ้าของ bind (ตัดสินว่า "หัวหน้า" ตัวไหนใช้ index นี้) มาจาก pool.bind_owner แบบ dynamic
    เพราะบอทพูดตัวนี้เป็นทรัพยากรกลาง แชร์ได้ระหว่างหัวหน้าหลายตัว ไม่ผูกตายกับ unit ใดตัวหนึ่ง
    """
    async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        owner = pool.bind_owner.get(index)
        if owner is None:
            return  # ไม่มีหัวหน้าตัวไหนตั้ง /relay bindspeaker ผูกกับตัวนี้ไว้เลยตอนนี้
        binding = owner.speaker_bindings.get(index)
        if not binding:
            return
        bound_channel_id = binding["channel_id"]

        before_id = before.channel.id if before.channel else None
        after_id = after.channel.id if after.channel else None
        if before_id == after_id:
            return

        if before_id == bound_channel_id and before.channel is not None:
            if _count_humans(before.channel) == 0:
                await owner.stop_speaking(index)

        if after_id == bound_channel_id and after.channel is not None:
            if pool.owner.get(index) is None:
                try:
                    await owner.start_speaking_at(index, after.channel)
                except Exception as e:
                    log.error(f"[{owner.name}/Speaker {index + 1}] Auto-join ล้มเหลว: {e}")

    return on_voice_state_update


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


async def mixer_pump(units: list, pool: SpeakerPool):
    """
    วน mix เฟรมทุก 20ms แล้วกระจาย (broadcast) เข้า queue ของทุกบอทพูดที่กำลังทำงานอยู่ — ไล่ทุก unit
    ที่ active อยู่ในลูปเดียว ใช้ timer แบบอิงเวลาสัมบูรณ์ (next_tick) แทนการ sleep(0.02) ตรงๆ
    เพราะการ sleep ตรงๆ จะสะสมความคลาดเคลื่อน (drift) ไปเรื่อยๆ เมื่อมี jitter จาก CPU/GC
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

        for unit in units:
            if not unit.relay_active:
                continue
            my_indices = pool.indices_for(unit)
            if not my_indices:
                continue
            frame = unit.mixer.pop_frame()
            for idx in my_indices:
                q = pool.queues[idx]
                try:
                    q.put_nowait(frame)
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()  # ทิ้งเฟรมเก่าสุด กันดีเลย์สะสม
                    except asyncio.QueueEmpty:
                        pass
                    q.put_nowait(frame)


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


async def main():
    speaker_bots = []
    for _ in SPEAKER_TOKENS:
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        speaker_bots.append(commands.Bot(command_prefix="!", intents=intents))
    pool = SpeakerPool(speaker_bots)

    for i, sbot in enumerate(speaker_bots):
        sbot.event(make_speaker_ready_handler(i, pool))
        sbot.event(make_speaker_voice_handler(i, pool))

    units = [RelayUnit("หัวหน้า", LISTENER_TOKEN, pool)]
    if LISTENER_TOKEN_2:
        units.append(RelayUnit("หัวหน้า 2", LISTENER_TOKEN_2, pool))

    async with AsyncExitStack() as stack:
        for unit in units:
            await stack.enter_async_context(unit.bot)
        for sbot in speaker_bots:
            await stack.enter_async_context(sbot)

        asyncio.create_task(mixer_pump(units, pool))
        asyncio.create_task(start_health_server())

        tasks = [_start_bot_safe(unit.bot, unit.token, unit.name) for unit in units]
        for i, (token, sbot) in enumerate(zip(SPEAKER_TOKENS, speaker_bots)):
            tasks.append(_start_bot_safe(sbot, token, f"Speaker {i + 1}"))
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
