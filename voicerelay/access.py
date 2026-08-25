"""
Stub ชั่วคราวสำหรับ billing/quota check ของ Voice Relay

สถานะ: ยังไม่มีระบบ tier/billing จริง (โปรเจกต์นี้ถูกพักไว้) — ไฟล์นี้มีไว้แค่กัน
`ModuleNotFoundError` ตอน relay bot.py import access ตอน startup ซึ่งทำให้บอท crash
ทันทีทุกครั้งที่รีสตาร์ท ไม่เกี่ยวกับการเปิด/ปิดฟีเจอร์ billing

ตอนนี้ทุก guild ผ่านการเช็คเสมอ ไม่มีการจำกัดโควต้าจริง — เมื่อพร้อมทำระบบ tier จริง
(ตาราง guild tier ใน Postgres ตามที่คุยไว้) ให้แทนที่ทั้งสองฟังก์ชันนี้ด้วย query จริง
"""

# จำกัดจำนวนบอทพูดสูงสุดต่อ guild แบบเปิดกว้างไว้ก่อน (มากกว่าจำนวนบอทพูดที่มีจริงเสมอ)
# เพื่อไม่ให้เป็นคอขวด จนกว่าจะมีระบบ tier จริงมาแทนที่
_DEFAULT_RELAY_BOT_LIMIT = 99


async def check_guild_access(guild_id: int) -> tuple[bool, str]:
    return True, ""


async def get_relay_bot_limit(guild_id: int) -> int:
    return _DEFAULT_RELAY_BOT_LIMIT
