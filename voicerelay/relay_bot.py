"""
ระบบถ่ายทอดเสียงทางเดียว (one-way live audio relay)
ห้องหลัก (source) -> ห้องย่อยหลายห้อง (targets) แบบเรียลไทม์ — รองรับ "หัวหน้า" (listener bot) ��[...]

สถาปัตยกรรม:
- RelayUnit  : ห่อ state ที่เคยเป็น global เดี่ยวๆ (relay_active, mixer, role_filter, bindings ฯลฯ)
               ให้เป็นของแต่ละ "หัวหน้า" (listener bot) ตัวใดตัวหนึ่งโดยเฉพาะ — แต่ละตัวม��[...]
               discord.Bot + command tree (`/relay ...`) เป็นของตัวเอง (คนละแอปดิสคอร์ด)
- SpeakerPool: บอทพูดทั้งหมด (SPEAKER_BOT_TOKEN_1..N) เป็นทรัพยากรกลางที่ "แชร์" ระหว่างทุก RelayUnit
               แต่ละตัวถูกจอง (owner) ให้ unit ใดหนึ่งได้ครั้งละ unit เดียวเท่านั้น
               (เพราะบอท 1 ตัวเข้าห้องเสียงได้ทีละ 1 ห้องอยู่แล้ว เป็นข้อจำกัดของ��[...]
- mixer_pump : วน loop เดียว ไล่ทุก unit ที่ active อยู่ ผสมเสียงแยกกันคนละ mixer แล้วป้อนเข้า
               queue เฉพาะของบอทพูดที่ unit นั้น "เป็นเจ้าของ" อยู่ตอนนั้น

Environment variables:
  LISTENER_BOT_TOKEN     = token หัวหน้าตัวที่ 1 (บังคับต้องมี)
  LISTENER_BOT_TOKEN_2   = token หัวหน้าตัวที่ 2 (ไม่ใส่ = รันแค่หัวหน้าตัวเดียว เหมือนสถาปั��[...]
  SPEAKER_BOT_TOKEN_1..N = บอทพูด (พูลกลาง แชร์กันทุกหัวหน้า)

/relay bindspeaker กันชนข้าม unit แล้ว — pool.try_bind()/release_bind() ปฏิเสธถ้าหัวหน้าอีกตัวผูกเลข
เดียวกันไว้กับห้องอื่นอยู่ก่อน (บอกชื่อหัวหน้าที่ถืออยู่ในข้อความ erro[...]
ผูกใหม่ข้าม unit ได้ — ดู SpeakerPool.try_bind/release_bind และ relay_bindspeaker ด้านล่าง

ข้อจำกัดที่ทราบอยู่แล้ว:
- หน่วงเวลาประมาณ 0.3-0.8 วินาที (รับ -> mix -> เข้ารหัส -> ส่ง -> เล่น)
- เป็นเสียงทางเดียวเท่านั้น ห้องย่อยพูดกลับห้องหลักไม่ได้
- ต้อง invite บอททุกตัวเข้าเซิร์ฟเวอร์เดียวกัน (คนละ token คนละแอป)
- ต้องมี libopus ติดตั้งในระบบ (ดู nixpacks.toml)
- จำนวนห้องย่อยที่กระจายพร้อมกันได้ ถูกจำกัดด้วยจำนวนบอทพูดที่ตั้งค�[...]
- โควต้าต่อเซิร์ฟเวอร์ (_quota_ok) นับเฉพาะบอทของ "หัวหน้าตัวนั้นๆ" เอง ไม่รวม��[...]
  เดียวกัน (เหมือนพฤติกรรมเดิมตอนมีหัวหน้าตัวเดียว) — ตอนนี้ billing_access ยังเ��[...]
  ไม่กระทบอะไรจริง จนกว่าจะมีระบบ tier จริงมาแทนที่

/relay setrole <role> จำกัดให้กระจายเสียงเฉพาะคนที่มีบทบาทนี้ในห้องหลัก — คนอื่นย�[...]
ได้ตามปกติ (ไม่ได้ถูกตัดไมค์/เตะออก) แค่เสียงของเขาจะไม่ถูกป้อนเข้า mixe[...]
ใช้ /relay clearrole เพื่อยกเลิกและกลับไปกระจายเสียงทุกคนตามเดิม
"""

import os
import asyncio
import logging
import time
import functools
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
    ปกติเกิดเวลามีคนพูดพร้อมกันหลายคน/แพ็กเก็ตขาดหาย/decoder เพิ่งถูกสร้าง��[...]
    ถ้าไม่แพตช์ error ตัวเดียวจะทำให้ thread รับเสียงทั้งหมดตาย ฟังเสียงต่อไม่��[...]
    หลังแพตช์: ข้าม packet ที่ decode ไม่ได้ทิ้งไปเฉยๆ (เสียงสะดุดแป๊บเดียว) แทนที��[...]
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


def _patch_voice_recv_jitter_buffer():
    """ให้ buffer ของ voice_recv ทนต่อ jitter ปกติมากขึ้นก่อนจะถือว่าแพ็กเก็ตหาย."""
    try:
        from discord.ext.voice_recv import opus as vr_opus
    except Exception as e:
        log.warning(f"ไม่พบโมดูล voice_recv.opus สำหรับปรับ JitterBuffer (ข้ามได้ ไม่ critical): {e}")
        return

    heap_cls = getattr(vr_opus, "HeapJitterBuffer", None)
    if heap_cls is None:
        log.warning("ไม่พบ HeapJitterBuffer ใน discord.ext.voice_recv.opus")
        return

    if getattr(vr_opus, "JitterBuffer", None) is not None:
        vr_opus.JitterBuffer = functools.partial(heap_cls, maxsize=20, prefsize=4, prefill=2)
        log.info("แพตช์ JitterBuffer สำเร็จ: maxsize=20, prefsize=4, prefill=2")
    else:
        log.warning("ไม่พบ JitterBuffer ใน voice_recv.opus สำหรับแพตช์")


_patch_voice_recv_resilience()
_patch_voice_recv_jitter_buffer()

FRAME_BYTES = 3840  # เฟรมเสียง 20ms ที่ 48kHz, 16-bit, stereo (มาตรฐานของ Discord voice)
