"""Tests for the quote book, aura, the grass check and debates."""
import asyncio
import json
import os
import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime
from types import SimpleNamespace

import discord

import aura
import debate
from grass import duration, report_time, wanted_sessions
from judge import DEBATE_PROMPT, Judge, SecondOpinion, Verdict, parse_verdict
from storage import Store


def temp_store() -> Store:
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    return Store(path)


class StubJudge(Judge):
    def __init__(self, *answers):
        self.model = "stub"
        self.answers = list(answers)
        self.requests = []

    async def send(self, system, turns, schema, max_tokens, images=()):
        self.requests.append((system, turns))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class Canned:
    def __init__(self, verdict):
        self.model, self.verdict, self.calls = "m", verdict, 0

    async def judge(self, *args):
        self.calls += 1
        return self.verdict


class StoreTests(unittest.TestCase):
    def test_quotes(self):
        store = temp_store()
        fields = dict(guild_id=1, channel_id=2, message_id=3, said_at=0.0, author_id=5, author_name="luke",
                      content="fortnite is a sport", has_image=False, saved_by_id=6, saved_by_name="shx")
        quote_id = store.add_quote(**fields)
        self.assertIsNotNone(quote_id)
        self.assertIsNone(store.add_quote(**fields), "same message twice")
        self.assertEqual(store.random_quote(1, author_id=5)["content"], "fortnite is a sport")
        self.assertEqual(store.random_quote(1, about="sport")["id"], quote_id)
        self.assertIsNone(store.random_quote(1, about="grass"))
        self.assertEqual(store.quotes_by(1, 5), ["fortnite is a sport"])
        store.delete_quote(1, quote_id)
        self.assertEqual(store.count_quotes(1), 0)

    def test_aura_totals_and_leaderboard(self):
        store = temp_store()
        store.add_aura(1, 5, "ben", -100, "false_claim", "said the moon is a hologram")
        store.add_aura(1, 6, "shx", 100, "comeback")
        store.add_aura(1, 6, "shx", 50, "callout")
        self.assertEqual(store.aura_total(1, 5), -100)
        self.assertEqual([row["user_name"] for row in store.aura_leaderboard(1)], ["shx", "ben"])
        self.assertEqual(store.aura_history(1, 6)[0]["kind"], "callout")
        self.assertGreater(store.last_aura(1, 6), 0)

    def test_presence_totals_are_clipped_to_the_window(self):
        store = temp_store()
        store.start_presence(9, "online", None, 0)
        session = store.start_presence(9, "game", "Fortnite", 100)
        store.touch_presence([session], 400)
        online = store.start_presence(9, "online", None, 500)
        store.touch_presence([online], 1000)
        totals = store.presence_totals(9, since=200, until=800)
        self.assertEqual(totals["games"], {"Fortnite": 200})
        self.assertEqual(totals["online"], 300)

    def test_state(self):
        store = temp_store()
        self.assertIsNone(store.get_state("x"))
        store.set_state("x", "1")
        store.set_state("x", "2")
        self.assertEqual(store.get_state("x"), "2")


class AuraJudgingTests(unittest.TestCase):
    def test_verdict_reads_aura(self):
        verdict = parse_verdict(json.dumps({
            "verdict": "ok", "reason": "fine", "confident": True, "aura": "callout",
            "aura_target": "Benny (@empireben)", "aura_reason": "the earth isn't flat",
        }))
        self.assertEqual((verdict.aura, verdict.aura_target), ("callout", "Benny (@empireben)"))
        old_style = parse_verdict(json.dumps({"verdict": "ok", "reason": "fine", "confident": True}))
        self.assertEqual(old_style.aura, "none")
        made_up = parse_verdict(json.dumps({"verdict": "ok", "reason": "x", "confident": True, "aura": "rizz"}))
        self.assertEqual(made_up.aura, "none")

    def test_aura_needs_the_second_model_to_agree(self):
        first = Canned(Verdict("ok", "fine", aura="false_claim", aura_reason="wrong"))
        second = Canned(Verdict("ok", "fine"))
        verdict = asyncio.run(SecondOpinion(first, second).judge("amy", [], "msg", "p"))
        self.assertEqual((verdict.aura, second.calls), ("none", 1))

        first = Canned(Verdict("ok", "fine", aura="comeback"))
        verdict = asyncio.run(SecondOpinion(first, Canned(None)).judge("amy", [], "msg", "p"))
        self.assertEqual(verdict.aura, "none", "no aura when the second model can't be reached")


def member(id, name, display=None, bot=False):
    return SimpleNamespace(id=id, name=name, display_name=display or name, bot=bot)


class AuraAwardTests(unittest.TestCase):
    def setUp(self):
        aura.AURA_ENABLED = True
        self.store = temp_store()
        self.replies = []
        self.guild = SimpleNamespace(id=1, me=SimpleNamespace(display_name="S"))
        self.ben = member(5, "empireben", "Benny")
        self.author = member(6, "shxbainu")

    def message(self, reference=None):
        async def reply(text, **kwargs):
            self.replies.append(text)
        return SimpleNamespace(guild=self.guild, author=self.author, clean_content="the earth is round",
                               reference=reference, channel=SimpleNamespace(id=7), reply=reply)

    def bot(self, talker=None, cached=()):
        return SimpleNamespace(store=self.store, chatter=talker, chat_budget=SimpleNamespace(take=lambda now: True),
                               cached_messages=list(cached))

    def test_callout_gives_and_takes_and_replies_with_points(self):
        talker = StubJudge("too smart??")
        earlier = SimpleNamespace(author=self.ben, channel=SimpleNamespace(id=7))
        verdict = Verdict("ok", "fine", aura="callout", aura_target="Benny", aura_reason="earth isn't flat")
        with unittest.mock.patch.object(discord, "Member", SimpleNamespace):
            done = asyncio.run(aura.award(self.bot(talker, [earlier]), self.message(), verdict, "persona"))
        self.assertEqual(self.store.aura_total(1, 6), 50)
        self.assertEqual(self.store.aura_total(1, 5), -50)
        self.assertEqual(self.replies, ["too smart?? +50 aura (Benny -50 aura)"])
        self.assertIn("callout", done)
        self.assertIn("2 to 6 words", talker.requests[0][1][0][1])

    def test_false_claim_falls_back_without_ai_and_has_a_cooldown(self):
        verdict = Verdict("ok", "fine", aura="false_claim", aura_reason="said 2+2=5")
        asyncio.run(aura.award(self.bot(), self.message(), verdict, "persona"))
        self.assertTrue(self.replies[0].endswith("-100 aura"))
        asyncio.run(aura.award(self.bot(), self.message(), verdict, "persona"))
        self.assertEqual(len(self.replies), 1, "second one inside the cooldown is ignored")
        self.assertEqual(self.store.aura_total(1, 6), -100)

    def test_find_member(self):
        people = [self.ben, self.author]
        for name in ("Benny", "empireben", "@empireben", "Benny (@empireben)"):
            self.assertIs(aura.find_member(name, people), self.ben)
        self.assertIsNone(aura.find_member("luke", people))


class GrassTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(duration(59), "0m")
        self.assertEqual(duration(3 * 3600 + 40 * 60), "3h 40m")

    def test_wanted_sessions(self):
        game = SimpleNamespace(type=discord.ActivityType.playing, name="Fortnite")
        song = SimpleNamespace(type=discord.ActivityType.listening, name="Spotify")
        online = SimpleNamespace(status=discord.Status.idle, activities=[game, song])
        self.assertEqual(wanted_sessions(online), {("online", None), ("game", "Fortnite")})
        offline = SimpleNamespace(status=discord.Status.offline, activities=[])
        self.assertEqual(wanted_sessions(offline), set())

    def test_report_time(self):
        self.assertEqual(report_time(datetime(2026, 11, 7, 8, 30)).strftime("%H:%M"), "21:00")


class DebateTests(unittest.TestCase):
    def test_speaking_order_alternates_and_rotates(self):
        sides = {"for": ["a", "b"], "against": ["c"]}
        order = debate.speaking_order(sides, 3)
        self.assertEqual(order, [
            ("Opening", "for", "a"), ("Opening", "against", "c"),
            ("Rebuttal", "for", "b"), ("Rebuttal", "against", "c"),
            ("Closing", "for", "a"), ("Closing", "against", "c"),
        ])
        self.assertEqual(len(debate.speaking_order(sides, 1)), 2)

    def test_tally(self):
        self.assertEqual(debate.tally({1: "for", 2: "for", 3: "against"}), {"for": 2, "against": 1})

    def test_adjudicate_is_neutral_and_reply_keeps_the_voice(self):
        answer = json.dumps({
            "winner": "against", "verdict": "Against wins 😭 because they answered every point.",
            "reasoning": "For never answered the cost argument.", "best_speaker": "Kim Jong-Un",
            "reply": "Against cooked them.",
        })
        judge = StubJudge(answer)
        ruling = asyncio.run(judge.adjudicate("ban homework", [("Opening", "For", "luke", "homework bad")], "persona"))
        self.assertEqual(ruling.winner, "against")
        self.assertNotIn("😭", ruling.verdict)
        self.assertEqual(ruling.best_speaker, "Kim John-Un")
        self.assertEqual(ruling.reply, "against cooked them")
        system, turns = judge.requests[0]
        self.assertIn("impartial adjudicator", system)
        self.assertIn('<speech round="Opening" side="For" speaker="luke">', turns[0][1])

    def test_bad_ruling_gives_none(self):
        self.assertIsNone(asyncio.run(StubJudge("nope").adjudicate("m", [], "p")))

    def test_prompt_judges_the_debate_not_the_motion(self):
        self.assertIn("Your own view of the motion doesn't matter", DEBATE_PROMPT)


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
