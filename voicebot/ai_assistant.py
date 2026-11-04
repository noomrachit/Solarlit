"""
โมดูลผู้ช่วย AI สำหรับบอท "ยาม" — แยกไฟล์ต่างหาก ไม่แตะโค้ดเดิมของบอท
ใช้ Claude API (Anthropic) ผ่านตัวแปรแวดล้อม ANTHROPIC_API_KEY

ฟีเจอร์ที่ทำแล้ว:
  - ask()          : ตอบคำถามทั่วไป จำบทสนทนาต่อห้อง (ใช้กับ /ถาม)
  - summarize()     : สรุปข้อความล่าสุดในห้อง (ใช้กับ /สรุปห้อง)
  - suggest_party() : ให้คำแนะนำเรื่องการจัดปาร์ตี้จากข้อมูลตี้ปัจจุบัน (ใช้กับ /แนะนำปาร์ตี้, อ่านอย่างเดียว ไม่เขียน DB)

ร่างไว้สำหรับอนาคต (ยังไม่ทำ):
  - เชื่อมราคาไอเทม ROMC จาก Poring Snapper Bot (repo noomrachit/rachitnoom-poring-snapper-bot)
    แนวทางคร่าวๆ: Poring Snapper เก็บราคาไอเทมไว้ใน Postgres ของตัวเอง — ถ้าจะให้ /ถาม ตอบราคาไอเทมได้
    ต้องเลือกทางใดทางหนึ่ง:
      (a) ให้บอทยามอ่าน DB ของ Poring Snapper ตรงๆ ผ่าน asyncpg pool อีกตัว (ต้องมี DATABASE_URL ที่ 2
          + ขอ schema ตารางราคาจากฝั่งนั้นก่อน)
      (b) ให้ Poring Snapper เปิด HTTP endpoint เล็กๆ ให้ query ราคา แล้วให้ฟังก์ชันนี้ยิง request ไปถาม
      (c) ทำเป็น "tool" ให้ Claude เรียกเอง (tool use) เวลาเจอคำถามเกี่ยวกับราคาไอเทมในคำถามของผู้ใช้
    แนะนำ (b) หรือ (c) เพราะไม่ต้องเปิด DB ให้บอทอื่นอ่านตรงๆ ปลอดภัยกว่า — รอให้ user ตัดสินใจก่อนค่อยทำจริง
"""

import os
import logging
from collections import defaultdict, deque
from typing import Optional

from anthropic import AsyncAnthropic

log = logging.getLogger("ยาม-ai")

MODEL = "claude-sonnet-4-5"
MAX_TURNS = 10          # จำสูงสุด 10 คู่ถาม-ตอบล่าสุดต่อห้อง (20 ข้อความ)
MAX_SUMMARY_CHARS = 12000  # กันข้อความที่จะส่งไปสรุปยาวเกินไป

_client: Optional[AsyncAnthropic] = None
# จำบทสนทนาต่อห้อง (channel_id -> deque ของ {"role", "content"}) — เก็บในแรม หายเมื่อบอทรีสตาร์ท
_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_TURNS * 2))

ASK_SYSTEM_PROMPT = (
    "คุณคือผู้ช่วย AI ของบอท 'ยาม' ในดิสคอร์ดกิลด์เกม Ragnarok M Classic (ROMC) "
    "ตอบเป็นภาษาไทยเท่านั้น กระชับ เป็นมิตร ถ้าไม่ทราบคำตอบให้บอกตรงๆ ว่าไม่ทราบ "
    "อย่าสร้างข้อมูลเกมหรือราคาไอเทมขึ้นมาเองถ้าไม่มั่นใจ"
)

SUMMARY_SYSTEM_PROMPT = (
    "สรุปบทสนทนาในห้องดิสคอร์ดต่อไปนี้เป็นภาษาไทย แบบกระชับ เป็นข้อๆ ไม่เกิน 8 ข้อ "
    "เน้นประเด็นสำคัญ/ข้อตกลง/สิ่งที่ต้องทำต่อ ถ้าไม่มีอะไรสำคัญให้บอกตรงๆ"
)

PARTY_SYSTEM_PROMPT = (
    "คุณช่วยแนะนำการจัดปาร์ตี้ตีบอสเกม ROMC จากข้อมูลตี้ปัจจุบันที่ได้รับ "
    "ตอบเป็นภาษาไทย กระชับ เป็นข้อๆ ชี้จุดที่ควรปรับ เช่น ตี้ไหนขาดพระ ตี้ไหนคนไม่ครบ "
    "ห้ามสั่งแก้ฐานข้อมูลหรือย้ายคนเอง แค่ให้คำแนะนำสำหรับแอดมินไปตัดสินใจเอง"
)


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY ไม่ได้ตั้งค่าไว้ใน .env")
        _client = AsyncAnthropic(api_key=api_key)
    return _client


async def ask(channel_id: int, question: str) -> str:
    """ถามคำถามทั่วไป จำบทสนทนาต่อห้อง (channel_id) ไว้ในแรม"""
    client = _get_client()
    history = _history[channel_id]
    history.append({"role": "user", "content": question})

    try:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=ASK_SYSTEM_PROMPT,
            messages=list(history),
        )
    except Exception:
        history.pop()  # คำถามที่ตอบไม่สำเร็จ ไม่เก็บไว้ใน history
        raise

    answer = resp.content[0].text if resp.content else "(ไม่มีคำตอบ)"
    history.append({"role": "assistant", "content": answer})
    return answer


def clear_history(channel_id: int) -> None:
    """ล้างบทสนทนาที่จำไว้ของห้องนี้"""
    _history.pop(channel_id, None)


async def summarize(messages: list[str]) -> str:
    """สรุปข้อความล่าสุดในห้อง (รายการข้อความดิบ ไม่เกี่ยวกับ history ของ ask())"""
    if not messages:
        return "ไม่มีข้อความให้สรุป"
    client = _get_client()
    joined = "\n".join(messages)[-MAX_SUMMARY_CHARS:]
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=SUMMARY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": joined}],
    )
    return resp.content[0].text if resp.content else "(สรุปไม่ได้)"


async def suggest_party(party_summary_text: str) -> str:
    """ให้คำแนะนำเรื่องปาร์ตี้ จากข้อความสรุปสถานะตี้ปัจจุบันที่ส่งเข้ามา (อ่านอย่างเดียว ไม่เขียน DB)"""
    client = _get_client()
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=PARTY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": party_summary_text}],
    )
    return resp.content[0].text if resp.content else "(แนะนำไม่ได้)"
