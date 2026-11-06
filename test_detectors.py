import asyncio
import json
import unittest
from unittest import mock
from types import SimpleNamespace

from detectors import GifSpamDetector, MsgRef, PerMinuteBudget, RepeatDetector, StrikeTracker, looks_like_gif
from judge import ClaudeJudge, FallbackJudge, GeminiJudge, Judge, OpenAICompatibleJudge, make_judge, make_judges, parse_models


def ref(n: int) -> MsgRef:
    return MsgRef(channel_id=1, message_id=n)


class LooksLikeGifTests(unittest.TestCase):
    def test_tenor_and_giphy_links(self):
        self.assertTrue(looks_like_gif("https://tenor.com/view/cat-dance-gif-123"))
        self.assertTrue(looks_like_gif("lol https://media.giphy.com/media/abc/giphy.gif"))
        self.assertTrue(looks_like_gif("https://example.com/funny.gif?width=200"))

    def test_uploads_embeds_and_stickers(self):
        self.assertTrue(looks_like_gif("", attachments=[("dance.GIF", None)]))
        self.assertTrue(looks_like_gif("", attachments=[("file", "image/gif")]))
        self.assertTrue(looks_like_gif("", embeds=[("gifv", None)]))
        self.assertTrue(looks_like_gif("", sticker_count=1))

    def test_normal_messages(self):
        self.assertFalse(looks_like_gif("anyone watching the game tonight?"))
        self.assertFalse(looks_like_gif("", attachments=[("photo.png", "image/png")]))
        self.assertFalse(looks_like_gif("this is a gift for you"))


class GifSpamTests(unittest.TestCase):
    def test_fires_at_limit_inside_window(self):
        d = GifSpamDetector(limit=3, window=60)
        self.assertIsNone(d.add(7, ref(1), now=0))
        self.assertIsNone(d.add(7, ref(2), now=10))
        self.assertEqual(d.add(7, ref(3), now=20), [ref(1), ref(2), ref(3)])
        self.assertIsNone(d.add(7, ref(4), now=21), "window clears after firing")

    def test_old_gifs_expire(self):
        d = GifSpamDetector(limit=3, window=60)
        d.add(7, ref(1), now=0)
        d.add(7, ref(2), now=10)
        self.assertIsNone(d.add(7, ref(3), now=100))

    def test_users_are_separate(self):
        d = GifSpamDetector(limit=2, window=60)
        d.add(1, ref(1), now=0)
        self.assertIsNone(d.add(2, ref(2), now=1))


class RepeatTests(unittest.TestCase):
    def test_exact_and_near_repeats(self):
        d = RepeatDetector(limit=3, window=120)
        self.assertIsNone(d.add(7, "BRO LOOK AT THIS", ref(1), now=0))
        self.assertIsNone(d.add(7, "bro look at this!!", ref(2), now=5))
        self.assertEqual(d.add(7, "bro look at this!!!", ref(3), now=9), [ref(1), ref(2), ref(3)])

    def test_different_messages_do_not_fire(self):
        d = RepeatDetector(limit=3, window=120)
        for i, text in enumerate(["hey", "what's everyone up to", "i just got home"]):
            self.assertIsNone(d.add(7, text, ref(i), now=i))

    def test_repeats_outside_window(self):
        d = RepeatDetector(limit=3, window=120)
        d.add(7, "spam", ref(1), now=0)
        d.add(7, "spam", ref(2), now=10)
        self.assertIsNone(d.add(7, "spam", ref(3), now=200))

    def test_empty_text_ignored(self):
        d = RepeatDetector(limit=2, window=120)
        self.assertIsNone(d.add(7, "", ref(1), now=0))
        self.assertIsNone(d.add(7, "!!!", ref(2), now=1))


class StrikeTests(unittest.TestCase):
    def test_strikes_accumulate_and_expire(self):
        s = StrikeTracker(limit=3, window=600)
        s.add(7, "a", now=0)
        s.add(7, "b", now=100)
        self.assertEqual(s.count(7), 2)
        self.assertIsNone(s.add(7, "c", now=650), "first strike expired")
        self.assertEqual(s.add(7, "d", now=660), ["b", "c", "d"])
        self.assertEqual(s.count(7), 0)


class FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload = payload
        self.stop_reason = stop_reason
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=json.dumps(self.payload))],
        )


class FakeGemini:
    def __init__(self, text):
        self.text = text
        self.last_kwargs = None
        self.aio = SimpleNamespace(models=self)

    async def generate_content(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(text=self.text)


CONTEXT = [("amy", "who's playing tonight?")]


class JudgeTests(unittest.TestCase):
    def test_claude_parses_verdict_and_sends_context(self):
        messages = FakeMessages({"verdict": "low_effort", "reason": "keyboard mash"})
        judge = ClaudeJudge("claude-haiku-5-5", client=SimpleNamespace(messages=messages))
        verdict = asyncio.run(judge.judge("dave", CONTEXT, "asdkjhasd"))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "keyboard mash")
        prompt = messages.last_kwargs["messages"][0]["content"]
        self.assertIn("amy: who's playing tonight?", prompt)
        self.assertIn("asdkjhasd", prompt)

    def test_claude_refusal_gives_no_verdict(self):
        judge = ClaudeJudge("claude-haiku-5-5", client=SimpleNamespace(messages=FakeMessages({}, stop_reason="refusal")))
        self.assertIsNone(asyncio.run(judge.judge("dave", CONTEXT, "hi")))

    def test_gemini_parses_verdict_and_sends_context(self):
        fake = FakeGemini(json.dumps({"verdict": "disruptive", "reason": "baiting"}))
        judge = GeminiJudge("gemini-3.5-flash-lite", client=fake)
        verdict = asyncio.run(judge.judge("dave", CONTEXT, "ur all idiots lol"))
        self.assertEqual(verdict.verdict, "disruptive")
        self.assertIn("amy: who's playing tonight?", fake.last_kwargs["contents"][0]["parts"][0]["text"])
        self.assertEqual(fake.last_kwargs["config"].response_mime_type, "application/json")

    def test_moderation_knows_server_slang_and_running_jokes(self):
        fake = FakeGemini(json.dumps({"verdict": "ok", "reason": "running joke"}))
        persona = "calls YianniDestroys Kim John-Un"
        asyncio.run(GeminiJudge("gemini-3.5-flash-lite", client=fake).judge("EMPBEN", CONTEXT, "kim john un", persona))
        system = fake.last_kwargs["config"].system_instruction
        self.assertIn("calls YianniDestroys Kim John-Un", system)
        self.assertIn("mog: to completely outclass someone", system)
        self.assertIn("running jokes", system)

    def test_gemini_garbage_answer_gives_no_verdict(self):
        judge = GeminiJudge("gemini-3.5-flash-lite", client=FakeGemini("not json"))
        self.assertIsNone(asyncio.run(judge.judge("dave", CONTEXT, "hi")))

    def test_claude_settles_argument(self):
        ruling = {"topic": "Is a hot dog a sandwich?", "sides": [{"who": "amy", "position": "yes"}, {"who": "dave", "position": "no"}],
                  "winner": "amy", "verdict": "Technically yes.", "reasoning": "Bread around filling.", "facts": ["USDA says so"],
                  "reply": "Arr, amy wins this one."}
        messages = FakeMessages(ruling)
        judge = ClaudeJudge("claude-haiku-5-5", client=SimpleNamespace(messages=messages))
        transcript = [("amy", "hot dog is a sandwich"), ("dave", "no way")]
        result = asyncio.run(judge.settle(transcript, "amy", comment="tell dave he's wrong", persona="a pirate"))
        self.assertEqual((result.winner, result.reply), ("amy", "arr amy wins this one"))
        prompt = messages.last_kwargs["messages"][0]["content"]
        self.assertIn("dave: no way", prompt)
        self.assertIn("amy asked you", prompt)
        self.assertIn("tell dave he's wrong", prompt)
        self.assertIn("a pirate", messages.last_kwargs["system"])
        self.assertIn("Always pick a winner", messages.last_kwargs["system"])
        self.assertEqual(result.reply, "arr amy wins this one", "reply tidied like chat")

    def test_gemini_settle_with_bad_answer_gives_none(self):
        judge = GeminiJudge("gemini-3.5-flash-lite", client=FakeGemini(json.dumps({"verdict": "ok"})))
        self.assertIsNone(asyncio.run(judge.settle([("amy", "pineapple belongs on pizza")], "amy")))

    def test_gemini_chat_is_plain_text(self):
        fake = FakeGemini("lmao imagine")
        judge = GeminiJudge("gemini-3.5-flash-lite", client=fake)
        reply = asyncio.run(judge.chat("Spreader", "a pirate", CONTEXT, "amy"))
        self.assertEqual(reply, "lmao imagine")
        self.assertIsNone(fake.last_kwargs["config"].response_mime_type)
        self.assertIn("a pirate", fake.last_kwargs["config"].system_instruction)

    def test_chat_replaces_em_dashes(self):
        judge = GeminiJudge("gemini-3.5-flash-lite", client=FakeGemini("keep that energy high\u2014you're the best \u2013 fr"))
        self.assertEqual(asyncio.run(judge.chat("S", "x", CONTEXT, "amy")), "keep that energy high you're the best fr")

    def test_chat_sends_real_turns(self):
        fake = FakeGemini("fair")
        history = [("amy", "yianni is a dictator"), ("bob", "fr"), ("Spreader", "kim john-un strikes again"), ("amy", "deadass")]
        asyncio.run(GeminiJudge("gemini-3.5-flash-lite", client=fake).chat("Spreader", "x", history, "amy"))
        turns = [(c["role"], c["parts"][0]["text"]) for c in fake.last_kwargs["contents"]]
        self.assertEqual(turns, [
            ("user", "amy: yianni is a dictator\nbob: fr"),
            ("model", "kim john-un strikes again"),
            ("user", "amy: deadass"),
        ])

    def test_chat_drops_final_period_but_keeps_ellipsis(self):
        for raw, expected in (("yo.", "yo"), ("bro what...", "bro what..."), ("u.s. is mid", "u.s. is mid")):
            judge = GeminiJudge("gemini-3.5-flash-lite", client=FakeGemini(raw))
            self.assertEqual(asyncio.run(judge.chat("S", "x", CONTEXT, "amy")), expected)

    def test_chat_keeps_one_emoji_and_none_twice_in_a_row(self):
        judge = GeminiJudge("gemini-3.5-flash-lite", client=FakeGemini("nah \U0001F62D\U0001F62D you cooked \U0001F44D\U0001F3FD"))
        self.assertEqual(asyncio.run(judge.chat("S", "x", CONTEXT, "amy")), "nah \U0001F62D you cooked")
        after_emoji = CONTEXT + [("S", "lol \U0001F480"), ("amy", "ok")]
        self.assertEqual(asyncio.run(judge.chat("S", "x", after_emoji, "amy")), "nah you cooked")

    def test_chat_cleans_real_replies_from_discord(self):
        cases = (
            ("lol, just trying to keep the vibe interesting, you feelin\u2019 the drama?", "just trying to keep the interesting"),  # "vibe" cut when the rewrite still says it
            ("alright, i'll zip it. \U0001F644", "i'll zip it \U0001F644"),
            ("Yo Shxbainu, hope that \u201clast test\u201d wasn\u2019t another one of your \u201cI\u2019m basically a walking Wikipedia\u201d essays \U0001F602. Get some sleep before you turn into a meme",
             "yo shxbainu hope that last test wasn\u2019t another one of your i\u2019m basically a walking"),
            ("Sir Shxbainu, my man. Guess I\u2019ll have to bow down to the ultimate test-master. Good luck, don\u2019t let the questions bite",
             "sir shxbainu my man. guess i\u2019ll have to bow down to the ultimate test-master"),
            ("nah. why would i even care? LMAO you wild", "nah. lmao you wild"),
            ("why would you even say that?", "why would you even say that?"),  # only a question: kept
            ("that's crazy, who asked?", "that's crazy who asked?"),  # too short to be a tacked-on question
            ("bro, why?", "why?"),
            ("why would anyone care about that?", "why would anyone care about that?"),
        )
        for raw, expected in cases:
            judge = GeminiJudge("gemini-3.5-flash-lite", client=FakeGemini(raw))
            self.assertEqual(asyncio.run(judge.chat("S", "x", CONTEXT, "amy")), expected, raw)

    def test_chat_is_curt_without_names_openers_or_commands(self):
        history = [("tkum22", "why u slow"), ("Shxbalnu (@shxbainu)", "lol")]
        cases = (
            ("chill, tkum, just a bunch of bored kids pretending we\u2019re deep", "just a bunch of bored kids pretending we\u2019re deep"),
            ("because you\u2019re the one asking dumb questions, tkum. \U0001F644 get a clue or just mute us",
             "because you\u2019re the one asking dumb questions"),
            ("lol, sure, let me just pull a phd out of thin air for you. do it yourself, tkum \U0001F60F",
             "let me just pull a phd out of thin air for you \U0001F60F"),
            ("nice, shxbainu, if you lobotomize the bot you\u2019ll finally get some original content",
             "if you lobotomize the bot you\u2019ll finally get some original content"),
            ("ngl tkum has been asking the same three questions all night, at this point it is genuinely impressive how little changes",
             "ngl tkum has been asking the same three questions all night"),
            ("bro stfu. shut up you bum \U0001F62D", "bro stfu. shut up you bum \U0001F62D"),  # telling people off stays in
            ("syfm", "syfm"),
        )
        for raw, expected in cases:
            judge = GeminiJudge("gemini-3.5-flash-lite", client=FakeGemini(raw))
            self.assertEqual(asyncio.run(judge.chat("S", "x", history, "tkum22")), expected, raw)

    def test_chat_uses_only_persona_emojis_no_commas_and_lowk(self):
        persona = "edgy gen z chatter. use emojis like \U0001F62D\u270C\ufe0f\U0001F940\U0001FAE9."
        for raw, expected in (
            ("nah, you\u2019re basic \U0001F643", "nah you\u2019re basic"),
            ("\U0001F974", None),
            ("shut up, bum \u270C\ufe0f", "shut up bum \u270C\ufe0f"),
            ("yeah, lowkey respect", "yeah lowk respect"),
        ):
            judge = GeminiJudge("gemini-3.5-flash-lite", client=FakeGemini(raw))
            self.assertEqual(asyncio.run(judge.chat("S", persona, CONTEXT, "amy")), expected, raw)

    def test_chat_cuts_opening_echo_question(self):
        for raw, expected in (
            ("yianni? the self-proclaimed genius", "the self-proclaimed genius"),
            ("next level? more like next meme", "more like next meme"),
            ("what do you even mean by that?", "what do you even mean by that?"),
        ):
            judge = GeminiJudge("gemini-3.5-flash-lite", client=FakeGemini(raw))
            self.assertEqual(asyncio.run(judge.chat("S", "x", CONTEXT, "amy")), expected)

    def test_make_judge_rejects_unknown_provider(self):
        with self.assertRaises(ValueError):
            make_judge("openai")



class FakeCompletions:
    def __init__(self, content=None, error=None, fail_times=None):
        self.content = content
        self.error = error
        self.fail_times = fail_times  # None = always fail when error is set
        self.calls = 0
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.error and (self.fail_times is None or self.calls <= self.fail_times):
            raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


def rate_limit(retry_after):
    import httpx
    import openai

    response = httpx.Response(429, headers={"retry-after": retry_after}, request=httpx.Request("POST", "https://api.groq.com"))
    return openai.RateLimitError("Rate limit reached on tokens per minute (TPM)", response=response, body=None)


def groq_judge(completions, model="openai/gpt-oss-120b"):
    return OpenAICompatibleJudge("groq", model, client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))


class FakeGeminiSequence(FakeGemini):
    """Answers with each text in turn, recording every request."""

    def __init__(self, *texts):
        super().__init__(None)
        self.texts = list(texts)
        self.requests = []

    async def generate_content(self, **kwargs):
        self.requests.append(kwargs)
        self.last_kwargs = kwargs
        return SimpleNamespace(text=self.texts.pop(0) if len(self.texts) > 1 else self.texts[0])


class ChatRewriteTests(unittest.TestCase):
    def chat(self, fake, history=CONTEXT):
        return asyncio.run(GeminiJudge("gemini-3.5-flash-lite", client=fake).chat("S", "x", history, "amy"))

    def test_try_hard_word_gets_one_rewrite(self):
        fake = FakeGeminiSequence("ur vibe cooked bro", "ur so cooked")
        self.assertEqual(self.chat(fake), "ur so cooked")
        self.assertEqual(len(fake.requests), 2)
        self.assertIn("don't say vibe", fake.requests[1]["contents"][-1]["parts"][0]["text"])

    def test_slang_twice_in_a_row_gets_rewritten(self):
        history = CONTEXT + [("S", "yeah fine tuff"), ("amy", "why is ben a chud")]
        fake = FakeGeminiSequence("mid af bro", "cause he never shuts up")
        self.assertEqual(self.chat(fake, history), "cause he never shuts up")
        self.assertIn("plainly", fake.requests[1]["contents"][-1]["parts"][0]["text"])

    def test_fine_reply_is_sent_once(self):
        fake = FakeGeminiSequence("shut up you clown")
        self.assertEqual(self.chat(fake), "shut up you clown")
        self.assertEqual(len(fake.requests), 1)

    def test_bro_filler_trimmed(self):
        self.assertEqual(self.chat(FakeGeminiSequence("lol good luck bro \U0001F940")), "lol good luck \U0001F940")
        after_bro = CONTEXT + [("S", "nah bro"), ("amy", "ok")]
        self.assertEqual(self.chat(FakeGeminiSequence("bro stfu"), after_bro), "stfu")
        self.assertEqual(self.chat(FakeGeminiSequence("bro stfu")), "bro stfu", "fine when it hasn't said bro lately")


class ChatTurnTests(unittest.TestCase):
    def test_starts_and_ends_with_user(self):
        from judge import build_chat_turns
        self.assertEqual(build_chat_turns([("Spreader", "yo")], "Spreader"),
                         [("user", "(earlier messages not shown)"), ("assistant", "yo"), ("user", "(your turn)")])


class OpenAICompatibleTests(unittest.TestCase):
    def test_verdict_uses_json_schema(self):
        fake = FakeCompletions(json.dumps({"verdict": "low_effort", "reason": "mash"}))
        verdict = asyncio.run(groq_judge(fake).judge("dave", CONTEXT, "asdfgh"))
        self.assertEqual(verdict.verdict, "low_effort")
        self.assertEqual(fake.last_kwargs["response_format"]["type"], "json_schema")
        self.assertEqual(fake.last_kwargs["messages"][0]["role"], "system")
        self.assertIn("amy: who's playing tonight?", fake.last_kwargs["messages"][1]["content"])

    def test_chat_strips_thinking_and_skips_json(self):
        fake = FakeCompletions("<think>they want a roast</think>\nskill issue tbh")
        reply = asyncio.run(groq_judge(fake).chat("Spreader", "a gremlin", CONTEXT + [("dave", "roast me")], "dave"))
        self.assertEqual(reply, "skill issue tbh")
        self.assertNotIn("response_format", fake.last_kwargs)
        self.assertEqual(fake.last_kwargs["messages"][-1], {"role": "user", "content": "amy: who's playing tonight?\ndave: roast me"})

    def test_rate_limit_gives_none(self):
        fake = FakeCompletions(error=rate_limit(retry_after="60"))
        self.assertIsNone(asyncio.run(groq_judge(fake).chat("Spreader", "x", CONTEXT, "amy")))
        self.assertEqual(fake.calls, 1, "a long wait isn't retried")

    def test_short_rate_limit_is_retried_once(self):
        fake = FakeCompletions("ok fine", error=rate_limit(retry_after="0.01"), fail_times=1)
        self.assertEqual(asyncio.run(groq_judge(fake).chat("Spreader", "x", CONTEXT, "amy")), "ok fine")
        self.assertEqual(fake.calls, 2)

    def test_groq_reasoning_effort_and_small_chat_budget(self):
        fake = FakeCompletions("hi")
        asyncio.run(groq_judge(fake, "qwen/qwen3.8-27b").chat("Spreader", "x", CONTEXT, "amy"))
        self.assertEqual((fake.last_kwargs["reasoning_effort"], fake.last_kwargs["max_tokens"]), ("none", 768))
        asyncio.run(groq_judge(fake).judge("dave", CONTEXT, "hi"))
        self.assertEqual(fake.last_kwargs["reasoning_effort"], "low")
        openrouter = OpenAICompatibleJudge("openrouter", "google/gemma-4-31b-it:free",
                                           client=SimpleNamespace(chat=SimpleNamespace(completions=fake)))
        asyncio.run(openrouter.chat("Spreader", "x", CONTEXT, "amy"))
        self.assertNotIn("reasoning_effort", fake.last_kwargs, "only sent to Groq")


class StubJudge(Judge):  # canned answer
    def __init__(self, model, answer):
        self.model = model
        self.answer = answer
        self.calls = 0

    async def send(self, system, turns, schema, max_tokens):
        self.calls += 1
        return self.answer


class FallbackTests(unittest.TestCase):
    def test_parse_models(self):
        self.assertEqual(parse_models("", "groq"), [("groq", None)])
        self.assertEqual(
            parse_models("openai/gpt-oss-20b, gemini:gemini-3.5-flash-lite, openrouter:google/gemma-4-31b-it:free", "groq"),
            [("groq", "openai/gpt-oss-20b"), ("gemini", "gemini-3.5-flash-lite"), ("openrouter", "google/gemma-4-31b-it:free")],
        )
        self.assertEqual(parse_models("google/gemma-4-31b-it:free", "openrouter"), [("openrouter", "google/gemma-4-31b-it:free")])

    def test_uses_first_model_that_answers(self):
        busy, fast, spare = StubJudge("qwen", None), StubJudge("gpt-oss-20b", "yo"), StubJudge("gemini", "hi")
        judge = FallbackJudge([busy, fast, spare])
        self.assertEqual(asyncio.run(judge.chat("Spreader", "x", CONTEXT, "amy")), "yo")
        self.assertEqual((busy.calls, fast.calls, spare.calls), (1, 1, 0))
        self.assertEqual(judge.last_model, "gpt-oss-20b")
        self.assertIsNone(asyncio.run(FallbackJudge([busy, StubJudge("b", "  ")]).chat("S", "x", CONTEXT, "amy")))

    @mock.patch.dict("os.environ", {"GROQ_API_KEY": "test"})
    def test_make_judges_skips_providers_without_keys(self):
        judge = make_judges("claude:claude-haiku-5-5, groq:openai/gpt-oss-20b", "groq", available=lambda p: p == "groq")
        self.assertEqual(judge.model, "openai/gpt-oss-20b")
        self.assertIsNone(make_judges("claude:claude-haiku-5-5", "groq", available=lambda p: False))
        both = make_judges("openai/gpt-oss-20b, qwen/qwen3.8-27b", "groq")
        self.assertEqual(both.model, "openai/gpt-oss-20b → qwen/qwen3.8-27b")


class EffortTests(unittest.TestCase):
    def groq(self, model, effort):
        fake = FakeCompletions("ok")
        judge = OpenAICompatibleJudge("groq", model, client=SimpleNamespace(chat=SimpleNamespace(completions=fake)), effort=effort)
        asyncio.run(judge.chat("S", "x", CONTEXT, "amy"))
        return fake.last_kwargs

    def test_effort_raises_reasoning_and_room_to_think(self):
        sent = self.groq("openai/gpt-oss-120b", "medium")
        self.assertEqual((sent["reasoning_effort"], sent["max_tokens"]), ("medium", 1536))
        sent = self.groq("openai/gpt-oss-120b", None)
        self.assertEqual((sent["reasoning_effort"], sent["max_tokens"]), ("low", 768))
        self.assertNotIn("reasoning_effort", self.groq("llama-3.1-8b-instant", "high"), "models that don't reason")

    @mock.patch.dict("os.environ", {"GROQ_API_KEY": "test"})
    def test_fallback_list_carries_effort(self):
        judge = make_judges("openai/gpt-oss-120b, openai/gpt-oss-20b", "groq", effort="high")
        self.assertEqual([j.effort for j in judge.judges], ["high", "high"])
        self.assertEqual(judge.effort, "high")


class BudgetTests(unittest.TestCase):
    def test_per_minute_budget(self):
        budget = PerMinuteBudget(2)
        self.assertEqual([budget.take(0), budget.take(1), budget.take(2)], [True, True, False])
        self.assertTrue(budget.take(61), "first use expired")


if __name__ == "__main__":
    unittest.main()
