"""Tests for /daily, aura for being nice to the bot, and the duel games."""
import asyncio
import random
import time
import unittest
import unittest.mock
from types import SimpleNamespace

import aura
import casino
import duels
from test_features import temp_store

GUILD = SimpleNamespace(id=1)


def person(id, name):
    return SimpleNamespace(id=id, name=name, display_name=name, bot=False, guild=GUILD,
                           display_avatar=SimpleNamespace(url="https://example.com/a.png"))


class FakeResponse:
    def __init__(self):
        self.edits, self.sent, self.deferred = [], [], False

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_message(self, text=None, **kwargs):
        self.sent.append(text)

    async def defer(self):
        self.deferred = True


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


def press(user, message=None, client=None):
    return SimpleNamespace(user=user, response=FakeResponse(), message=message or FakeMessage(), channel_id=7, client=client)


def last_title(embed):
    return embed.description.splitlines()[0]


class DailyTests(unittest.TestCase):
    def test_streak_grows_caps_and_resets(self):
        store, ben = temp_store(), person(5, "ben")
        day = 86400
        self.assertEqual(casino.claim_daily(store, ben, now=1000)[:2], (100, 1))
        self.assertEqual(casino.claim_daily(store, ben, now=1000 + day / 2)[0], 0, "once a day")
        self.assertEqual(casino.claim_daily(store, ben, now=1000 + day)[:2], (120, 2))
        for n in range(3, 9):
            casino.claim_daily(store, ben, now=1000 + day * (n - 1))
        self.assertEqual(casino.daily_amount(20), casino.DAILY_MAX)
        self.assertEqual(casino.claim_daily(store, ben, now=1000 + day * 20)[:2], (100, 1), "missed days reset the streak")
        self.assertEqual(store.aura_history(1, 5)[0]["kind"], "daily")


class NiceTests(unittest.TestCase):
    def test_spots_nice_messages_but_not_sarcasm(self):
        for text in ["thanks", "ty bro", "thank u fr", "ur funny", "you're the best", "good bot", "w bot", "ily",
                     "ur actually goated", "appreciate it", "you're right", "lowk ur smart"]:
            self.assertTrue(aura.is_nice(text), text)
        for text in ["thanks for nothing", "no thanks", "ur not funny", "thanks but ur still dumb", "stfu thanks",
                     "what time is it", "ur mid", "clanker"]:
            self.assertFalse(aura.is_nice(text), text)

    def test_roll_points_and_cooldown(self):
        store, ben = temp_store(), person(5, "ben")
        self.assertEqual(aura.nice_aura(store, ben, "what is 2+2", now=0, roll=lambda: 0), 0, "not nice")
        points = aura.nice_aura(store, ben, "thanks", now=100, roll=lambda: 0.5)
        self.assertTrue(aura.NICE_POINTS[0] <= points <= aura.NICE_POINTS[1])
        self.assertEqual(aura.nice_aura(store, ben, "ty", now=200, roll=lambda: 0), 0, "cooldown")
        later = 100 + aura.NICE_COOLDOWN_MINUTES * 60
        self.assertEqual(aura.nice_aura(store, ben, "ty", now=later, roll=lambda: 0.9), 0, "the roll missed (75%)")
        self.assertEqual(aura.nice_aura(store, ben, "ty", now=later + 10, roll=lambda: 0), 0, "a miss still starts the cooldown")
        self.assertEqual(store.aura_from_moments(1, 5), points, "counts as aura from chat")
        self.assertEqual(store.aura_history(1, 5)[0]["kind"], "nice")

    def test_chance_is_about_75_percent(self):
        hits = sum(aura.nice_aura(temp_store(), person(5, "ben"), "thanks", now=0, roll=random.Random(i).random) > 0
                   for i in range(400))
        self.assertTrue(250 < hits < 350, hits)

    def test_only_counts_messages_aimed_at_the_bot(self):
        import bot
        me, other = SimpleNamespace(id=99), SimpleNamespace(id=6)
        fake = SimpleNamespace(user=me)
        talking = lambda **kw: bot.SpamGuard.talking_to_bot(fake, SimpleNamespace(mentions=kw.get("mentions", []), reference=kw.get("ref")), kw.get("chat", False))
        reply_to = lambda author: SimpleNamespace(resolved=unittest.mock.Mock(spec=bot.discord.Message, author=author))
        self.assertTrue(talking(mentions=[me]))
        self.assertTrue(talking(ref=reply_to(me)))
        self.assertTrue(talking(chat=True))
        self.assertFalse(talking(chat=True, mentions=[other]), "thanking someone else in the chat channel")
        self.assertFalse(talking(chat=True, ref=reply_to(other)))
        self.assertFalse(talking(), "a normal channel without a mention")


class RepTests(unittest.TestCase):
    def test_rude_and_nice(self):
        import rep
        for text in ["stfu", "shut up bot", "clanker", "ur so dumb", "dumb bot", "i hate u", "nobody asked", "ur not funny"]:
            self.assertTrue(rep.is_rude(text), text)
        for text in ["thanks", "good bot", "what time is it", "this game is dumb"]:
            self.assertFalse(rep.is_rude(text), text)

    def test_score_moves_with_cooldown_and_limits(self):
        import rep
        store, ben = temp_store(), person(5, "ben")
        self.assertEqual(rep.react(store, ben, "thanks", now=0), 3)
        self.assertEqual(rep.react(store, ben, "ty", now=10), 0, "cooldown")
        self.assertEqual(rep.react(store, ben, "what's up", now=1000), 0, "neither")
        self.assertEqual(rep.react(store, ben, "thanks but ur dumb", now=1000), -5, "rude wins over nice")
        self.assertEqual(store.rep_score(1, 5), -2)
        for i in range(40):
            rep.react(store, ben, "stfu clanker", now=2000 + i * 200)
        self.assertEqual(store.rep_score(1, 5), -100, "never below -100")
        self.assertIn("can't stand", rep.opinion(store, ben))
        self.assertIn("stfu clanker", rep.opinion(store, ben), "remembers what bugged it")

    def test_levels_and_opinion(self):
        import rep
        store, ben = temp_store(), person(5, "ben")
        self.assertIsNone(rep.opinion(store, ben), "neutral says nothing")
        store.add_rep(1, 5, 70, "nice", "ur the goat", 0)
        self.assertIn("genuinely like", rep.opinion(store, ben))
        self.assertEqual(rep.level(0)[1], "😐 Neutral")
        self.assertEqual(rep.level(-20)[1], "😒 Annoyed")
        self.assertEqual(rep.level(25)[1], "🙂 Likes them")
        self.assertEqual(rep.bar(-100), "▱" * 10)
        self.assertEqual(rep.bar(100), "▰" * 10)
        store.add_rep(1, 6, -30, "rude", "stfu", 0)
        self.assertEqual(store.rep_ranking(1), [(5, 70), (6, -30)])

    def test_slang_fallback(self):
        import rep
        for text in ["ty twin", "fr twin", "ts tuff", "ur tuff", "u got mad aura", "+500 aura", "im crine", "ur him", "fw u heavy"]:
            self.assertTrue(aura.is_nice(text) and not rep.is_rude(text), text)
        for text in ["ur mid", "ur cooked", "ur chopped", "u got no aura", "-1000 aura", "i mog you", "stop larping",
                     "ur a bum", "who asked", "L bot"]:
            self.assertTrue(rep.is_rude(text), text)
        for text in ["this game is fire", "ben is cooked lol", "is ts mid or nah", "whats the score"]:
            self.assertFalse(aura.is_nice(text) or rep.is_rude(text), text)

    def test_ai_tone_tag(self):
        from judge import split_tone
        self.assertEqual(split_tone("lol ok twin\n[nice]"), ("lol ok twin", "nice"))
        self.assertEqual(split_tone("nah ur cooked [Rude]."), ("nah ur cooked", "rude"))
        self.assertEqual(split_tone("what"), ("what", None))
        self.assertEqual(split_tone("[neutral]"), (None, "neutral"))
        self.assertEqual(split_tone("[loud] ur aura just died"), ("ur aura just died", None), "made-up tags go too")

    def test_chat_reads_tone_in_the_same_call(self):
        from test_features import StubJudge
        judge = StubJudge("bet twin\n[nice]")
        text, tone = asyncio.run(judge.chat_and_tone("S", "p", [("ben", "ts tuff")], "ben", rate_tone=True))
        self.assertEqual((text, tone), ("bet twin", "nice"))
        self.assertEqual(len(judge.requests), 1, "no extra AI call")
        self.assertIn("[rude]", judge.requests[0][0])
        plain = StubJudge("bet twin\n[nice]")
        text, tone = asyncio.run(plain.chat_and_tone("S", "p", [("ben", "hi")], "ben"))
        self.assertEqual((text, tone), ("bet twin", None), "tag stripped even when it wasn't asked for")
        self.assertNotIn("[rude]", plain.requests[0][0])

    def test_slang_list_first_then_the_ai(self):
        import rep
        store, ben = temp_store(), person(5, "ben")
        self.assertEqual(rep.react(store, ben, "u lowk saved me there", now=0, tone="nice"), 3, "AI covers unknown wording")
        self.assertEqual(rep.react(store, ben, "thanks", now=1000, tone="rude"), 3, "the slang list wins when it knows")
        self.assertEqual(rep.react(store, ben, "whats up", now=2000, tone="neutral"), 0)
        self.assertGreater(aura.nice_aura(temp_store(), ben, "u lowk saved me there", now=0, roll=lambda: 0, tone="nice"), 0)
        self.assertEqual(aura.nice_aura(temp_store(), ben, "ok whatever", now=0, roll=lambda: 0, tone="neutral"), 0)
        self.assertEqual(aura.nice_aura(temp_store(), ben, "ur cooked", now=0, roll=lambda: 0, tone="nice"), 0,
                         "the AI can't call a known put-down nice")

    def test_chat_prompt_gets_the_opinion(self):
        from test_features import StubJudge
        judge = StubJudge("yo")
        asyncio.run(judge.chat("S", "p", [("ben", "hi")], "ben", opinion="How you feel about ben right now: cold."))
        self.assertIn("How you feel about ben", judge.requests[0][0])


class DuelLogicTests(unittest.TestCase):
    def test_split_or_steal(self):
        self.assertEqual(duels.split_steal_payouts("split", "split", 100)[:2], (125, 125))
        self.assertEqual(duels.split_steal_payouts("steal", "split", 100)[:2], (200, 0))
        self.assertEqual(duels.split_steal_payouts("split", "steal", 100)[:2], (0, 200))
        self.assertEqual(duels.split_steal_payouts("steal", "steal", 100)[:2], (0, 0))

    def test_ten_seconds_closest_wins(self):
        self.assertEqual(duels.ten_seconds_result({5: 9.7, 6: 10.4})[0], 5)
        self.assertEqual(duels.ten_seconds_result({5: 11.0, 6: 9.2})[0], 6)
        self.assertIsNone(duels.ten_seconds_result({5: 9.5, 6: 10.5})[0], "exact tie")


class DuelGameTests(unittest.TestCase):
    def setUp(self):
        self.store = temp_store()
        self.a, self.b, self.c = person(5, "ben"), person(6, "kai"), person(7, "luke")
        for p in (self.a, self.b):
            self.store.add_aura(1, p.id, p.name, 1000, "gift", "start")
            self.store.spend_aura(1, p.id, p.name, 100, "duel", "bet")  # what Accept does

    def totals(self):
        return self.store.aura_total(1, 5), self.store.aura_total(1, 6)

    def run_game(self, cls, steps, client=None):
        async def go():
            game = cls(self.a, self.b, 100, self.store)
            message = FakeMessage()
            await game.start(press(self.b, message=message, client=client))
            game.message = message
            return game, await steps(game)
        return asyncio.run(go())

    def test_outsiders_cant_play(self):
        async def steps(game):
            click = press(self.c)
            self.assertFalse(await game.interaction_check(click))
            game.done = True
            return click
        _, click = self.run_game(duels.SplitOrSteal, steps)
        self.assertIn("isn't yours", click.response.sent[0])

    def test_split_or_steal_secret_picks(self):
        async def steps(game):
            button = {item.label.lower(): item for item in game.children}
            first = press(self.a)
            await button["steal"].callback(first)
            self.assertIn("Locked in", first.response.sent[0])
            self.assertNotIn("steal", str(game.message.edits[-1]["embed"].description).lower().split("waiting")[1])
            again = press(self.a)
            await button["split"].callback(again)
            self.assertIn("locked in on steal", again.response.sent[0])
            await button["split"].callback(press(self.b))
            return game
        game, _ = self.run_game(duels.SplitOrSteal, steps)
        self.assertTrue(game.done)
        self.assertEqual(self.totals(), (1100, 900))

    def test_split_or_steal_no_pick_counts_as_split(self):
        async def steps(game):
            await {item.label.lower(): item for item in game.children}["split"].callback(press(self.b))
            await game.on_timeout()
            return game
        self.run_game(duels.SplitOrSteal, steps)
        self.assertEqual(self.totals(), (1025, 1025))

    def test_quick_draw_early_click_loses(self):
        async def steps(game):
            click = press(self.a)
            await game.trigger.callback(click)
            return click
        _, click = self.run_game(duels.QuickDraw, steps)
        self.assertIn("kai wins", last_title(click.response.edits[0]["embed"]))
        self.assertEqual(self.totals(), (900, 1100))

    def test_quick_draw_first_after_draw_wins(self):
        async def steps(game):
            await game.draw_later(0)
            first, second = press(self.b), press(self.a)
            if await game.interaction_check(first):
                await game.trigger.callback(first)
            self.assertFalse(await game.interaction_check(second), "too late")
            return first
        _, click = self.run_game(duels.QuickDraw, steps)
        self.assertIn("kai wins", last_title(click.response.edits[0]["embed"]))
        self.assertEqual(self.totals(), (900, 1100))

    def test_ten_seconds(self):
        async def steps(game):
            game.started_at = time.monotonic() - 9.8
            await game.stop_clock.callback(press(self.a))
            again = press(self.a)
            await game.stop_clock.callback(again)
            self.assertIn("already stopped", again.response.sent[0])
            game.started_at -= 1.0  # kai stops at about 10.8s
            last = press(self.b)
            await game.stop_clock.callback(last)
            return last
        _, click = self.run_game(duels.TenSeconds, steps)
        embed = click.response.edits[0]["embed"]
        self.assertIn("ben wins", last_title(embed))
        self.assertIn("9.8", embed.description)
        self.assertEqual(self.totals(), (1100, 900))

    def test_ten_seconds_timeouts(self):
        async def one(game):
            await game.stop_clock.callback(press(self.b))
            await game.on_timeout()
            return game
        game, _ = self.run_game(duels.TenSeconds, one)
        self.assertEqual(self.totals(), (900, 1100), "the only one who stopped wins")
        self.assertIn("never stopped", game.message.edits[-1]["embed"].description)

        self.setUp()
        async def nobody(game):
            await game.on_timeout()
            return game
        self.run_game(duels.TenSeconds, nobody)
        self.assertEqual(self.totals(), (1000, 1000), "both bets back")


if __name__ == "__main__":
    unittest.main()
