"""SpamGuard: watches specific users for GIF spam, repeated messages and low-effort
noise, and removes them from the conversation for a few minutes."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from detectors import GifSpamDetector, MsgRef, RepeatDetector, StrikeTracker, looks_like_gif
from judge import make_judge

load_dotenv()


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_ids(name: str) -> set[int]:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x}


TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = env_int("GUILD_ID", 0)
TARGET_USER_IDS = env_ids("TARGET_USER_IDS")
WATCH_CHANNEL_IDS = env_ids("WATCH_CHANNEL_IDS")
LOG_CHANNEL_ID = env_int("LOG_CHANNEL_ID", 0)

ACTION = os.getenv("ACTION", "timeout").strip().lower()  # "timeout" or "kick"
PUNISH_MINUTES = env_int("PUNISH_MINUTES", 3)
DELETE_SPAM = env_bool("DELETE_SPAM", True)
ANNOUNCE = env_bool("ANNOUNCE", True)

GIF_LIMIT = env_int("GIF_LIMIT", 4)
GIF_WINDOW_SECONDS = env_int("GIF_WINDOW_SECONDS", 60)
REPEAT_LIMIT = env_int("REPEAT_LIMIT", 3)
REPEAT_WINDOW_SECONDS = env_int("REPEAT_WINDOW_SECONDS", 120)

AI_ENABLED = env_bool("AI_ENABLED", True)
AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").strip().lower()  # "gemini" (free tier) or "claude"
AI_MODEL = os.getenv("AI_MODEL", "").strip() or None  # empty = provider default
AI_STRIKES = env_int("AI_STRIKES", 3)
AI_STRIKE_WINDOW_SECONDS = env_int("AI_STRIKE_WINDOW_SECONDS", 600)
CONTEXT_MESSAGES = env_int("CONTEXT_MESSAGES", 15)

WATCHED_FILE = Path(__file__).with_name("watched.json")

log = logging.getLogger("spamguard")


class SpamGuard(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable it in the Developer Portal
        intents.members = True  # privileged: needed to restore roles when a kicked user rejoins
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

        self.watched: set[int] = self._load_watched()
        self.gifs = GifSpamDetector(GIF_LIMIT, GIF_WINDOW_SECONDS)
        self.repeats = RepeatDetector(REPEAT_LIMIT, REPEAT_WINDOW_SECONDS)
        self.strikes = StrikeTracker(AI_STRIKES, AI_STRIKE_WINDOW_SECONDS)
        self.judge = make_judge(AI_PROVIDER, AI_MODEL) if AI_ENABLED else None

        # Recent messages per channel, used as context for the AI judge.
        self.history: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=CONTEXT_MESSAGES))
        # (guild_id, user_id) -> monotonic time their penalty ends.
        self.penalized: dict[tuple[int, int], float] = {}
        # (guild_id, user_id) -> role ids to give back after a temporary kick.
        self.saved_roles: dict[tuple[int, int], list[int]] = {}
        self._tasks: set[asyncio.Task] = set()

    # ---- persistence -------------------------------------------------------

    def _load_watched(self) -> set[int]:
        watched = set(TARGET_USER_IDS)
        if WATCHED_FILE.exists():
            watched |= set(json.loads(WATCHED_FILE.read_text()))
        return watched

    def save_watched(self) -> None:
        WATCHED_FILE.write_text(json.dumps(sorted(self.watched)))

    # ---- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        self.tree.add_command(spamguard_commands)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)  # instant in one server
        else:
            await self.tree.sync()  # global; can take a while to show up

    async def on_ready(self) -> None:
        ai = f"{AI_PROVIDER} ({self.judge.model})" if self.judge else "off"
        log.info("Logged in as %s, watching %d user(s), action=%s, AI=%s", self.user, len(self.watched), ACTION, ai)

    # ---- detection ---------------------------------------------------------

    def remaining_penalty(self, guild_id: int, user_id: int) -> float:
        key = (guild_id, user_id)
        left = self.penalized.get(key, 0) - time.monotonic()
        if left <= 0:
            self.penalized.pop(key, None)
            return 0
        return left

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not isinstance(message.author, discord.Member):
            return
        if WATCH_CHANNEL_IDS and message.channel.id not in WATCH_CHANNEL_IDS:
            return

        text = message.clean_content
        is_gif = looks_like_gif(
            text,
            attachments=[(a.filename, a.content_type) for a in message.attachments],
            embeds=[(e.type, e.url) for e in message.embeds],
            sticker_count=len(message.stickers),
        )
        history = self.history[message.channel.id]
        context = list(history)
        history.append((message.author.display_name, "[GIF]" if is_gif else text or "[attachment]"))

        user_id = message.author.id
        if user_id not in self.watched or self.remaining_penalty(message.guild.id, user_id):
            return

        ref = MsgRef(message.channel.id, message.id)
        now = time.monotonic()

        if is_gif and (refs := self.gifs.add(user_id, ref, now)):
            await self.punish(message, f"GIF spam ({len(refs)} GIFs in under {GIF_WINDOW_SECONDS}s)", refs)
            return
        if refs := self.repeats.add(user_id, text, ref, now):
            await self.punish(message, f"Repeating the same message ({len(refs)} times)", refs)
            return
        if self.judge and not is_gif and text.strip():
            task = asyncio.create_task(self.check_contribution(message, context, text))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def check_contribution(self, message: discord.Message, context: list[tuple[str, str]], text: str) -> None:
        verdict = await self.judge.judge(message.author.display_name, context, text)
        if verdict is None or verdict.ok:
            return
        user_id = message.author.id
        log.info("Strike for %s (%s): %s", message.author, verdict.verdict, verdict.reason)
        reasons = self.strikes.add(user_id, verdict.reason, time.monotonic())
        if reasons and not self.remaining_penalty(message.guild.id, user_id):
            await self.punish(message, "Not contributing: " + "; ".join(reasons[-3:]), [])

    # ---- punishment --------------------------------------------------------

    async def punish(self, message: discord.Message, reason: str, refs: list[MsgRef]) -> None:
        member: discord.Member = message.author
        guild = message.guild
        key = (guild.id, member.id)
        self.penalized[key] = time.monotonic() + PUNISH_MINUTES * 60
        for detector in (self.gifs, self.repeats, self.strikes):
            detector.reset(member.id)

        if DELETE_SPAM and refs:
            await self.delete_messages(guild, refs)

        try:
            if ACTION == "kick":
                await self.temp_kick(member, message.channel, reason)
                verb = "kicked"
            else:
                await member.timeout(timedelta(minutes=PUNISH_MINUTES), reason=reason)
                verb = "timed out"
        except discord.Forbidden:
            self.penalized.pop(key, None)
            log.warning("Missing permission to punish %s (are they an admin, owner, or above my role?)", member)
            await self.send_log(guild, f"⚠️ Couldn't punish {member.mention}: missing permission or they outrank me. Reason was: {reason}")
            return

        log.info("%s %s for %d min: %s", verb.capitalize(), member, PUNISH_MINUTES, reason)
        if ANNOUNCE:
            await message.channel.send(
                f"{member.mention} was {verb} for {PUNISH_MINUTES} minutes: {reason}",
                silent=True,
                delete_after=60,
            )
        await self.send_log(guild, f"🔨 {member.mention} {verb} for {PUNISH_MINUTES} min in {message.channel.mention}: {reason}")

    async def temp_kick(self, member: discord.Member, channel: discord.abc.GuildChannel, reason: str) -> None:
        """Kick, but DM an invite first and remember roles so they come back intact."""
        guild = member.guild
        self.saved_roles[(guild.id, member.id)] = [r.id for r in member.roles if not r.is_default() and not r.managed]
        invite_channel = channel.parent if isinstance(channel, discord.Thread) else channel
        invite = await invite_channel.create_invite(max_age=24 * 3600, max_uses=5, unique=True, reason="SpamGuard temp kick")
        try:
            await member.send(
                f"You were removed from **{guild.name}** for {PUNISH_MINUTES} minutes ({reason}).\n"
                f"After that you can rejoin here and your roles will be restored: {invite.url}"
            )
        except discord.HTTPException:
            pass  # DMs closed; the invite is still in the log channel
        await member.kick(reason=reason)
        await self.send_log(guild, f"Invite for {member} to rejoin after the cooldown: {invite.url}")

    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        key = (guild.id, member.id)
        left = self.remaining_penalty(guild.id, member.id)
        if left:
            try:
                await member.send(f"You're still on cooldown in **{guild.name}** for {int(left)}s. Use the same invite after that.")
            except discord.HTTPException:
                pass
            await member.kick(reason="Rejoined before the SpamGuard cooldown ended")
            return
        role_ids = self.saved_roles.pop(key, None)
        if role_ids:
            roles = [r for r in map(guild.get_role, role_ids) if r and r < guild.me.top_role]
            if roles:
                await member.add_roles(*roles, reason="Restoring roles after SpamGuard temp kick")

    async def delete_messages(self, guild: discord.Guild, refs: list[MsgRef]) -> None:
        for ref in refs:
            channel = guild.get_channel_or_thread(ref.channel_id)
            if channel is None:
                continue
            try:
                await channel.get_partial_message(ref.message_id).delete()
            except discord.HTTPException:
                pass  # already deleted or no Manage Messages permission

    async def send_log(self, guild: discord.Guild, text: str) -> None:
        channel = guild.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
        if channel is not None:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())


# ---- slash commands --------------------------------------------------------

spamguard_commands = app_commands.Group(
    name="spamguard",
    description="Control who SpamGuard watches",
    guild_only=True,
    default_permissions=discord.Permissions(moderate_members=True),
)


@spamguard_commands.command(name="watch", description="Start watching a user for spam")
async def watch_cmd(interaction: discord.Interaction, user: discord.Member) -> None:
    bot: SpamGuard = interaction.client
    bot.watched.add(user.id)
    bot.save_watched()
    await interaction.response.send_message(f"Now watching {user.mention}.", ephemeral=True)


@spamguard_commands.command(name="unwatch", description="Stop watching a user")
async def unwatch_cmd(interaction: discord.Interaction, user: discord.Member) -> None:
    bot: SpamGuard = interaction.client
    bot.watched.discard(user.id)
    bot.save_watched()
    await interaction.response.send_message(f"Stopped watching {user.mention}.", ephemeral=True)


@spamguard_commands.command(name="list", description="Show who is being watched")
async def list_cmd(interaction: discord.Interaction) -> None:
    bot: SpamGuard = interaction.client
    lines = [f"<@{uid}> (AI strikes: {bot.strikes.count(uid)}/{AI_STRIKES})" for uid in sorted(bot.watched)]
    await interaction.response.send_message("\n".join(lines) or "Nobody is being watched.", ephemeral=True)


@spamguard_commands.command(name="pardon", description="End a user's SpamGuard timeout early")
async def pardon_cmd(interaction: discord.Interaction, user: discord.Member) -> None:
    bot: SpamGuard = interaction.client
    bot.penalized.pop((interaction.guild_id, user.id), None)
    bot.strikes.reset(user.id)
    if user.is_timed_out():
        await user.timeout(None, reason=f"Pardoned by {interaction.user}")
    await interaction.response.send_message(f"Pardoned {user.mention}.", ephemeral=True)


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example)")
    key_name = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY"}.get(AI_PROVIDER)
    if AI_ENABLED and key_name is None:
        raise SystemExit("AI_PROVIDER must be 'gemini' or 'claude'")
    if AI_ENABLED and not os.getenv(key_name):
        raise SystemExit(f"Set {key_name} in .env, or AI_ENABLED=false to run without the AI check")
    if ACTION not in ("timeout", "kick"):
        raise SystemExit("ACTION must be 'timeout' or 'kick'")
    SpamGuard().run(TOKEN, root_logger=True)


if __name__ == "__main__":
    main()
