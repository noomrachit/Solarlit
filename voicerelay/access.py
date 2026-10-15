"""
สิทธิ์ใช้ Voice Relay ต่อเซิร์ฟเวอร์ — แอดมินเปิดให้เองหลังได้รับสลิป (พร้อมเพย์/ทรูมันนี่)

ตาราง relay_access (สร้างอัตโนมัติ):
  guild_id    BIGINT PK
  listeners   จำนวนบอทหัวหน้าที่ใช้ได้ (1-2)
  speakers    จำนวนบอทลูกน้องที่ใช้ได้พร้อมกัน
  paid_until  ใช้ได้ถึงเมื่อไหร่ (เลยแล้ว = หมดสิทธิ์)

กติกา:
- เซิร์ฟเวอร์ใน EXEMPT_GUILD_IDS ใช้ฟรีถาวร ไม่จำกัด
- ยังไม่ได้ตั้ง BILLING_DATABASE_URL = ปล่อยผ่านทุกเซิร์ฟเวอร์ (เหมือนก่อนมีระบบนี้)
  เพื่อไม่ให้บอทพังทันทีที่ deploy ก่อนต่อฐานข้อมูล
- ต่อฐานข้อมูลแล้วแต่ต่อไม่ติด = ปล่อยผ่านชั่วคราว (fail-open) แล้ว log ไว้
- คิดราคา: ตัวละ 10 บาท/เดือน ทั้งหัวหน้าและลูกน้อง
"""
import logging
import os
import time
from datetime import datetime
from typing import NamedTuple, Optional

import asyncpg

log = logging.getLogger("relay.access")

# เซิร์ฟเวอร์ฟรีถาวร (ทดลอง/companion) — เพิ่มได้ผ่าน env EXEMPT_GUILD_IDS คั่นด้วย ,
EXEMPT_GUILD_IDS = {
    1359530731872718858,  # COMPANION - SOLARLIT
    1420296466718658613,
} | {int(x) for x in os.getenv("EXEMPT_GUILD_IDS", "").replace(" ", "").split(",") if x.isdigit()}

# ผู้ใช้ดิสคอร์ดที่สั่ง /admin ได้ (คั่นด้วย ,)
ADMIN_USER_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()}

PRICE_PER_BOT = 10
_CACHE_TTL = 60


class Access(NamedTuple):
    listeners: int
    speakers: int
    paid_until: Optional[datetime]  # None = ไม่มีวันหมด (ฟรีถาวร / ยังไม่เปิดระบบ)


UNLIMITED = Access(listeners=2, speakers=99, paid_until=None)

_pool: Optional[asyncpg.Pool] = None
_pool_tried = False
_cache: dict = {}


def _normalize_url(url: str) -> str:
    return "postgresql://" + url[len("postgres://"):] if url.startswith("postgres://") else url


def is_enabled() -> bool:
    return bool(os.getenv("BILLING_DATABASE_URL"))


async def _get_pool() -> Optional[asyncpg.Pool]:
    global _pool, _pool_tried
    if _pool is not None or _pool_tried:
        return _pool
    _pool_tried = True
    url = os.getenv("BILLING_DATABASE_URL")
    if not url:
        log.warning("ยังไม่ได้ตั้ง BILLING_DATABASE_URL — ปล่อยทุกเซิร์ฟเวอร์ใช้ได้ไม่จำกัด")
        return None
    try:
        _pool = await asyncpg.create_pool(_normalize_url(url), min_size=1, max_size=3)
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_access (
                    guild_id    BIGINT PRIMARY KEY,
                    listeners   INTEGER NOT NULL DEFAULT 1,
                    speakers    INTEGER NOT NULL,
                    paid_until  TIMESTAMPTZ NOT NULL,
                    note        TEXT,
                    granted_by  BIGINT,
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_payments (
                    trans_ref   TEXT PRIMARY KEY,
                    guild_id    BIGINT NOT NULL,
                    user_id     BIGINT NOT NULL,
                    amount      NUMERIC(12, 2) NOT NULL,
                    listeners   INTEGER NOT NULL,
                    speakers    INTEGER NOT NULL,
                    days        INTEGER NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        log.info("เชื่อมฐานข้อมูลสิทธิ์ Voice Relay สำเร็จ")
    except Exception:
        log.exception("เชื่อม BILLING_DATABASE_URL ไม่สำเร็จ — ปล่อยผ่านชั่วคราว (จะลองใหม่ในรอบถัดไป)")
        _pool = None
        _pool_tried = False
    return _pool


def invalidate(guild_id: int) -> None:
    _cache.pop(guild_id, None)


async def get_access(guild_id: int) -> Optional[Access]:
    """คืนสิทธิ์ของเซิร์ฟเวอร์นี้ หรือ None ถ้ายังไม่ได้จ่าย/หมดอายุ"""
    if guild_id in EXEMPT_GUILD_IDS:
        return UNLIMITED
    hit = _cache.get(guild_id)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    pool = await _get_pool()
    if pool is None:
        return UNLIMITED
    try:
        row = await pool.fetchrow(
            "SELECT listeners, speakers, paid_until FROM relay_access WHERE guild_id = $1 AND paid_until > now()",
            guild_id,
        )
    except Exception:
        log.exception("อ่านสิทธิ์ไม่สำเร็จ — ปล่อยผ่านชั่วคราว")
        return UNLIMITED
    result = Access(row["listeners"], row["speakers"], row["paid_until"]) if row else None
    _cache[guild_id] = (time.monotonic(), result)
    return result


async def grant(guild_id: int, listeners: int, speakers: int, days: int, by: int, note: str = "") -> Optional[Access]:
    """เปิด/ต่ออายุสิทธิ์ — ต่อจากวันหมดเดิมถ้ายังไม่หมด ไม่งั้นนับจากตอนนี้"""
    pool = await _get_pool()
    if pool is None:
        return None
    row = await pool.fetchrow(
        """
        INSERT INTO relay_access (guild_id, listeners, speakers, paid_until, note, granted_by, updated_at)
        VALUES ($1, $2, $3, now() + make_interval(days => $4), $5, $6, now())
        ON CONFLICT (guild_id) DO UPDATE SET
            listeners  = EXCLUDED.listeners,
            speakers   = EXCLUDED.speakers,
            paid_until = GREATEST(relay_access.paid_until, now()) + make_interval(days => $4),
            note       = EXCLUDED.note,
            granted_by = EXCLUDED.granted_by,
            updated_at = now()
        RETURNING listeners, speakers, paid_until
        """,
        guild_id, listeners, speakers, days, note or None, by,
    )
    invalidate(guild_id)
    return Access(row["listeners"], row["speakers"], row["paid_until"])


async def grant_from_slip(guild_id: int, listeners: int, speakers: int, days: int,
                          trans_ref: str, amount: float, user_id: int) -> Optional[Access]:
    """
    บันทึกสลิปแล้วเปิด/ต่ออายุสิทธิ์ในธุรกรรมเดียว
    คืน None ถ้าเลขอ้างอิงสลิปนี้เคยใช้แล้ว (กันส่งสลิปเดิมซ้ำ แม้ EasySlip จะพลาด)
    """
    pool = await _get_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval(
                """
                INSERT INTO relay_payments (trans_ref, guild_id, user_id, amount, listeners, speakers, days)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (trans_ref) DO NOTHING
                RETURNING trans_ref
                """,
                trans_ref, guild_id, user_id, amount, listeners, speakers, days,
            )
            if inserted is None:
                return None
            row = await conn.fetchrow(
                """
                INSERT INTO relay_access (guild_id, listeners, speakers, paid_until, note, granted_by, updated_at)
                VALUES ($1, $2, $3, now() + make_interval(days => $4), $5, $6, now())
                ON CONFLICT (guild_id) DO UPDATE SET
                    listeners  = EXCLUDED.listeners,
                    speakers   = EXCLUDED.speakers,
                    paid_until = GREATEST(relay_access.paid_until, now()) + make_interval(days => $4),
                    note       = EXCLUDED.note,
                    granted_by = EXCLUDED.granted_by,
                    updated_at = now()
                RETURNING listeners, speakers, paid_until
                """,
                guild_id, listeners, speakers, days, f"สลิป {trans_ref}", user_id,
            )
    invalidate(guild_id)
    return Access(row["listeners"], row["speakers"], row["paid_until"])


def days_for_amount(amount: float, listeners: int, speakers: int) -> int:
    """ยอดโอน → จำนวนวัน (ราคาเต็มเดือน = 30 วัน, โอนเกินได้วันเพิ่มตามสัดส่วน)"""
    per_month = (listeners + speakers) * PRICE_PER_BOT
    return int(amount / per_month * 30) if per_month else 0


async def revoke(guild_id: int) -> bool:
    pool = await _get_pool()
    if pool is None:
        return False
    status = await pool.execute("DELETE FROM relay_access WHERE guild_id = $1", guild_id)
    invalidate(guild_id)
    return status.endswith("1")


async def list_all() -> list:
    pool = await _get_pool()
    if pool is None:
        return []
    return await pool.fetch(
        "SELECT guild_id, listeners, speakers, paid_until, note FROM relay_access ORDER BY paid_until DESC LIMIT 50"
    )


def monthly_price(access: Access) -> int:
    return (access.listeners + access.speakers) * PRICE_PER_BOT
