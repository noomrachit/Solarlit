"""
อ่านรูปตารางปาร์ตี้จากชีต (TEAM SUN / MOON / DARK / FLASH ทีมละ 4 ตี้ ตี้ละ 6 คน)

วิธีอ่าน (ไม่พึ่งตำแหน่งตายตัว ใช้ได้กับรูปหลายขนาด):
1. หาแถบหัวคอลัมน์สีเขียวเข้ม (ลำดับ / รายชื่อ / อาชีพ) ของแต่ละตี้ → ได้กรอบตี้
2. ดูสีแถบชื่อตี้ด้านบน → รู้ว่าเป็นทีมไหน (SUN เหลือง, MOON ม่วง, DARK เทา, FLASH ชมพู)
3. ตี้ในแถวเดียวกันของทีมเดียวกัน เรียงซ้าย→ขวา = ตี้ 1-4
4. อาชีพ: ดูจากสีช่อง dropdown (แม่นกว่า OCR มาก) ถ้าสีไม่ตรงค่อยใช้ OCR
5. ชื่อ: OCR หลายแบบ แล้วเอาไปจับคู่กับรายชื่อแนะนำตัว (match_names)
"""
import difflib
import io
import re
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytesseract
from PIL import Image, ImageOps

TEAM_COLORS = {
    "SUN": (240, 194, 49),
    "MOON": (180, 167, 214),
    "DARK": (192, 192, 192),
    "FLASH": (213, 166, 189),
}
TEAM_ORDER = ["SUN", "MOON", "DARK", "FLASH"]

# สีช่องอาชีพในชีต (วัดจากรูปจริง)
CLASS_COLORS = {
    "Archbishop": (47, 118, 79),
    "WarLock": (25, 84, 161),
    "RoyalGuard": (164, 30, 28),
    "RuneKnight": (194, 112, 112),
    "Assassin": (92, 55, 132),
    "Sorceror": (181, 220, 241),
    "Shadows": (222, 196, 237),
    "Shura": (207, 235, 189),
    "BardDance": (164, 141, 36),
    "Machanic": (188, 130, 23),
    "Genetic": (245, 197, 164),
    "Rangers": (245, 225, 159),
}
EMPTY_COLOR = (3, 2, 2)  # ช่อง None (ว่าง)


def NAME_VARIANTS(c1, c2):
    """OCR ชื่อหลายแบบ (ครอบต่างกัน / โหมดต่างกัน / อังกฤษล้วน) แล้วเลือกแบบที่ตรงรายชื่อที่สุดตอนจับคู่"""
    return [(c1, 7, "tha+eng"), (c1, 8, "tha+eng"), (c2, 7, "tha+eng"), (c2, 8, "tha+eng"), (c1, 7, "eng")]


def _find_blocks(a: np.ndarray):
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mask = (g - r > 25) & (g - b > 5) & (r < 90) & (g > 70) & (g < 130)
    H, W = mask.shape
    rows = [y for y in range(H) if mask[y].sum() > W * 0.15]
    bands = []
    for y in rows:
        if bands and y - bands[-1][1] <= 2:
            bands[-1][1] = y
        else:
            bands.append([y, y])

    blocks = []
    for y0, y1 in bands:
        if y1 - y0 < 8:
            continue
        line = mask[y0:y1 + 1].mean(axis=0) > 0.3
        runs, x = [], 0
        while x < W:
            if line[x]:
                s = x
                while x < W and line[x]:
                    x += 1
                if x - s > 3:
                    runs.append([s, x])
            x += 1
        merged = []
        for s, e in runs:
            if merged and s - merged[-1][1] < 30:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        for s, e in merged:
            if e - s >= 150:
                blocks.append((s, e, y0, y1))
    return blocks


def _nearest(px, table: dict):
    best, bd = None, 1e9
    for k, c in table.items():
        d = sum((float(px[i]) - c[i]) ** 2 for i in range(3)) ** 0.5
        if d < bd:
            best, bd = k, d
    return best, bd


def _ocr(img: Image.Image, psm: int = 7, lang: str = "tha+eng") -> str:
    g = ImageOps.autocontrast(img.convert("L"))
    g = g.resize((g.width * 4, g.height * 4), Image.LANCZOS)
    g = ImageOps.expand(g, border=20, fill=g.getpixel((2, 2)))
    return pytesseract.image_to_string(g, lang=lang, config=f"--psm {psm}").strip()


def _clean(t: str) -> str:
    t = re.sub(r"[|\[\]{}_]", " ", t)
    return re.sub(r"\s+", " ", t).strip(" .,'\"-—")


def _class_from_text(t: str):
    t = t.lower()
    if not t:
        return None
    names = {k.lower(): k for k in CLASS_COLORS}
    m = difflib.get_close_matches(t[:10], [n[:10] for n in names], n=1, cutoff=0.6)
    if m:
        for n, k in names.items():
            if n[:10] == m[0]:
                return k
    return None


def read_party_image(image_bytes: bytes) -> list:
    """
    คืน list ของ dict: {group, party_num, slot, cls, name_texts(list), empty(bool)}
    (party_num คำนวณทีหลังใน read_party_images เพราะต้องรวมทุกรูปก่อน)
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    a = np.asarray(img).astype(int)
    H = a.shape[0]
    out, jobs = [], []
    for s, e, y0, y1 in _find_blocks(a):
        w = e - s
        # ทีม: สีแถบชื่อตี้ด้านบนแถบเขียว
        ys = max(0, y0 - 12)
        px = a[ys, s + int(w * 0.55):s + int(w * 0.8)].reshape(-1, 3).mean(axis=0)
        team, _ = _nearest(px, TEAM_COLORS)

        # ขอบล่างของตี้ = เจอพื้นขาวของชีต
        x = s + int(w * 0.15)
        y = y1 + 3
        while y < H - 1 and not (a[y, x] > 235).all():
            y += 1
        top = y1 + 1
        pitch = (y - top) / 6
        if pitch < 8:
            continue

        for i in range(6):
            ty, by = int(top + i * pitch), int(top + (i + 1) * pitch)
            cell = a[ty + 3:by - 3, s + int(w * 0.66):s + int(w * 0.97)].reshape(-1, 3)
            mx, mn = cell.max(1), cell.min(1)
            keep = cell[(mx - mn > 25) | (mx < 60)]
            med = np.median(keep if len(keep) > 10 else cell, axis=0)

            if sum((float(med[k]) - EMPTY_COLOR[k]) ** 2 for k in range(3)) ** 0.5 < 40:
                cls = None  # ช่อง None
            else:
                cls, dist = _nearest(med, CLASS_COLORS)
                if dist > 45:
                    job_txt = _ocr(img.crop((s + int(w * 0.68), ty + 1, s + int(w * 0.9), by - 1)))
                    cls = _class_from_text(job_txt) or cls

            if cls is None:
                continue  # ช่องว่าง (None) ไม่ต้องอ่านชื่อ
            name_box = (s + int(w * 0.37), ty + 1, s + int(w * 0.645), by - 1)
            c1 = img.crop(name_box)
            c2 = img.crop((name_box[0] - 6, name_box[1], name_box[2] + 6, name_box[3]))
            jobs.append((len(out), [(c, psm, lang) for c, psm, lang in NAME_VARIANTS(c1, c2)]))
            out.append({"group": team, "x": s, "y": y0, "slot": i + 1, "cls": cls,
                        "name_texts": [], "empty": False})

    # OCR ชื่อแบบขนาน (tesseract เป็น process แยก ใช้ thread ได้)
    flat = [(idx, crop, psm, lang) for idx, variants in jobs for crop, psm, lang in variants]
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda j: (j[0], _clean(_ocr(j[1], j[2], j[3]))), flat))
    for idx, t in results:
        if t and t not in out[idx]["name_texts"]:
            out[idx]["name_texts"].append(t)
    return out


def read_party_images(images: list) -> list:
    """รวมหลายรูป แล้วเรียงเลขตี้ 1-4 ตามตำแหน่งซ้าย→ขวาในแต่ละทีม"""
    rows = []
    for idx, b in enumerate(images):
        for r in read_party_image(b):
            r["img"] = idx
            rows.append(r)
    blocks = sorted({(r["group"], r["img"], r["y"], r["x"]) for r in rows},
                    key=lambda k: (k[1], k[2], k[3]))
    counter, num_of = {}, {}
    for g, im, y, x in blocks:
        counter[g] = counter.get(g, 0) + 1
        num_of[(g, im, y, x)] = counter[g]
    for r in rows:
        r["party_num"] = num_of[(r["group"], r["img"], r["y"], r["x"])]
    return rows


def _norm(s: str) -> str:
    return re.sub(r"[\s._\-'’•●]", "", s).lower()


def match_names(rows: list, profiles: list, cutoff: float = 0.55):
    """
    จับคู่ชื่อที่อ่านได้กับ player_profiles แบบไม่ซ้ำคน (คะแนนสูงสุดได้ก่อน)
    ใส่ r["profile"] = profile ที่จับคู่ได้ หรือ None
    """
    cands = []
    for ri, r in enumerate(rows):
        for pi, p in enumerate(profiles):
            pn = _norm(p["in_game_name"])
            if not pn:
                continue
            best = 0.0
            for t in r["name_texts"]:
                tn = _norm(t)
                if not tn:
                    continue
                sc = difflib.SequenceMatcher(None, tn, pn).ratio()
                if tn == pn:
                    sc = 1.0
                elif len(pn) >= 4 and (pn in tn or tn in pn):
                    sc = max(sc, 0.8)
                best = max(best, sc)
            if best >= max(cutoff, p.get("min_score", 0)):
                cands.append((best + p.get("bonus", 0), ri, pi))
    cands.sort(reverse=True)
    used_r, used_p = set(), set()
    for r in rows:
        r["profile"] = None
    for sc, ri, pi in cands:
        uid = profiles[pi]["discord_user_id"]
        if ri in used_r or uid in used_p:
            continue
        rows[ri]["profile"] = profiles[pi]
        used_r.add(ri)
        used_p.add(uid)
    return rows
