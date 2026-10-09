"""
ตรวจไฟล์บอท Python ก่อนรวมเข้า main / ก่อนขึ้นระบบ — กันเหตุแบบคอมมิต 5a56130
(relay_bot.py ถูกวางทับด้วยข้อความที่ถูกตัด เหลือ 162 จาก 918 บรรทัด บอทจบเองทันทีหลังเริ่ม)

สิ่งที่ตรวจ:
1. คอมไพล์ผ่าน (ไม่มี syntax error)
2. ไม่มีร่องรอยข้อความถูกตัด: ตัวอักษรเสีย U+FFFD (U+FFFD) หรือบรรทัดที่ลงท้ายด้วย "[...]"
3. ยังมีชิ้นส่วนสำคัญครบ (เช่น async def main, if __name__ == "__main__")
4. จำนวนบรรทัดไม่ต่ำกว่าเกณฑ์ขั้นต่ำ
5. (ถ้าระบุ --base <ref>) ไฟล์ไม่หดลงเกิน 30% เทียบกับ ref นั้น — ถ้าตั้งใจลบจริง ให้ใส่
   ALLOW_SHRINK=1 ใน environment

ใช้:  python scripts/check_bot_integrity.py [--base origin/main]
"""

import os
import re
import subprocess
import sys
import py_compile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ไฟล์ -> (จำนวนบรรทัดขั้นต่ำ, ข้อความที่ต้องมี)
FILES = {
    "voicerelay/relay_bot.py": (700, [
        "class SpeakerPool",
        "class RelayUnit",
        "async def mixer_pump",
        "async def main",
        'if __name__ == "__main__"',
    ]),
    "voicerelay/access.py": (5, []),
    "voicerelay/preflight.py": (30, ["def main"]),
    "voicebot/voice_bot.py": (1500, ["async def main", 'if __name__ == "__main__"']),
    "voicebot/database.py": (20, []),
    "bot/moonlit_bot.py": (2000, ["async def main", 'if __name__ == "__main__"']),
    "bot/database.py": (50, ["init_db"]),
}

MAX_SHRINK = 0.30
TRUNC_LINE = re.compile(r"\[\.\.\.\]\s*$")


def _base_lines(base: str, rel: str):
    try:
        out = subprocess.run(
            ["git", "show", f"{base}:{rel}"], cwd=ROOT, capture_output=True, check=True
        ).stdout
        return out.decode("utf-8", "replace").count("\n")
    except Exception:
        return None


def main() -> int:
    base = None
    if "--base" in sys.argv:
        base = sys.argv[sys.argv.index("--base") + 1]
    allow_shrink = os.getenv("ALLOW_SHRINK") == "1"

    errors = []
    for rel, (min_lines, required) in FILES.items():
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            errors.append(f"{rel}: ไม่พบไฟล์")
            continue

        raw = open(path, "rb").read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            errors.append(f"{rel}: ไม่ใช่ UTF-8 ที่ถูกต้อง ({e})")
            continue

        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"{rel}: คอมไพล์ไม่ผ่าน\n    {e.msg.strip()}")

        lines = text.splitlines()
        for no, line in enumerate(lines, 1):
            if "\ufffd" in line:
                errors.append(f"{rel}:{no}: มีตัวอักษรเสีย (U+FFFD) — ข้อความน่าจะถูกตัดตอนคัดลอก")
                break
        for no, line in enumerate(lines, 1):
            if TRUNC_LINE.search(line):
                errors.append(f"{rel}:{no}: บรรทัดลงท้ายด้วย [...] — ข้อความน่าจะถูกตัดตอนคัดลอก")
                break

        for needle in required:
            if needle not in text:
                errors.append(f"{rel}: ไม่พบส่วนสำคัญ `{needle}` — ไฟล์น่าจะถูกตัดหาย")

        if len(lines) < min_lines:
            errors.append(f"{rel}: เหลือ {len(lines)} บรรทัด (ต่ำกว่าขั้นต่ำ {min_lines})")

        if base and not allow_shrink:
            old = _base_lines(base, rel)
            if old and len(lines) < old * (1 - MAX_SHRINK):
                errors.append(
                    f"{rel}: หดจาก {old} เหลือ {len(lines)} บรรทัด (เกิน {int(MAX_SHRINK * 100)}%) "
                    f"— ถ้าตั้งใจลบจริงให้ตั้ง ALLOW_SHRINK=1"
                )

    if errors:
        print("❌ ตรวจไฟล์บอทไม่ผ่าน:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"✅ ตรวจไฟล์บอทผ่านครบ {len(FILES)} ไฟล์")
    return 0


if __name__ == "__main__":
    sys.exit(main())
