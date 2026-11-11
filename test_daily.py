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
        self.sent, self.views, self.edits = [], [], []
        self.mention = "#casino"

    async def send(self, content=None, embed=None, view=None, **kwargs):
        self.sent.append(embed)
        self.views.append(view)
        return SimpleNamespace(id=1000 + len(self.sent))

    def get_partial_message(self, message_id):
        channel = self

        class Partial:
            async def edit(self, **kwargs):
                channel.edits.append((message_id, kwargs))
        return Partial()


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


WORLD = {"markets": [
    {"question": "How many points does Wemby drop tonight?", "outcomes": ["Under 25", "25 to 29", "30+"],
     "chances": [30, 40, 30], "check": "wembanyama points tonight"},
    {"question": "Will it rain in London today?", "outcomes": ["Yes", "No"], "chances": [80, 20], "check": "london rain today"},
    {"question": "broken", "outcomes": ["only one"], "chances": [100], "check": "x"},
]}


def bettors(channel):
    people = [person(5, "ben"), person(6, "kai"), person(7, "luke")]
    for who in people:
        who.guild = channel.guild
    return people


class PayoutTests(unittest.TestCase):
    def test_prices_follow_the_chances(self):
        self.assertEqual(predict.prices([50, 50], 2), [1.9, 1.9])
        self.assertEqual(predict.prices([80, 20], 2), [1.2, 4.8])
        self.assertEqual(predict.prices([99, 1], 2), [1.1, 8.0], "kept between 1.1x and 8x")
        self.assertEqual(predict.prices(None, 4), [3.8] * 4, "even odds when the chances are missing")
        self.assertEqual(predict.prices([60, 60, 80], 3), predict.prices([30, 30, 40], 3), "scaled to add up")

    def test_fixed_payouts_whoever_else_bets(self):
        market = {"outcomes": ["A", "B"], "pays": [1.9, 4.8], "bets": {"5": [1, 100, "ben"]}}
        self.assertEqual(predict.payouts(market, 1), {"5": 480}, "alone and still paid")
        self.assertEqual(predict.payouts(market, 0), {})
        market["bets"]["6"] = [0, 50, "kai"]
        self.assertEqual(predict.payouts(market, 0), {"6": 95})
        self.assertEqual(predict.payouts(market, -1), {"5": 100, "6": 50}, "unknown: bets back")

    def test_old_yes_no_days_still_settle(self):
        old = {"kind": "casino", "question": "Will anyone lose 1,000+ aura in the casino today?", "status": "open",
               "bets": {"5": ["no", 100, "ben"]}}
        new = predict.upgrade(old)
        market = new["markets"][0]
        self.assertEqual(market["outcomes"], ["Yes", "No"])
        self.assertEqual(market["bets"]["5"][0], 1)
        self.assertEqual(predict.payouts(market, 1), {"5": 200}, "the 2x it promised")


class ServerMarketTests(unittest.TestCase):
    def test_ranges_come_from_recent_days(self):
        history = {"messages": [437, 380, 512, 300, 410, 290], "timeout": [0] * 14,
                   "moment": [50, 0, 100, 25, 50, 0, 50, 100, 0, 25, 50, 0, 0, 50],
                   "casino": [200, 0, 300, 150, 0, 250, 0, 200, 0, 0, 150, 300, 0, 200]}
        found = {m["kind"]: m for m in predict.server_markets(history)}
        self.assertNotIn("timeout", found, "never happens here, so it's no market")
        self.assertEqual(found["messages"]["outcomes"], ["Under 375", "375 to 424", "425+"])
        casino = found["casino"]
        self.assertTrue(all(cut <= 300 for cut in casino["cuts"]), "realistic for how much people lose here")
        for market in found.values():
            self.assertGreaterEqual(min(market["shares"]), predict.MIN_SHARE, market)

    def test_quiet_server_has_no_markets(self):
        self.assertEqual(predict.server_markets({"messages": [], "timeout": [0, 0], "moment": [], "casino": []}), [])
        self.assertEqual(predict.server_markets({"timeout": [1] * 14}), [], "happens every day, so it's a sure thing")

    def test_buckets(self):
        self.assertEqual([predict.bucket(v, [100, 250]) for v in (0, 99, 100, 249, 250, 999)], [0, 0, 1, 1, 2, 2])

    def test_server_values(self):
        store = temp_store()
        store.db.execute(
            "INSERT INTO punishments (created_at, guild_id, user_id, user_name, action, duration_seconds, offense, category, reason) "
            "VALUES (?, 1, 5, 'ben', 'timeout', 60, 1, 'gif', 'GIF spam')", (START + 100,))
        self.assertEqual(predict.server_value(store, 1, "timeout", SPAN, 0)[0], 1)
        self.assertEqual(predict.server_value(store, 1, "timeout", (SPAN[1], SPAN[1] + 86400), 0)[0], 0, "only that day")
        store.add_aura(1, 6, "kai", -600, "slots", "bet", now=START + 5)
        store.add_aura(1, 6, "kai", -500, "roulette", "bet", now=START + 6)
        self.assertEqual(predict.server_value(store, 1, "casino", SPAN, 0)[0], 1100)
        store.add_aura(1, 5, "ben", -100, "false_claim", "x", now=START + 5)
        self.assertEqual(predict.server_value(store, 1, "moment", SPAN, 0), (100, "biggest swing was -100"))

    def test_history(self):
        store = temp_store()
        p, _ = predictor(store)
        store.add_aura(1, 6, "kai", -300, "slots", "bet", now=START - 86400 + 60)  # yesterday
        store.add_aura(1, 6, "kai", -999, "slots", "bet", now=START + 60)  # today: not history
        p.messages[(1, "2026-11-09")] = 120
        days = p.history(1, DAY)
        self.assertEqual(days["casino"][0], 300)
        self.assertEqual(days["messages"], [120])
        self.assertEqual(len(days["timeout"]), predict.HISTORY_DAYS)


class MarketDayTests(unittest.TestCase):
    def test_world_markets_need_real_search_results(self):
        p, _ = predictor(temp_store(), Writer(WORLD, sources=()))
        self.assertEqual(asyncio.run(p.world_markets(DAY, 2)), [], "no sources, so nothing made up")
        p.bot.settler = Writer(WORLD)
        markets = asyncio.run(p.world_markets(DAY, 3))
        self.assertEqual(len(markets), 2, "the broken one is dropped")
        self.assertEqual(markets[1]["pays"], [1.2, 4.8])
        p.bot.settler = Writer(WORLD, searches=False)
        self.assertEqual(asyncio.run(p.world_markets(DAY, 2)), [])
        self.assertEqual(p.bot.settler.calls, 0, "no search model, no AI call")

    def test_quiet_server_without_search_gets_house_draws(self):
        p, channel = predictor(temp_store())
        asyncio.run(p.tick())
        data = p.load(1, "2026-11-10")
        self.assertEqual([m["kind"] for m in data["markets"]], ["draw"] * 3)
        self.assertEqual(len(channel.sent), 3)
        asyncio.run(p.tick())
        self.assertEqual(len(channel.sent), 3, "once a day")
        ben = bettors(channel)[0]
        p.place(ben, "2026-11-10", 0, 0, "100")
        p.today = lambda: DAY.replace(hour=23, minute=45)
        asyncio.run(p.tick())
        market = p.load(1, "2026-11-10")["markets"][0]
        self.assertIn(market["answer"], range(len(market["outcomes"])), "the bot drew a result")
        won = market["answer"] == 0
        self.assertEqual(store_total := p.store.aura_total(1, 5), 400 + (int(100 * market["pays"][0]) if won else 0), store_total)

    def test_post_now(self):
        p, channel = predictor(temp_store())
        p.save(1, "2026-11-10", {"status": "skipped", "markets": []})  # what an old version left today
        p.today = lambda: DAY.replace(hour=17, minute=10)
        said = asyncio.run(p.post_now())
        self.assertIn("Posted 3 markets", said)
        self.assertIn("bets close 19:10", said, "two hours to bet when it's posted late")
        self.assertEqual(p.load(1, "2026-11-10")["close"], "19:10")
        ben = bettors(channel)[0]
        p.today = lambda: DAY.replace(hour=18, minute=30)
        self.assertNotIsInstance(p.place(ben, "2026-11-10", 0, 0, "10"), str, "still open after the usual 18:00")
        self.assertIn("already up", asyncio.run(p.post_now()))
        p2, _ = predictor(temp_store())
        p2.today = lambda: DAY.replace(hour=23, minute=10)
        self.assertIn("Too late", asyncio.run(p2.post_now()))

    def test_house_draws(self):
        import random
        for market in predict.HOUSE_MARKETS:
            seen = {predict.draw(market, random.Random(i))[0] for i in range(500)}
            self.assertEqual(seen, set(range(len(market["outcomes"]))), market["draw"])
            self.assertAlmostEqual(sum(market["shares"]), 1)

    def test_full_day(self):
        store = temp_store()
        p, channel = predictor(store, Writer(WORLD))
        ben, kai, luke = bettors(channel)
        asyncio.run(p.tick())
        data = p.load(1, "2026-11-10")
        self.assertEqual([m["kind"] for m in data["markets"]], ["world", "world", "draw"], "nothing about the server yet, so a draw")
        self.assertEqual(len(channel.sent), 3)
        self.assertEqual([m["message_id"] for m in data["markets"]], [1001, 1002, 1003])
        buttons = channel.views[1].children
        self.assertEqual([b.item.label for b in buttons], ["A. Yes", "B. No"])
        self.assertEqual(buttons[1].item.custom_id, "predict:2026-11-10:1:1")
        self.assertIn("pays **4.8x**", channel.sent[1].description)

        market, said = p.place(ben, "2026-11-10", 1, 1, "100")
        self.assertIn("pays 480 aura if right", said)
        self.assertIn("already put", p.place(ben, "2026-11-10", 1, 0, "100"))
        self.assertIn("most you can bet", p.place(kai, "2026-11-10", 1, 0, "9999"))
        p.place(kai, "2026-11-10", 0, 2, "50")
        self.assertIn("closed", p.place(luke, "2026-11-09", 0, 0, "10"), "yesterday's market")
        p.today = lambda: DAY.replace(hour=19)
        self.assertIn("closed", p.place(luke, "2026-11-10", 0, 0, "10"))

        p.today = lambda: DAY.replace(hour=23, minute=45)
        p.bot.settler = Writer({"results": [{"market": 0, "outcome": 2, "reason": "Wemby had 34"},
                                            {"market": 1, "outcome": -1, "reason": "no report"}]})
        asyncio.run(p.tick())
        data = p.load(1, "2026-11-10")
        self.assertEqual(data["status"], "settled")
        self.assertEqual(store.aura_total(1, 6), 500 - 50 + int(50 * data["markets"][0]["pays"][2]), "kai had 30+")
        self.assertEqual(store.aura_total(1, 5), 500, "rain unknown, so ben's bet came back")
        self.assertIn(data["markets"][2]["answer"], range(len(data["markets"][2]["outcomes"])))
        self.assertEqual(len(channel.edits), 3, "every market post shows the result and loses its buttons")
        self.assertIsNone(channel.edits[0][1]["view"])
        self.assertIn("Wemby had 34", channel.edits[0][1]["embed"].description)
        self.assertIn("Today's results", channel.sent[-1].title)

    def test_tap_an_outcome_then_type_the_bet(self):
        store = temp_store()
        p, channel = predictor(store, Writer(WORLD))
        ben = bettors(channel)[0]
        asyncio.run(p.tick())
        button = channel.views[1].children[1]
        modals, replies, edits = [], [], []

        class Response:
            async def send_modal(self, modal):
                modals.append(modal)

            async def send_message(self, text=None, **kwargs):
                replies.append(text)

        class Message:
            async def edit(self, **kwargs):
                edits.append(kwargs)

        message = Message()
        tap = SimpleNamespace(user=ben, response=Response(), message=message, guild=channel.guild,
                              client=SimpleNamespace(predictions=p))
        asyncio.run(button.callback(tap))
        modal = modals[0]
        self.assertIn("B. No", modal.title)
        modal.amount._value = "half"
        asyncio.run(modal.on_submit(tap))
        self.assertIn("put 250 aura on **No**", replies[0])
        self.assertIn("1 bet", edits[0]["embed"].description, "the post updates")
        self.assertEqual(store.aura_total(1, 5), 250)

        restored = asyncio.run(predict.OutcomeButton.from_custom_id(
            tap, SimpleNamespace(label="A. Yes"), predict.re.match(predict.OutcomeButton.__discord_ui_compiled_template__,
                                                                 "predict:2026-11-10:1:0")))
        self.assertEqual((restored.date, restored.market_id, restored.outcome), ("2026-11-10", 1, 0), "works after a restart")

    def test_server_markets_settle_without_ai(self):
        store = temp_store()
        p, channel = predictor(store)
        for back in range(1, 15):
            store.add_aura(1, 8, "luke", -[100, 300, 600][back % 3], "slots", "bet", now=START - back * 86400 + 60)
        asyncio.run(p.tick())
        data = p.load(1, "2026-11-10")
        casino = next(m for m in data["markets"] if m["kind"] == "casino")
        ben = bettors(channel)[0]
        p.place(ben, "2026-11-10", casino["id"], 2, "100")
        store.add_aura(1, 8, "luke", -700, "slots", "bet", now=START + 60)
        p.today = lambda: DAY.replace(hour=23, minute=45)
        asyncio.run(p.tick())
        settled = next(m for m in p.load(1, "2026-11-10")["markets"] if m["kind"] == "casino")
        self.assertEqual(settled["answer"], 2)
        self.assertEqual(store.aura_total(1, 5), 400 + int(100 * settled["pays"][2]))

    def test_settles_yesterday_after_being_offline(self):
        p, _ = predictor(temp_store())
        p.save(1, "2026-11-09", {"status": "open", "markets": []})
        p.today = lambda: DAY.replace(hour=8)
        asyncio.run(p.tick())
        self.assertEqual(p.load(1, "2026-11-09")["status"], "settled")
        self.assertIsNone(p.load(1, "2026-11-10"), "not open yet")


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
