import asyncpg
import os
from typing import Optional

_pool: Optional[asyncpg.Pool] = None


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    lower = url.lower()
    is_local = "localhost" in lower or "127.0.0.1" in lower
    if not is_local and "sslmode=" not in lower:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not set")
        database_url = _normalize_database_url(database_url)
        _pool = await asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=10,
            command_timeout=60,
        )
    return _pool


async def close_pool():
    """ปิด pool อย่างปลอดภัย"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def reset_pool() -> asyncpg.Pool:
    """รีเซ็ต pool: ปิดของเก่า แล้วสร้างใหม่"""
    await close_pool()
    return await get_pool()


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                guild_id BIGINT PRIMARY KEY,
                log_channel BIGINT,
                welcome_channel BIGINT,
                welcome_message TEXT DEFAULT 'ยินดีต้อนรับ {mention} เข้าสู่ {server}!',
                leave_message TEXT,
                prefix TEXT DEFAULT '!',
                automod_enabled BOOLEAN DEFAULT FALSE,
                anti_invite BOOLEAN DEFAULT FALSE,
                anti_mention_spam BOOLEAN DEFAULT FALSE,
                mention_limit INTEGER DEFAULT 5
            );

            CREATE TABLE IF NOT EXISTS warnings (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                reason TEXT,
                moderator_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS custom_commands (
                guild_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                response TEXT NOT NULL,
                PRIMARY KEY (guild_id, name)
            );

            CREATE TABLE IF NOT EXISTS reaction_roles (
                message_id BIGINT NOT NULL,
                emoji TEXT NOT NULL,
                role_id BIGINT NOT NULL,
                guild_id BIGINT,
                PRIMARY KEY (message_id, emoji)
            );

            CREATE TABLE IF NOT EXISTS presence_logs (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                status TEXT,
                last_seen TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS queue (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                position INTEGER,
                joined_at TIMESTAMPTZ DEFAULT NOW(),
                called BOOLEAN DEFAULT FALSE,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS banned_words (
                guild_id BIGINT NOT NULL,
                word TEXT NOT NULL,
                PRIMARY KEY (guild_id, word)
            );

            CREATE TABLE IF NOT EXISTS queue_board (
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS dashboard_board (
                guild_id BIGINT PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS queue_bookings (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                slot_time TIMESTAMPTZ NOT NULL,
                reminded BOOLEAN DEFAULT FALSE,
                activated BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_queue_bookings_pending
                ON queue_bookings (guild_id, slot_time)
                WHERE activated = FALSE;

            CREATE TABLE IF NOT EXISTS breakout_sessions (
                guild_id BIGINT PRIMARY KEY,
                source_channel_id BIGINT NOT NULL,
                room_channel_ids BIGINT[] NOT NULL,
                owner_ids BIGINT[] DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS player_profiles (
                guild_id BIGINT NOT NULL,
                discord_user_id BIGINT NOT NULL,
                in_game_name TEXT NOT NULL,
                discord_name TEXT NOT NULL,
                character_class TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (guild_id, discord_user_id)
            );

            CREATE TABLE IF NOT EXISTS job_roles (
                guild_id BIGINT NOT NULL,
                role_id BIGINT NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            );
            """
        )

        # Migration: เผื่อตาราง breakout_sessions ถูกสร้างไปแล้วก่อนมีคอลัมน์ owner_ids
        await conn.execute(
            """
            ALTER TABLE breakout_sessions ADD COLUMN IF NOT EXISTS owner_ids BIGINT[] DEFAULT '{}';
            """
        )

        # Migration: เผื่อตาราง queue เดิมถูกสร้างไปแล้วก่อนที่จะมีคอลัมน์ called
        # (CREATE TABLE IF NOT EXISTS จะไม่แก้ตารางที่มีอยู่แล้ว จึงต้อง ALTER แยก)
        await conn.execute(
            """
            ALTER TABLE queue ADD COLUMN IF NOT EXISTS called BOOLEAN DEFAULT FALSE;
            """
        )

        # Migration: role ที่จะถูกปิงเมื่อมีคนกดปุ่ม "เรียกแอดมิน"/"ขอความช่วยเหลือ" ใน Support Panel
        await conn.execute(
            """
            ALTER TABLE settings ADD COLUMN IF NOT EXISTS support_role BIGINT;
            """
        )

        # Migration: ช่องที่จะปักหมุดกระดานรวมคำสั่งบอททุกตัว (/settings dashboardchannel)
        await conn.execute(
            """
            ALTER TABLE settings ADD COLUMN IF NOT EXISTS dashboard_channel BIGINT;
            """
        )

        # Migration: ห้องที่จะโพสต์กระดานแนะนำตัวผู้เล่น + Embed แนะนำตัวเมื่อสมาชิกกรอกฟอร์ม (/setup-introduction)
        await conn.execute(
            """
            ALTER TABLE settings ADD COLUMN IF NOT EXISTS intro_channel BIGINT;
            """
        )
