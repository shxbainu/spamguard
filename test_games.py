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
    def test_rps(self):
        self.assertEqual(duels.rps_winner("rock", "scissors"), 1)
        self.assertEqual(duels.rps_winner("rock", "paper"), -1)
        self.assertEqual(duels.rps_winner("paper", "paper"), 0)

    def test_math_problems_have_one_right_answer(self):
        for seed in range(200):
            question, answer, choices = duels.math_problem(random.Random(seed))
            self.assertEqual(eval(question.replace("×", "*").replace("−", "-")), answer, question)
            self.assertEqual(len(set(choices)), 4)
            self.assertIn(answer, choices)
            self.assertTrue(all(c > 0 for c in choices))

    def test_scramble_changes_the_word(self):
        for word in duels.WORDS:
            mixed = duels.scramble(word, random.Random(1))
            self.assertNotEqual(mixed.replace(" ", "").lower(), word)
            self.assertEqual(sorted(mixed.replace(" ", "").lower()), sorted(word))


class DuelGameTests(unittest.TestCase):
    def setUp(self):
        self.store = temp_store()
        self.a, self.b, self.c = person(5, "ben"), person(6, "kai"), person(7, "luke")
        for p in (self.a, self.b):
            self.store.add_aura(1, p.id, p.name, 1000, "gift", "start")
            self.store.spend_aura(1, p.id, p.name, 100, "duel", "bet")  # what Accept does

    def totals(self):
        return self.store.aura_total(1, 5), self.store.aura_total(1, 6)

    def run_game(self, cls, steps):
        async def go():
            game = cls(self.a, self.b, 100, self.store)
            await game.start(press(self.b))
            game.message = FakeMessage()
            return game, await steps(game)
        return asyncio.run(go())

    def test_quick_draw_early_click_loses(self):
        async def steps(game):
            click = press(self.a)
            await game.trigger.callback(click)
            return click
        game, click = self.run_game(duels.QuickDraw, steps)
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
        game, click = self.run_game(duels.QuickDraw, steps)
        self.assertIn("kai wins", last_title(click.response.edits[0]["embed"]))
        self.assertEqual(self.totals(), (900, 1100))

    def test_outsiders_cant_play(self):
        async def steps(game):
            click = press(self.c)
            self.assertFalse(await game.interaction_check(click))
            return click
        _, click = self.run_game(duels.QuickDraw, steps)
        self.assertIn("isn't yours", click.response.sent[0])

    def test_math_wrong_answer_loses(self):
        async def steps(game):
            wrong = next(item for item in game.children if item.label != str(game.answer))
            click = press(self.a)
            await wrong.callback(click)
            return click
        _, click = self.run_game(duels.MathRace, steps)
        self.assertIn("kai wins", last_title(click.response.edits[0]["embed"]))
        self.assertEqual(self.totals(), (900, 1100))

    def test_math_right_answer_wins(self):
        async def steps(game):
            right = next(item for item in game.children if item.label == str(game.answer))
            click = press(self.a)
            await right.callback(click)
            return click
        self.run_game(duels.MathRace, steps)
        self.assertEqual(self.totals(), (1100, 900))

    def test_rps_tie_then_win(self):
        async def steps(game):
            button = {item.label.lower(): item for item in game.children}
            await button["rock"].callback(press(self.a))
            await button["rock"].callback(press(self.a))  # already picked
            await button["rock"].callback(press(self.b))
            self.assertEqual(game.ties, 1)
            self.assertFalse(game.done)
            await button["paper"].callback(press(self.a))
            await button["rock"].callback(press(self.b))
            return game
        game, _ = self.run_game(duels.RockPaperScissors, steps)
        self.assertTrue(game.done)
        self.assertIn("ben wins", last_title(game.message.edits[-1]["embed"]))
        self.assertEqual(self.totals(), (1100, 900))

    def test_rps_no_show_loses_and_nobody_refunds(self):
        async def steps(game):
            button = {item.label.lower(): item for item in game.children}
            await button["paper"].callback(press(self.b))
            await game.on_timeout()
            return game
        self.run_game(duels.RockPaperScissors, steps)
        self.assertEqual(self.totals(), (900, 1100))

        self.setUp()
        async def nobody(game):
            await game.on_timeout()
            return game
        game, _ = self.run_game(duels.MathRace, nobody)
        self.assertEqual(self.totals(), (1000, 1000), "both bets back")
        self.assertIn("Nobody wins", last_title(game.message.edits[-1]["embed"]))

    def test_unscramble(self):
        guesses = []

        async def wait_for(event, timeout, check):
            word = duels.Unscramble.last.word
            self.assertIn((7, 5), duels.busy, "guesses skip the AI check while racing")
            wrong = SimpleNamespace(channel=SimpleNamespace(id=7), author=self.a, content="nope")
            outsider = SimpleNamespace(channel=SimpleNamespace(id=7), author=self.c, content=word)
            right = SimpleNamespace(channel=SimpleNamespace(id=7), author=self.b, content=f" {word.upper()} ",
                                    add_reaction=lambda e: guesses.append(e) or asyncio.sleep(0))
            self.assertFalse(check(wrong))
            self.assertFalse(check(outsider))
            self.assertTrue(check(right))
            return right

        original = duels.Unscramble.__init__

        def remember(game, *args):
            original(game, *args)
            duels.Unscramble.last = game

        async def go():
            with unittest.mock.patch.object(duels.Unscramble, "__init__", remember):
                game = duels.Unscramble(self.a, self.b, 100, self.store)
            message = FakeMessage()
            await game.start(press(self.b, message=message, client=SimpleNamespace(wait_for=wait_for)))
            return message

        message = asyncio.run(go())
        self.assertIn("kai wins", last_title(message.edits[-1]["embed"]))
        self.assertEqual(self.totals(), (900, 1100))
        self.assertEqual(duels.busy, set())
        self.assertEqual(guesses, ["✅"])


if __name__ == "__main__":
    unittest.main()
