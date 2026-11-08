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
        self.assertEqual(verdict.aura_level, 2, "missing level counts as 2")
        leveled = parse_verdict(json.dumps({"verdict": "ok", "reason": "x", "confident": True, "aura": "comeback",
                                            "aura_target": "", "aura_reason": "cooked", "aura_level": 7}))
        self.assertEqual(leveled.aura_level, 5)
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
        verdict = Verdict("ok", "fine", aura="callout", aura_target="Benny", aura_reason="earth isn't flat", aura_level=2)
        with unittest.mock.patch.object(discord, "Member", SimpleNamespace):
            done = asyncio.run(aura.award(self.bot(talker, [earlier]), self.message(), verdict, "persona"))
        self.assertEqual(self.store.aura_total(1, 6), 50)
        self.assertEqual(self.store.aura_total(1, 5), -50)
        self.assertEqual(self.replies, ["too smart?? +50 aura (Benny -50 aura)"])
        self.assertIn("callout", done)
        self.assertIn("2 to 6 words", talker.requests[0][1][0][1])
        self.assertIn("(clear)", talker.requests[0][1][0][1], "the line knows how big it was")

    def test_points_scale_with_the_level(self):
        legendary = Verdict("ok", "fine", aura="comeback", aura_reason="ended him", aura_level=5)
        asyncio.run(aura.award(self.bot(), self.message(), legendary, "persona"))
        self.assertEqual(self.store.aura_total(1, 6), 500)
        self.assertTrue(self.replies[0].endswith("+500 aura"))
        self.assertTrue(self.store.aura_history(1, 6)[0]["reason"].startswith("legendary"))
        self.assertEqual([aura.points_for("false_claim", level) for level in range(1, 6)], [-25, -50, -100, -250, -500])
        self.assertEqual(aura.points_for("comeback", 9), 500, "out of range is clamped")

    def test_false_claim_falls_back_without_ai_and_has_a_cooldown(self):
        verdict = Verdict("ok", "fine", aura="false_claim", aura_reason="said 2+2=5", aura_level=3)
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
    def setUp(self):
        no_wikipedia(self)

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

    def test_adjudicate_is_formal(self):
        answer = json.dumps({
            "winner": "against", "verdict": "Against wins 😭 because they answered every point.",
            "reasoning": "For never answered the cost argument.", "best_speaker": "Kim Jong-Un",
            "facts": ["Homework is assigned in most schools."],
        })
        judge = StubJudge(answer)
        ruling = asyncio.run(judge.adjudicate("ban homework", [("Opening", "For", "luke", "homework bad")], "persona"))
        self.assertEqual(ruling.winner, "against")
        self.assertNotIn("😭", ruling.verdict)
        self.assertEqual(ruling.best_speaker, "Kim John-Un")
        self.assertEqual(ruling.facts, ["Homework is assigned in most schools."])
        self.assertEqual(ruling.sources, [], "no search, no sources")
        system, turns = judge.requests[0]
        self.assertIn("impartial adjudicator", system)
        self.assertIn("formal", system)
        self.assertNotIn("persona", system)
        self.assertIn('<speech round="Opening" side="For" speaker="luke">', turns[0][1])

    def test_bad_ruling_gives_none(self):
        self.assertIsNone(asyncio.run(StubJudge("nope").adjudicate("m", [], "p")))

    def test_prompt_judges_the_debate_not_the_motion(self):
        self.assertIn("Your own view of the motion doesn't matter", DEBATE_PROMPT)


class SearchingJudge(StubJudge):
    can_search = True

    def __init__(self, searched, plain=None):
        super().__init__(plain or "{}")
        self.searched = searched

    async def search(self, system, prompt, max_tokens):
        return self.searched, [("NASA", "https://nasa.gov/wall")]


FACT = {"rating": "false", "claim": "The Great Wall is visible from space.", "summary": "It is not visible to the naked eye.",
        "explanation": "Astronauts report it can't be seen 😭.", "correction": "It is too narrow to see from orbit."}


ARTICLES = [
    {"title": "Great Wall of China", "url": "https://en.wikipedia.org/wiki/Great_Wall_of_China", "text": "A wall."},
    {"title": "Artificial structures visible from space", "url": "https://en.wikipedia.org/wiki/Artificial_structures_visible_from_space", "text": "Not the wall."},
    {"title": "Factoid", "url": "https://en.wikipedia.org/wiki/Factoid", "text": "Unrelated."},
]


def no_wikipedia(test_case, articles=()):
    """Stops tests from reaching Wikipedia; `articles` is what a lookup returns."""
    async def lookup(query, limit=3):
        test_case.queries.append(query)
        return list(articles)
    test_case.queries = []
    patcher = unittest.mock.patch("wiki.lookup", lookup)
    patcher.start()
    test_case.addCleanup(patcher.stop)


class FactCheckTests(unittest.TestCase):
    def setUp(self):
        no_wikipedia(self)

    def test_wikipedia_sources_are_the_ones_the_ai_used(self):
        no_wikipedia(self, ARTICLES)
        judge = StubJudge(json.dumps(dict(FACT, sources_used=[2, 2, 9, "x"])))
        check = asyncio.run(judge.factcheck("great wall visible from space"))
        self.assertEqual(check.sources, [("Artificial structures visible from space", ARTICLES[1]["url"])])
        self.assertEqual(self.queries, ["great wall visible from space"])
        self.assertIn('<source n="2" title="Artificial structures visible from space">', judge.requests[0][1][0][1])

    def test_failed_search_falls_back_to_wikipedia(self):
        no_wikipedia(self, ARTICLES)
        judge = SearchingJudge(None, plain=json.dumps(dict(FACT, sources_used=[1])))
        check = asyncio.run(judge.factcheck("great wall"))
        self.assertEqual(check.sources, [("Great Wall of China", ARTICLES[0]["url"])])

    def test_search_answer_keeps_its_sources_even_with_prose_around_the_json(self):
        judge = SearchingJudge("Here is my answer:\n```json\n" + json.dumps(FACT) + "\n```")
        check = asyncio.run(judge.factcheck("great wall visible from space", [("luke", "trust")], "luke"))
        self.assertEqual(check.rating, "false")
        self.assertEqual(check.sources, [("NASA", "https://nasa.gov/wall")])
        self.assertNotIn("😭", check.explanation)
        self.assertEqual(judge.requests, [], "no second call needed")

    def test_falls_back_to_plain_answer_without_sources(self):
        judge = SearchingJudge("I could not find anything", plain=json.dumps(dict(FACT, rating="Mostly True")))
        check = asyncio.run(judge.factcheck("claim"))
        self.assertEqual((check.rating, check.sources), ("mostly_true", []))

    def test_unknown_rating_becomes_unverifiable(self):
        check = asyncio.run(StubJudge(json.dumps(dict(FACT, rating="kinda"))).factcheck("claim"))
        self.assertEqual(check.rating, "unverifiable")

    def test_grounding_sources(self):
        from judge import grounding_sources
        web = lambda uri, title: SimpleNamespace(web=SimpleNamespace(uri=uri, title=title))
        response = SimpleNamespace(candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(
            grounding_chunks=[web("https://a", "A"), web("https://a", "A"), web("https://b", None)]))])
        self.assertEqual(grounding_sources(response), [("A", "https://a"), ("https://b", "https://b")])
        self.assertEqual(grounding_sources(SimpleNamespace(candidates=None)), [])


class CasinoTests(unittest.TestCase):
    def test_parse_bet(self):
        from casino import parse_bet
        self.assertEqual(parse_bet("all", 500), 500)
        self.assertEqual(parse_bet("half", 501), 250)
        self.assertEqual(parse_bet("1k", 5000), 1000)
        self.assertEqual(parse_bet("2.5k", 5000), 2500)
        self.assertEqual(parse_bet("1,200", 5000), 1200)
        self.assertIn("only have", parse_bet("600", 500))
        self.assertIn("broke", parse_bet("10", -100))
        self.assertIn("number", parse_bet("lots", 500))
        self.assertIn("at least", parse_bet("0", 500))

    def test_slots(self):
        from casino import slot_payout
        self.assertEqual(slot_payout(["🍑", "🍑", "🍑"], 100), (2500, "JACKPOT"))
        self.assertEqual(slot_payout(["💎", "💎", "💎"], 100)[0], 1000)
        self.assertEqual(slot_payout(["💎", "🍒", "💎"], 100)[0], 150)
        self.assertEqual(slot_payout(["💎", "🍒", "🍋"], 100)[0], 0)

    def test_roulette(self):
        from casino import roulette_payout
        self.assertEqual(roulette_payout("red", 1, 100), 200)
        self.assertEqual(roulette_payout("black", 1, 100), 0)
        self.assertEqual(roulette_payout("even", 0, 100), 0, "zero isn't even")
        self.assertEqual(roulette_payout("17", 17, 10), 360)
        self.assertEqual(roulette_payout("green", 0, 10), 360)
        self.assertIsNone(roulette_payout("purple", 3, 10))
        self.assertIsNone(roulette_payout("37", 3, 10))

    def test_blackjack(self):
        from casino import blackjack_payout, hand_value
        self.assertEqual(hand_value([("A", "♠"), ("K", "♥")]), 21)
        self.assertEqual(hand_value([("A", "♠"), ("A", "♥"), ("9", "♦")]), 21)
        self.assertEqual(hand_value([("10", "♠"), ("9", "♥"), ("5", "♦")]), 24)
        natural, nineteen = [("A", "♠"), ("K", "♥")], [("10", "♠"), ("9", "♥")]
        self.assertEqual(blackjack_payout(natural, nineteen, 100), (250, "blackjack!"))
        self.assertEqual(blackjack_payout(nineteen, nineteen, 100), (100, "push"))
        self.assertEqual(blackjack_payout(nineteen, [("10", "♠"), ("8", "♥")], 100)[0], 200)
        self.assertEqual(blackjack_payout([("10", "♠"), ("9", "♥"), ("5", "♦")], nineteen, 100)[0], 0)
        self.assertEqual(blackjack_payout(nineteen, [("10", "♠"), ("6", "♥"), ("9", "♦")], 100)[1], "dealer busts")

    def test_higher_lower_odds(self):
        from casino import higher_lower_odds
        self.assertEqual(higher_lower_odds(1, "higher"), 1)
        self.assertEqual(higher_lower_odds(13, "higher"), 0)
        self.assertAlmostEqual(higher_lower_odds(7, "lower"), 0.5)

    def test_spending_and_welcome(self):
        from casino import START_AURA, welcome
        store = temp_store()
        person = SimpleNamespace(id=5, display_name="ben", guild=SimpleNamespace(id=1))
        self.assertEqual(welcome(store, person), START_AURA)
        self.assertEqual(welcome(store, person), 0, "only once")
        self.assertFalse(store.spend_aura(1, 5, "ben", START_AURA + 1, "slots"))
        self.assertTrue(store.spend_aura(1, 5, "ben", 100, "slots"))
        store.add_aura(1, 5, "ben", 300, "slots", "win")
        self.assertEqual(store.game_net(1, 5), 200)
        self.assertEqual(store.aura_total(1, 5), START_AURA + 200)
        self.assertEqual([row["kind"] for row in store.aura_history(1, 5)], ["gift"], "games stay out of the history")
        self.assertEqual(store.last_aura(1, 5), 0, "casino doesn't trigger the message cooldown")

    def test_items(self):
        store = temp_store()
        self.assertFalse(store.use_item(1, 5, "shutup"))
        store.add_item(1, 5, "shutup")
        store.add_item(1, 5, "shutup")
        self.assertEqual(store.items(1, 5), {"shutup": 2})
        self.assertTrue(store.use_item(1, 5, "shutup"))
        self.assertEqual(store.items(1, 5), {"shutup": 1})


class WikiTests(unittest.TestCase):
    def test_keywords_and_evidence(self):
        import wiki
        self.assertEqual(wiki.keywords("could ja morant dunk over the great wall of china"), "ja morant dunk over great wall")
        self.assertEqual(wiki.evidence_block([]), "")
        self.assertIn("sources_used", wiki.evidence_block(ARTICLES))
        self.assertEqual(wiki.used_sources(ARTICLES, None), [])
        self.assertEqual(len(wiki.used_sources(ARTICLES, [1, 3])), 2)


class ShopTests(unittest.TestCase):
    def test_clean_text_drops_pings_and_links(self):
        from shop import clean_text
        self.assertEqual(clean_text("hi @everyone <@123> https://x.co/y  there", 60), "hi there")
        self.assertEqual(len(clean_text("a" * 100, 32)), 32)

    def test_shield_and_status_override(self):
        from shop import shield_until, status_override
        store = temp_store()
        self.assertEqual(shield_until(store, 1, 5), 0)
        store.set_state("shield:1:5", str(time.time() + 60))
        self.assertGreater(shield_until(store, 1, 5), time.time())
        self.assertIsNone(status_override(store))
        store.set_state("status_override", json.dumps({"text": "ben owes me money", "until": time.time() + 60}))
        self.assertEqual(status_override(store), "ben owes me money")
        store.set_state("status_override", json.dumps({"text": "old", "until": time.time() - 1}))
        self.assertIsNone(status_override(store))
        self.assertEqual(set(store.states("shield:")), {"shield:1:5"})


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
