
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
            """
        )

        # Migration: เผื่อตาราง queue เดิมถูกสร้างไปแล้วก่อนที่จะมีคอลัมน์ called
        # (CREATE TABLE IF NOT EXISTS จะไม่แก้ตารางที่มีอยู่แล้ว จึงต้อง ALTER แยก)
        await conn.execute(
            """
            ALTER TABLE queue ADD COLUMN IF NOT EXISTS called BOOLEAN DEFAULT FALSE;
            """
        )
        )
