"""Tests for /daily, aura for being nice to the bot, and the duel games."""
import asyncio
import random
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


class DuelLogicTests(unittest.TestCase):
    def test_split_or_steal(self):
        self.assertEqual(duels.split_steal_payouts("split", "split", 100)[:2], (125, 125))
        self.assertEqual(duels.split_steal_payouts("steal", "split", 100)[:2], (200, 0))
        self.assertEqual(duels.split_steal_payouts("split", "steal", 100)[:2], (0, 200))
        self.assertEqual(duels.split_steal_payouts("steal", "steal", 100)[:2], (0, 0))

    def test_chicken_pot_adds_up(self):
        self.assertEqual(duels.chicken_payouts(101), (50, 152))

    def test_quote_round_needs_three_people(self):
        store = temp_store()
        a, b = person(5, "ben"), person(6, "kai")
        add = lambda author, name, text, msg: store.add_quote(
            guild_id=1, channel_id=2, message_id=msg, said_at=0.0, author_id=author, author_name=name, content=text,
            has_image=False, saved_by_id=9, saved_by_name="x")
        add(5, "ben", "fortnite is a sport", 1)
        self.assertIsNone(duels.quote_round(store, 1, (a, b)), "only two people to pick from")
        add(8, "luke", "grass is overrated", 2)
        quote, choices = duels.quote_round(store, 1, (a, b), random.Random(3))
        self.assertIn(quote["author_id"], [i for i, _ in choices])
        self.assertEqual(len(choices), 3)
        self.assertEqual(len({i for i, _ in choices}), 3)


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

    def test_chicken_bail_and_boom(self):
        async def steps(game):
            game.fuse = 999
            await game.bail.callback(press(self.a))
            return game
        game, _ = self.run_game(duels.Chicken, steps)
        self.assertEqual(self.totals(), (950, 1050), "bailer keeps half, the other gets the rest")

        self.setUp()
        async def boom(game):
            game.fuse = 0
            await game.burn()
            return game
        game, _ = self.run_game(duels.Chicken, boom)
        self.assertEqual(self.totals(), (900, 900), "both lose")
        self.assertIn("BOOM", game.message.edits[-1]["embed"].description)

    def test_who_said_it(self):
        add = lambda author, name, text, msg: self.store.add_quote(
            guild_id=1, channel_id=2, message_id=msg, said_at=0.0, author_id=author, author_name=name, content=text,
            has_image=False, saved_by_id=9, saved_by_name="x")
        add(8, "luke", "grass is overrated", 1)
        self.assertIsNone(duels.WhoSaidIt.available(self.store, 1, (self.a, self.b)))

        async def steps(game):
            wrong = [item for item in game.children if item.label != "luke"]
            right = next(item for item in game.children if item.label == "luke")
            await wrong[0].callback(press(self.a))
            locked = press(self.a)
            await right.callback(locked)
            self.assertIn("already guessed wrong", locked.response.sent[0])
            click = press(self.b)
            await right.callback(click)
            return click
        _, click = self.run_game(duels.WhoSaidIt, steps)
        self.assertIn("kai wins", last_title(click.response.edits[0]["embed"]))
        self.assertEqual(self.totals(), (900, 1100))

    def test_roast_battle(self):
        class Judge:
            def __init__(self, pick):
                self.pick, self.prompts = pick, []

            async def complete(self, system, prompt, schema, max_tokens):
                self.prompts.append(prompt)
                label = "A" if prompt.index(self.pick) < prompt.index("roast_B") else "B"
                return '{"winner": "%s", "reason": "more specific"}' % label

        def client(messages, judge):
            queue = list(messages)

            async def wait_for(event, timeout, check):
                self.assertIn((7, 5), duels.busy, "roasts skip moderation")
                while queue:
                    m = queue.pop(0)
                    if check(m):
                        return m
                raise asyncio.TimeoutError
            return SimpleNamespace(wait_for=wait_for, settler=judge, chatter=None)

        def said(who, text):
            async def react(emoji):
                pass
            return SimpleNamespace(channel=SimpleNamespace(id=7), author=who, content=text, add_reaction=react)

        judge = Judge("ur hairline")
        messages = [said(self.c, "outsider"), said(self.a, "ur hairline is in witness protection"), said(self.a, "second one"),
                    said(self.b, "ur mom")]
        game, _ = self.run_game(duels.RoastBattle, lambda g: asyncio.sleep(0, g), client(messages, judge))
        self.assertEqual(self.totals(), (1100, 900))
        self.assertNotIn("second one", judge.prompts[0], "only the first roast counts")
        self.assertIn("hairline", game.message.edits[-1]["embed"].description)
        self.assertEqual(duels.busy, set())

        self.setUp()
        game, _ = self.run_game(duels.RoastBattle, lambda g: asyncio.sleep(0, g), client([said(self.b, "ur mid")], judge))
        self.assertEqual(self.totals(), (900, 1100), "the only one who roasted wins")

        self.setUp()
        broken = SimpleNamespace(complete=lambda *a: asyncio.sleep(0, None))
        self.run_game(duels.RoastBattle, lambda g: asyncio.sleep(0, g),
                      client([said(self.a, "x"), said(self.b, "y")], broken))
        self.assertEqual(self.totals(), (1000, 1000), "no judge means both get their bet back")


if __name__ == "__main__":
    unittest.main()
