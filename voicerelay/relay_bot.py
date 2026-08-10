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
from collections import defaultdict
from contextlib import AsyncExitStack

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import voice_recv
from dotenv import load_dotenv
from aiohttp import web

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
                samples = struct.unpack(f"<{len(chunk) // 2}h", chunk)
                if mixed is None:
                    mixed = list(samples)
                else:
                    mixed = [max(-32768, min(32767, a + b)) for a, b in zip(mixed, samples)]
            if len(buf) == 0:
                del self.buffers[uid]
        if mixed is None:
            return b"\x00" * FRAME_BYTES
        return struct.pack(f"<{len(mixed)}h", *mixed)


mixer = Mixer()


class RelaySink(voice_recv.AudioSink):
    """รับเสียง PCM ที่ decode แล้วจากทุกคนในห้องหลัก แล้วป้อนเข้า mixer"""

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        if user is None or user.bot:
            return
        mixer.feed(user.id, data.pcm)

    def cleanup(self):
        pass


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
    """วน mix เฟรมทุก 20ms แล้วกระจาย (broadcast) เข้า queue ของทุกบอทพูดที่กำลังทำงานอยู่"""
    while True:
        await asyncio.sleep(0.02)
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
# ระบบผูกบอทกับ "เจ้าของห้อง" ให้เข้า/ออกอัตโนมัติ
# listener_binding: บอทฟัง ผูกกับเจ้าของห้องหลักได้ 1 คน
# speaker_bindings: บอทพูดแต่ละตัว ผูกกับเจ้าของห้องย่อยได้คนละ 1 คน
# ─────────────────────────────────────────────
listener_binding: dict = {"owner_id": None, "channel_id": None}
speaker_bindings: dict = {}  # index -> {"owner_id":..., "channel_id":...}


async def start_listening(channel: discord.VoiceChannel):
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
@app_commands.describe(source="ห้องหลัก (จุดที่จะดักเสียง)")
async def relay_start(interaction: discord.Interaction, source: discord.VoiceChannel):
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


@relay_group.command(name="bindlistener", description="ผูกบอทฟังให้เข้า/ออกห้องหลักอัตโนมัติตามเจ้าของห้อง")
@has_relay_perms()
@app_commands.describe(owner="เจ้าของห้อง (บอทจะเข้า/ออกตามคนนี้เข้า-ออกห้อง)", channel="ห้องหลักที่จะผูกไว้")
async def relay_bindlistener(interaction: discord.Interaction, owner: discord.Member, channel: discord.VoiceChannel):
    listener_binding["owner_id"] = owner.id
    listener_binding["channel_id"] = channel.id
    await interaction.response.send_message(
        f"🔗 ผูกบอทฟังกับ {owner.mention} ในห้อง {channel.mention} แล้ว\n"
        f"ต่อไปนี้: {owner.mention} เข้าห้องนี้ → บอทฟังตามเข้าอัตโนมัติ / {owner.mention} ออกจากห้องนี้ → บอทฟังตามออกอัตโนมัติ",
        ephemeral=True
    )


@relay_group.command(name="bindspeaker", description="ผูกบอทพูดตัวที่ระบุให้เข้า/ออกห้องย่อยอัตโนมัติตามเจ้าของห้อง")
@has_relay_perms()
@app_commands.describe(
    index=f"หมายเลขบอทพูด (1-{len(SPEAKER_TOKENS)})",
    owner="เจ้าของห้องย่อย (บอทจะเข้า/ออกตามคนนี้เข้า-ออกห้อง)",
    channel="ห้องย่อยที่จะผูกไว้"
)
async def relay_bindspeaker(interaction: discord.Interaction, index: int, owner: discord.Member, channel: discord.VoiceChannel):
    if index < 1 or index > len(speaker_bots):
        return await interaction.response.send_message(f"❌ หมายเลขบอทพูดต้องอยู่ระหว่าง 1-{len(speaker_bots)}", ephemeral=True)

    idx0 = index - 1
    speaker_bindings[idx0] = {"owner_id": owner.id, "channel_id": channel.id}
    await interaction.response.send_message(
        f"🔗 ผูกบอทพูดตัวที่ {index} กับ {owner.mention} ในห้อง {channel.mention} แล้ว\n"
        f"ต่อไปนี้: {owner.mention} เข้าห้องนี้ → บอทพูดตัวที่ {index} ตามเข้าอัตโนมัติ / {owner.mention} ออก → บอทตามออกอัตโนมัติ",
        ephemeral=True
    )


@relay_group.command(name="unbind", description="ยกเลิกการผูกอัตโนมัติทั้งหมด (บอทจะไม่ตามเข้า-ออกใครอีก)")
@has_relay_perms()
async def relay_unbind(interaction: discord.Interaction):
    listener_binding["owner_id"] = None
    listener_binding["channel_id"] = None
    speaker_bindings.clear()
    await interaction.response.send_message("🔓 ยกเลิกการผูกอัตโนมัติทั้งหมดแล้ว (ยังใช้คำสั่งแบบ manual ได้ปกติ)", ephemeral=True)


@relay_group.command(name="status", description="เช็คสถานะการถ่ายทอดเสียงตอนนี้")
async def relay_status(interaction: discord.Interaction):
    lines = []

    if listener_binding["owner_id"]:
        owner = interaction.guild.get_member(listener_binding["owner_id"])
        ch = interaction.guild.get_channel(listener_binding["channel_id"])
        lines.append(f"🔗 บอทฟัง ผูกกับ {owner.mention if owner else '?'} ในห้อง {ch.mention if ch else '?'}")

    for idx, binding in speaker_bindings.items():
        owner = interaction.guild.get_member(binding["owner_id"])
        ch = interaction.guild.get_channel(binding["channel_id"])
        lines.append(f"🔗 บอทพูดตัวที่ {idx + 1} ผูกกับ {owner.mention if owner else '?'} ในห้อง {ch.mention if ch else '?'}")

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
    """บอทฟัง: ตามเจ้าของห้องหลักเข้า-ออกอัตโนมัติ ตาม /relay bindlistener ที่ตั้งไว้"""
    if listener_binding["owner_id"] is None:
        return
    if member.id != listener_binding["owner_id"]:
        return

    bound_channel_id = listener_binding["channel_id"]
    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None

    if after_id == bound_channel_id and before_id != bound_channel_id:
        try:
            await start_listening(after.channel)
        except Exception as e:
            log.error(f"[Listener] Auto-join ล้มเหลว: {e}")
    elif before_id == bound_channel_id and after_id != bound_channel_id:
        await stop_listening()


def make_speaker_ready_handler(index: int):
    async def on_ready():
        log.info(f"[Speaker {index + 1}] Logged in as {speaker_bots[index].user}")
    return on_ready


def make_speaker_voice_handler(index: int):
    async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """บอทพูดตัวที่ index: ตามเจ้าของห้องย่อยเข้า-ออกอัตโนมัติ ตาม /relay bindspeaker ที่ตั้งไว้"""
        binding = speaker_bindings.get(index)
        if not binding or member.id != binding["owner_id"]:
            return

        bound_channel_id = binding["channel_id"]
        before_id = before.channel.id if before.channel else None
        after_id = after.channel.id if after.channel else None

        if after_id == bound_channel_id and before_id != bound_channel_id:
            try:
                await start_speaking(index, after.channel)
            except Exception as e:
                log.error(f"[Speaker {index + 1}] Auto-join ล้มเหลว: {e}")
        elif before_id == bound_channel_id and after_id != bound_channel_id:
            await stop_speaking(index)

    return on_voice_state_update


for i, sbot in enumerate(speaker_bots):
    sbot.event(make_speaker_ready_handler(i))
    sbot.event(make_speaker_voice_handler(i))


async def main():
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(listener_bot)
        for sbot in speaker_bots:
            await stack.enter_async_context(sbot)

        tasks = [listener_bot.start(LISTENER_TOKEN)]
        for token, sbot in zip(SPEAKER_TOKENS, speaker_bots):
            tasks.append(sbot.start(token))
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
