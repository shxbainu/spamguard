"""SpamGuard: watches every member for GIF spam, repeated messages and low-effort
noise, and punishes repeat offenders with escalating timeouts that end in a kick."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from dashboard import start_dashboard
from detectors import GifSpamDetector, MsgRef, RepeatDetector, StrikeTracker, looks_like_gif
from escalation import format_duration, parse_ladder, step_for
from judge import make_judge
from storage import Store

load_dotenv()


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


def env_bool(name: str, default: bool) -> bool:
    return (os.getenv(name) or str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_ids(name: str) -> set[int]:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x}


TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = env_int("GUILD_ID", 0)
WATCH_CHANNEL_IDS = env_ids("WATCH_CHANNEL_IDS")
IGNORE_CHANNEL_IDS = env_ids("IGNORE_CHANNEL_IDS")
LOG_CHANNEL_ID = env_int("LOG_CHANNEL_ID", 0)
EXEMPT_ROLE_IDS = env_ids("EXEMPT_ROLE_IDS")
EXEMPT_MODS = env_bool("EXEMPT_MODS", True)

KICK_MINUTES = env_int("KICK_MINUTES", 10)
PUNISHMENTS = parse_ladder(os.getenv("PUNISHMENTS") or "30s,2m,10m,1h,kick", kick_seconds=KICK_MINUTES * 60)
OFFENSE_RESET_HOURS = env_int("OFFENSE_RESET_HOURS", 24)
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
AI_MAX_PER_MINUTE = env_int("AI_MAX_PER_MINUTE", 12)
AI_MIN_LENGTH = env_int("AI_MIN_LENGTH", 3)
CONTEXT_MESSAGES = env_int("CONTEXT_MESSAGES", 15)

DASHBOARD_ENABLED = env_bool("DASHBOARD_ENABLED", True)
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST") or "127.0.0.1"
DASHBOARD_PORT = env_int("DASHBOARD_PORT", 8080)
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

DB_PATH = Path(__file__).with_name("spamguard.db")

log = logging.getLogger("spamguard")


class SpamGuard(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable it in the Developer Portal
        intents.members = True  # privileged: needed to restore roles when a kicked user rejoins
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

        self.store = Store(str(DB_PATH))
        self.gifs = GifSpamDetector(GIF_LIMIT, GIF_WINDOW_SECONDS)
        self.repeats = RepeatDetector(REPEAT_LIMIT, REPEAT_WINDOW_SECONDS)
        self.strikes = StrikeTracker(AI_STRIKES, AI_STRIKE_WINDOW_SECONDS)
        self.judge = make_judge(AI_PROVIDER, AI_MODEL) if AI_ENABLED else None
        self.ai_calls: deque[float] = deque()

        # Recent messages per channel, used as context for the AI judge.
        self.history: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=CONTEXT_MESSAGES))
        # (guild_id, user_id) -> monotonic time their current punishment ends.
        self.penalized: dict[tuple[int, int], float] = {}
        # (guild_id, user_id) -> role ids to give back after a kick.
        self.saved_roles: dict[tuple[int, int], list[int]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._dashboard = None

    # ---- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        self.tree.add_command(spamguard_commands)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)  # instant in one server
        else:
            await self.tree.sync()  # global; can take a while to show up
        if DASHBOARD_ENABLED:
            self._dashboard = await start_dashboard(self.store, DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_PASSWORD)
            log.info("Dashboard running at http://%s:%d", DASHBOARD_HOST, DASHBOARD_PORT)

    async def close(self) -> None:
        if self._dashboard:
            await self._dashboard.cleanup()
        await super().close()

    async def on_ready(self) -> None:
        ai = f"{AI_PROVIDER} ({self.judge.model})" if self.judge else "off"
        ladder = " → ".join(step.describe() for step in PUNISHMENTS)
        log.info("Logged in as %s. AI=%s. Punishments: %s", self.user, ai, ladder)

    # ---- detection ---------------------------------------------------------

    def remaining_penalty(self, guild_id: int, user_id: int) -> float:
        key = (guild_id, user_id)
        left = self.penalized.get(key, 0) - time.monotonic()
        if left <= 0:
            self.penalized.pop(key, None)
            return 0
        return left

    def is_exempt(self, member: discord.Member) -> bool:
        if member.bot or member.id == member.guild.owner_id:
            return True
        perms = member.guild_permissions
        if perms.administrator or (EXEMPT_MODS and (perms.manage_messages or perms.moderate_members)):
            return True
        return any(role.id in EXEMPT_ROLE_IDS for role in member.roles)

    def ai_budget_available(self) -> bool:
        """Keeps AI calls under AI_MAX_PER_MINUTE so the free tier isn't exceeded."""
        now = time.monotonic()
        while self.ai_calls and now - self.ai_calls[0] > 60:
            self.ai_calls.popleft()
        if len(self.ai_calls) >= AI_MAX_PER_MINUTE:
            return False
        self.ai_calls.append(now)
        return True

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not isinstance(message.author, discord.Member):
            return
        channel_id = message.channel.id
        if (WATCH_CHANNEL_IDS and channel_id not in WATCH_CHANNEL_IDS) or channel_id in IGNORE_CHANNEL_IDS:
            return

        text = message.clean_content
        is_gif = looks_like_gif(
            text,
            attachments=[(a.filename, a.content_type) for a in message.attachments],
            embeds=[(e.type, e.url) for e in message.embeds],
            sticker_count=len(message.stickers),
        )
        history = self.history[channel_id]
        context = list(history)
        history.append((message.author.display_name, "[GIF]" if is_gif else text or "[attachment]"))

        member = message.author
        if self.is_exempt(member) or self.remaining_penalty(message.guild.id, member.id):
            return

        ref = MsgRef(channel_id, message.id)
        now = time.monotonic()

        if is_gif and (refs := self.gifs.add(member.id, ref, now)):
            reason = f"GIF spam ({len(refs)} GIFs in under {GIF_WINDOW_SECONDS}s)"
            await self.punish(message, "gif", reason, f"{len(refs)} GIFs/stickers", refs)
            return
        if refs := self.repeats.add(member.id, text, ref, now):
            reason = f"Repeated the same message {len(refs)} times"
            await self.punish(message, "repeat", reason, text[:300], refs)
            return
        if self.judge and not is_gif and len(text.strip()) >= AI_MIN_LENGTH and self.ai_budget_available():
            task = asyncio.create_task(self.check_contribution(message, context, text))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def check_contribution(self, message: discord.Message, context: list[tuple[str, str]], text: str) -> None:
        verdict = await self.judge.judge(message.author.display_name, context, text)
        if verdict is None or verdict.ok:
            return
        member = message.author
        log.info("Strike for %s (%s): %s | %r", member, verdict.verdict, verdict.reason, text[:100])
        strikes = self.strikes.add(member.id, f"{verdict.reason}\n“{text[:150]}”", time.monotonic())
        if strikes and not self.remaining_penalty(message.guild.id, member.id):
            reasons = [s.split("\n", 1)[0] for s in strikes]
            evidence = "\n".join(s.split("\n", 1)[1] for s in strikes)
            await self.punish(message, "ai", "Not contributing: " + "; ".join(reasons[-3:]), evidence, [])

    # ---- punishment --------------------------------------------------------

    async def punish(self, message: discord.Message, category: str, reason: str, evidence: str, refs: list[MsgRef]) -> None:
        member: discord.Member = message.author
        guild = message.guild
        key = (guild.id, member.id)
        offense = self.store.next_offense(guild.id, member.id, time.time(), OFFENSE_RESET_HOURS * 3600)
        step = step_for(PUNISHMENTS, offense)
        self.penalized[key] = time.monotonic() + step.seconds
        for detector in (self.gifs, self.repeats, self.strikes):
            detector.reset(member.id)

        if DELETE_SPAM and refs:
            await self.delete_messages(guild, refs)

        record = dict(
            guild_id=guild.id, user_id=member.id, user_name=str(member), channel_name=getattr(message.channel, "name", None),
            duration_seconds=step.seconds, offense=offense, category=category, reason=reason, evidence=evidence,
        )
        try:
            if step.action == "kick":
                await self.temp_kick(member, message.channel, reason, step.seconds)
                did = f"was kicked (can rejoin in {format_duration(step.seconds)})"
            else:
                await member.timeout(timedelta(seconds=step.seconds), reason=reason)
                did = f"was timed out for {format_duration(step.seconds)}"
        except discord.Forbidden:
            self.penalized.pop(key, None)
            self.store.record(action="failed", **record)
            log.warning("Missing permission to punish %s (is their role above mine?)", member)
            await self.send_log(guild, f"⚠️ Couldn't punish {member.mention}: missing permission or their role is above mine. Reason was: {reason}")
            return

        self.store.record(action=step.action, **record)
        log.info("%s %s (offense #%d): %s", member, did, offense, reason)
        if ANNOUNCE:
            await message.channel.send(f"{member.mention} {did} — offense #{offense}: {reason}", silent=True, delete_after=60)
        await self.send_log(guild, f"🔨 {member.mention} {did} in {message.channel.mention} — offense #{offense}: {reason}")

    async def temp_kick(self, member: discord.Member, channel: discord.abc.GuildChannel, reason: str, seconds: int) -> None:
        """Kick, but DM an invite first and remember roles so they come back intact."""
        guild = member.guild
        self.saved_roles[(guild.id, member.id)] = [r.id for r in member.roles if not r.is_default() and not r.managed]
        invite_channel = channel.parent if isinstance(channel, discord.Thread) else channel
        invite = await invite_channel.create_invite(max_age=24 * 3600, max_uses=5, unique=True, reason="SpamGuard kick")
        try:
            await member.send(
                f"You were kicked from **{guild.name}** ({reason}).\n"
                f"You can rejoin in {format_duration(seconds)} with this link, and your roles will be restored: {invite.url}"
            )
        except discord.HTTPException:
            pass  # DMs closed; the invite is still in the log channel
        await member.kick(reason=reason)
        await self.send_log(guild, f"Invite for {member} to rejoin after {format_duration(seconds)}: {invite.url}")

    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        left = self.remaining_penalty(guild.id, member.id)
        if left:
            try:
                await member.send(f"You can't rejoin **{guild.name}** for another {int(left)}s. Use the same invite after that.")
            except discord.HTTPException:
                pass
            await member.kick(reason="Rejoined before the SpamGuard kick ended")
            return
        role_ids = self.saved_roles.pop((guild.id, member.id), None)
        if role_ids:
            roles = [r for r in map(guild.get_role, role_ids) if r and r < guild.me.top_role]
            if roles:
                await member.add_roles(*roles, reason="Restoring roles after SpamGuard kick")

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
    description="SpamGuard moderation",
    guild_only=True,
    default_permissions=discord.Permissions(moderate_members=True),
)


@spamguard_commands.command(name="history", description="Show a member's offenses and recent punishments")
async def history_cmd(interaction: discord.Interaction, user: discord.Member) -> None:
    bot: SpamGuard = interaction.client
    offenses = bot.store.offenses(interaction.guild_id, user.id, time.time(), OFFENSE_RESET_HOURS * 3600)
    next_step = step_for(PUNISHMENTS, offenses + 1).describe()
    lines = [
        f"**{user}**: {offenses} offense(s) in the last {OFFENSE_RESET_HOURS}h, "
        f"AI strikes {bot.strikes.count(user.id)}/{AI_STRIKES}. Next punishment: {next_step}."
    ]
    for p in bot.store.punishments(limit=5, user=str(user.id)):
        lines.append(f"• <t:{int(p['created_at'])}:R> {p['action']} {format_duration(p['duration_seconds'])}: {p['reason']}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@spamguard_commands.command(name="pardon", description="End a member's timeout and reset their offenses")
async def pardon_cmd(interaction: discord.Interaction, user: discord.Member) -> None:
    bot: SpamGuard = interaction.client
    bot.penalized.pop((interaction.guild_id, user.id), None)
    bot.strikes.reset(user.id)
    bot.store.forgive(interaction.guild_id, user.id)
    if user.is_timed_out():
        await user.timeout(None, reason=f"Pardoned by {interaction.user}")
    await interaction.response.send_message(f"Pardoned {user.mention}; their offenses are reset.", ephemeral=True)


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example)")
    key_name = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY"}.get(AI_PROVIDER)
    if AI_ENABLED and key_name is None:
        raise SystemExit("AI_PROVIDER must be 'gemini' or 'claude'")
    if AI_ENABLED and not os.getenv(key_name):
        raise SystemExit(f"Set {key_name} in .env, or AI_ENABLED=false to run without the AI check")
    SpamGuard().run(TOKEN, root_logger=True)


if __name__ == "__main__":
    main()
