"""
Billing/quota check ของ Voice Relay — เชื่อมกับตาราง guild_subscriptions

สถานะ: ระบบสมัครจ่ายเงินยังอยู่ระหว่างวางโครง (Omise ยังไม่มี API key จริง
— ดู artifacts/api-server/src/routes/billing.ts) ไฟล์นี้เลย fail-open โดย
ตั้งใจ: ถ้า BILLING_DATABASE_URL ยังไม่ถูกตั้งค่าใน environment (ยังไม่ได้
เพิ่มใน Railway variables ของ service นี้) ทุก guild จะผ่านเหมือนเดิมทุก
ประการ (ของเดิมก่อนหน้านี้) เพื่อไม่ให้บอทที่ deploy อยู่ตอนนี้พังจากการ
เปลี่ยนแปลงนี้

เมื่อ BILLING_DATABASE_URL ถูกตั้งค่าแล้ว (ชี้ไปที่ Postgres ของโปรเจกต์
"Solarlit Billing" บน Railway) ไฟล์นี้จะ query ตาราง guild_subscriptions
จริง — ตารางถูกสร้างแบบ idempotent (CREATE TABLE IF NOT EXISTS) ที่นี่ด้วย
เผื่อ service นี้เริ่มก่อน api-server (ต้องตรงกับ schema ใน
artifacts/api-server/src/db/billingPool.ts เป๊ะๆ ถ้าแก้ที่นี่ต้องแก้ที่นั่นด้วย)

Tier -> จำนวนบอทสูงสุด (นับรวมบอทฟัง "หัวหน้า" + บอทพูด "ลูกน้อง" ทุกตัว
ที่ใช้งานพร้อมกัน) อิงตามหน้าราคาเว็บ (website/index.html):
  trial (หรือ active แต่ query DB ไม่เจอ tier ที่รู้จัก) -> 6 (เท่า PRO เต็ม)
  starter  -> 2   (บอทฟัง 1 + บอทพูด 1)
  standard -> 4   (บอทฟัง 1 + บอทพูด 3)
  pro      -> 6   (บอทฟัง 1 + บอทพูด 5)
"""

import os
import time
import logging
from typing import Optional

import asyncpg

log = logging.getLogger("voice-relay.access")

_RELAY_BOT_LIMITS = {
    "starter": 2,
    "standard": 4,
    "pro": 6,
}
_TRIAL_RELAY_BOT_LIMIT = _RELAY_BOT_LIMITS["pro"]  # full access during trial (billing DB is live, guild just has no row yet)
# ใช้เฉพาะตอน BILLING_DATABASE_URL ยังไม่ถูกตั้งค่าเลย (ระบบ billing ทั้งระบบยังไม่เปิด) —
# ต้องมากกว่าจำนวนบอทที่ deploy จริงเสมอ (ตอนนี้ 10 ลูกน้อง + หัวหน้าได้ถึง 2 ตัว = สูงสุด 12)
# ไม่งั้น guild ที่ใช้งานอยู่ก่อนจะโดนบล็อกทันทีที่ deploy โค้ดนี้ ทั้งที่ยังไม่ได้ provision billing DB
_FAILOPEN_RELAY_BOT_LIMIT = 99

_pool: Optional[asyncpg.Pool] = None
_pool_init_failed = False

# แคชผลลัพธ์สั้นๆ ต่อ guild กัน query DB ถี่เกินไป (เช็คทุกครั้งที่บอทพูด
# จะเข้า/ออกห้อง) — ไม่จำเป็นต้อง real-time เป๊ะระดับวินาที
_CACHE_TTL_SECONDS = 60
_cache: dict[int, tuple[float, bool, str, int]] = {}


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    lower = url.lower()
    is_local = "localhost" in lower or "127.0.0.1" in lower
    if not is_local and "sslmode=" not in lower:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


async def _get_pool() -> Optional[asyncpg.Pool]:
    """คืน pool ถ้า BILLING_DATABASE_URL ถูกตั้งค่าและเชื่อมต่อได้ ไม่งั้นคืน None
    (เรียก fail-open ที่ผู้เรียกต้องจัดการเอง)"""
    global _pool, _pool_init_failed
    if _pool is not None:
        return _pool
    if _pool_init_failed:
        return None

    database_url = os.getenv("BILLING_DATABASE_URL")
    if not database_url:
        log.warning(
            "BILLING_DATABASE_URL ยังไม่ได้ตั้งค่า — ข้ามการเช็ค billing "
            "(ทุก guild ผ่านหมด เหมือนพฤติกรรมเดิม)"
        )
        _pool_init_failed = True
        return None

    try:
        database_url = _normalize_database_url(database_url)
        _pool = await asyncpg.create_pool(
            database_url, min_size=1, max_size=5, command_timeout=10,
        )
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_subscriptions (
                    guild_id BIGINT PRIMARY KEY,
                    tier TEXT NOT NULL DEFAULT 'trial',
                    status TEXT NOT NULL DEFAULT 'trialing',
                    trial_ends_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days'),
                    current_period_end TIMESTAMPTZ,
                    omise_customer_id TEXT,
                    omise_charge_id TEXT,
                    created_by_user_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
        return _pool
    except Exception:
        log.exception("เชื่อมต่อ BILLING_DATABASE_URL ไม่สำเร็จ — fail-open ชั่วคราว")
        _pool_init_failed = True
        return None


async def _fetch_guild_state(guild_id: int) -> tuple[bool, str, int]:
    """(allowed, reason_if_blocked, relay_bot_limit) จริงจาก DB ไม่ผ่าน cache"""
    pool = await _get_pool()
    if pool is None:
        return True, "", _FAILOPEN_RELAY_BOT_LIMIT

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tier, status, trial_ends_at
            FROM guild_subscriptions WHERE guild_id = $1
            """,
            guild_id,
        )

    if row is None:
        # เซิร์ฟเวอร์ใหม่ที่ยังไม่เคยสมัครสมาชิก/ยังไม่เคยเห็นในระบบ billing เลย
        # ให้ผ่านแบบทดลองใช้เต็มสิทธิ์ (ตามหน้าเว็บ "ทดลองใช้ฟรี 30 วัน") —
        # แถวจะถูกสร้างจริงตอนกด subscribe ครั้งแรกในหน้า dashboard
        return True, "", _TRIAL_RELAY_BOT_LIMIT

    tier = row["tier"]
    status = row["status"]
    trial_ends_at = row["trial_ends_at"]

    if status == "active" and tier in _RELAY_BOT_LIMITS:
        return True, "", _RELAY_BOT_LIMITS[tier]

    if status == "trialing":
        import datetime
        if trial_ends_at and trial_ends_at.replace(tzinfo=datetime.timezone.utc) > datetime.datetime.now(datetime.timezone.utc):
            return True, "", _TRIAL_RELAY_BOT_LIMIT
        return (
            False,
            "⏰ ทดลองใช้ฟรี 30 วันหมดอายุแล้ว — สมัครแพ็กเกจต่อได้ที่หน้า dashboard "
            "เพื่อใช้งาน Voice Relay ต่อ",
        )

    # status เป็น past_due / canceled หรืออื่นๆ ที่ไม่ใช่ active/trialing
    return (
        False,
        "❌ แพ็กเกจของเซิร์ฟเวอร์นี้ไม่ได้ใช้งานอยู่ — ตรวจสอบสถานะการชำระเงินที่หน้า dashboard",
    )


async def _get_cached(guild_id: int) -> tuple[bool, str, int]:
    now = time.monotonic()
    cached = _cache.get(guild_id)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1], cached[2], cached[3]

    allowed, reason, limit = await _fetch_guild_state(guild_id)
    _cache[guild_id] = (now, allowed, reason, limit)
    return allowed, reason, limit


async def check_guild_access(guild_id: int) -> tuple[bool, str]:
    allowed, reason, _limit = await _get_cached(guild_id)
    return allowed, reason


async def get_relay_bot_limit(guild_id: int) -> int:
    _allowed, _reason, limit = await _get_cached(guild_id)
    return limit
