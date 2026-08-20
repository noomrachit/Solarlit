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
    """
    Schema ของบอทยาม (voice-tracker) เท่านั้น
    หมายเหตุ: ไฟล์ database.py ตัวเดิมของ service นี้ถูกอัปโหลดผิด — เป็น schema ของบอทใจปู
    (queue/breakout/moderation) ทำให้ตาราง tracked_channels และ voice_sessions ที่ voice_bot.py
    ใช้จริงไม่เคยถูกสร้าง เกิด UndefinedTableError ตอนใช้ /voice, /track — แก้แล้วในไฟล์นี้
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_channels (
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                added_by BIGINT,
                PRIMARY KEY (guild_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS voice_sessions (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                joined_at TIMESTAMPTZ NOT NULL,
                left_at TIMESTAMPTZ,
                duration_seconds INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_voice_sessions_open_session
                ON voice_sessions (guild_id, channel_id, user_id)
                WHERE left_at IS NULL;

            CREATE INDEX IF NOT EXISTS idx_voice_sessions_guild_channel_joined
                ON voice_sessions (guild_id, channel_id, joined_at);
            """
        )
