"""
ตรวจสลิปโอนเงินอัตโนมัติผ่าน EasySlip API v2 (https://document.easyslip.com)

ใช้กับสลิปธนาคาร/พร้อมเพย์เท่านั้น — สลิปทรูมันนี่ยังต้องให้แอดมินเปิดเอง (/admin grant)

ตั้งค่า:
  EASYSLIP_API_KEY   คีย์จาก EasySlip (ไม่ตั้ง = ส่งสลิปให้แอดมินตรวจเองแทน)
  ต้องลงทะเบียนบัญชีรับเงินของเราใน EasySlip ก่อน เพื่อให้ matchAccount เช็คว่าโอนเข้าบัญชีเราจริง
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

import aiohttp

log = logging.getLogger("relay.slip")

API_URL = "https://api.easyslip.com/v2/verify/bank"
MAX_SLIP_AGE = timedelta(days=3)


class SlipResult(NamedTuple):
    ok: bool
    reason: str                 # ข้อความภาษาไทยสำหรับตอบลูกค้า
    trans_ref: Optional[str] = None
    amount: float = 0.0
    sender: str = ""
    retry_later: bool = False   # สลิปกรุงเทพที่เพิ่งโอน รอ 5 นาทีแล้วลองใหม่
    needs_admin: bool = False   # ตรวจอัตโนมัติไม่ได้ ให้แอดมินดูเอง


def is_enabled() -> bool:
    return bool(os.getenv("EASYSLIP_API_KEY"))


_ERRORS = {
    "SLIP_NOT_FOUND": "ไม่พบ QR ในสลิป หรือสลิปไม่ถูกต้อง",
    "IMAGE_SIZE_TOO_LARGE": "รูปใหญ่เกิน 4MB",
    "INVALID_IMAGE_FORMAT": "ไฟล์ไม่ใช่รูปภาพที่รองรับ (JPG/PNG/WebP)",
}


async def verify(image: bytes, filename: str, expected_min: float) -> SlipResult:
    """ส่งรูปสลิปไปตรวจ คืนผลที่ตัดสินแล้วว่าใช้เปิดสิทธิ์ได้หรือไม่"""
    key = os.getenv("EASYSLIP_API_KEY")
    if not key:
        return SlipResult(False, "ส่งให้แอดมินตรวจ", needs_admin=True)

    form = aiohttp.FormData()
    form.add_field("image", image, filename=filename or "slip.jpg")
    form.add_field("checkDuplicate", "true")
    form.add_field("matchAccount", "true")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.post(API_URL, data=form, headers={"Authorization": f"Bearer {key}"}) as resp:
                body = await resp.json(content_type=None)
    except Exception:
        log.exception("เรียก EasySlip ไม่สำเร็จ")
        return SlipResult(False, "ระบบตรวจสลิปติดต่อไม่ได้ชั่วคราว ส่งให้แอดมินตรวจแล้ว", needs_admin=True)

    if not body.get("success"):
        code = (body.get("error") or {}).get("code", "")
        if code == "SLIP_PENDING":
            return SlipResult(False, "สลิปกรุงเทพที่เพิ่งโอนยังตรวจไม่ได้ รอ 5 นาทีแล้วส่งใหม่", retry_later=True)
        if code in _ERRORS:
            return SlipResult(False, _ERRORS[code])
        log.error(f"EasySlip ตอบ error: {code} {body.get('message')}")
        return SlipResult(False, "ระบบตรวจสลิปมีปัญหา ส่งให้แอดมินตรวจแล้ว", needs_admin=True)

    data = body.get("data") or {}
    raw = data.get("rawSlip") or {}
    trans_ref = raw.get("transRef") or data.get("transRef")
    amount = float(((raw.get("amount") or {}).get("amount")) or 0)
    sender = (((raw.get("sender") or {}).get("account") or {}).get("name") or {}).get("th", "")

    if not trans_ref:
        return SlipResult(False, "อ่านเลขอ้างอิงสลิปไม่ได้ ส่งให้แอดมินตรวจแล้ว", needs_admin=True)
    if data.get("isDuplicate"):
        return SlipResult(False, "สลิปนี้ถูกใช้ไปแล้ว", trans_ref, amount, sender)

    # ต้องโอนเข้าบัญชีที่ลงทะเบียนไว้ใน EasySlip — ถ้า API ไม่ตอบผลการจับคู่มา ให้แอดมินดูเอง ไม่เปิดให้อัตโนมัติ
    matched = data.get("matchedAccount")
    if matched is None:
        return SlipResult(False, "ยืนยันบัญชีผู้รับอัตโนมัติไม่ได้ ส่งให้แอดมินตรวจแล้ว", trans_ref, amount, sender, needs_admin=True)
    if not matched:
        return SlipResult(False, "สลิปนี้ไม่ได้โอนเข้าบัญชีของ Solarlit", trans_ref, amount, sender)

    try:
        slip_time = datetime.fromisoformat(str(raw.get("date")).replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - slip_time > MAX_SLIP_AGE:
            return SlipResult(False, "สลิปเก่าเกิน 3 วัน ส่งให้แอดมินตรวจแล้ว", trans_ref, amount, sender, needs_admin=True)
    except (TypeError, ValueError):
        pass

    if amount < expected_min:
        return SlipResult(False, f"ยอดโอน {amount:,.0f} บาท ไม่ถึงราคาขั้นต่ำ {expected_min:,.0f} บาท", trans_ref, amount, sender)

    return SlipResult(True, "ตรวจสลิปผ่าน", trans_ref, amount, sender)
