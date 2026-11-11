"""Tests for the daily prediction and the Daily Aura."""
import asyncio
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import news
import predict
from test_features import temp_store
from test_games import person

ZONE = ZoneInfo("America/New_York")
DAY = datetime(2026, 11, 10, 12, 0, tzinfo=ZONE)
START = DAY.replace(hour=0).timestamp()
SPAN = (START, START + 86400)


class FakeChannel:
    def __init__(self):
        self.guild = SimpleNamespace(id=1, me=person(99, "bot"))
        self.sent = []

    async def send(self, content=None, embed=None, **kwargs):
        self.sent.append(embed)


class Writer:
    def __init__(self, answer, searches=True, sources=(("ESPN", "https://espn.com"),)):
        self.answer, self.calls, self.can_search, self.sources = answer, 0, searches, list(sources)
        self.last_model = "writer"

    async def complete(self, system, prompt, schema, max_tokens):
        self.calls += 1
        self.prompt = prompt
        return json.dumps(self.answer)

    async def research(self, system, prompt, schema, max_tokens=4096):
        self.calls += 1
        return self.answer, self.sources


def predictor(store, settler=None, now=DAY):
    channel = FakeChannel()
    bot = SimpleNamespace(store=store, settler=settler, grass=SimpleNamespace(zone=ZONE), get_channel=lambda i: channel)
    p = predict.Predictions(bot)
    p.today = lambda: now
    p.channel = lambda: channel
    return p, channel


class PredictionTests(unittest.TestCase):
    def test_chat_questions_settle_from_records(self):
        store = temp_store()
        self.assertEqual(predict.settle_chat(store, 1, {"kind": "timeout"}, SPAN, 0)[0], "no")
        store.db.execute(
            "INSERT INTO punishments (created_at, guild_id, user_id, user_name, action, duration_seconds, offense, category, reason) "
            "VALUES (?, 1, 5, 'ben', 'timeout', 60, 1, 'gif', 'GIF spam')", (START + 100,))
        self.assertEqual(predict.settle_chat(store, 1, {"kind": "timeout"}, SPAN, 0)[0], "yes")
        self.assertEqual(predict.settle_chat(store, 1, {"kind": "timeout"}, (SPAN[1], SPAN[1] + 86400), 0)[0], "no", "only that day")
        store.add_aura(1, 5, "ben", -100, "false_claim", "confidently: x", now=START + 5)
        self.assertEqual(predict.settle_chat(store, 1, {"kind": "moment"}, SPAN, 0), ("yes", "biggest was -100"))
        store.add_aura(1, 6, "kai", -600, "slots", "bet", now=START + 5)
        self.assertEqual(predict.settle_chat(store, 1, {"kind": "casino"}, SPAN, 0)[0], "no")
        store.add_aura(1, 6, "kai", -500, "roulette", "bet", now=START + 6)
        self.assertEqual(predict.settle_chat(store, 1, {"kind": "casino"}, SPAN, 0)[0], "yes")
        self.assertEqual(predict.settle_chat(store, 1, {"kind": "messages", "line": 300}, SPAN, 301)[0], "yes")

    def test_lines_come_from_recent_days(self):
        # A small server: casino losses around 150-300 a day, nobody near 1,000.
        history = {"messages": [437, 380, 512, 300], "timeout": [0] * 14,
                   "moment": [50, 0, 100, 25, 50, 0, 50, 100, 0, 25, 50, 0, 0, 50],
                   "casino": [200, 0, 300, 150, 0, 250, 0, 200, 0, 0, 150, 300, 0, 200]}
        found = {q["kind"]: q for q in predict.chat_questions(history)}
        self.assertEqual(found["messages"]["line"], 400)
        self.assertEqual(found["casino"]["line"], 200, "realistic for how much people actually lose here")
        self.assertIn("200+ aura in the casino", found["casino"]["question"])
        self.assertEqual(found["moment"]["line"], 50)
        self.assertNotIn("timeout", found, "never happens here, so it's not a real question")
        for question in found.values():
            self.assertTrue(0.2 <= question["rate"] <= 0.8, question)

    def test_no_history_means_no_server_questions(self):
        self.assertEqual(predict.chat_questions({"messages": [], "timeout": [0, 0], "moment": [], "casino": []}), [])
        self.assertEqual(predict.chat_questions({"timeout": [1] * 14}), [], "happens every day, so it's a sure thing")

    def test_history_reads_the_past_days(self):
        store = temp_store()
        p, _ = predictor(store)
        store.add_aura(1, 6, "kai", -300, "slots", "bet", now=START - 86400 + 60)  # yesterday
        store.add_aura(1, 6, "kai", -999, "slots", "bet", now=START + 60)  # today: not history
        p.messages[(1, "2026-11-09")] = 120
        days = p.history(1, DAY)
        self.assertEqual(days["casino"][0], 300)
        self.assertEqual(days["messages"], [120])
        self.assertEqual(len(days["timeout"]), predict.HISTORY_DAYS)

    def test_quiet_server_skips_the_day(self):
        store = temp_store()
        p, channel = predictor(store)
        asyncio.run(p.tick())
        self.assertEqual(p.load(1, "2026-11-10")["status"], "skipped")
        self.assertEqual(channel.sent, [], "nothing posted")
        asyncio.run(p.tick())
        self.assertEqual(channel.sent, [], "and it doesn't retry every minute")
        self.assertIn("no question today", p.place(person(5, "ben"), "yes", "10"))

    def test_payouts(self):
        bets = {"5": ["yes", 100, "ben"], "6": ["no", 50, "kai"]}
        self.assertEqual(predict.payouts(bets, "yes"), {"5": 200})
        self.assertEqual(predict.payouts(bets, "unknown"), {"5": 100, "6": 50})

    def test_full_day_with_bets(self):
        store = temp_store()
        p, channel = predictor(store)
        ben, kai, luke = person(5, "ben"), person(6, "kai"), person(7, "luke")
        for who in (ben, kai, luke):
            who.guild = channel.guild
        self.assertIn("goes up at", p.place(ben, "yes", "100"))
        for back in range(1, 8):  # a week where somebody got a 100-aura moment every other day
            store.add_aura(1, 8, "luke", 100 if back % 2 else 25, "comeback", "x", now=START - back * 86400 + 60)
        asyncio.run(p.tick())
        data = p.load(1, "2026-11-10")
        self.assertEqual(data["status"], "open")
        self.assertEqual(len(channel.sent), 1)
        asyncio.run(p.tick())
        self.assertEqual(len(channel.sent), 1, "one question a day")
        self.assertIsInstance(p.place(ben, "yes", "100"), predict.discord.Embed)
        self.assertIn("already bet", p.place(ben, "no", "100"))
        self.assertIn("most you can bet", p.place(kai, "no", "9999"))
        p.place(kai, "no", "50")
        data = p.load(1, "2026-11-10")
        data.update(kind="timeout", question="Will the bot time anyone out today?")
        p.save(1, "2026-11-10", data)
        p.today = lambda: DAY.replace(hour=19)
        self.assertIn("closed", p.place(luke, "yes", "10"))
        p.today = lambda: DAY.replace(hour=23, minute=45)
        asyncio.run(p.tick())
        self.assertEqual(p.load(1, "2026-11-10")["answer"], "no")
        self.assertEqual(store.aura_total(1, 5), 400, "500 start - 100")
        self.assertEqual(store.aura_total(1, 6), 550, "500 start - 50 + 100")
        self.assertIn("kai", channel.sent[-1].fields[0].value)

    def test_settles_yesterday_after_being_offline(self):
        store = temp_store()
        p, channel = predictor(store)
        p.save(1, "2026-11-09", {"kind": "timeout", "question": "q", "status": "open", "bets": {}})
        p.today = lambda: DAY.replace(hour=8)
        asyncio.run(p.tick())
        self.assertEqual(p.load(1, "2026-11-09")["status"], "settled")
        self.assertIsNone(p.load(1, "2026-11-10"), "not open yet")

    def test_world_question_needs_real_search_results(self):
        store = temp_store()
        no_sources = Writer({"question": "Will the Knicks beat the Celtics tonight?", "check": "knicks celtics"}, sources=())
        p, _ = predictor(store, no_sources)
        predict.PREDICT_WORLD_CHANCE, old = 1.0, predict.PREDICT_WORLD_CHANCE
        try:
            data = asyncio.run(p.make_question(1, DAY))
            self.assertNotEqual(data.get("kind"), "world", "no search results, so no made-up world question")
            p.bot.settler = Writer({"question": "Will the Knicks beat the Celtics tonight?", "check": "knicks celtics"})
            data = asyncio.run(p.make_question(1, DAY))
            self.assertEqual(data["kind"], "world")
            p.bot.settler = Writer({"answer": "unknown", "reason": "postponed"})
            self.assertEqual(asyncio.run(p.check_world(data, DAY))[0], "unknown")
            p.bot.settler = Writer({"answer": "yes", "reason": "Knicks won 110-104"}, sources=())
            self.assertEqual(asyncio.run(p.check_world(data, DAY))[0], "unknown", "no sources, so it isn't trusted")
            p.bot.settler = Writer({"answer": "yes", "reason": "Knicks won 110-104"})
            self.assertEqual(asyncio.run(p.check_world(data, DAY)), ("yes", "Knicks won 110-104"))
        finally:
            predict.PREDICT_WORLD_CHANCE = old

    def test_no_search_means_chat_questions_only(self):
        p, _ = predictor(temp_store(), Writer({}, searches=False))
        predict.PREDICT_WORLD_CHANCE, old = 1.0, predict.PREDICT_WORLD_CHANCE
        try:
            self.assertNotEqual(asyncio.run(p.make_question(1, DAY)).get("kind"), "world")
            self.assertEqual(p.bot.settler.calls, 0)
        finally:
            predict.PREDICT_WORLD_CHANCE = old


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.store = temp_store()

    def paper(self, writer):
        channel = FakeChannel()
        bot = SimpleNamespace(store=self.store, chatter=writer, settler=None, grass=SimpleNamespace(zone=ZONE))
        events = news.todays_events(self.store, 1, *SPAN)
        posted = asyncio.run(news.DailyNews(bot).post(channel, events))
        return posted, channel, events

    def test_quiet_day_costs_nothing(self):
        self.store.add_aura(1, 5, "ben", 25, "comeback", "decent: ok", now=START + 1)  # too small to count
        self.store.add_aura(1, 5, "ben", -100, "slots", "bet", now=START + 2)
        writer = Writer({"headline": "X", "story": "y", "also": []})
        posted, channel, events = self.paper(writer)
        self.assertFalse(posted)
        self.assertEqual(events, [])
        self.assertEqual(writer.calls, 0, "no AI call on a quiet day")
        self.assertEqual(channel.sent, [])

    def test_busy_day_makes_one_page(self):
        self.store.add_aura(1, 5, "ben", 250, "comeback", "brutal: ended kai", "ur hairline", now=START + 1)
        self.store.add_aura(1, 6, "kai", -800, "slots", "bet", now=START + 2)
        self.store.add_aura(1, 7, "luke", -100, "bad_take", "terrible: hates oranges", now=START + 3)
        self.store.add_aura(1, 7, "luke", 999, "comeback", "legendary: yesterday", now=START - 10)  # yesterday
        writer = Writer({"headline": "BEN ENDS KAI", "story": "Carnage.", "also": ["LUKE HATES ORANGES"]})
        posted, channel, events = self.paper(writer)
        self.assertTrue(posted)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(len(events), 3)
        self.assertIn("kai lost 800 aura in the casino", writer.prompt)
        self.assertNotIn("yesterday", writer.prompt)
        self.assertIn("BEN ENDS KAI", channel.sent[0].description)

    def test_bad_ai_answer_posts_nothing(self):
        for i in range(3):
            self.store.add_aura(1, 5, "ben", 100, "comeback", "big: x", now=START + i)
        posted, channel, _ = self.paper(Writer({"headline": "", "story": ""}))
        self.assertFalse(posted)
        self.assertEqual(channel.sent, [])


if __name__ == "__main__":
    unittest.main()
