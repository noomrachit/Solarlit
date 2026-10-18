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

/relay setrole <role> จำกัดให้กระจายเสียงเฉพาะคนที่มีบทบาทนี้ในห้องหลัก — คนอื่นยังพูดคุยในห้องหลัก
ได้ตามปกติ (ไม่ได้ถูกตัดไมค์/เตะออก) แค่เสียงของเขาจะไม่ถูกป้อนเข้า mixer จึงไม่ถูกส่งต่อไปห้องย่อย
ใช้ /relay clearrole เพื่อยกเลิกและกลับไปกระจายเสียงทุกคนตามเดิม
"""

import os
import asyncio
import logging
import time
import functools
import threading
from collections import defaultdict, deque
from contextlib import AsyncExitStack
from io import BytesIO
from datetime import timedelta, timezone
from typing import Optional, Union

import numpy as np

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import voice_recv
from dotenv import load_dotenv
from aiohttp import web

import hashlib

import access
import slip_verify

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("voice-relay")

TH_TZ = timezone(timedelta(hours=7))
WEBSITE_URL = os.getenv("WEBSITE_URL", "https://website-preview-production-75cb.up.railway.app")

LISTENER_TOKEN = os.getenv("LISTENER_BOT_TOKEN")
# ภูมิภาคเซิร์ฟเสียงที่บังคับให้ทุกห้องใช้ (ตรงกับที่บอทรันอยู่) กันเสียงอ้อมไปทวีปอื่นจนแตก — ตั้งเป็นค่าว่างเพื่อปิด
VOICE_REGION = os.getenv("RELAY_VOICE_REGION", "singapore").strip() or None
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


# ── ตัวเฝ้าเสียง: ตัวนับปัญหาเสียง (รีเซ็ตทุกนาทีหลังเขียนบันทึก HEALTH) ──
HEALTH = {"underrun": 0, "overflow": 0, "decode_err": 0, "pump_late": 0, "reconnect": 0}
RECONNECT_EVENTS: list = []  # (guild_id, ข้อความ) ที่เกิดในนาทีนี้ — ส่งเป็นประกาศในห้องสถานะ
_HEALTH_LOCK = threading.Lock()


def health_inc(key: str, n: int = 1):
    with _HEALTH_LOCK:
        HEALTH[key] = HEALTH.get(key, 0) + n


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
                    health_inc("decode_err")
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


def _patch_voice_recv_jitter_buffer():
    """ให้ buffer ของ voice_recv ทนต่อ jitter ปกติมากขึ้นก่อนจะถือว่าแพ็กเก็ตหาย.

    ไลบรารีรุ่นใหม่ย้าย HeapJitterBuffer ไปไว้ใน voice_recv.buffer แล้ว opus.py นำเข้าเป็นชื่อ
    JitterBuffer — จึงหาคลาสจาก buffer ก่อน แล้วแทนชื่อ JitterBuffer ใน opus ที่ decoder ใช้สร้าง buffer
    """
    try:
        from discord.ext.voice_recv import opus as vr_opus
    except Exception as e:
        log.warning(f"ไม่พบโมดูล voice_recv.opus สำหรับปรับ JitterBuffer (ข้ามได้ ไม่ critical): {e}")
        return

    heap_cls = None
    try:
        from discord.ext.voice_recv import buffer as vr_buffer
        heap_cls = getattr(vr_buffer, "HeapJitterBuffer", None)
    except Exception:
        pass
    if heap_cls is None:
        heap_cls = getattr(vr_opus, "HeapJitterBuffer", None)
    if heap_cls is None:
        log.warning("ไม่พบ HeapJitterBuffer ทั้งใน voice_recv.buffer และ voice_recv.opus")
        return

    if getattr(vr_opus, "JitterBuffer", None) is None:
        log.warning("ไม่พบ JitterBuffer ใน voice_recv.opus สำหรับแพตช์")
        return

    vr_opus.JitterBuffer = functools.partial(heap_cls, maxsize=20, prefsize=4, prefill=2)
    log.info("แพตช์ JitterBuffer สำเร็จ: maxsize=20, prefsize=4, prefill=2")


_patch_voice_recv_jitter_buffer()

FRAME_BYTES = 3840  # เฟรมเสียง 20ms ที่ 48kHz, 16-bit, stereo (มาตรฐานของ Discord voice)


class Mixer:
    """ตัวผสมเสียง (Mixer): รวมเสียงทุกคนที่พูดพร้อมกันในห้องหลักให้กลายเป็นเฟรมเดียว — 1 instance ต่อ 1 RelayUnit"""

    # กันเสียงแตก/ขาดๆ หายๆ: แพ็กเก็ตจาก Discord มาไม่สม่ำเสมอ (มาเป็นก้อน) ถ้าดึงทันทีที่มี 1 เฟรม
    # พอแพ็กเก็ตถัดไปมาช้านิดเดียว เฟรมนั้นจะกลายเป็นความเงียบแทรกกลางคำพูด = เสียงแตก
    # จึงรอให้มีสำรองก่อน PREFILL เฟรม (60ms) ค่อยเริ่มดึง และตัดส่วนเกิน MAX เฟรม กันดีเลย์สะสม
    PREFILL_FRAMES = 3
    MAX_FRAMES = 12

    def __init__(self):
        self.buffers: dict = defaultdict(bytearray)
        self.primed: set = set()

    def feed(self, user_id: int, pcm: bytes):
        buf = self.buffers[user_id]
        buf.extend(pcm)
        limit = FRAME_BYTES * self.MAX_FRAMES
        if len(buf) > limit:
            del buf[:len(buf) - limit]

    def pop_frame(self) -> bytes:
        mixed = None
        for uid, buf in list(self.buffers.items()):
            if uid not in self.primed:
                if len(buf) < FRAME_BYTES * self.PREFILL_FRAMES:
                    continue
                self.primed.add(uid)
            if len(buf) >= FRAME_BYTES:
                chunk = bytes(buf[:FRAME_BYTES])
                del buf[:FRAME_BYTES]
                # ใช้ numpy แทน struct/list loop เดิม เร็วกว่ามาก ลดโอกาสจังหวะเฟรมเพี้ยนจน CPU ตามไม่ทัน
                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.int32)
                mixed = samples if mixed is None else mixed + samples
            if len(buf) < FRAME_BYTES:
                # หมดสำรอง (หยุดพูด หรือเน็ตสะดุด) -> กลับไปรอสำรองใหม่ เศษที่ไม่ครบเฟรมทิ้งได้
                self.primed.discard(uid)
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


class FrameBuffer:
    """
    คิวเฟรมเสียงของบอทพูดแต่ละตัว — ปลอดภัยข้ามเธรด (ตัวเล่นเสียงของ discord.py อ่านจากเธรดของมันเอง
    ส่วน mixer_pump เขียนจาก event loop เดิมใช้ asyncio.Queue ซึ่งไม่ปลอดภัยข้ามเธรด)
    มีสำรองก่อนเล่น (prefill) กันนาฬิกา 2 ฝั่งเหลื่อมกันจนเกิดช่องเงียบสลับเสียง = เสียงแตก
    """
    PREFILL = 3   # 60ms
    MAXLEN = 15   # 300ms เกินนี้ทิ้งเฟรมเก่าสุด กันดีเลย์สะสม

    def __init__(self):
        self._q = deque(maxlen=self.MAXLEN)
        self._lock = threading.Lock()
        self._playing = False

    def put(self, frame: bytes):
        with self._lock:
            if len(self._q) == self.MAXLEN:
                health_inc("overflow")  # ค้างเกิน 300ms ต้องทิ้งเฟรมเก่า = ดีเลย์/หน่วง
            self._q.append(frame)

    def get(self) -> Optional[bytes]:
        with self._lock:
            if not self._playing:
                if len(self._q) < self.PREFILL:
                    return None
                self._playing = True
            if not self._q:
                self._playing = False
                health_inc("underrun")  # กำลังเล่นแล้วเฟรมไม่มา = เสียงขาด/แตก
                return None
            return self._q.popleft()

    def clear(self):
        with self._lock:
            self._q.clear()
            self._playing = False


SILENCE_FRAME = b"\x00" * FRAME_BYTES


class QueueAudioSource(discord.AudioSource):
    """ดึงเฟรมที่ mix แล้วจาก FrameBuffer เฉพาะของบอทพูดตัวนั้นๆ"""

    def __init__(self, queue: "FrameBuffer"):
        self.queue = queue

    def read(self) -> bytes:
        frame = self.queue.get()
        return SILENCE_FRAME if frame is None else frame

    def is_opus(self) -> bool:
        return False


def _member_voice_channel(guild, user_id: int):
    """ห้องเสียงที่คนนี้อยู่ตอนนี้ในเซิร์ฟนี้ (None = ไม่ได้อยู่ห้องเสียง)"""
    if guild is None:
        return None
    for ch in list(guild.voice_channels) + list(guild.stage_channels):
        if any(m.id == user_id for m in ch.members):
            return ch
    return None


def follow_target(bot, binding: dict):
    """
    ห้องที่บอทควรอยู่ตอนนี้ตามการผูก:
    - ล็อกคน (user_id): ไปห้องที่คนนั้นอยู่ (ถ้าระบุ channel_id ต้องเป็นห้องนั้นเท่านั้น) / คนนั้นไม่อยู่ = None
    - ผูกห้อง (channel_id อย่างเดียว): ห้องนั้น ถ้ามีคนอยู่
    """
    if not binding:
        return None
    uid = binding.get("user_id")
    cid = binding.get("channel_id")
    if uid:
        ch = _member_voice_channel(bot.get_guild(binding.get("guild_id") or 0), uid)
        if ch is None or (cid and ch.id != cid):
            return None
        return ch
    if cid:
        ch = bot.get_channel(cid)
        if ch is not None and _count_humans(ch) > 0:
            return ch
    return None


def _count_humans(channel) -> int:
    """นับจำนวนคนที่ไม่ใช่บอทในห้องเสียง/สเตจ"""
    if channel is None:
        return 0
    return len([m for m in channel.members if not m.bot])


async def ensure_voice_region(channel) -> None:
    """ตั้งภูมิภาคเสียงของห้องให้ตรง VOICE_REGION ก่อนบอทเข้า (ต้องมีสิทธิ์ Manage Channels — ไม่มีก็ข้าม ไม่ล่ม)"""
    if VOICE_REGION is None or channel is None:
        return
    if getattr(channel, "rtc_region", None) == VOICE_REGION:
        return
    try:
        await channel.edit(rtc_region=VOICE_REGION, reason="Voice Relay: ตั้งภูมิภาคเดียวกันกันเสียงแตก")
        log.info(f"ตั้งภูมิภาคห้อง {channel.name} เป็น {VOICE_REGION} แล้ว")
    except discord.Forbidden:
        log.warning(f"ตั้งภูมิภาคห้อง {channel.name} ไม่ได้: บอทไม่มีสิทธิ์ Manage Channels")
    except Exception as e:
        log.warning(f"ตั้งภูมิภาคห้อง {channel.name} ไม่ได้: {e}")


class SpeakerPool:
    """
    บอทพูดทั้งหมด (แชร์กันทุก RelayUnit) — จองได้ครั้งละ 1 unit ต่อ 1 index เท่านั้น
    (เพราะบอท 1 ตัวเข้าได้แค่ 1 ห้องเสียงพร้อมกันอยู่แล้ว เป็นข้อจำกัดของ Discord เอง ไม่ใช่ของโค้ดนี้)
    """

    def __init__(self, speaker_bots: list):
        self.speaker_bots = speaker_bots
        self.queues: list = [FrameBuffer() for _ in speaker_bots]
        self.owner: dict = {}              # index -> RelayUnit ที่กำลังใช้ index นี้ "สด" อยู่ตอนนี้ (connect อยู่จริง)
        self.channel_map: dict = {}        # index -> channel_id (ไว้โชว์ /relay status)
        self.guild_map: dict = {}          # index -> guild_id (ไว้นับโควต้าลูกน้องต่อเซิร์ฟเวอร์)
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
        self.guild_map.pop(index, None)
        self.queues[index].clear()

    def indices_for(self, unit) -> list:
        return [i for i, owner in self.owner.items() if owner is unit]

    def total_in_use(self) -> int:
        return len(self.owner)

    def in_use_for_guild(self, guild_id: int) -> int:
        return sum(1 for g in self.guild_map.values() if g == guild_id)

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


class RelayUnit:
    """
    หนึ่ง "หัวหน้า" (listener bot) พร้อม state ที่เดิมเป็น global เดี่ยว — ตอนนี้แยกเป็นของตัวเองแต่ละตัว
    ทุก unit ยืมบอทพูดจาก SpeakerPool กลางตัวเดียวกัน (ดูคำเตือนเรื่อง bindspeaker ที่หัวไฟล์)
    """

    def __init__(self, name: str, token: str, pool: SpeakerPool, unit_no: int = 1):
        self.name = name
        self.unit_no = unit_no  # หัวหน้าตัวที่เท่าไหร่ (เทียบกับจำนวนหัวหน้าที่เซิร์ฟเวอร์จ่าย)
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
        # เช็คสิทธิ์ก่อนทุกคำสั่ง (ยกเว้น /admin ของแอดมิน)
        self.tree.interaction_check = self._access_check

        self._build_commands()
        self._register_events()

    # ── สิทธิ์และโควต้า (ดู access.py) ──

    async def _access_check(self, interaction: discord.Interaction) -> bool:
        cmd = interaction.command.qualified_name if interaction.command else ""
        if cmd.startswith(("admin", "pay")) or not interaction.guild:
            return True
        acc = await access.get_access(interaction.guild.id)
        msg = None
        if acc is None:
            msg = ("🔒 เซิร์ฟเวอร์นี้ยังไม่ได้เปิดใช้ Voice Relay หรือหมดอายุแล้ว\n"
                   f"ราคาตัวละ 10 บาท/เดือน เริ่ม 60 บาท โอนพร้อมเพย์แล้วส่งสลิปด้วย `/pay` ใช้ได้ทันทีชั่วคราว {access.PROVISIONAL_DAYS} วันระหว่างรอแอดมินตรวจ: {WEBSITE_URL}/#plans\n"
                   f"แจ้งเลขเซิร์ฟเวอร์นี้ให้แอดมินด้วย: `{interaction.guild.id}`")
        elif self.unit_no > acc.listeners:
            msg = f"🔒 แพ็กเกจของเซิร์ฟเวอร์นี้ใช้หัวหน้าได้ {acc.listeners} ตัว — ใช้คำสั่งของหัวหน้าตัวแรกแทน หรือซื้อหัวหน้าเพิ่ม"
        if msg:
            try:
                await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass
            return False
        return True

    async def _quota_ok(self, guild_id: int) -> bool:
        """เช็คก่อนเปิดลูกน้องเพิ่ม: จำนวนลูกน้องที่ใช้อยู่ในเซิร์ฟเวอร์นี้ (ทุกหัวหน้ารวมกัน) ต้องไม่เกินที่จ่าย"""
        acc = await access.get_access(guild_id)
        limit = acc.speakers if acc else 0
        if self.pool.in_use_for_guild(guild_id) >= limit:
            self.last_speaking_error = "quota"
            log.warning(f"[{self.name}] ไม่เปิดลูกน้องเพิ่มในเซิร์ฟ {guild_id}: ครบโควต้า {limit} ตัวแล้ว")
            return False
        return True

    # ── connect/disconnect ──

    async def start_listening(self, channel: Union[discord.VoiceChannel, discord.StageChannel]):
        """เริ่มให้บอทฟังเข้าห้องหลักและดักจับเสียง (ใช้ได้ทั้งเรียกเองผ่านคำสั่ง และเรียกอัตโนมัติ)"""
        if self.relay_active:
            return
        acc = await access.get_access(channel.guild.id)
        if acc is None or self.unit_no > acc.listeners:
            log.warning(f"[{self.name}] ไม่เข้าห้องหลักในเซิร์ฟ {channel.guild.id}: ยังไม่ได้เปิดสิทธิ์")
            return
        await ensure_voice_region(channel)
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

        await ensure_voice_region(channel)  # แก้ผ่านหัวหน้า (มักมีสิทธิ์มากกว่าลูกน้อง)
        vc = await target_channel.connect()
        vc.play(QueueAudioSource(self.pool.queues[index]))
        self.pool.channel_map[index] = channel.id
        self.pool.guild_map[index] = channel.guild.id
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
                        f"❌ ใช้ลูกน้องครบตามแพ็กเกจแล้ว ซื้อเพิ่มได้ตัวละ 10 บาท: {WEBSITE_URL}/#plans",
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
            unit.listener_binding.clear()
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

        @relay_group.command(
            name="bindspeakers",
            description="ผูกบอทพูดสูงสุด 5 ตัวพร้อมกันในคำสั่งเดียว (เลือกตัวว่างให้อัตโนมัติ) — เข้า/ออกห้องตามคนเข้า-ออก"
        )
        @has_relay_perms()
        @app_commands.describe(
            channel1="ห้องย่อยที่ 1 (บังคับ)",
            channel2="ห้องย่อยที่ 2 (ไม่ใส่ = ข้าม)",
            channel3="ห้องย่อยที่ 3 (ไม่ใส่ = ข้าม)",
            channel4="ห้องย่อยที่ 4 (ไม่ใส่ = ข้าม)",
            channel5="ห้องย่อยที่ 5 (ไม่ใส่ = ข้าม)",
        )
        async def relay_bindspeakers(
            interaction: discord.Interaction,
            channel1: discord.VoiceChannel,
            channel2: Optional[discord.VoiceChannel] = None,
            channel3: Optional[discord.VoiceChannel] = None,
            channel4: Optional[discord.VoiceChannel] = None,
            channel5: Optional[discord.VoiceChannel] = None,
        ):
            """
            ผูกบอทพูดหลายตัวพร้อมกันในคำสั่งเดียว — หาเลขบอทพูดที่ "ว่าง" (ไม่มีใคร static-bind ไว้เลย
            ไม่ว่าจะเป็นของ unit นี้หรือหัวหน้าตัวอื่น) ให้อัตโนมัติทีละห้องตามลำดับที่กรอกมา
            ต่างจาก /relay bindspeaker ตรงที่ไม่ต้องระบุเลขบอทพูดเอง — เหมาะกับตอนเพิ่มบอทพูดมาทั้งชุด (เช่น 5 ตัว)
            แล้วอยากผูกเข้าห้องย่อย 5 ห้องรวดเดียว
            """
            channels = [c for c in (channel1, channel2, channel3, channel4, channel5) if c is not None]

            success_lines = []
            fail_lines = []
            for channel in channels:
                idx0 = None
                for i in range(len(pool.speaker_bots)):
                    if pool.bind_owner.get(i) is None:
                        idx0 = i
                        break

                if idx0 is None:
                    fail_lines.append(f"❌ {channel.mention}: ไม่มีบอทพูดว่างเหลือแล้ว (ผูกครบทั้ง {len(pool.speaker_bots)} ตัว)")
                    continue

                other = pool.try_bind(idx0, unit)
                if other is not None:
                    # แทบไม่เกิด เพราะเพิ่งเช็ค bind_owner ว่าง แต่กันไว้เผื่อ race ระหว่างคำสั่งชนกัน
                    fail_lines.append(f"❌ {channel.mention}: บอทพูดตัวที่ {idx0 + 1} ถูก {other.name} ผูกไปพอดี ลองใหม่อีกครั้ง")
                    continue

                unit.speaker_bindings[idx0] = {"channel_id": channel.id}
                success_lines.append(f"🔗 บอทพูดตัวที่ {idx0 + 1} → {channel.mention}")

            header = f"ผูกสำเร็จ {len(success_lines)}/{len(channels)} ห้อง ({unit.name})"
            lines = [header] + success_lines + fail_lines
            lines.append("")
            lines.append("ต่อไปนี้: มีคนเข้าห้องย่อยไหน (คนแรก) → บอทพูดที่ผูกไว้ตามเข้าอัตโนมัติ / ห้องว่าง → ตามออกอัตโนมัติ")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @relay_group.command(
            name="go",
            description="ตั้งอัตโนมัติครั้งเดียว: หัวหน้า+ลูกน้องเข้า/ออกห้องตามคนเข้า-ออก (สูงสุด 20 ห้อง)"
        )
        @has_relay_perms()
        @app_commands.describe(
            source="ห้องหลักที่หัวหน้าจะฟัง (Voice หรือ Stage)",
            room1="ห้องลูกน้อง 1 (บังคับ)",
            room2="ห้องลูกน้อง 2",
            room3="ห้องลูกน้อง 3",
            room4="ห้องลูกน้อง 4",
            room5="ห้องลูกน้อง 5",
            room6="ห้องลูกน้อง 6",
            room7="ห้องลูกน้อง 7",
            room8="ห้องลูกน้อง 8",
            room9="ห้องลูกน้อง 9",
            room10="ห้องลูกน้อง 10",
            room11="ห้องลูกน้อง 11",
            room12="ห้องลูกน้อง 12",
            room13="ห้องลูกน้อง 13",
            room14="ห้องลูกน้อง 14",
            room15="ห้องลูกน้อง 15",
            room16="ห้องลูกน้อง 16",
            room17="ห้องลูกน้อง 17",
            room18="ห้องลูกน้อง 18",
            room19="ห้องลูกน้อง 19",
            room20="ห้องลูกน้อง 20",
        )
        async def relay_go(
            interaction: discord.Interaction,
            source: Union[discord.VoiceChannel, discord.StageChannel],
            room1: discord.VoiceChannel,
            room2: Optional[discord.VoiceChannel] = None,
            room3: Optional[discord.VoiceChannel] = None,
            room4: Optional[discord.VoiceChannel] = None,
            room5: Optional[discord.VoiceChannel] = None,
            room6: Optional[discord.VoiceChannel] = None,
            room7: Optional[discord.VoiceChannel] = None,
            room8: Optional[discord.VoiceChannel] = None,
            room9: Optional[discord.VoiceChannel] = None,
            room10: Optional[discord.VoiceChannel] = None,
            room11: Optional[discord.VoiceChannel] = None,
            room12: Optional[discord.VoiceChannel] = None,
            room13: Optional[discord.VoiceChannel] = None,
            room14: Optional[discord.VoiceChannel] = None,
            room15: Optional[discord.VoiceChannel] = None,
            room16: Optional[discord.VoiceChannel] = None,
            room17: Optional[discord.VoiceChannel] = None,
            room18: Optional[discord.VoiceChannel] = None,
            room19: Optional[discord.VoiceChannel] = None,
            room20: Optional[discord.VoiceChannel] = None,
        ):
            """
            ผูกอัตโนมัติทั้งชุดในคำสั่งเดียว (เหมือน bindlistener + bindspeakers รวมกัน)
            - หัวหน้า: เข้าห้องหลักเมื่อมีคน / ออกเมื่อห้องว่าง
            - ลูกน้อง: แต่ละห้องได้ลูกน้องที่ยังไม่ถูกผูก 1 ตัว เข้าเมื่อมีคน / ออกเมื่อห้องว่าง
            - ห้องไหนมีคนอยู่แล้วตอนสั่ง จะเข้าให้ทันที
            """
            await interaction.response.defer(ephemeral=True)

            rooms, seen = [], {source.id}
            for r in (room1, room2, room3, room4, room5, room6, room7, room8, room9, room10, room11, room12, room13, room14, room15, room16, room17, room18, room19, room20):
                if r is not None and r.id not in seen:
                    seen.add(r.id)
                    rooms.append(r)

            # ล้างการผูกเดิมของหัวหน้าตัวนี้ก่อน กันเลขลูกน้องค้าง
            for idx0 in list(unit.speaker_bindings.keys()):
                pool.release_bind(idx0, unit)
            unit.speaker_bindings.clear()

            unit.listener_binding.clear()
            unit.listener_binding["channel_id"] = source.id
            lines = [f"🔗 {unit.name} ผูกกับห้องหลัก {source.mention}"]
            if _count_humans(source) > 0 and not unit.relay_active:
                try:
                    await unit.start_listening(source)
                    lines[0] += " (มีคนอยู่ เข้าแล้ว)"
                except Exception as e:
                    lines.append(f"⚠️ เข้าห้องหลักตอนนี้ไม่ได้: {e} (จะลองใหม่เมื่อมีคนเข้า)")

            ok = 0
            for r in rooms:
                idx0 = next((i for i in range(len(pool.speaker_bots)) if pool.bind_owner.get(i) is None), None)
                if idx0 is None or pool.try_bind(idx0, unit) is not None:
                    lines.append(f"❌ {r.mention}: ลูกน้องว่างไม่พอ (มี {len(pool.speaker_bots)} ตัว)")
                    continue
                unit.speaker_bindings[idx0] = {"channel_id": r.id}
                ok += 1
                note = ""
                if unit.relay_active and _count_humans(r) > 0 and pool.owner.get(idx0) is None:
                    try:
                        joined = await unit.start_speaking_at(idx0, r)
                        note = " (มีคนอยู่ เข้าแล้ว)" if joined else " (เข้าไม่ได้ — เชิญบอทเข้าเซิร์ฟหรือยัง?)"
                    except Exception as e:
                        note = f" (เข้าไม่ได้: {e})"
                lines.append(f"🔗 ลูกน้อง {idx0 + 1} → {r.mention}{note}")

            lines.insert(0, f"ผูกอัตโนมัติสำเร็จ {ok}/{len(rooms)} ห้อง")
            lines.append("")
            lines.append("ต่อไปนี้: ห้องไหนมีคนเข้า → บอทเข้าเอง / ห้องว่าง → บอทออกเอง · ยกเลิกด้วย `/relay unbind`")
            await interaction.followup.send("\n".join(lines), ephemeral=True)

        @relay_group.command(name="name", description="ตั้งชื่อบอทในเซิร์ฟนี้: หัวหน้า และลูกน้องทุกตัว (เรียงเลข 1, 2, 3...)")
        @has_relay_perms()
        @app_commands.describe(
            head="ชื่อของหัวหน้าตัวนี้ (พิมพ์ - เพื่อคืนชื่อเดิม)",
            speakers="ชื่อนำหน้าของลูกน้อง เช่น 'ห้องทีม' → ห้องทีม 1, ห้องทีม 2 (พิมพ์ - เพื่อคืนชื่อเดิม)",
        )
        async def relay_name(interaction: discord.Interaction,
                             head: Optional[app_commands.Range[str, 1, 32]] = None,
                             speakers: Optional[app_commands.Range[str, 1, 28]] = None):
            if head is None and speakers is None:
                return await interaction.response.send_message("ใส่ชื่ออย่างน้อย 1 ช่อง (head หรือ speakers)", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            guild_id = interaction.guild.id
            lines = []
            if head is not None:
                me = interaction.guild.me
                try:
                    await me.edit(nick=None if head.strip() == "-" else head.strip())
                    lines.append(f"✅ {unit.name} → " + ("คืนชื่อเดิมแล้ว" if head.strip() == "-" else head.strip()))
                except discord.Forbidden:
                    lines.append(f"❌ {unit.name}: บอทไม่มีสิทธิ์ Change Nickname ในเซิร์ฟนี้")
                except discord.HTTPException as e:
                    lines.append(f"❌ {unit.name}: {e.text or e}")
            if speakers is not None:
                prefix = speakers.strip()
                acc = await access.get_access(guild_id)
                limit = acc.speakers if acc else 0
                done = fail = 0
                no = 0
                for sbot in pool.speaker_bots:
                    if no >= limit:
                        break
                    g = sbot.get_guild(guild_id)
                    if g is None or g.me is None:
                        continue  # ลูกน้องตัวนี้ยังไม่ได้เชิญเข้าเซิร์ฟนี้
                    no += 1
                    try:
                        await g.me.edit(nick=None if prefix == "-" else f"{prefix} {no}")
                        done += 1
                    except discord.HTTPException:
                        fail += 1
                    await asyncio.sleep(0.5)  # กันโดน rate limit ตอนเปลี่ยนหลายตัว
                if no == 0:
                    lines.append("⚠️ ยังไม่มีบอทลูกน้องอยู่ในเซิร์ฟนี้ — เชิญลูกน้องก่อน")
                else:
                    what = "คืนชื่อเดิม" if prefix == "-" else f"ตั้งชื่อ {prefix} 1–{no}"
                    lines.append(f"✅ ลูกน้อง {what} สำเร็จ {done} ตัว" + (f" · ไม่สำเร็จ {fail} ตัว (ไม่มีสิทธิ์ Change Nickname)" if fail else ""))
            await interaction.followup.send("\n".join(lines), ephemeral=True)

        @relay_group.command(name="followleader", description="หัวหน้าล็อกตามคนที่เลือก: คนนั้นเข้าห้องไหน หัวหน้าเข้าไปฟังห้องนั้น ออกเมื่อคนนั้นออก")
        @has_relay_perms()
        @app_commands.describe(
            user="คนที่จะให้หัวหน้าตาม (เช่น คนสั่งวอร์)",
            channel="จำกัดเฉพาะห้องนี้ (ไม่ใส่ = ตามไปทุกห้องที่คนนั้นเข้า)",
        )
        async def relay_followleader(interaction: discord.Interaction, user: discord.Member,
                                     channel: Optional[Union[discord.VoiceChannel, discord.StageChannel]] = None):
            if user.bot:
                return await interaction.response.send_message("❌ เลือกคน ไม่ใช่บอท", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            unit.listener_binding.clear()
            unit.listener_binding.update(user_id=user.id, guild_id=interaction.guild.id,
                                         channel_id=channel.id if channel else None)
            target = follow_target(unit.bot, unit.listener_binding)
            note = ""
            if target is not None:
                cur = _vc_of(unit.bot, interaction.guild.id)
                if not (_vc_ok(cur) and cur.channel.id == target.id):
                    if unit.relay_active:
                        await unit.stop_listening()
                    try:
                        await unit.start_listening(target)
                        note = f"\n🎧 {user.display_name} อยู่ใน {target.mention} ตอนนี้ — เข้าแล้ว"
                    except Exception as e:
                        note = f"\n⚠️ เข้าห้องตอนนี้ไม่ได้: {e}"
            where = f"เฉพาะห้อง {channel.mention}" if channel else "ทุกห้องที่เขาเข้า"
            await interaction.followup.send(
                f"🔒 {unit.name} ล็อกตาม {user.mention} ({where})\n"
                f"เขาเข้าห้อง → หัวหน้าเข้าตาม / ย้ายห้อง → ย้ายตาม / ออก → หัวหน้าออก{note}\n"
                f"ยกเลิกด้วย `/relay unbind`", ephemeral=True)

        @relay_group.command(name="followspeaker", description="ลูกน้องล็อกตามคนที่เลือก: คนนั้นเข้าห้องไหน ลูกน้องเข้าไปเล่นเสียงห้องนั้น")
        @has_relay_perms()
        @app_commands.describe(
            user="คนที่จะให้ลูกน้องตาม",
            channel="จำกัดเฉพาะห้องนี้ (ไม่ใส่ = ตามไปทุกห้องที่คนนั้นเข้า)",
        )
        async def relay_followspeaker(interaction: discord.Interaction, user: discord.Member,
                                      channel: Optional[discord.VoiceChannel] = None):
            if user.bot:
                return await interaction.response.send_message("❌ เลือกคน ไม่ใช่บอท", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            # ตามคนเดิมอยู่แล้ว = ใช้ลูกน้องตัวเดิม
            idx0 = next((i for i, b in unit.speaker_bindings.items() if b.get("user_id") == user.id), None)
            if idx0 is None:
                idx0 = next((i for i in range(len(pool.speaker_bots)) if pool.bind_owner.get(i) is None), None)
                if idx0 is None or pool.try_bind(idx0, unit) is not None:
                    return await interaction.followup.send(f"❌ ลูกน้องว่างไม่พอ (มี {len(pool.speaker_bots)} ตัว)", ephemeral=True)
            binding = {"user_id": user.id, "guild_id": interaction.guild.id,
                       "channel_id": channel.id if channel else None}
            unit.speaker_bindings[idx0] = binding
            note = ""
            target = follow_target(unit.bot, binding)
            if target is not None and unit.relay_active:
                if pool.owner.get(idx0) is unit and pool.channel_map.get(idx0) != target.id:
                    await unit.stop_speaking(idx0)
                if pool.owner.get(idx0) is None:
                    try:
                        if await unit.start_speaking_at(idx0, target):
                            note = f"\n🔊 {user.display_name} อยู่ใน {target.mention} ตอนนี้ — เข้าแล้ว"
                    except Exception as e:
                        note = f"\n⚠️ เข้าห้องตอนนี้ไม่ได้: {e}"
            elif target is not None:
                note = "\n⏳ หัวหน้ายังไม่ได้ฟังห้องหลัก ลูกน้องจะเข้าเมื่อหัวหน้าเริ่มฟัง"
            where = f"เฉพาะห้อง {channel.mention}" if channel else "ทุกห้องที่เขาเข้า"
            await interaction.followup.send(
                f"🔒 ลูกน้อง {idx0 + 1} ล็อกตาม {user.mention} ({where})\n"
                f"เขาเข้าห้อง → ลูกน้องเข้าเล่นเสียง / ย้ายห้อง → ย้ายตาม / ออก → ลูกน้องออก{note}\n"
                f"ยกเลิกด้วย `/relay unbind`", ephemeral=True)

        @relay_group.command(name="unbind", description="ยกเลิกการผูกอัตโนมัติทั้งหมดของหัวหน้าตัวนี้ (บอทจะไม่ตามเข้า-ออกห้องไหนอีก)")
        @has_relay_perms()
        async def relay_unbind(interaction: discord.Interaction):
            unit.listener_binding.clear()
            unit.listener_binding["channel_id"] = None
            for idx0 in list(unit.speaker_bindings.keys()):
                pool.release_bind(idx0, unit)
            unit.speaker_bindings.clear()
            await interaction.response.send_message("🔓 ยกเลิกการผูกอัตโนมัติทั้งหมดแล้ว (ยังใช้คำสั่งแบบ manual ได้ปกติ)", ephemeral=True)

        @relay_group.command(name="health", description="ตัวเฝ้าเสียง: เช็คเสียงขาด/ดีเลย์/ปิง/บอทหลุด ในนาทีล่าสุด")
        async def relay_health(interaction: discord.Interaction):
            h = LAST_HEALTH
            if not h:
                return await interaction.response.send_message("⏳ ยังไม่มีข้อมูล รอประมาณ 1-2 นาทีหลังบอทเริ่มทำงาน", ephemeral=True)
            def mark(v, lim):
                return "🟢" if v <= lim else "🔴"
            lines = [
                f"🩺 สุขภาพเสียง (1 นาทีล่าสุด, <t:{int(h['at'])}:R>)",
                f"{mark(h['underrun'], 30)} เสียงขาด/แตก: {h['underrun']} ครั้ง",
                f"{mark(h['overflow'], 30)} เสียงค้าง/หน่วง (ทิ้งเฟรม): {h['overflow']} ครั้ง",
                f"{mark(h['decode_err'], 20)} แพ็กเก็ตเสียจากห้องหลัก: {h['decode_err']} ครั้ง",
                f"{mark(h['pump_late'], 10)} เครื่องประมวลผลไม่ทัน: {h['pump_late']} ครั้ง",
                f"{mark(h['max_ping_ms'], 250)} ปิงเสียงสูงสุด: {h['max_ping_ms']} ms",
                f"🔁 บอทหลุดแล้วต่อใหม่เอง: {h['reconnect']} ครั้ง",
            ]
            if h["decode_err"] > 20 or h["max_ping_ms"] > 250:
                lines.append("\n💡 แพ็กเก็ตเสีย/ปิงสูง มักเกิดจากภูมิภาคห้องไกล — ตั้งห้องเป็น Singapore")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @relay_group.command(name="healthchannel", description="ตั้งห้องข้อความเป็นห้องสถานะเสียงเรียลไทม์ + ประกาศเตือนเมื่อเสียงมีปัญหา/บอทหลุด")
        @has_relay_perms()
        @app_commands.describe(channel="ห้องข้อความที่จะให้บอทโพสต์สถานะ (แนะนำห้องที่ปิดไม่ให้คนพิมพ์)")
        async def relay_healthchannel(interaction: discord.Interaction, channel: discord.TextChannel):
            if not access.is_enabled():
                return await interaction.response.send_message("❌ ยังไม่ได้ต่อฐานข้อมูล ตั้งห้องสถานะไม่ได้", ephemeral=True)
            perms = channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                return await interaction.response.send_message(
                    f"❌ {unit.name} ไม่มีสิทธิ์ ดูห้อง / ส่งข้อความ / แนบลิงก์ ใน {channel.mention}", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            await access.set_health_channel(interaction.guild.id, channel.id)
            sent = await channel.send(embed=build_health_embed(units_ref(), pool, interaction.guild, False))
            await access.set_health_message(interaction.guild.id, sent.id)
            await interaction.followup.send(
                f"✅ ตั้ง {channel.mention} เป็นห้องสถานะแล้ว — อัปเดตทุก 1 นาที และประกาศเตือนเมื่อเสียงมีปัญหา/บอทหลุด",
                ephemeral=True)

        @relay_group.command(name="healthoff", description="ยกเลิกห้องสถานะเสียงเรียลไทม์ของเซิร์ฟนี้")
        @has_relay_perms()
        async def relay_healthoff(interaction: discord.Interaction):
            ok = await access.clear_health_channel(interaction.guild.id)
            await interaction.response.send_message("🔕 ยกเลิกห้องสถานะแล้ว" if ok else "เซิร์ฟนี้ยังไม่ได้ตั้งห้องสถานะ", ephemeral=True)

        @relay_group.command(name="status", description="เช็คสถานะการถ่ายทอดเสียงของหัวหน้าตัวนี้ตอนนี้")
        async def relay_status(interaction: discord.Interaction):
            lines = [f"— {unit.name} —"]

            if unit.listener_binding.get("user_id"):
                lines.append(f"🔒 บอทฟัง ล็อกตาม <@{unit.listener_binding['user_id']}>")
            elif unit.listener_binding.get("channel_id"):
                ch = interaction.guild.get_channel(unit.listener_binding["channel_id"])
                lines.append(f"🔗 บอทฟัง ผูกกับห้อง {ch.mention if ch else '?'} (เข้า-ออกตามคนในห้อง)")

            for idx0, binding in unit.speaker_bindings.items():
                if binding.get("user_id"):
                    lines.append(f"🔒 บอทพูดตัวที่ {idx0 + 1} ล็อกตาม <@{binding['user_id']}>")
                    continue
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
        if unit.unit_no == 1:
            self._build_admin_commands()

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

    def _build_admin_commands(self):
        """/admin — ให้แอดมิน Solarlit (ADMIN_USER_IDS) เปิดสิทธิ์รายเซิร์ฟเวอร์หลังได้สลิป"""
        admin_group = app_commands.Group(
            name="admin", description="แอดมิน Solarlit: เปิด/ปิดสิทธิ์ Voice Relay รายเซิร์ฟเวอร์",
            default_permissions=discord.Permissions(administrator=True),
        )

        def _is_admin(interaction: discord.Interaction) -> bool:
            return interaction.user.id in access.ADMIN_USER_IDS

        def _fmt(dt) -> str:
            return dt.astimezone(TH_TZ).strftime("%d/%m/%Y %H:%M") if dt else "ไม่มีวันหมด"

        async def _deny(interaction: discord.Interaction):
            await interaction.response.send_message("❌ คำสั่งนี้สำหรับแอดมิน Solarlit เท่านั้น", ephemeral=True)

        @admin_group.command(name="grant", description="เปิด/ต่ออายุสิทธิ์ Voice Relay ให้เซิร์ฟเวอร์ (หลังได้สลิป)")
        @app_commands.describe(
            guild_id="เลขเซิร์ฟเวอร์ (ลูกค้าเห็นเลขนี้ตอนกดคำสั่งแล้วติดล็อก)",
            speakers="จำนวนบอทลูกน้อง (ขั้นต่ำ 5)",
            listeners="จำนวนบอทหัวหน้า 1 หรือ 2",
            days="กี่วัน (ปกติ 30)",
            note="บันทึก เช่น เลขอ้างอิงสลิป",
            user_id="เลขผู้ใช้ที่ส่งสลิป — ใส่แล้วบอทจะ DM แจ้งเขาว่าเปิดแล้ว",
        )
        async def admin_grant(interaction: discord.Interaction, guild_id: str, speakers: app_commands.Range[int, 5, 99],
                              listeners: app_commands.Range[int, 1, 2] = 1, days: app_commands.Range[int, 1, 366] = 30,
                              note: Optional[str] = None, user_id: Optional[str] = None):
            if not _is_admin(interaction):
                return await _deny(interaction)
            if not guild_id.isdigit():
                return await interaction.response.send_message("❌ เลขเซิร์ฟเวอร์ต้องเป็นตัวเลขเท่านั้น", ephemeral=True)
            if not access.is_enabled():
                return await interaction.response.send_message("❌ ยังไม่ได้ต่อฐานข้อมูล (BILLING_DATABASE_URL) — ตอนนี้ทุกเซิร์ฟใช้ฟรีอยู่", ephemeral=True)
            acc = await access.grant(int(guild_id), listeners, speakers, days, interaction.user.id, note or "")
            if acc is None:
                return await interaction.response.send_message("❌ บันทึกไม่สำเร็จ ฐานข้อมูลต่อไม่ติด", ephemeral=True)
            summary = (f"หัวหน้า {acc.listeners} + ลูกน้อง {acc.speakers} = {acc.listeners + acc.speakers} บอท · "
                       f"{access.monthly_price(acc)} บาท/เดือน\nใช้ได้ถึง {_fmt(acc.paid_until)}")
            dm_note = ""
            if user_id and user_id.isdigit():
                try:
                    customer = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
                    g = self.bot.get_guild(int(guild_id))
                    await customer.send(f"✅ เปิด Voice Relay ให้เซิร์ฟ {g.name if g else guild_id} แล้ว\n{summary}\nใช้คำสั่ง /relay go ได้เลย")
                    dm_note = "\n📨 แจ้งลูกค้าทาง DM แล้ว"
                except Exception:
                    dm_note = "\n⚠️ ส่ง DM แจ้งลูกค้าไม่ได้ (เขาอาจปิด DM)"
            await interaction.response.send_message(f"✅ เปิดสิทธิ์เซิร์ฟ `{guild_id}` แล้ว\n{summary}{dm_note}", ephemeral=True)

        @admin_group.command(name="revoke", description="ปิดสิทธิ์ Voice Relay ของเซิร์ฟเวอร์ทันที")
        @app_commands.describe(guild_id="เลขเซิร์ฟเวอร์")
        async def admin_revoke(interaction: discord.Interaction, guild_id: str):
            if not _is_admin(interaction):
                return await _deny(interaction)
            if not guild_id.isdigit():
                return await interaction.response.send_message("❌ เลขเซิร์ฟเวอร์ต้องเป็นตัวเลขเท่านั้น", ephemeral=True)
            ok = await access.revoke(int(guild_id))
            await interaction.response.send_message(f"{'✅ ปิดสิทธิ์แล้ว' if ok else 'ℹ️ ไม่พบสิทธิ์ของ'} `{guild_id}`", ephemeral=True)

        @admin_group.command(name="list", description="ดูรายชื่อเซิร์ฟเวอร์ที่เปิดสิทธิ์ไว้")
        async def admin_list(interaction: discord.Interaction):
            if not _is_admin(interaction):
                return await _deny(interaction)
            rows = await access.list_all()
            lines = [f"ฟรีถาวร: {', '.join(f'`{g}`' for g in sorted(access.EXEMPT_GUILD_IDS))}"]
            if not access.is_enabled():
                lines.append("⚠️ ยังไม่ได้ต่อฐานข้อมูล — ตอนนี้ทุกเซิร์ฟใช้ฟรี")
            for r in rows:
                g = self.bot.get_guild(r["guild_id"])
                name = g.name if g else "?"
                lines.append(f"• `{r['guild_id']}` {name} — หัวหน้า {r['listeners']} + ลูกน้อง {r['speakers']} · ถึง {_fmt(r['paid_until'])}"
                             + (f" · {r['note']}" if r["note"] else ""))
            if not rows:
                lines.append("ยังไม่มีเซิร์ฟเวอร์ที่จ่ายเงิน")
            await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

        async def _dm(user_id: int, text: str) -> bool:
            try:
                u = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await u.send(text[:1900])
                return True
            except Exception:
                return False

        @admin_group.command(name="approve", description="อนุมัติสลิปที่รอตรวจ → เปิดสิทธิ์เต็ม (ปกติ 30 วัน) และแจ้งลูกค้า")
        @app_commands.describe(pending_id="เลขสลิปรอตรวจ (#) จาก DM หรือ /admin pending", days="กี่วัน (ปกติ 30)")
        async def admin_approve(interaction: discord.Interaction, pending_id: int, days: app_commands.Range[int, 1, 366] = 30):
            if not _is_admin(interaction):
                return await _deny(interaction)
            res = await access.approve_pending(pending_id, interaction.user.id, days)
            if res is None:
                return await interaction.response.send_message(f"❌ ไม่พบสลิป #{pending_id} ที่ยังรอตรวจ", ephemeral=True)
            row, acc = res
            g = self.bot.get_guild(row["guild_id"])
            gname = g.name if g else str(row["guild_id"])
            sent = await _dm(row["user_id"], f"✅ ตรวจสลิปแล้ว เปิด Voice Relay ให้เซิร์ฟ {gname}\n"
                                              f"หัวหน้า {acc.listeners} + ลูกน้อง {acc.speakers} · ใช้ได้ถึง {_fmt(acc.paid_until)}")
            await interaction.response.send_message(
                f"✅ อนุมัติสลิป #{pending_id} เซิร์ฟ {gname} ใช้ได้ถึง {_fmt(acc.paid_until)}"
                + ("\n📨 แจ้งลูกค้าแล้ว" if sent else "\n⚠️ ส่ง DM แจ้งลูกค้าไม่ได้"), ephemeral=True)

        @admin_group.command(name="reject", description="ปฏิเสธสลิป → ยกเลิกสิทธิ์ชั่วคราว และแจ้งลูกค้า")
        @app_commands.describe(pending_id="เลขสลิปรอตรวจ (#)", reason="เหตุผลที่แจ้งลูกค้า เช่น ยอดไม่ครบ / สลิปไม่ถูกต้อง")
        async def admin_reject(interaction: discord.Interaction, pending_id: int, reason: Optional[str] = None):
            if not _is_admin(interaction):
                return await _deny(interaction)
            row = await access.reject_pending(pending_id, interaction.user.id)
            if row is None:
                return await interaction.response.send_message(f"❌ ไม่พบสลิป #{pending_id} ที่ยังรอตรวจ", ephemeral=True)
            g = self.bot.get_guild(row["guild_id"])
            gname = g.name if g else str(row["guild_id"])
            sent = await _dm(row["user_id"], f"❌ สลิปที่ส่งสำหรับเซิร์ฟ {gname} ไม่ผ่านการตรวจ"
                                              + (f": {reason}" if reason else "")
                                              + "\nสิทธิ์ชั่วคราวถูกยกเลิกแล้ว ติดต่อแอดมินถ้าคิดว่าผิดพลาด")
            await interaction.response.send_message(
                f"🚫 ปฏิเสธสลิป #{pending_id} เซิร์ฟ {gname} แล้ว" + ("\n📨 แจ้งลูกค้าแล้ว" if sent else "\n⚠️ ส่ง DM แจ้งลูกค้าไม่ได้"),
                ephemeral=True)

        @admin_group.command(name="pending", description="ดูสลิปที่รอตรวจ")
        async def admin_pending(interaction: discord.Interaction):
            if not _is_admin(interaction):
                return await _deny(interaction)
            rows = await access.list_pending()
            if not rows:
                return await interaction.response.send_message("ไม่มีสลิปรอตรวจ", ephemeral=True)
            lines = []
            for r in rows:
                g = self.bot.get_guild(r["guild_id"])
                price = (r["listeners"] + r["speakers"]) * access.PRICE_PER_BOT
                lines.append(f"• #{r['id']} {g.name if g else r['guild_id']} · หัวหน้า {r['listeners']} + ลูกน้อง {r['speakers']} = {price} บาท"
                             f" · ส่งเมื่อ {_fmt(r['created_at'])}" + ("" if r["provisional"] else " · ไม่ได้สิทธิ์ชั่วคราว"))
            lines.append("อนุมัติ `/admin approve pending_id:#` · ปฏิเสธ `/admin reject pending_id:#`")
            await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

        self.tree.add_command(admin_group)

        unit = self
        pool = self.pool

        async def _notify_admins(text: str, image: Optional[bytes] = None, filename: str = "slip.jpg"):
            for uid in access.ADMIN_USER_IDS:
                try:
                    user = unit.bot.get_user(uid) or await unit.bot.fetch_user(uid)
                    file = discord.File(BytesIO(image), filename=filename) if image else None
                    await user.send(text[:1900], file=file) if file else await user.send(text[:1900])
                except Exception as e:
                    log.warning(f"ส่ง DM แจ้งแอดมิน {uid} ไม่ได้: {e}")

        @app_commands.command(name="pay", description="ส่งสลิปพร้อมเพย์ เปิด/ต่ออายุ Voice Relay (ใช้ได้ทันทีระหว่างรอตรวจ)")
        @app_commands.guild_only()
        @app_commands.describe(
            slip="รูปสลิปโอนเงิน (พร้อมเพย์/ธนาคาร)",
            speakers="จำนวนบอทลูกน้องที่ซื้อ (ขั้นต่ำ 5)",
            listeners="จำนวนบอทหัวหน้า 1 หรือ 2",
        )
        async def pay(interaction: discord.Interaction, slip: discord.Attachment,
                      speakers: app_commands.Range[int, 5, 99] = 5, listeners: app_commands.Range[int, 1, 2] = 1):
            await interaction.response.defer(ephemeral=True)
            guild = interaction.guild
            if guild.id in access.EXEMPT_GUILD_IDS or not access.is_enabled():
                return await interaction.followup.send("ℹ️ เซิร์ฟนี้ใช้ Voice Relay ได้ฟรีอยู่แล้ว ไม่ต้องจ่าย", ephemeral=True)
            if speakers > len(pool.speaker_bots):
                return await interaction.followup.send(f"❌ ตอนนี้มีบอทลูกน้องให้ซื้อได้สูงสุด {len(pool.speaker_bots)} ตัว", ephemeral=True)
            if not (slip.content_type or "").startswith("image/") or slip.size > 4 * 1024 * 1024:
                return await interaction.followup.send("❌ แนบเป็นรูปสลิป (JPG/PNG/WebP) ขนาดไม่เกิน 4MB", ephemeral=True)

            image = await slip.read()
            price = (listeners + speakers) * access.PRICE_PER_BOT
            res = await slip_verify.verify(image, slip.filename, price)
            who = f"{interaction.user} (`{interaction.user.id}`) เซิร์ฟ {guild.name} (`{guild.id}`)"
            order = f"หัวหน้า {listeners} + ลูกน้อง {speakers} = {price} บาท/เดือน"

            if res.ok:
                days = access.days_for_amount(res.amount, listeners, speakers)
                acc = await access.grant_from_slip(guild.id, listeners, speakers, days, res.trans_ref, res.amount, interaction.user.id)
                if acc is None:
                    await interaction.followup.send("❌ สลิปนี้ถูกใช้ไปแล้ว", ephemeral=True)
                    return await _notify_admins(f"⚠️ มีคนส่งสลิปซ้ำ: {who}\nเลขอ้างอิง {res.trans_ref}", image, slip.filename)
                until = acc.paid_until.astimezone(TH_TZ).strftime("%d/%m/%Y %H:%M")
                await interaction.followup.send(
                    f"✅ ได้รับเงิน {res.amount:,.0f} บาทแล้ว เปิด Voice Relay ให้เซิร์ฟนี้\n{order}\nใช้ได้ {days} วัน ถึง {until}",
                    ephemeral=True)
                return await _notify_admins(f"💰 เปิดสิทธิ์อัตโนมัติ: {who}\n{order} · โอน {res.amount:,.0f} บาท จาก {res.sender or '?'}\n"
                                            f"{days} วัน ถึง {until} · อ้างอิง {res.trans_ref}")

            if not res.needs_admin:
                return await interaction.followup.send(f"❌ {res.reason}", ephemeral=True)

            # ตรวจอัตโนมัติไม่ได้ → ให้แอดมินตรวจ ระหว่างนี้เปิดให้ใช้ชั่วคราว
            pend = await access.create_pending(guild.id, interaction.user.id, listeners, speakers,
                                               hashlib.sha256(image).hexdigest())
            if pend.status == "duplicate_image":
                return await interaction.followup.send("❌ สลิปนี้เคยส่งมาแล้ว", ephemeral=True)
            if pend.status == "already_pending":
                return await interaction.followup.send("ℹ️ เซิร์ฟนี้มีสลิปรอแอดมินตรวจอยู่แล้ว รอผลทาง DM ได้เลย", ephemeral=True)
            if pend.status != "created":
                return await interaction.followup.send("❌ ระบบบันทึกสลิปมีปัญหา ติดต่อแอดมิน", ephemeral=True)

            if pend.provisional_until:
                msg = (f"✅ ได้รับสลิปแล้ว (#{pend.pending_id}) เปิดให้ใช้ชั่วคราวระหว่างรอแอดมินตรวจ ถึง "
                       f"{pend.provisional_until.astimezone(TH_TZ).strftime('%d/%m/%Y %H:%M')}\n{order}\n"
                       "ใช้ /relay go ได้เลย เมื่อแอดมินตรวจแล้วจะแจ้งทาง DM")
            else:
                msg = f"✅ ได้รับสลิปแล้ว (#{pend.pending_id}) รอแอดมินตรวจ จะแจ้งผลทาง DM\n{order}"
            await interaction.followup.send(msg, ephemeral=True)
            await _notify_admins(
                f"📝 สลิปรอตรวจ #{pend.pending_id}: {who}\n{order}"
                + (f"\nให้ใช้ชั่วคราวแล้ว {access.PROVISIONAL_DAYS} วัน" if pend.provisional_until else "\n⚠️ เซิร์ฟนี้เคยโดนปฏิเสธสลิป — ไม่ได้สิทธิ์ชั่วคราว")
                + f"\nอนุมัติ: `/admin approve pending_id:{pend.pending_id}` · ปฏิเสธ: `/admin reject pending_id:{pend.pending_id}`",
                image, slip.filename)

        self.tree.add_command(pay)

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
            uid = unit.listener_binding.get("user_id")
            if uid:
                # ล็อกคน: สนใจแค่คนนี้ ตามเข้า/ย้าย/ออกตามเขา
                if member.id != uid:
                    return
                target = follow_target(unit.bot, unit.listener_binding)
                cur = _vc_of(unit.bot, unit.guild_id)
                cur_id = cur.channel.id if _vc_ok(cur) else None
                if target is None:
                    if unit.relay_active:
                        await unit.stop_listening()
                elif target.id != cur_id:
                    if unit.relay_active:
                        await unit.stop_listening()
                    try:
                        await unit.start_listening(target)
                    except Exception as e:
                        log.error(f"[{unit.name}] ตามคนเข้าห้องไม่สำเร็จ: {e}")
                return
            bound_channel_id = unit.listener_binding.get("channel_id")
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
        uid = binding.get("user_id")
        if uid:
            if member.id != uid:
                return
            target = follow_target(owner.bot, binding)
            cur_id = pool.channel_map.get(index) if pool.owner.get(index) is owner else None
            if target is None:
                if cur_id is not None:
                    await owner.stop_speaking(index)
            elif target.id != cur_id:
                if cur_id is not None:
                    await owner.stop_speaking(index)
                if pool.owner.get(index) is None:
                    try:
                        await owner.start_speaking_at(index, target)
                    except Exception as e:
                        log.error(f"[{owner.name}/Speaker {index + 1}] ตามคนเข้าห้องไม่สำเร็จ: {e}")
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


async def pending_reminder_loop(unit: "RelayUnit"):
    """ทุกชั่วโมง: เตือนแอดมินถ้ามีสลิปรอตรวจใกล้หมดช่วงใช้ชั่วคราว"""
    await asyncio.sleep(120)
    while True:
        try:
            if access.is_enabled() and access.ADMIN_USER_IDS:
                rows = await access.due_reminders()
                if rows:
                    text = "⏰ สลิปรอตรวจใกล้หมดช่วงใช้ชั่วคราว: " + ", ".join(f"#{r['id']}" for r in rows) + " — ดู `/admin pending`"
                    for uid in access.ADMIN_USER_IDS:
                        try:
                            u = unit.bot.get_user(uid) or await unit.bot.fetch_user(uid)
                            await u.send(text)
                        except Exception as e:
                            log.warning(f"เตือนแอดมิน {uid} ไม่ได้: {e}")
        except Exception:
            log.exception("pending_reminder_loop ผิดพลาด")
        await asyncio.sleep(3600)


def _vc_of(bot, guild_id):
    g = bot.get_guild(guild_id) if guild_id else None
    return g.voice_client if g else None


def _vc_ok(vc) -> bool:
    return vc is not None and vc.is_connected()


async def watchdog_loop(units: list, pool: SpeakerPool):
    """
    ตัวเฝ้าเสียง ทุก 10 วินาที:
    - บอทหลุด: หัวหน้า/ลูกน้องที่ควรอยู่ในห้องแต่หลุดไป -> ต่อใหม่เอง (ห้องที่ผูกไว้และยังมีคนอยู่)
    - ห้องที่ผูกไว้มีคนแต่บอทไม่อยู่ (เช่นพลาด event ตอนรีสตาร์ท) -> เข้าให้
    - ทุก 60 วินาที เขียนบันทึก HEALTH (เสียงขาด/ล้น/แพ็กเก็ตเสีย/ปิงสูง) และเตือนถ้าเกินเกณฑ์
    """
    await asyncio.sleep(30)
    tick = 0
    while True:
        try:
            for unit in units:
                bind_id = unit.listener_binding.get("channel_id")
                # หัวหน้า: คิดว่าฟังอยู่ แต่ไม่ได้ต่อจริง = หลุด
                if unit.relay_active and not _vc_ok(_vc_of(unit.bot, unit.guild_id)):
                    log.warning(f"[ตัวเฝ้า] {unit.name} หลุดจากห้องหลัก -> ต่อใหม่")
                    RECONNECT_EVENTS.append((unit.guild_id, f"{unit.name} หลุดจากห้องหลัก — กำลังต่อใหม่"))
                    old = _vc_of(unit.bot, unit.guild_id)
                    unit.relay_active = False
                    if old is not None:
                        try:
                            await old.disconnect(force=True)
                        except Exception:
                            pass
                    health_inc("reconnect")
                # หัวหน้า: ห้องที่ผูกไว้มีคน (หรือคนที่ล็อกไว้อยู่ในห้อง) แต่ไม่ได้ฟัง -> เข้า
                if not unit.relay_active and (bind_id or unit.listener_binding.get("user_id")):
                    ch = follow_target(unit.bot, unit.listener_binding)
                    if ch is not None:
                        try:
                            await unit.start_listening(ch)
                        except Exception as e:
                            log.warning(f"[ตัวเฝ้า] {unit.name} เข้าห้องหลักไม่สำเร็จ: {e}")

                # ลูกน้องของหัวหน้าตัวนี้
                for idx in list(pool.indices_for(unit)):
                    if idx not in pool.channel_map:
                        continue  # กำลังต่ออยู่ (ยังไม่เสร็จ) อย่าเพิ่งตัดสินว่าหลุด
                    sb = pool.speaker_bots[idx]
                    if not _vc_ok(_vc_of(sb, pool.guild_map.get(idx))):
                        log.warning(f"[ตัวเฝ้า] ลูกน้อง {idx + 1} หลุดจากห้อง -> ต่อใหม่")
                        RECONNECT_EVENTS.append((pool.guild_map.get(idx), f"ลูกน้อง {idx + 1} หลุดจากห้อง — กำลังต่อใหม่"))
                        old = _vc_of(sb, pool.guild_map.get(idx))
                        if old is not None:
                            try:
                                await old.disconnect(force=True)
                            except Exception:
                                pass
                        pool.release(idx)
                        health_inc("reconnect")
                if unit.relay_active:
                    for idx0, b in list(unit.speaker_bindings.items()):
                        if pool.owner.get(idx0) is not None:
                            continue
                        ch = follow_target(unit.bot, b)
                        if ch is not None:
                            try:
                                await unit.start_speaking_at(idx0, ch)
                            except Exception as e:
                                log.warning(f"[ตัวเฝ้า] ลูกน้อง {idx0 + 1} เข้าห้องไม่สำเร็จ: {e}")

            tick += 1
            if tick % 6 == 0:
                pings = []
                for unit in units:
                    vc = _vc_of(unit.bot, unit.guild_id)
                    if _vc_ok(vc):
                        pings.append(vc.average_latency)
                for idx, gid in list(pool.guild_map.items()):
                    vc = _vc_of(pool.speaker_bots[idx], gid)
                    if _vc_ok(vc):
                        pings.append(vc.average_latency)
                pings = [p for p in pings if p and p != float("inf")]
                max_ping = int(max(pings) * 1000) if pings else 0
                with _HEALTH_LOCK:
                    snap = dict(HEALTH)
                    for k in HEALTH:
                        HEALTH[k] = 0
                LAST_HEALTH.clear()
                LAST_HEALTH.update(snap, max_ping_ms=max_ping, at=time.time())
                line = " ".join(f"{k}={v}" for k, v in snap.items())
                bad = (snap["underrun"] > 30 or snap["overflow"] > 30 or snap["decode_err"] > 20
                       or snap["pump_late"] > 10 or max_ping > 250)
                (log.warning if bad else log.info)(f"HEALTH {line} max_ping_ms={max_ping}"
                                                   + (" ⚠️ เสียงมีปัญหา" if bad else ""))
                events = list(RECONNECT_EVENTS)
                RECONNECT_EVENTS.clear()
                try:
                    await update_health_channels(units, pool, bad, events)
                except Exception:
                    log.exception("อัปเดตห้องสถานะไม่สำเร็จ")
        except Exception:
            log.exception("watchdog_loop ผิดพลาด")
        await asyncio.sleep(10)


LAST_HEALTH: dict = {}
ALL_UNITS: list = []


def units_ref() -> list:
    return ALL_UNITS
_LAST_ALERT: dict = {}  # guild_id -> เวลาประกาศเตือนเสียงล่าสุด (กันสแปม)
ALERT_COOLDOWN = 600


def _health_lines(h: dict) -> list:
    def mark(v, lim):
        return "🟢" if v <= lim else "🔴"
    return [
        f"{mark(h['underrun'], 30)} เสียงขาด/แตก: **{h['underrun']}** ครั้ง",
        f"{mark(h['overflow'], 30)} เสียงค้าง/หน่วง: **{h['overflow']}** ครั้ง",
        f"{mark(h['decode_err'], 20)} แพ็กเก็ตเสียจากห้องหลัก: **{h['decode_err']}** ครั้ง",
        f"{mark(h['pump_late'], 10)} เครื่องประมวลผลไม่ทัน: **{h['pump_late']}** ครั้ง",
        f"{mark(h['max_ping_ms'], 250)} ปิงเสียงสูงสุด: **{h['max_ping_ms']}** ms",
        f"🔁 บอทหลุดแล้วต่อใหม่เอง: **{h['reconnect']}** ครั้ง",
    ]


def _guild_relay_lines(units: list, pool: SpeakerPool, guild) -> list:
    lines = []
    for unit in units:
        vc = _vc_of(unit.bot, guild.id)
        if _vc_ok(vc):
            lines.append(f"🎧 {unit.name} → {vc.channel.mention}")
    for idx, gid in sorted(pool.guild_map.items()):
        if gid != guild.id:
            continue
        vc = _vc_of(pool.speaker_bots[idx], gid)
        ch = guild.get_channel(pool.channel_map.get(idx))
        state = "🔊" if _vc_ok(vc) else "⚠️ หลุด"
        lines.append(f"{state} ลูกน้อง {idx + 1} → {ch.mention if ch else '?'}")
    return lines or ["💤 ตอนนี้ไม่มีบอทอยู่ในห้องเสียง"]


def build_health_embed(units: list, pool: SpeakerPool, guild, bad: bool) -> discord.Embed:
    h = LAST_HEALTH
    e = discord.Embed(
        title="🩺 สถานะ Voice Relay (เรียลไทม์)",
        color=discord.Color.red() if bad else discord.Color.green(),
        description=("⚠️ **เสียงมีปัญหาในนาทีล่าสุด**" if bad else "✅ เสียงปกติ"),
    )
    e.add_field(name="บอทในเซิร์ฟนี้", value="\n".join(_guild_relay_lines(units, pool, guild))[:1024], inline=False)
    if h:
        e.add_field(name="สุขภาพเสียง (1 นาทีล่าสุด)", value="\n".join(_health_lines(h)), inline=False)
    e.add_field(name="อัปเดตล่าสุด", value=f"<t:{int(time.time())}:R> · อัปเดตทุก 1 นาที", inline=False)
    return e


async def update_health_channels(units: list, pool: SpeakerPool, bad: bool, events: list):
    """แก้ข้อความสถานะเดิมในห้องที่ตั้งไว้ (เรียลไทม์) + ประกาศแยกเมื่อเสียงมีปัญหา/บอทหลุด"""
    if not access.is_enabled():
        return
    rows = await access.list_health_channels()
    bot = units[0].bot
    for guild_id, channel_id, message_id in rows:
        ch = None
        for u in units:
            ch = u.bot.get_channel(channel_id)
            if ch is not None:
                bot = u.bot
                break
        if ch is None:
            continue
        embed = build_health_embed(units, pool, ch.guild, bad)
        msg = None
        if message_id:
            try:
                msg = ch.get_partial_message(message_id)
                await msg.edit(embed=embed)
            except discord.NotFound:
                msg = None
            except discord.Forbidden:
                log.warning(f"ห้องสถานะ {channel_id}: ไม่มีสิทธิ์แก้ข้อความ")
                continue
        if msg is None:
            try:
                sent = await ch.send(embed=embed)
                await access.set_health_message(guild_id, sent.id)
            except discord.Forbidden:
                log.warning(f"ห้องสถานะ {channel_id}: ไม่มีสิทธิ์ส่งข้อความ")
                continue

        notes = [t for g, t in events if g == guild_id]
        now = time.time()
        if bad and now - _LAST_ALERT.get(guild_id, 0) > ALERT_COOLDOWN:
            _LAST_ALERT[guild_id] = now
            notes.append("เสียงมีปัญหาในนาทีล่าสุด — ดูรายละเอียดในข้อความสถานะด้านบน")
        if notes:
            try:
                await ch.send("📢 **แจ้งเตือนเสียง**\n" + "\n".join(f"• {t}" for t in notes),
                              delete_after=3600)
            except Exception:
                pass


async def stats_loop(units: list, pool: SpeakerPool):
    """
    ทุก 5 นาที: เขียนสรุปหนึ่งบรรทัดลงบันทึก ให้หน้าแดชบอร์ดอ่านจากบันทึกของเรลเวย์
    อ่านอย่างเดียว ไม่แตะสถานะของบอท — ถ้านับพลาดก็ข้ามรอบนั้น บอททำงานต่อตามปกติ
    """
    await asyncio.sleep(60)
    while True:
        try:
            bots = [u.bot for u in units] + list(pool.speaker_bots)
            online = sum(1 for b in bots if b.is_ready())
            relaying = sum(1 for u in units if u.relay_active) + len(pool.owner)
            servers = len({g.id for b in bots if b.is_ready() for g in b.guilds})
            paid = pending = "-"
            if access.is_enabled():
                try:
                    c = await asyncio.wait_for(access.counts(), timeout=10)
                    if c:
                        paid, pending = c
                except Exception as e:
                    log.warning(f"stats: นับลูกค้าไม่ได้: {e}")
            log.info(f"STATS bots_online={online} bots_total={len(bots)} relaying={relaying} "
                     f"servers={servers} paid={paid} pending={pending}")
        except Exception:
            log.exception("stats_loop ผิดพลาด")
        await asyncio.sleep(300)


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
            if delay < -0.04:
                health_inc("pump_late")
            next_tick = loop.time()

        for unit in units:
            if not unit.relay_active:
                continue
            # ต้อง pop_frame() ทุก tick แม้ยังไม่มีบอทพูดตัวไหนผูกอยู่เลย (my_indices ว่าง) —
            # ไม่งั้น RelaySink.write() ที่ feed() เข้า mixer.buffers ตลอดเวลาจะสะสมเสียงค้างไว้ไม่มีเพดาน
            # (ไม่มีใครมาดึงออกไปใช้) พอมีบอทพูดต่อเข้ามาทีหลัง เสียงค้างทั้งหมดจะไหลออกมาทีเดียว
            # ฟังดูเหมือน "เสียงอัดเทป" เพราะเป็นเสียงที่พูดไปนานแล้วเพิ่งมาเล่นทีหลัง ไม่ realtime
            # — pop ทิ้งทุก tick เพื่อให้ buffer โตเท่าที่ไม่มีคนฟังเท่านั้น ไม่สะสมข้ามช่วงเวลา
            frame = unit.mixer.pop_frame()
            my_indices = pool.indices_for(unit)
            if not my_indices:
                continue
            for idx in my_indices:
                # FrameBuffer ทิ้งเฟรมเก่าสุดเองเมื่อเต็ม กันดีเลย์สะสม
                pool.queues[idx].put(frame)


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
        units.append(RelayUnit("หัวหน้า 2", LISTENER_TOKEN_2, pool, unit_no=2))
    ALL_UNITS.extend(units)

    async with AsyncExitStack() as stack:
        for unit in units:
            await stack.enter_async_context(unit.bot)
        for sbot in speaker_bots:
            await stack.enter_async_context(sbot)

        asyncio.create_task(mixer_pump(units, pool))
        asyncio.create_task(pending_reminder_loop(units[0]))
        asyncio.create_task(stats_loop(units, pool))
        asyncio.create_task(watchdog_loop(units, pool))
        asyncio.create_task(start_health_server())

        tasks = [_start_bot_safe(unit.bot, unit.token, unit.name) for unit in units]
        for i, (token, sbot) in enumerate(zip(SPEAKER_TOKENS, speaker_bots)):
            tasks.append(_start_bot_safe(sbot, token, f"Speaker {i + 1}"))
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
