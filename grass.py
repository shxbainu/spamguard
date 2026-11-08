"""The daily grass check: tracks how long one person (DestructorLuke by default) is online on Discord and which
games Discord shows them playing, posts a report every evening, and nags once if they play the same game for hours.

Online time counts whenever Discord shows them as online, idle or do not disturb (on any device), so it's Discord
screen time, not their phone's. Games only show up if they share their activity in Discord's settings
(Activity Privacy → "Share your detected activities"). Needs the Presence Intent turned on in the Developer Portal.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("spamguard.grass")

# Who to watch: a user ID or username. Empty turns the grass check off.
GRASS_USER = os.getenv("GRASS_USER", "destructorluke").strip()
GRASS_CHANNEL_ID = int(os.getenv("GRASS_CHANNEL_ID") or os.getenv("CHAT_CHANNEL_ID") or 0)
GRASS_TIME = (os.getenv("GRASS_TIME") or "21:00").strip()  # 24-hour clock
GRASS_TIMEZONE = (os.getenv("GRASS_TIMEZONE") or "America/New_York").strip()
GRASS_PING = (os.getenv("GRASS_PING") or "true").strip().lower() in ("1", "true", "yes", "on")
# Say something once when they've played the same game this many hours in a row; 0 = never.
GRASS_NAG_HOURS = float(os.getenv("GRASS_NAG_HOURS") or 3)
HEARTBEAT_SECONDS = 60


def duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def wanted_sessions(member: discord.Member) -> set[tuple[str, str | None]]:
    """What should be counting right now: ("online", None) unless offline, plus ("game", name) per game."""
    sessions: set[tuple[str, str | None]] = set()
    if member.status != discord.Status.offline:
        sessions.add(("online", None))
    for activity in member.activities:
        if activity.type == discord.ActivityType.playing and activity.name:
            sessions.add(("game", activity.name))
    return sessions


def report_time(now: datetime) -> datetime:
    """Today's report time in the configured timezone."""
    hour, _, minute = GRASS_TIME.partition(":")
    return now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)


class GrassWatcher:
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.member_id: int | None = int(GRASS_USER) if GRASS_USER.isdigit() else None
        self.guild_id: int | None = None
        self.open: dict[tuple[str, str | None], int] = {}  # session -> row id
        self.opened_at: dict[tuple[str, str | None], float] = {}
        self.nagged: set[tuple[str, str | None]] = set()
        self.task: asyncio.Task | None = None
        try:
            self.zone = ZoneInfo(GRASS_TIMEZONE)
        except Exception:
            log.warning("Unknown GRASS_TIMEZONE %r; using UTC", GRASS_TIMEZONE)
            self.zone = ZoneInfo("UTC")

    @property
    def enabled(self) -> bool:
        return bool(GRASS_USER)

    def member(self) -> discord.Member | None:
        """The watched person, found by ID or username/display name in the bot's servers."""
        for guild in self.bot.guilds:
            if self.member_id:
                member = guild.get_member(self.member_id)
            else:
                wanted = GRASS_USER.lower().lstrip("@")
                member = next((m for m in guild.members if wanted in (m.name.lower(), m.display_name.lower())), None)
            if member:
                return member
        return None

    def start(self) -> None:
        if not self.enabled or self.task:
            return
        member = self.member()
        if member is None:
            log.warning("Grass check: couldn't find %r in the server, so it's off", GRASS_USER)
            return
        if not self.bot.intents.presences:
            log.warning("Grass check: the Presence Intent is off, so it can't see when %s is online", member)
        self.member_id, self.guild_id = member.id, member.guild.id
        now = datetime.now(self.zone)
        if self.store.get_state("grass_report_date") is None and now >= report_time(now):
            # First run ever: start tomorrow instead of posting a report with a few minutes of data.
            self.store.set_state("grass_report_date", now.date().isoformat())
        self.sync(member)
        self.task = asyncio.create_task(self.loop())
        log.info("Grass check watching %s; report at %s %s", member, GRASS_TIME, GRASS_TIMEZONE)

    def stop(self) -> None:
        if self.task:
            self.task.cancel()
        self.store.touch_presence(list(self.open.values()), time.time())

    def sync(self, member: discord.Member) -> None:
        """Ends sessions that stopped and starts new ones from the member's current status and games."""
        now = time.time()
        wanted = wanted_sessions(member)
        for session in list(self.open):
            if session not in wanted:
                self.store.touch_presence([self.open.pop(session)], now)
                self.opened_at.pop(session, None)
                self.nagged.discard(session)
        for kind, name in wanted - set(self.open):
            self.open[(kind, name)] = self.store.start_presence(member.id, kind, name, now)
            self.opened_at[(kind, name)] = now

    def on_presence_update(self, after: discord.Member) -> None:
        if self.task and after.id == self.member_id and after.guild.id == self.guild_id:
            self.sync(after)

    def today(self) -> dict:
        """Online time and games since midnight, in the configured timezone."""
        now = datetime.now(self.zone)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.store.touch_presence(list(self.open.values()), time.time())
        return self.store.presence_totals(self.member_id, midnight.timestamp(), now.timestamp())

    def embed(self, member: discord.Member) -> discord.Embed:
        totals = self.today()
        games = "\n".join(f"{name}: {duration(seconds)}" for name, seconds in list(totals["games"].items())[:6] if seconds >= 60)
        playing = [a.name for a in member.activities if a.type == discord.ActivityType.playing and a.name]
        if member.status == discord.Status.offline:
            now = "⚫ offline"
        else:
            now = f"🟢 {str(member.status).replace('dnd', 'do not disturb')}" + (f", playing {', '.join(playing)}" if playing else "")
        embed = discord.Embed(title=f"🌱 Grass check: {member.display_name}", color=0x2ECC71)
        embed.add_field(name="On Discord today", value=duration(totals["online"]), inline=True)
        embed.add_field(name="Right now", value=now, inline=True)
        embed.add_field(name="Games today", value=games or "none 🙏", inline=False)
        embed.set_footer(text="Counts time Discord shows them online, and games they share in their status")
        return embed

    async def loop(self) -> None:
        beats = 0
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            beats += 1
            try:
                if beats % 5 == 0:
                    self.store.touch_presence(list(self.open.values()), time.time())
                await self.maybe_nag()
                await self.maybe_report()
                if beats % 1440 == 0:
                    self.store.prune_presence(time.time())
            except Exception:
                log.exception("Grass check failed")

    def channel(self):
        return self.bot.get_channel(GRASS_CHANNEL_ID) if GRASS_CHANNEL_ID else None

    async def maybe_report(self) -> None:
        now = datetime.now(self.zone)
        today = now.date().isoformat()
        if now < report_time(now) or self.store.get_state("grass_report_date") == today:
            return
        self.store.set_state("grass_report_date", today)
        await self.post_report()

    async def post_report(self) -> None:
        member, channel = self.member(), self.channel()
        if member is None or channel is None:
            log.warning("Grass check: no member or no GRASS_CHANNEL_ID/CHAT_CHANNEL_ID to post in")
            return
        totals = self.today()
        games = ", ".join(f"{name} for {duration(sec)}" for name, sec in totals["games"].items() if sec >= 60) or "no games"
        status = "offline" if member.status == discord.Status.offline else "online right now"
        situation = (
            f"It's the daily grass check on {member.display_name}: today they were on Discord for "
            f"{duration(totals['online'])} and played {games}, and they're {status}. React to how much they "
            f"touched grass today in one short message."
        )
        line = await self.line(situation) or "daily grass check"
        mention = f"{member.mention} " if GRASS_PING else ""
        await channel.send(
            f"{mention}{line}", embed=self.embed(member),
            allowed_mentions=discord.AllowedMentions(users=[member] if GRASS_PING else False),
        )

    async def maybe_nag(self) -> None:
        if not GRASS_NAG_HOURS:
            return
        now = time.time()
        for session, started in list(self.opened_at.items()):
            kind, name = session
            if kind != "game" or session in self.nagged or now - started < GRASS_NAG_HOURS * 3600:
                continue
            self.nagged.add(session)
            member, channel = self.member(), self.channel()
            if member is None or channel is None:
                return
            line = await self.line(
                f"{member.display_name} has been playing {name} for {duration(now - started)} straight. Say something about it."
            )
            await channel.send(line or f"{member.display_name} has been on {name} for {duration(now - started)} straight",
                               allowed_mentions=discord.AllowedMentions.none())

    async def line(self, situation: str) -> str | None:
        bot = self.bot
        talker = bot.mention_chatter or bot.chatter
        if talker is None:
            return None
        guild = bot.get_guild(self.guild_id) if self.guild_id else None
        name = guild.me.display_name if guild else bot.user.name
        return await talker.say(name, bot.persona, situation)


@app_commands.command(name="grass", description="How long the grass-check person has been on Discord and gaming today")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 30, key=lambda i: i.channel_id)
async def grass_cmd(interaction: discord.Interaction) -> None:
    watcher: GrassWatcher | None = getattr(interaction.client, "grass", None)
    member = watcher.member() if watcher and watcher.task else None
    if member is None:
        await interaction.response.send_message("The grass check isn't running (set GRASS_USER in .env).", ephemeral=True)
        return
    await interaction.response.send_message(embed=watcher.embed(member), allowed_mentions=discord.AllowedMentions.none())
