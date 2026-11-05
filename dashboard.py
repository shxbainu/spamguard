"""Small web dashboard listing every timeout and kick, served by the bot itself.

Run `python dashboard.py` to view the saved history without starting the bot.
"""
from __future__ import annotations

import base64
import hmac
import time
from pathlib import Path
from typing import Callable, Optional

from aiohttp import web

from storage import Store

PAGE = Path(__file__).with_name("dashboard.html")


def ids_as_text(rows: list[dict]) -> list[dict]:
    """Discord IDs don't fit in a JavaScript number, so the page gets them as strings."""
    return [{k: str(v) if k in ("guild_id", "user_id") else v for k, v in row.items()} for row in rows]


ResetCallback = Callable[[Optional[int], Optional[int]], None]


def create_app(
    store: Store,
    password: str = "",
    reset_after: float = 72 * 3600,
    on_reset: ResetCallback | None = None,
) -> web.Application:
    """on_reset(guild_id, user_id) clears offenses; user_id None means everyone.
    Without it (dashboard running on its own), only the database is cleared."""
    if on_reset is None:
        def on_reset(guild_id, user_id):
            if user_id is None:
                store.forgive_all(guild_id)
            else:
                store.forgive(guild_id, user_id)

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
        return web.json_response(ids_as_text(rows))

    async def stats(request: web.Request) -> web.Response:
        stats = store.stats(time.time())
        stats["top_users_7d"] = ids_as_text(stats["top_users_7d"])
        return web.json_response(stats)

    async def offenders(request: web.Request) -> web.Response:
        return web.json_response(ids_as_text(store.active_offenders(time.time(), reset_after)))

    async def reset(request: web.Request) -> web.Response:
        # Requiring a JSON body stops other websites from triggering this through your browser.
        if request.content_type != "application/json":
            return web.json_response({"error": "expected JSON"}, status=415)
        body = await request.json()
        guild_id = body.get("guild_id")
        user_id = body.get("user_id")
        if user_id is None and not body.get("all"):
            return web.json_response({"error": "give user_id, or all: true"}, status=400)
        on_reset(int(guild_id) if guild_id is not None else None, int(user_id) if user_id is not None else None)
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[require_password])
    app.add_routes([
        web.get("/", index),
        web.get("/api/punishments", punishments),
        web.get("/api/stats", stats),
        web.get("/api/offenders", offenders),
        web.post("/api/reset", reset),
    ])
    return app


async def start_dashboard(
    store: Store,
    host: str,
    port: int,
    password: str = "",
    reset_after: float = 72 * 3600,
    on_reset: ResetCallback | None = None,
) -> web.AppRunner:
    runner = web.AppRunner(create_app(store, password, reset_after, on_reset))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner


if __name__ == "__main__":
    import os

    from dotenv import load_dotenv

    load_dotenv()
    store = Store(str(Path(__file__).with_name("spamguard.db")))
    web.run_app(
        create_app(store, os.getenv("DASHBOARD_PASSWORD", ""), int(os.getenv("OFFENSE_RESET_HOURS") or 72) * 3600),
        host=os.getenv("DASHBOARD_HOST") or "127.0.0.1",
        port=int(os.getenv("DASHBOARD_PORT") or 8080),
    )
