import html
import os
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


async def health(request):
    return web.Response(text="OK", status=200)


def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/docs.html", docs)
    # หน้าเก่าย้ายไปรวมในหน้าแรกแล้ว — ลิงก์เดิมที่แชร์ไว้ยังพาไปถูกที่
    app.router.add_get("/index.html", _redirect("/"))
    app.router.add_get("/invite.html", _redirect("/#invite"))
    app.router.add_get("/pricing.html", _redirect("/#plans"))
    app.router.add_get("/commands.html", _redirect("/#commands"))
    app.router.add_get("/terms.html", terms)
    app.router.add_get("/privacy.html", privacy)
    app.router.add_get("/health", health)
    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
