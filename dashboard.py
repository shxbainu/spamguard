"""Small web dashboard listing every timeout and kick, served by the bot itself.

Run `python dashboard.py` to view the saved history without starting the bot.
"""
from __future__ import annotations

import base64
import hmac
import time
from pathlib import Path

from aiohttp import web

from storage import Store

PAGE = Path(__file__).with_name("dashboard.html")


def create_app(store: Store, password: str = "") -> web.Application:
    @web.middleware
    async def require_password(request: web.Request, handler):
        if password:
            expected = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
            if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
                return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Bens Butt Spreader"'})
        return await handler(request)

    async def index(request: web.Request) -> web.Response:
        return web.Response(text=PAGE.read_text(), content_type="text/html")

    async def punishments(request: web.Request) -> web.Response:
        q = request.query
        rows = store.punishments(
            limit=min(int(q.get("limit", 200)), 1000),
            user=q.get("user") or None,
            category=q.get("category") or None,
            action=q.get("action") or None,
        )
        return web.json_response(rows)

    async def stats(request: web.Request) -> web.Response:
        return web.json_response(store.stats(time.time()))

    app = web.Application(middlewares=[require_password])
    app.add_routes([
        web.get("/", index),
        web.get("/api/punishments", punishments),
        web.get("/api/stats", stats),
    ])
    return app


async def start_dashboard(store: Store, host: str, port: int, password: str = "") -> web.AppRunner:
    runner = web.AppRunner(create_app(store, password))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner


if __name__ == "__main__":
    import os

    from dotenv import load_dotenv

    load_dotenv()
    store = Store(str(Path(__file__).with_name("spamguard.db")))
    web.run_app(
        create_app(store, os.getenv("DASHBOARD_PASSWORD", "")),
        host=os.getenv("DASHBOARD_HOST") or "127.0.0.1",
        port=int(os.getenv("DASHBOARD_PORT") or 8080),
    )
