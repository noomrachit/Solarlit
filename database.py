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
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            -- ห้องเสียงที่ต้องการติดตาม (ต่อกิลด์)
            CREATE TABLE IF NOT EXISTS tracked_channels (
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                added_by BIGINT,
                added_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (guild_id, channel_id)
            );

            -- session การเข้า/ออกห้องเสียงของแต่ละคน
            -- left_at เป็น NULL หมายถึงยังอยู่ในห้องขณะนี้ (session เปิดอยู่)
            CREATE TABLE IF NOT EXISTS voice_sessions (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                joined_at TIMESTAMPTZ NOT NULL,
                left_at TIMESTAMPTZ,
                duration_seconds INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_voice_sessions_open
                ON voice_sessions (guild_id, channel_id, user_id)
                WHERE left_at IS NULL;

            CREATE INDEX IF NOT EXISTS idx_voice_sessions_lookup
                ON voice_sessions (guild_id, channel_id, user_id, joined_at);

            -- ตารางอเนกประสงค์สำรองไว้สำหรับข้อมูลอื่นๆ ในอนาคต
            -- ใช้เก็บค่าแบบ key-value ต่อกิลด์ ไม่ต้อง migrate schema ใหม่ทุกครั้งที่เพิ่มฟีเจอร์เล็กๆ
            -- ตัวอย่าง: key='settings', value='{"prefix": "!"}'
            CREATE TABLE IF NOT EXISTS misc_data (
                guild_id BIGINT NOT NULL,
                key TEXT NOT NULL,
                value JSONB,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (guild_id, key)
            );
            """
        )
