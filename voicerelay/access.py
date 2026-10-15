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
PROVISIONAL_DAYS = 7      # ส่งสลิปแล้วใช้ได้ทันทีระหว่างรอแอดมินตรวจ
REJECT_COOLDOWN_DAYS = 30  # เซิร์ฟที่เคยโดนปฏิเสธสลิป ไม่ได้สิทธิ์ชั่วคราวอีกช่วงนี้
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
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_pending (
                    id               SERIAL PRIMARY KEY,
                    guild_id         BIGINT NOT NULL,
                    user_id          BIGINT NOT NULL,
                    listeners        INTEGER NOT NULL,
                    speakers         INTEGER NOT NULL,
                    image_sha256     TEXT NOT NULL UNIQUE,
                    status           TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
                    provisional      BOOLEAN NOT NULL DEFAULT FALSE,
                    prev_listeners   INTEGER,
                    prev_speakers    INTEGER,
                    prev_paid_until  TIMESTAMPTZ,
                    reminded         BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                    decided_by       BIGINT,
                    decided_at       TIMESTAMPTZ
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


# ── สลิปรอตรวจ (ระบบไม่เสียค่าบริการ: แอดมินตรวจเอง, ลูกค้าใช้ได้ชั่วคราวระหว่างรอ) ──

class PendingResult(NamedTuple):
    status: str                      # created | duplicate_image | already_pending | error
    pending_id: Optional[int] = None
    provisional_until: Optional[datetime] = None  # None = ไม่ได้สิทธิ์ชั่วคราว (เคยโดนปฏิเสธ)


async def create_pending(guild_id: int, user_id: int, listeners: int, speakers: int, image_sha256: str) -> PendingResult:
    """บันทึกสลิปรอตรวจ + เปิดสิทธิ์ชั่วคราว PROVISIONAL_DAYS วัน (ถ้าเซิร์ฟนี้ไม่เคยโดนปฏิเสธใน 30 วัน)"""
    pool = await _get_pool()
    if pool is None:
        return PendingResult("error")
    async with pool.acquire() as conn:
        async with conn.transaction():
            if await conn.fetchval("SELECT 1 FROM relay_pending WHERE image_sha256 = $1", image_sha256):
                return PendingResult("duplicate_image")
            if await conn.fetchval("SELECT 1 FROM relay_pending WHERE guild_id = $1 AND status = 'pending'", guild_id):
                return PendingResult("already_pending")
            rejected_recently = await conn.fetchval(
                "SELECT 1 FROM relay_pending WHERE guild_id = $1 AND status = 'rejected' "
                "AND decided_at > now() - make_interval(days => $2)",
                guild_id, REJECT_COOLDOWN_DAYS,
            )
            prev = await conn.fetchrow(
                "SELECT listeners, speakers, paid_until FROM relay_access WHERE guild_id = $1", guild_id
            )
            provisional = not rejected_recently
            pid = await conn.fetchval(
                """
                INSERT INTO relay_pending (guild_id, user_id, listeners, speakers, image_sha256, provisional,
                                           prev_listeners, prev_speakers, prev_paid_until)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id
                """,
                guild_id, user_id, listeners, speakers, image_sha256, provisional,
                prev["listeners"] if prev else None, prev["speakers"] if prev else None,
                prev["paid_until"] if prev else None,
            )
            until = None
            if provisional:
                until = await conn.fetchval(
                    """
                    INSERT INTO relay_access (guild_id, listeners, speakers, paid_until, note, granted_by, updated_at)
                    VALUES ($1, $2, $3, now() + make_interval(days => $4), $5, $6, now())
                    ON CONFLICT (guild_id) DO UPDATE SET
                        listeners  = GREATEST(relay_access.listeners, EXCLUDED.listeners),
                        speakers   = GREATEST(relay_access.speakers, EXCLUDED.speakers),
                        paid_until = GREATEST(relay_access.paid_until, EXCLUDED.paid_until),
                        note       = EXCLUDED.note,
                        updated_at = now()
                    RETURNING paid_until
                    """,
                    guild_id, listeners, speakers, PROVISIONAL_DAYS, f"ชั่วคราว รอตรวจสลิป #{pid}", user_id,
                )
    invalidate(guild_id)
    return PendingResult("created", pid, until)


async def get_pending(pending_id: int):
    pool = await _get_pool()
    if pool is None:
        return None
    return await pool.fetchrow("SELECT * FROM relay_pending WHERE id = $1", pending_id)


async def approve_pending(pending_id: int, admin_id: int, days: int = 30) -> Optional[tuple]:
    """อนุมัติสลิป: นับ days วันต่อจากวันหมดที่จ่ายจริงก่อนหน้า (ไม่นับช่วงชั่วคราว) คืน (row, Access)"""
    pool = await _get_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE relay_pending SET status = 'approved', decided_by = $2, decided_at = now() "
                "WHERE id = $1 AND status = 'pending' RETURNING *", pending_id, admin_id,
            )
            if row is None:
                return None
            acc = await conn.fetchrow(
                """
                INSERT INTO relay_access (guild_id, listeners, speakers, paid_until, note, granted_by, updated_at)
                VALUES ($1, $2, $3, GREATEST($4::timestamptz, now()) + make_interval(days => $5), $6, $7, now())
                ON CONFLICT (guild_id) DO UPDATE SET
                    listeners  = EXCLUDED.listeners,
                    speakers   = EXCLUDED.speakers,
                    paid_until = EXCLUDED.paid_until,
                    note       = EXCLUDED.note,
                    granted_by = EXCLUDED.granted_by,
                    updated_at = now()
                RETURNING listeners, speakers, paid_until
                """,
                row["guild_id"], row["listeners"], row["speakers"], row["prev_paid_until"], days,
                f"อนุมัติสลิป #{pending_id}", admin_id,
            )
    invalidate(row["guild_id"])
    return row, Access(acc["listeners"], acc["speakers"], acc["paid_until"])


async def reject_pending(pending_id: int, admin_id: int):
    """ปฏิเสธสลิป: คืนสิทธิ์กลับเป็นเหมือนก่อนส่งสลิป (ไม่มี = ปิดสิทธิ์) คืน row ของสลิป"""
    pool = await _get_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE relay_pending SET status = 'rejected', decided_by = $2, decided_at = now() "
                "WHERE id = $1 AND status = 'pending' RETURNING *", pending_id, admin_id,
            )
            if row is None:
                return None
            if row["provisional"]:
                if row["prev_paid_until"] is None:
                    await conn.execute("DELETE FROM relay_access WHERE guild_id = $1", row["guild_id"])
                else:
                    await conn.execute(
                        "UPDATE relay_access SET listeners = $2, speakers = $3, paid_until = $4, "
                        "note = $5, updated_at = now() WHERE guild_id = $1",
                        row["guild_id"], row["prev_listeners"], row["prev_speakers"], row["prev_paid_until"],
                        f"ปฏิเสธสลิป #{pending_id}",
                    )
    invalidate(row["guild_id"])
    return row


async def list_pending() -> list:
    pool = await _get_pool()
    if pool is None:
        return []
    return await pool.fetch(
        "SELECT * FROM relay_pending WHERE status = 'pending' ORDER BY created_at ASC LIMIT 25"
    )


async def due_reminders() -> list:
    """สลิปที่รอเกิน PROVISIONAL_DAYS-1 วันและยังไม่เคยเตือน — ทำเครื่องหมายว่าเตือนแล้วในคราวเดียว"""
    pool = await _get_pool()
    if pool is None:
        return []
    return await pool.fetch(
        "UPDATE relay_pending SET reminded = TRUE WHERE status = 'pending' AND reminded = FALSE "
        "AND created_at < now() - make_interval(days => $1) RETURNING *",
        PROVISIONAL_DAYS - 1,
    )


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
