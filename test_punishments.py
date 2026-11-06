import asyncio
import base64
import time
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

    def test_forgive_all_and_active_offenders(self):
        self.store.next_offense(1, 7, now=0, reset_after=3 * DAY)
        self.store.next_offense(1, 7, now=10, reset_after=3 * DAY)
        self.store.next_offense(1, 8, now=10, reset_after=3 * DAY)
        self.store.next_offense(2, 9, now=10, reset_after=3 * DAY)
        active = self.store.active_offenders(now=20, reset_after=3 * DAY)
        self.assertEqual([(o["user_id"], o["offenses"]) for o in active], [(7, 2), (8, 1), (9, 1)])
        self.assertEqual(active[0]["resets_at"], 10 + 3 * DAY)
        self.assertEqual(self.store.active_offenders(now=11 + 3 * DAY, reset_after=3 * DAY), [], "expired after 3 days")
        self.store.forgive_all(guild_id=1)
        self.assertEqual([o["user_id"] for o in self.store.active_offenders(now=20, reset_after=3 * DAY)], [9])

    def test_shutups_used_in_last_week(self):
        week = 7 * DAY
        for t in (0, 10, 20):
            self.store.record_shutup(1, 7, 8, now=t)
        self.store.record_shutup(2, 7, 8, now=20)
        self.assertEqual(self.store.shutups_used(1, 7, now=30, window=week), [0, 10, 20], "per server")
        self.assertEqual(self.store.shutups_used(1, 8, now=30, window=week), [], "per user")
        self.assertEqual(self.store.shutups_used(1, 7, now=week + 5, window=week), [10, 20], "oldest one expired")

    def test_event_log_filters_counts_and_prunes(self):
        now = time.time()
        log = self.store.log_event
        log(guild_id=1, kind="check", user_id=7, user_name="ben", content="wit", verdict="ok", reason="reaction",
            model="gemini", escalated=True, overruled=True, detail="first check: gpt-oss-20b: low_effort (gibberish)")
        log(guild_id=1, kind="check", user_id=7, user_name="ben", content="asdfgh", verdict="low_effort", reason="mash",
            model="gemini", escalated=True, detail="strike 1 of 3 in a row")
        log(guild_id=1, kind="reply", user_id=8, user_name="amy", content="syfm", model="gpt-oss-120b")
        log(guild_id=1, kind="check", user_id=7, user_name="ben", content="old", verdict="ok", created_at=now - 9 * DAY)

        self.assertEqual([e["content"] for e in self.store.events(kind="check", flagged=True)], ["asdfgh"])
        self.assertEqual(len(self.store.events(escalated=True)), 2)
        self.assertEqual([e["kind"] for e in self.store.events(user="amy")], ["reply"])
        activity = self.store.activity(now)
        self.assertEqual((activity["checked"], activity["flagged"], activity["second_opinions"], activity["overruled"],
                          activity["replies"]), (2, 1, 2, 1, 1))
        self.store.prune_events(now)
        self.assertNotIn("old", [e["content"] for e in self.store.events()], "older than 7 days is cleared")
        with self.assertRaises(TypeError):
            log(guild_id=1, kind="check", surprise="x")

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
        self.assertEqual(stats["by_category"], {"gif": 1, "repeat": 1, "ai": 1, "shutup": 0})
        self.assertEqual(stats["top_users_7d"][0], {"user_id": 7, "user_name": "dave", "count": 2})


class DashboardTests(unittest.TestCase):
    def call(self, store, path, password="", auth=None, post=None, on_reset=None):
        async def run():
            async with TestClient(TestServer(create_app(store, password, on_reset=on_reset))) as client:
                headers = {"Authorization": auth} if auth else {}
                if post is not None:
                    resp = await client.post(path, json=post, headers=headers)
                else:
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

    def test_big_ids_are_strings(self):
        store = Store(":memory:")
        big = 1222222222222222221
        store.next_offense(1, big, now=time.time(), reset_after=DAY)
        store.record(guild_id=1, user_id=big, user_name="dave", channel_name="general", action="timeout",
                     duration_seconds=10, offense=1, category="gif", reason="GIF spam")
        self.assertEqual(self.call(store, "/api/punishments")[1][0]["user_id"], str(big))
        self.assertEqual(self.call(store, "/api/offenders")[1][0]["user_id"], str(big))

    def test_event_api(self):
        store = Store(":memory:")
        big = 1222222222222222221
        store.log_event(guild_id=1, kind="check", user_id=big, user_name="ben", content="wit", verdict="low_effort",
                        reason="gibberish", model="gemini")
        status, rows = self.call(store, "/api/events?kind=check&flagged=1")
        self.assertEqual((status, rows[0]["user_id"], rows[0]["content"]), (200, str(big), "wit"))
        status, stats = self.call(store, "/api/stats")
        self.assertEqual(stats["activity"]["flagged"], 1)
        status, page = self.call(store, "/")
        self.assertIn("Message log", page)

    def test_reset_one_and_all(self):
        calls = []
        store = Store(":memory:")
        status, _ = self.call(store, "/api/reset", post={"guild_id": "1", "user_id": "1222222222222222221"},
                              on_reset=lambda g, u: calls.append((g, u)))
        self.assertEqual(status, 200)
        self.call(store, "/api/reset", post={"all": True}, on_reset=lambda g, u: calls.append((g, u)))
        self.assertEqual(calls, [(1, 1222222222222222221), (None, None)])
        self.assertEqual(self.call(store, "/api/reset", post={})[0], 400)

    def test_password(self):
        store = Store(":memory:")
        self.assertEqual(self.call(store, "/api/stats", password="hunter2")[0], 401)
        good = "Basic " + base64.b64encode(b"admin:hunter2").decode()
        self.assertEqual(self.call(store, "/api/stats", password="hunter2", auth=good)[0], 200)


if __name__ == "__main__":
    unittest.main()
