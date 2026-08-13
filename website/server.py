import os
from aiohttp import web

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


async def index(request):
    return web.FileResponse(os.path.join(BASE_DIR, "index.html"))


async def docs(request):
    return web.FileResponse(os.path.join(BASE_DIR, "docs.html"))


async def health(request):
    return web.Response(text="OK", status=200)


def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/docs.html", docs)
    app.router.add_get("/health", health)
    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
