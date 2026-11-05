import unittest
from types import SimpleNamespace

from bot import CHAT_COOLDOWN_SECONDS, SpamGuard, shutup_blocker, speaker
from detectors import PerMinuteBudget

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


if __name__ == "__main__":
    unittest.main()
