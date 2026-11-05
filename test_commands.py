import unittest
from types import SimpleNamespace

from bot import shutup_blocker

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


if __name__ == "__main__":
    unittest.main()
