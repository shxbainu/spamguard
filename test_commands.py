import asyncio
import unittest
from types import SimpleNamespace

import discord

from bot import CHAT_COOLDOWN_SECONDS, STATUSES, SpamGuard, current_status, make_status, shutup_blocker, speaker
from detectors import PerMinuteBudget, StrikeTracker
from judge import Verdict

# Role positions stand in for discord.Role, which compares by position the same way.
GUILD = SimpleNamespace(owner_id=1, me=SimpleNamespace(top_role=10))


def member(id, top_role=1, bot=False, admin=False, timed_out=False):
    return SimpleNamespace(
        id=id, bot=bot, guild=GUILD, top_role=top_role, mention=f"<@{id}>",
        guild_permissions=SimpleNamespace(administrator=admin), is_timed_out=lambda: timed_out,
    )


class ShutupTests(unittest.TestCase):
    def test_normal_member_can_be_shut_up(self):
        self.assertIsNone(shutup_blocker(member(5), member(6)))

    def test_who_cannot_be_shut_up(self):
        author = member(5)
        cases = {
            "yourself": author,
            "Bots": member(6, bot=True),
            "admin": member(1),  # server owner
            "above mine": member(6, top_role=10),
            "already": member(6, timed_out=True),
        }
        for expected, target in cases.items():
            self.assertIn(expected, shutup_blocker(author, target))
        self.assertIn("admin", shutup_blocker(author, member(6, admin=True)))


BOT_USER = SimpleNamespace(id=99)


def fake_bot():
    return SimpleNamespace(
        chatter=object(), user=BOT_USER, last_reply={}, chat_budget=PerMinuteBudget(100),
        remaining_penalty=lambda guild_id, user_id: 0,
    )


def chat_message(author_id=5, mentions=()):
    return SimpleNamespace(author=SimpleNamespace(id=author_id), guild=SimpleNamespace(id=1), mentions=list(mentions), reference=None)


class ChatReplyTests(unittest.TestCase):
    def wants(self, bot, message, in_chat_channel=False):
        return SpamGuard.wants_reply(bot, message, in_chat_channel)

    def test_replies_in_chat_channel_and_to_mentions_only(self):
        self.assertTrue(self.wants(fake_bot(), chat_message(), in_chat_channel=True))
        self.assertTrue(self.wants(fake_bot(), chat_message(mentions=[BOT_USER])))
        self.assertFalse(self.wants(fake_bot(), chat_message()), "ordinary message elsewhere")

    def test_per_user_cooldown(self):
        bot = fake_bot()
        self.assertTrue(self.wants(bot, chat_message(5), in_chat_channel=True))
        self.assertFalse(self.wants(bot, chat_message(5), in_chat_channel=True), "same person right away")
        self.assertTrue(self.wants(bot, chat_message(6), in_chat_channel=True), "someone else is fine")
        bot.last_reply[5] -= CHAT_COOLDOWN_SECONDS
        self.assertTrue(self.wants(bot, chat_message(5), in_chat_channel=True), "after the cooldown")

    def test_no_reply_when_chat_off_or_punished(self):
        bot = fake_bot()
        bot.chatter = None
        self.assertFalse(self.wants(bot, chat_message(), in_chat_channel=True))
        bot = fake_bot()
        bot.remaining_penalty = lambda guild_id, user_id: 10
        self.assertFalse(self.wants(bot, chat_message(), in_chat_channel=True))



class SpeakerTests(unittest.TestCase):
    def test_speaker_shows_username_when_nickname_differs(self):
        self.assertEqual(speaker(SimpleNamespace(display_name="Benny", name="empireben")), "Benny (@empireben)")
        self.assertEqual(speaker(SimpleNamespace(display_name="EmpireBen", name="empireben")), "empireben")



class InARowTests(unittest.TestCase):
    def run_messages(self, verdicts, logged=None):
        punished = []
        answers = iter(verdicts)

        async def judge(*args):
            return next(answers)

        async def punish(message, category, reason, evidence, refs):
            punished.append(reason)

        bot = SimpleNamespace(
            judge=SimpleNamespace(judge=judge), strikes=StrikeTracker(3, 600),
            remaining_penalty=lambda guild_id, user_id: 0, punish=punish,
            log_event=lambda guild, channel, kind, member, **fields: (logged if logged is not None else []).append(fields),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(id=5, display_name="ben", name="ben"), guild=SimpleNamespace(id=1), channel=None,
        )
        for _ in verdicts:
            asyncio.run(SpamGuard.check_contribution(bot, message, [], "msg"))
        return punished

    def test_normal_message_in_between_resets_the_count(self):
        flag, ok = Verdict("low_effort", "random"), Verdict("ok", "fine")
        self.assertEqual(self.run_messages([flag, flag, ok, flag, flag]), [], "never 3 in a row")
        self.assertEqual(len(self.run_messages([flag, flag, flag])), 1, "3 in a row is an offense")

    def test_each_check_is_logged_with_its_strike_count(self):
        flag, ok = Verdict("low_effort", "random"), Verdict("ok", "fine")
        logged = []
        self.run_messages([flag, flag, ok], logged)
        self.assertEqual([entry["detail"] for entry in logged],
                         ["strike 1 of 3 in a row", "strike 2 of 3 in a row", "cleared 2 strike(s) in a row"])
        self.assertEqual(logged[0]["verdict"], "low_effort")


class StatusTests(unittest.TestCase):
    def test_statuses_are_plain_custom_statuses(self):
        for index in range(len(STATUSES)):
            self.assertIsInstance(current_status(index), discord.CustomActivity)
        self.assertNotEqual(current_status(0).name, current_status(1).name)
        self.assertEqual(make_status("running from dictator kim jong-un").name, "running from dictator kim john-un")
        self.assertEqual(current_status(3, bought="ben owes me 500 aura").name, "ben owes me 500 aura")

    def test_default_statuses(self):
        self.assertIn("forcing luke to touch grass", STATUSES)
        self.assertIn("watching over the server for ShxbaInu", STATUSES)


class NicknameTests(unittest.TestCase):
    def test_always_john_un(self):
        from judge import casual, fix_nicknames
        for text in ("Kim Jong-Un", "kim jong un", "kim jongun", "jong-un", "kim jung un", "Kim Jong"):
            self.assertNotIn("jong", fix_nicknames(text).lower(), text)
            self.assertNotIn("jung", fix_nicknames(text).lower(), text)
        self.assertEqual(fix_nicknames("Kim Jong-Un is short"), "Kim John-Un is short")
        self.assertEqual(fix_nicknames("kim jong un again"), "kim john-un again")
        self.assertEqual(casual("sit down kim jong-un"), "sit down kim john-un")
        self.assertEqual(fix_nicknames("john said hi"), "john said hi")


if __name__ == "__main__":
    unittest.main()


class PurgeTests(unittest.TestCase):
    def test_filter(self):
        import bot
        msg = lambda author, text, pinned=False, is_bot=False: SimpleNamespace(
            author=SimpleNamespace(id=author, bot=is_bot), content=text, pinned=pinned)
        everything = bot.purge_filter(None, None, False)
        self.assertTrue(everything(msg(1, "hi")))
        self.assertFalse(everything(msg(1, "rules", pinned=True)), "pinned messages stay")
        ben = SimpleNamespace(id=5)
        self.assertTrue(bot.purge_filter(ben, None, False)(msg(5, "spam")))
        self.assertFalse(bot.purge_filter(ben, None, False)(msg(6, "spam")))
        self.assertTrue(bot.purge_filter(None, "GIF", False)(msg(1, "look at this gif")))
        self.assertFalse(bot.purge_filter(None, "gif", False)(msg(1, "hello")))
        self.assertTrue(bot.purge_filter(None, None, True)(msg(1, "beep", is_bot=True)))
        self.assertFalse(bot.purge_filter(None, None, True)(msg(1, "human")))

    def test_command(self):
        import asyncio
        import bot
        sent, purged = [], {}

        class Channel:
            name = "general"

            def permissions_for(self, who):
                return SimpleNamespace(manage_messages=True)

            async def purge(self, limit, check, reason):
                purged.update(limit=limit, reason=reason)
                return [object()] * 3

        async def defer(**kwargs):
            pass

        async def followup(text, **kwargs):
            sent.append(text)
        events = []
        interaction = SimpleNamespace(
            channel=Channel(), guild=SimpleNamespace(me=object()), user="mod#1",
            permissions=SimpleNamespace(manage_messages=True),
            response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=followup),
            client=SimpleNamespace(log_event=lambda *a, **k: events.append(k)),
        )
        asyncio.run(bot.purge_cmd.callback(interaction, 50, None, "spam"))
        self.assertEqual(purged["limit"], 50)
        self.assertIn('Deleted **3** messages containing "spam"', sent[0])
        self.assertIn("deleted 3 of the last 50", events[0]["detail"])
