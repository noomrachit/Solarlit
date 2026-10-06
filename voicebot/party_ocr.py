"""
อ่านรูปตารางปาร์ตี้ (SUN01..FLASH04, 4 ทีม x 4 ตี้ x 6 ช่อง) ด้วย OCR แล้วจับคู่ชื่อที่อ่านได้กับผู้เล่นจริง

หมายเหตุ: เขียนแบบ best-effort โดยไม่มีรูปตัวอย่างจริงของตารางให้ดูประกอบตอนเขียน — ต้องทดสอบกับรูปจริง
แล้วปรับสัดส่วนคอลัมน์/ค่า threshold ด้านล่างให้ตรงกับฟอนต์และเลย์เอาต์จริงที่กิลด์ใช้ ก่อนใช้งานจริงกับ /party upload
"""

import re
import io
import difflib

from PIL import Image, ImageOps
import pytesseract

GROUPS = ["SUN", "MOON", "DARK", "FLASH"]
PARTIES_PER_GROUP = 4
PARTY_SIZE = 6

HEADER_RE = re.compile(r"^(SUN|MOON|DARK|FLASH)0?([1-4])$", re.IGNORECASE)

# สีอาชีพ (RGB 0-255) ใช้ระบายสีช่องในกระดานตาราง — ค่าประมาณ ปรับแก้ได้ตามที่กิลด์อยากเห็น
# "Archbishop" ต้องมีอยู่เสมอ (ใช้เป็นสีสำรองสำหรับทุกชื่อที่เข้าเกณฑ์พระใน _is_priest)
CLASS_COLORS = {
    "Archbishop": (255, 221, 150),
    "Royal Guard": (160, 196, 255),
    "Rune Knight": (205, 92, 92),
    "Warlock": (130, 90, 190),
    "Sorcerer": (120, 160, 220),
    "Minstrel": (235, 180, 210),
    "Wanderer": (235, 180, 210),
    "Shadow Chaser": (110, 110, 150),
    "Genetic": (140, 200, 120),
    "Mechanic": (190, 190, 190),
    "Guillotine Cross": (95, 60, 95),
    "Sura": (220, 140, 90),
    "Star Emperor": (230, 200, 90),
    "Soul Reaper": (140, 95, 160),
    "Soul Ascetic": (195, 155, 225),
    "Night Watch": (95, 95, 115),
    "Hyper Novice": (210, 210, 210),
}


def _ocr_words(img: Image.Image) -> list:
    """คืน list ของ dict {text, left, top, width, height} จากทั้งรูป (กรองคำที่ความมั่นใจต่ำออก)"""
    gray = ImageOps.autocontrast(img.convert("L"))
    data = pytesseract.image_to_data(gray, lang="tha+eng", config="--psm 11",
                                      output_type=pytesseract.Output.DICT)
    words = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < 30:
            continue
        words.append({
            "text": text,
            "left": data["left"][i],
            "top": data["top"][i],
            "width": data["width"][i],
            "height": data["height"][i],
        })
    return words


def _group_lines(words: list, y_tolerance: int = 10) -> list:
    """จัดกลุ่มคำเป็นบรรทัดตามตำแหน่งแนวตั้ง (จุดกึ่งกลาง y ใกล้กันถือว่าอยู่บรรทัดเดียวกัน)"""
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["left"])):
        cy = w["top"] + w["height"] / 2
        for line in lines:
            line_cy = sum(x["top"] + x["height"] / 2 for x in line) / len(line)
            if abs(cy - line_cy) <= y_tolerance:
                line.append(w)
                break
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["left"])
    lines.sort(key=lambda line: sum(w["top"] for w in line) / len(line))
    return lines


def _find_headers(lines: list) -> list:
    """หาบรรทัดที่เป็นหัวตี้ เช่น 'SUN01' คืน list ของ dict {group, party_num, top, bottom, left}"""
    headers = []
    for line in lines:
        joined = "".join(w["text"] for w in line).upper()
        m = HEADER_RE.match(joined)
        if m:
            headers.append({
                "group": m.group(1).upper(),
                "party_num": int(m.group(2)),
                "top": min(w["top"] for w in line),
                "bottom": max(w["top"] + w["height"] for w in line),
                "left": min(w["left"] for w in line),
            })
    return headers


def read_party_images(images: list) -> list:
    """
    อ่านรูปตารางปาร์ตี้ (1-2 รูป เป็น bytes) คืน list ของ dict ต่อ "ช่อง" ที่อ่านได้:
    {"group", "party_num", "slot", "name_texts": [...], "cls"}
    สมมติเลย์เอาต์: หัวตี้ชื่อ "SUN01".."FLASH04" ตามด้วยแถวข้อมูล 6 แถว (ลำดับ/รายชื่อ/อาชีพ)
    """
    all_rows = []
    for image_bytes in images:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        lines = _group_lines(_ocr_words(img))
        headers = sorted(_find_headers(lines), key=lambda h: h["top"])
        if not headers:
            continue

        for idx, h in enumerate(headers):
            # ขอบล่างของโซนตี้นี้ = หัวตี้ถัดไปที่อยู่คอลัมน์เดียวกัน (left ใกล้เคียง) หรือสุดภาพ
            next_top = img.height
            for other in headers[idx + 1:]:
                if abs(other["left"] - h["left"]) < img.width * 0.15:
                    next_top = other["top"]
                    break

            zone_lines = [
                line for line in lines
                if h["bottom"] < (sum(w["top"] for w in line) / len(line)) < next_top
            ]

            data_lines = []
            for line in zone_lines:
                m = re.match(r"^(\d{1,2})", line[0]["text"]) if line else None
                if m and 1 <= int(m.group(1)) <= PARTY_SIZE:
                    data_lines.append(line)
                if len(data_lines) >= PARTY_SIZE:
                    break

            for slot_i, line in enumerate(data_lines, start=1):
                rest = line[1:]
                if not rest:
                    continue
                # ประมาณคอลัมน์: ซ้าย 60% ของความกว้างส่วนที่เหลือ = ชื่อ, ที่เหลือทางขวา = อาชีพ
                line_left = rest[0]["left"]
                line_right = max(w["left"] + w["width"] for w in rest)
                split_x = line_left + (line_right - line_left) * 0.6
                name_text = " ".join(w["text"] for w in rest if w["left"] < split_x).strip()
                cls_text = " ".join(w["text"] for w in rest if w["left"] >= split_x).strip()
                if not name_text:
                    continue
                all_rows.append({
                    "group": h["group"],
                    "party_num": h["party_num"],
                    "slot": slot_i,
                    "name_texts": [name_text],
                    "cls": cls_text or None,
                })
    return all_rows


def match_names(rows: list, profiles: list) -> None:
    """
    เติม key "profile" ให้ทุก row ใน rows (แก้ไข in-place) โดย fuzzy match "name_texts[0]"
    กับ profiles — แต่ละ dict ใน profiles มี in_game_name, discord_user_id, character_class และอาจมี:
    - "bonus": บวกเพิ่มให้คะแนนความคล้าย (เช่น ชื่อที่เคยผูกไว้แล้วตอนกดลาก่อนหน้า)
    - "min_score": คะแนนต่ำสุดที่ต้องถึงเพื่อ match (ใช้กับชื่อเล่นในดิส ที่ต้องคล้ายมากๆ ถึงจะยอม)
    ไม่มี min_score จะใช้ค่าเริ่มต้น 0.6
    """
    for row in rows:
        name = (row["name_texts"][0] if row["name_texts"] else "").strip().lower()
        row["profile"] = None
        if not name:
            continue
        best_score, best_profile = 0.0, None
        for p in profiles:
            candidate = (p.get("in_game_name") or "").strip().lower()
            if not candidate:
                continue
            score = difflib.SequenceMatcher(None, name, candidate).ratio() + p.get("bonus", 0.0)
            if score >= p.get("min_score", 0.6) and score > best_score:
                best_score, best_profile = score, p
        row["profile"] = best_profile
