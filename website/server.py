import html
import os
import time
from collections import deque

from aiohttp import web

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _read_html(filename: str) -> str:
    with open(os.path.join(BASE_DIR, filename), "r", encoding="utf-8") as f:
        return f.read()


def _render_index() -> str:
    """ใส่เลขพร้อมเพย์/ลิงก์ซัพพอร์ตจาก environment variable ของ Railway (ไม่เก็บไว้ใน git)"""
    page = _read_html("index.html")
    promptpay = os.getenv("PROMPTPAY_ID", "").strip()
    promptpay_name = os.getenv("PROMPTPAY_NAME", "").strip()
    support_url = os.getenv("SUPPORT_URL", "").strip()
    page = page.replace("{{PROMPTPAY_ID}}", html.escape(promptpay or "ขอเลขจากแอดมิน"))
    page = page.replace("{{PROMPTPAY_NAME}}", html.escape(promptpay_name))
    page = page.replace("{{TRUEMONEY_ID}}", html.escape(os.getenv("TRUEMONEY_ID", "").strip() or "ขอเลขจากแอดมิน"))
    if support_url.startswith("https://"):
        page = page.replace("{{SUPPORT_URL}}", html.escape(support_url, quote=True))
    else:
        page = page.replace('<a class="btn primary" href="{{SUPPORT_URL}}" rel="noopener">เข้าเซิร์ฟเวอร์ซัพพอร์ต</a>', "")
    return page


async def index(request):
    return web.Response(text=_render_index(), content_type="text/html", charset="utf-8")


def _redirect(target: str):
    async def handler(request):
        raise web.HTTPMovedPermanently(target)
    return handler


async def docs(request):
    return web.Response(text=_read_html("docs.html"), content_type="text/html", charset="utf-8")


async def invite(request):
    return web.Response(text=_read_html("invite.html"), content_type="text/html", charset="utf-8")


async def pricing(request):
    return web.Response(text=_read_html("pricing.html"), content_type="text/html", charset="utf-8")


async def commands(request):
    return web.Response(text=_read_html("commands.html"), content_type="text/html", charset="utf-8")


async def terms(request):
    return web.Response(text=_read_html("terms.html"), content_type="text/html", charset="utf-8")


async def privacy(request):
    return web.Response(text=_read_html("privacy.html"), content_type="text/html", charset="utf-8")


async def favicon(request):
    return web.FileResponse(os.path.join(BASE_DIR, "favicon.svg"),
                            headers={"Content-Type": "image/svg+xml", "Cache-Control": "public, max-age=604800"})


async def robots(request):
    return web.Response(text="User-agent: *\nAllow: /\n", content_type="text/plain")


async def health(request):
    return web.Response(text="OK", status=200)


# ── ป้องกันการยิงเว็บ ──
# เว็บนี้เป็นหน้าอ่านอย่างเดียว ไม่มีฟอร์ม/ล็อกอิน จุดเสี่ยงหลักคือโดนยิงคำขอรัวๆ ให้เซิร์ฟเวอร์ล่ม
# กับโดนเว็บอื่นเอาไปฝัง iframe หลอกคนดู จึงกันไว้ 4 ชั้น:
#   1) รับแค่ GET/HEAD  2) จำกัดจำนวนคำขอต่อ IP  3) ไม่รับ body/URL ยาวผิดปกติ  4) header ความปลอดภัยทุกหน้า

RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "120"))  # คำขอต่อ IP ต่อนาที
_hits: dict = {}
_last_sweep = 0.0

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https://cdn.discordapp.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com; object-src 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _client_ip(request) -> str:
    # Railway ส่ง IP จริงมาใน X-Forwarded-For ตัวแรก
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or (request.remote or "?")


def _rate_limited(ip: str) -> bool:
    global _last_sweep
    now = time.monotonic()
    q = _hits.get(ip)
    if q is None:
        q = _hits[ip] = deque()
    while q and now - q[0] > 60:
        q.popleft()
    q.append(now)
    if now - _last_sweep > 300:  # ล้าง IP ที่เงียบไปแล้ว กันหน่วยความจำบวม
        _last_sweep = now
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
            _hits.pop(k, None)
    return len(q) > RATE_LIMIT


@web.middleware
async def protect(request, handler):
    if request.path != "/health":
        if request.method not in ("GET", "HEAD"):
            return web.Response(status=405, text="Method Not Allowed", headers={"Allow": "GET, HEAD"})
        if len(request.raw_path) > 2048 or (request.content_length or 0) > 0:
            return web.Response(status=400, text="Bad Request")
        if _rate_limited(_client_ip(request)):
            return web.Response(status=429, text="Too Many Requests — รอสักครู่แล้วลองใหม่", headers={"Retry-After": "60"})
    return await handler(request)


async def add_security_headers(request, response):
    """ใส่ทุกคำตอบ รวมหน้า 404/redirect/429"""
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    response.headers.pop("Server", None)


def main():
    app = web.Application(middlewares=[protect], client_max_size=1024)
    app.on_response_prepare.append(add_security_headers)
    app.router.add_get("/", index)
    app.router.add_get("/docs.html", docs)
    # หน้าเก่าย้ายไปรวมในหน้าแรกแล้ว — ลิงก์เดิมที่แชร์ไว้ยังพาไปถูกที่
    app.router.add_get("/index.html", _redirect("/"))
    app.router.add_get("/invite.html", _redirect("/#invite"))
    app.router.add_get("/pricing.html", _redirect("/#plans"))
    app.router.add_get("/commands.html", _redirect("/#commands"))
    app.router.add_get("/terms.html", terms)
    app.router.add_get("/privacy.html", privacy)
    app.router.add_get("/favicon.svg", favicon)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/health", health)
    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
