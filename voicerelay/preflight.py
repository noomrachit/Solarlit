"""
ตรวจ relay_bot.py ก่อน Railway สลับไปใช้รุ่นใหม่ (ตั้งเป็น Pre-deploy command: python preflight.py)
ถ้าไม่ผ่าน Railway จะยกเลิกรุ่นใหม่ และบอทรุ่นเดิมทำงานต่อ ไม่ล่มทั้งชุดแบบคอมมิต 5a56130

ตรวจ: คอมไพล์ผ่าน, ไม่มีร่องรอยข้อความถูกตัด (U+FFFD หรือ [...]), มีชิ้นส่วนสำคัญครบ, ความยาวไม่ต่ำผิดปกติ
"""

import os
import py_compile
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "relay_bot.py")
MIN_LINES = 700
REQUIRED = [
    "class SpeakerPool",
    "class RelayUnit",
    "async def mixer_pump",
    "async def main",
    'if __name__ == "__main__"',
]


def main() -> int:
    errors = []
    try:
        text = open(TARGET, encoding="utf-8").read()
    except Exception as e:
        print(f"❌ preflight: อ่าน relay_bot.py ไม่ได้: {e}")
        return 1

    try:
        py_compile.compile(TARGET, doraise=True)
    except py_compile.PyCompileError as e:
        errors.append(f"คอมไพล์ไม่ผ่าน: {e.msg.strip()}")

    lines = text.splitlines()
    if any("\ufffd" in ln for ln in lines):
        errors.append("มีตัวอักษรเสีย (U+FFFD) — ข้อความน่าจะถูกตัดตอนคัดลอก")
    if any(re.search(r"\[\.\.\.\]\s*$", ln) for ln in lines):
        errors.append("มีบรรทัดลงท้ายด้วย [...] — ข้อความน่าจะถูกตัดตอนคัดลอก")
    for needle in REQUIRED:
        if needle not in text:
            errors.append(f"ไม่พบ `{needle}` — ไฟล์น่าจะถูกตัดหาย")
    if len(lines) < MIN_LINES:
        errors.append(f"เหลือ {len(lines)} บรรทัด (ต่ำกว่า {MIN_LINES})")

    if errors:
        print("❌ preflight ไม่ผ่าน — ยกเลิกรุ่นนี้ บอทรุ่นเดิมทำงานต่อ:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"✅ preflight ผ่าน ({len(lines)} บรรทัด)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
