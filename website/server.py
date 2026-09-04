import os
from aiohttp import web

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _read_html(filename: str) -> str:
    with open(os.path.join(BASE_DIR, filename), "r", encoding="utf-8") as f:
        return f.read()


async def index(request):
    return web.Response(text=_read_html("index.html"), content_type="text/html", charset="utf-8")


async def docs(request):
    return web.Response(text=_read_html("docs.html"), content_type="text/html", charset="utf-8")


async def invite(request):
    return web.Response(text=_read_html("invite.html"), content_type="text/html", charset="utf-8")


async def health(request):
    return web.Response(text="OK", status=200)


def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/docs.html", docs)
    app.router.add_get("/invite.html", invite)
    app.router.add_get("/health", health)
    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
