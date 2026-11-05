import asyncio
import json
import unittest
from types import SimpleNamespace

from detectors import GifSpamDetector, MsgRef, RepeatDetector, StrikeTracker, looks_like_gif
from judge import ClaudeJudge, GeminiJudge, make_judge


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
        self.assertIn("amy: who's playing tonight?", fake.last_kwargs["contents"])
        self.assertEqual(fake.last_kwargs["config"].response_mime_type, "application/json")

    def test_gemini_garbage_answer_gives_no_verdict(self):
        judge = GeminiJudge("gemini-3.5-flash-lite", client=FakeGemini("not json"))
        self.assertIsNone(asyncio.run(judge.judge("dave", CONTEXT, "hi")))

    def test_make_judge_rejects_unknown_provider(self):
        with self.assertRaises(ValueError):
            make_judge("openai")


if __name__ == "__main__":
    unittest.main()
