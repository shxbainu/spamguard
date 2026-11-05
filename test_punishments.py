import asyncio
import base64
import unittest

from aiohttp.test_utils import TestClient, TestServer

from dashboard import create_app
from escalation import Step, format_duration, parse_ladder, step_for
from storage import Store

DAY = 86400


class LadderTests(unittest.TestCase):
    def test_parse_default_ladder(self):
        ladder = parse_ladder("30s, 2m, 10m, 1h, kick", kick_seconds=600)
        self.assertEqual(ladder, [
            Step("timeout", 30), Step("timeout", 120), Step("timeout", 600), Step("timeout", 3600), Step("kick", 600),
        ])

    def test_offenses_climb_then_stay_on_last_step(self):
        ladder = parse_ladder("30s,2m,kick", kick_seconds=600)
        self.assertEqual(step_for(ladder, 1), Step("timeout", 30))
        self.assertEqual(step_for(ladder, 2), Step("timeout", 120))
        self.assertEqual(step_for(ladder, 3).action, "kick")
        self.assertEqual(step_for(ladder, 9).action, "kick")

    def test_bad_ladders(self):
        for bad in ("", "30x", "29d", "ten minutes"):
            with self.assertRaises(ValueError, msg=bad):
                parse_ladder(bad, kick_seconds=600)

    def test_format_duration(self):
        self.assertEqual([format_duration(s) for s in (30, 90, 120, 3600, DAY)], ["30s", "90s", "2m", "1h", "1d"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_offenses_count_up_and_reset_after_quiet_period(self):
        self.assertEqual(self.store.next_offense(1, 7, now=0, reset_after=DAY), 1)
        self.assertEqual(self.store.next_offense(1, 7, now=100, reset_after=DAY), 2)
        self.assertEqual(self.store.next_offense(1, 8, now=100, reset_after=DAY), 1, "users are separate")
        self.assertEqual(self.store.offenses(1, 7, now=200, reset_after=DAY), 2)
        self.assertEqual(self.store.next_offense(1, 7, now=100 + DAY + 1, reset_after=DAY), 1, "reset after a quiet day")

    def test_forgive(self):
        self.store.next_offense(1, 7, now=0, reset_after=DAY)
        self.store.forgive(1, 7)
        self.assertEqual(self.store.offenses(1, 7, now=1, reset_after=DAY), 0)

    def record(self, **overrides):
        fields = dict(guild_id=1, user_id=7, user_name="dave", channel_name="general", action="timeout",
                      duration_seconds=30, offense=1, category="gif", reason="GIF spam", evidence="4 GIFs")
        fields.update(overrides)
        return self.store.record(**fields)

    def test_history_filters_and_stats(self):
        self.record(created_at=1000)
        self.record(created_at=2000, user_id=8, user_name="amy", category="ai", action="kick", duration_seconds=600)
        self.record(created_at=3000, category="repeat", offense=2)

        self.assertEqual([p["user_name"] for p in self.store.punishments()], ["dave", "amy", "dave"], "newest first")
        self.assertEqual(len(self.store.punishments(user="dav")), 2)
        self.assertEqual(len(self.store.punishments(user="8")), 1)
        self.assertEqual(len(self.store.punishments(category="ai")), 1)
        self.assertEqual(len(self.store.punishments(action="kick")), 1)

        stats = self.store.stats(now=3000)
        self.assertEqual((stats["total"], stats["timeouts"], stats["kicks"]), (3, 2, 1))
        self.assertEqual(stats["by_category"], {"gif": 1, "repeat": 1, "ai": 1})
        self.assertEqual(stats["top_users_7d"][0], {"user_id": 7, "user_name": "dave", "count": 2})


class DashboardTests(unittest.TestCase):
    def call(self, store, path, password="", auth=None):
        async def run():
            async with TestClient(TestServer(create_app(store, password))) as client:
                headers = {"Authorization": auth} if auth else {}
                resp = await client.get(path, headers=headers)
                body = await resp.json() if resp.content_type == "application/json" else await resp.text()
                return resp.status, body
        return asyncio.run(run())

    def test_api_and_page(self):
        store = Store(":memory:")
        store.record(guild_id=1, user_id=7, user_name="dave", channel_name="general", action="timeout",
                     duration_seconds=30, offense=1, category="gif", reason="GIF spam")
        status, rows = self.call(store, "/api/punishments?category=gif")
        self.assertEqual((status, rows[0]["reason"]), (200, "GIF spam"))
        status, stats = self.call(store, "/api/stats")
        self.assertEqual(stats["total"], 1)
        status, page = self.call(store, "/")
        self.assertIn("Butt Spreader", page)

    def test_password(self):
        store = Store(":memory:")
        self.assertEqual(self.call(store, "/api/stats", password="hunter2")[0], 401)
        good = "Basic " + base64.b64encode(b"admin:hunter2").decode()
        self.assertEqual(self.call(store, "/api/stats", password="hunter2", auth=good)[0], 200)


if __name__ == "__main__":
    unittest.main()
