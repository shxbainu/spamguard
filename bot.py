"""Ben's Butt Spreader (SpamGuard): watches every member for GIF spam, repeated messages and low-effort
noise, and punishes repeat offenders with escalating timeouts that end in a kick. Also chats in character
in one channel and when mentioned, and has /shutup and /settleargument."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

import aura
import casino
import debate
import duels
import factcheck
import news
import predict
import rep
import shop
from cards import LOSS, card
from dashboard import start_dashboard
from detectors import GifSpamDetector, MsgRef, PerMinuteBudget, RepeatDetector, StrikeTracker, looks_like_gif
from escalation import format_duration, parse_ladder, step_for
from judge import API_KEYS, DEFAULT_MODELS, DEFAULT_PERSONA, Judge, SecondOpinion, fix_nicknames, make_judges
from grass import GRASS_USER, GrassWatcher, grass_cmd
from quotes import quote_commands, quote_menu
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
PUNISHMENTS = parse_ladder(os.getenv("PUNISHMENTS") or "10s,20s,30s,kick", kick_seconds=KICK_MINUTES * 60)
OFFENSE_RESET_HOURS = env_int("OFFENSE_RESET_HOURS", 72)
DELETE_SPAM = env_bool("DELETE_SPAM", True)
ANNOUNCE = env_bool("ANNOUNCE", True)

GIF_LIMIT = env_int("GIF_LIMIT", 4)
GIF_WINDOW_SECONDS = env_int("GIF_WINDOW_SECONDS", 60)
REPEAT_LIMIT = env_int("REPEAT_LIMIT", 3)
REPEAT_WINDOW_SECONDS = env_int("REPEAT_WINDOW_SECONDS", 120)

AI_ENABLED = env_bool("AI_ENABLED", True)
AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").strip().lower()  # gemini, groq, openrouter or claude
# Each *_MODEL setting can be a comma-separated list, tried in order; see .env.example.
AI_MODEL = os.getenv("AI_MODEL", "").strip() or None  # empty = provider default
# A smarter model asked only when the AI_MODEL check isn't sure; empty = no second opinion.
AI_UNSURE_PROVIDER = (os.getenv("AI_UNSURE_PROVIDER") or AI_PROVIDER).strip().lower()
AI_UNSURE_MODEL = os.getenv("AI_UNSURE_MODEL", "").strip() or None
AI_STRIKES = env_int("AI_STRIKES", 3)
AI_STRIKE_WINDOW_SECONDS = env_int("AI_STRIKE_WINDOW_SECONDS", 600)
AI_MAX_PER_MINUTE = env_int("AI_MAX_PER_MINUTE", 12)
AI_MIN_LENGTH = env_int("AI_MIN_LENGTH", 3)
CONTEXT_MESSAGES = env_int("CONTEXT_MESSAGES", 15)  # what the "not contributing" check sees
CHAT_CONTEXT_MESSAGES = env_int("CHAT_CONTEXT_MESSAGES", 30)  # what the bot remembers when chatting
# Pictures the AI chat can look at (models that can't see get a note instead).
SEEABLE_IMAGES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
MAX_IMAGES = 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# /settleargument and the chat each pick their own AI; empty means the same as the one before.
SETTLE_PROVIDER = (os.getenv("SETTLE_PROVIDER") or AI_PROVIDER).strip().lower()
SETTLE_MODEL = os.getenv("SETTLE_MODEL", "").strip() or None
CHAT_ENABLED = env_bool("CHAT_ENABLED", True)
CHAT_PROVIDER = (os.getenv("CHAT_PROVIDER") or SETTLE_PROVIDER).strip().lower()
CHAT_MODEL = os.getenv("CHAT_MODEL", "").strip() or None
# @mentions and replies to the bot outside CHAT_CHANNEL_ID, where a good answer matters more than speed.
# Empty = the same models as /settleargument.
MENTION_PROVIDER = (os.getenv("MENTION_PROVIDER") or SETTLE_PROVIDER).strip().lower()
MENTION_MODEL = os.getenv("MENTION_MODEL", "").strip() or SETTLE_MODEL
CHAT_CHANNEL_ID = env_int("CHAT_CHANNEL_ID", 0)
CHAT_COOLDOWN_SECONDS = env_int("CHAT_COOLDOWN_SECONDS", 4)
CHAT_MAX_PER_MINUTE = env_int("CHAT_MAX_PER_MINUTE", 20)
PERSONA = os.getenv("PERSONA", "").strip() or DEFAULT_PERSONA  # only set in .env, never from Discord

SHUTUP_PER_WEEK = env_int("SHUTUP_PER_WEEK", 3)
SHUTUP_SECONDS = env_int("SHUTUP_SECONDS", 10)
SHUTUP_WINDOW = 7 * 86400
SETTLE_COOLDOWN_SECONDS = env_int("SETTLE_COOLDOWN_SECONDS", 30)

# Custom statuses the bot shows on its profile, taking turns every STATUS_MINUTES. Separate them with |.
DEFAULT_STATUSES = (
    "watching over the server for ShxbaInu|serving ShxbaInu|destroying ben's cheeks|"
    "running from dictator kim john-un|forcing luke to touch grass|watching ben get timed out"
)
STATUSES = [entry.strip() for entry in (os.getenv("STATUSES", "").strip() or DEFAULT_STATUSES).split("|") if entry.strip()]
STATUS_MINUTES = max(1, env_int("STATUS_MINUTES", 10))

# Keep a log of checked messages and the bot's replies and rulings for the dashboard, for this many days.
LOG_MESSAGES = env_bool("LOG_MESSAGES", True)
LOG_DAYS = env_int("LOG_DAYS", 7)

DASHBOARD_ENABLED = env_bool("DASHBOARD_ENABLED", True)
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST") or "127.0.0.1"
DASHBOARD_PORT = env_int("DASHBOARD_PORT", 8080)
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

DB_PATH = Path(__file__).with_name("spamguard.db")

log = logging.getLogger("spamguard")
# The Gemini SDK logs "AFC is enabled" on every call; the bot logs which model answered instead.
logging.getLogger("google_genai.models").setLevel(logging.WARNING)


def history_line(message: discord.Message, is_gif: bool | None = None) -> tuple[str, str]:
    """(who, what) for the chat memory. The bot's own messages use its display name, which the AI reads as
    "what I said"; pictures are marked so the bot knows one was shared."""
    if is_gif is None:
        is_gif = looks_like_gif(
            message.clean_content,
            attachments=[(a.filename, a.content_type) for a in message.attachments],
            embeds=[(e.type, e.url) for e in message.embeds],
            sticker_count=len(message.stickers),
        )
    me = message.guild.me
    who = me.display_name if message.author.id == me.id else speaker(message.author)
    if is_gif:
        return who, "[GIF]"
    text = message.clean_content
    if any((a.content_type or "").startswith("image/") for a in message.attachments):
        text = f"{text} [image]".strip()
    return who, text or "[attachment]"


async def images_in(message: discord.Message) -> list[tuple[str, bytes]]:
    """Pictures in the message, or in the message it replies to, that the AI can look at."""
    attachments = list(message.attachments)
    ref = message.reference
    if ref is not None and isinstance(ref.resolved, discord.Message):
        attachments += ref.resolved.attachments
    images = []
    for attachment in attachments:
        mime = (attachment.content_type or "").split(";")[0]
        if mime not in SEEABLE_IMAGES or attachment.size > MAX_IMAGE_BYTES:
            continue
        try:
            images.append((mime, await attachment.read()))
        except discord.HTTPException:
            continue
        if len(images) >= MAX_IMAGES:
            break
    return images


def make_status(text: str) -> discord.CustomActivity:
    return discord.CustomActivity(name=fix_nicknames(text.strip())[:128])


def current_status(index: int, bought: str | None = None) -> discord.CustomActivity:
    """The custom status for a turn of the rotation, or a status someone bought in the shop."""
    return make_status(bought or STATUSES[index % len(STATUSES)])


def speaker(member: discord.abc.User) -> str:
    """How a person appears to the AI: nickname plus @username, so it can tell who's who."""
    if member.display_name.lower() == member.name.lower():
        return member.name
    return f"{member.display_name} (@{member.name})"


class SpamGuard(discord.Client):
    def __init__(self, presences: bool = bool(GRASS_USER)) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable it in the Developer Portal
        intents.members = True  # privileged: needed to restore roles when a kicked user rejoins
        intents.presences = presences  # privileged: lets the grass check see who's online and what they play
        super().__init__(intents=intents)
        self.persona = PERSONA
        self.tree = app_commands.CommandTree(self)
        self.tree.error(self.on_app_command_error)

        self.store = Store(str(DB_PATH), event_days=LOG_DAYS)
        self.gifs = GifSpamDetector(GIF_LIMIT, GIF_WINDOW_SECONDS)
        self.repeats = RepeatDetector(REPEAT_LIMIT, REPEAT_WINDOW_SECONDS)
        self.strikes = StrikeTracker(AI_STRIKES, AI_STRIKE_WINDOW_SECONDS)
        self.judge = optional_judge(AI_PROVIDER, AI_MODEL, "the AI check") if AI_ENABLED else None
        if self.judge and AI_UNSURE_MODEL:
            unsure = optional_judge(AI_UNSURE_PROVIDER, AI_UNSURE_MODEL, "the AI check's second opinion")
            if unsure:
                self.judge = SecondOpinion(self.judge, unsure)
        # Reasoning models think hardest where answers matter most: settling arguments, then mentions elsewhere.
        self.settler = optional_judge(SETTLE_PROVIDER, SETTLE_MODEL, "/settleargument", effort="high")
        self.chatter = optional_judge(CHAT_PROVIDER, CHAT_MODEL, "the AI chat") if CHAT_ENABLED else None
        self.mention_chatter = (
            optional_judge(MENTION_PROVIDER, MENTION_MODEL, "replies to mentions", effort="medium") if CHAT_ENABLED else None
        )
        self.ai_budget = PerMinuteBudget(AI_MAX_PER_MINUTE)
        self.chat_budget = PerMinuteBudget(CHAT_MAX_PER_MINUTE)  # chat replies and /settleargument
        self.last_reply: dict[int, float] = {}  # user id -> monotonic time the bot last answered them

        # Recent messages per channel, used as context for the AI judge.
        self.history: dict[int, deque[tuple[str, str]]] = defaultdict(
            lambda: deque(maxlen=max(CONTEXT_MESSAGES, CHAT_CONTEXT_MESSAGES))
        )
        self.backfilled: set[int] = set()  # channels whose history was reloaded from Discord after a restart
        # (channel id, channel name, text) of the bot's latest replies anywhere, so it stays consistent across channels.
        self.recent_replies: deque[tuple[int, str, str]] = deque(maxlen=20)
        # (guild_id, user_id) -> monotonic time their current punishment ends.
        self.penalized: dict[tuple[int, int], float] = {}
        # (guild_id, user_id) -> role ids to give back after a kick.
        self.saved_roles: dict[tuple[int, int], list[int]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._dashboard = None
        self._status_task: asyncio.Task | None = None
        self.grass = GrassWatcher(self)
        self.predictions = predict.Predictions(self)
        self.news = news.DailyNews(self)
        self.last_factcheck: dict[int, float] = {}  # user id -> monotonic time of their last fact-check
        self._shop_task: asyncio.Task | None = None

    # ---- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        for command in (
            spamguard_commands, purge_cmd, shutup_cmd, settle_cmd, quote_menu, quote_commands, aura.aura_commands,
            debate.debate_commands, grass_cmd, factcheck.factcheck_menu, factcheck.factcheck_cmd,
            *casino.COMMANDS, *duels.COMMANDS, *predict.COMMANDS, *rep.COMMANDS, *shop.COMMANDS,
        ):
            self.tree.add_command(command)
        self.add_dynamic_items(predict.OutcomeButton)  # market buttons keep working after a restart
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)  # instant in one server
        else:
            await self.tree.sync()  # global; can take a while to show up
        if DASHBOARD_ENABLED:
            self._dashboard = await start_dashboard(
                self.store, DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_PASSWORD,
                reset_after=OFFENSE_RESET_HOURS * 3600, on_reset=self.reset_offenses,
            )
            log.info("Dashboard running at http://%s:%d", DASHBOARD_HOST, DASHBOARD_PORT)

    async def close(self) -> None:
        if self._status_task:
            self._status_task.cancel()
        self.grass.stop()
        self.predictions.stop()
        self.news.stop()
        if self._dashboard:
            await self._dashboard.cleanup()
        await super().close()

    def reset_aura_once(self) -> None:
        """AURA_RESET_TO in .env puts everyone at that much aura, once per value (so leaving it set does nothing more)."""
        target = os.getenv("AURA_RESET_TO", "").strip().replace(",", "")
        if not target:
            return
        try:
            amount = int(target)
        except ValueError:
            log.warning("AURA_RESET_TO must be a whole number, not %r", target)
            return
        for guild in self.guilds:
            key = f"aura_reset:{guild.id}"
            if self.store.get_state(key) == str(amount):
                continue
            changed = self.store.reset_aura(guild.id, amount)
            self.store.set_state(key, str(amount))
            log.warning("AURA_RESET_TO: set everyone in %s to %s aura (%d people changed). Remove AURA_RESET_TO now.",
                        guild.name, f"{amount:,}", changed)

    async def on_ready(self) -> None:
        def describe(judge: Judge | None) -> str:
            return judge.model if judge else "off"

        self.reset_aura_once()

        ladder = " → ".join(step.describe() for step in PUNISHMENTS)
        log.info("Logged in as %s. Punishments: %s", self.user, ladder)
        log.info(
            "AI: moderation=%s, settle=%s, chat channel=%s, mentions elsewhere=%s",
            describe(self.judge), describe(self.settler), describe(self.chatter), describe(self.mention_chatter),
        )
        if PERSONA == DEFAULT_PERSONA:
            log.warning(
                "Using the default personality: PERSONA is empty or couldn't be read from .env "
                "(a double quote inside the text breaks it; look for 'could not parse' above)"
            )
        else:
            log.info("Personality: %s", PERSONA if len(PERSONA) <= 100 else PERSONA[:100] + "…")
        if STATUSES and not self._status_task:  # on_ready runs again after reconnects
            self._status_task = asyncio.create_task(self.rotate_statuses())
        self.grass.start()
        self.predictions.start()
        self.news.start()
        if not self._shop_task:
            self._shop_task = asyncio.create_task(shop.undo_expired(self))

    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        self.grass.on_presence_update(after)

    async def show_status(self, bought: str | None = None) -> None:
        """Shows a bought status right away."""
        await self.change_presence(activity=current_status(0, bought))

    async def rotate_statuses(self) -> None:
        """Shows each of STATUSES in turn (or a bought one), changing every STATUS_MINUTES."""
        index = 0
        while True:
            try:
                bought = shop.status_override(self.store)  # someone paid aura for the bot's status
                await self.change_presence(activity=current_status(index, bought))
            except Exception:
                log.exception("Couldn't change the bot's status")
            index += 1
            await asyncio.sleep(STATUS_MINUTES * 60)

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"Slow down, try again in {error.retry_after:.0f}s.", ephemeral=True)
            return
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("Only server admins can use that.", ephemeral=True)
            return
        log.exception("Slash command failed", exc_info=error)
        reply = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await reply("Something went wrong running that command.", ephemeral=True)

    def reset_offenses(self, guild_id: int | None, user_id: int | None) -> None:
        """Clears offense counts and AI strikes for one member, or everyone when user_id is None."""
        if user_id is None:
            self.store.forgive_all(guild_id)
            self.strikes = StrikeTracker(AI_STRIKES, AI_STRIKE_WINDOW_SECONDS)
        else:
            self.store.forgive(guild_id, user_id)
            self.strikes.reset(user_id)

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

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not isinstance(message.author, discord.Member):
            return
        if (running := debate.by_thread.get(message.channel.id)) is not None:
            await debate.on_thread_message(running, message)  # debate threads have their own rules
            return
        self.predictions.count(message)
        text = message.clean_content
        is_gif = looks_like_gif(
            text,
            attachments=[(a.filename, a.content_type) for a in message.attachments],
            embeds=[(e.type, e.url) for e in message.embeds],
            sticker_count=len(message.stickers),
        )
        history = self.history[message.channel.id]
        context = list(history)[-CONTEXT_MESSAGES:]
        history.append(history_line(message, is_gif))

        in_chat_channel = message.channel.id == CHAT_CHANNEL_ID
        if not in_chat_channel:  # the AI chat channel is for messing around, so it isn't moderated
            await self.moderate(message, text, is_gif, context)
        to_bot = self.talking_to_bot(message, in_chat_channel) and not self.remaining_penalty(message.guild.id, message.author.id)
        if self.wants_reply(message, in_chat_channel):
            # The chat AI reads how they treated the bot while it writes the reply.
            self.spawn(self.chat_reply(message, rate=to_bot))
        elif to_bot and (bonus := self.react_to(message, text)):
            self.spawn(self.thank(message, bonus))

    async def moderate(self, message: discord.Message, text: str, is_gif: bool, context: list[tuple[str, str]]) -> None:
        channel_id = message.channel.id
        if (WATCH_CHANNEL_IDS and channel_id not in WATCH_CHANNEL_IDS) or channel_id in IGNORE_CHANNEL_IDS:
            return
        member = message.author
        if self.remaining_penalty(message.guild.id, member.id):
            return
        now = time.monotonic()
        if self.is_exempt(member):
            # Mods can't be punished, but they can still gain or lose aura.
            if aura.AURA_ENABLED and self.judge and not is_gif and len(text.strip()) >= AI_MIN_LENGTH and self.ai_budget.take(now):
                self.spawn(self.check_contribution(message, context, text, exempt=True))
            return

        ref = MsgRef(channel_id, message.id)

        if is_gif and (refs := self.gifs.add(member.id, ref, now)):
            reason = f"GIF spam ({len(refs)} GIFs in under {GIF_WINDOW_SECONDS}s)"
            await self.punish(message, "gif", reason, f"{len(refs)} GIFs/stickers", refs)
            return
        if refs := self.repeats.add(member.id, text, ref, now):
            reason = f"Repeated the same message {len(refs)} times"
            await self.punish(message, "repeat", reason, text[:300], refs)
            return
        if self.judge and not is_gif and len(text.strip()) >= AI_MIN_LENGTH and self.ai_budget.take(now):
            self.spawn(self.check_contribution(message, context, text))

    def log_event(self, guild: discord.Guild, channel, kind: str, member: discord.abc.User | None, **fields) -> None:
        """Adds an entry to the dashboard's activity log (unless LOG_MESSAGES is off)."""
        if not LOG_MESSAGES:
            return
        self.store.log_event(
            guild_id=guild.id, kind=kind, channel_name=getattr(channel, "name", None),
            user_id=member.id if member else None, user_name=str(member) if member else None, **fields,
        )

    # ---- AI chat -----------------------------------------------------------

    def talking_to_bot(self, message: discord.Message, in_chat_channel: bool) -> bool:
        """A mention of the bot or a reply to it, or a message in the chat channel that isn't aimed at someone else."""
        ref = message.reference
        replied_to = ref.resolved.author if ref is not None and isinstance(ref.resolved, discord.Message) else None
        if self.user in message.mentions or replied_to == self.user:
            return True
        others = [m for m in message.mentions if m != self.user] or ([replied_to] if replied_to else [])
        return in_chat_channel and not others

    def react_to(self, message: discord.Message, text: str, tone: str | None = None) -> int:
        """Updates the bot's opinion of someone talking to it, and returns the aura they gain for being nice to it or
        lose for being rude, if the roll hits (0 otherwise). `tone` is the chat AI's read ("nice", "rude" or "neutral"); without it the wording
        decides."""
        if change := rep.react(self.store, message.author, text, tone=tone):
            log.info("Rep: %s %+d (%s, %r)", message.author, change, f"AI read it as {tone}" if tone else "from the wording", text[:80])
        kind = rep.classify(text, tone)
        if kind == "nice":
            points = aura.nice_aura(self.store, message.author, text, tone=tone)
        elif kind == "rude":
            points = aura.rude_aura(self.store, message.author, text)
        else:
            points = 0
        if points:
            log.info("%s to the bot: %s %+d aura (%r)", kind.title(), message.author, points, text[:80])
        return points

    async def thank(self, message: discord.Message, points: int) -> None:
        """The short reply for nice or rude aura when the AI chat isn't answering this message."""
        line = random.choice(aura.NICE_FALLBACK if points > 0 else aura.RUDE_FALLBACK)
        try:
            await message.reply(f"{line} {aura.aura_text(points)}", mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    def wants_reply(self, message: discord.Message, in_chat_channel: bool) -> bool:
        """Answer everything in the chat channel, and mentions or replies to the bot anywhere."""
        if self.chatter is None:
            return False
        ref = message.reference
        replied_to_bot = ref is not None and isinstance(ref.resolved, discord.Message) and ref.resolved.author == self.user
        if not (in_chat_channel or self.user in message.mentions or replied_to_bot):
            return False
        if self.remaining_penalty(message.guild.id, message.author.id):
            return False
        now = time.monotonic()
        if now - self.last_reply.get(message.author.id, -CHAT_COOLDOWN_SECONDS) < CHAT_COOLDOWN_SECONDS:
            return False
        if not self.chat_budget.take(now):
            return False
        self.last_reply[message.author.id] = now
        return True

    async def chat_reply(self, message: discord.Message, rate: bool = False) -> None:
        """Replies in character; with `rate` (they're talking to the bot) it also updates its opinion of them and
        may give nice-to-the-bot aura, using the AI's read of their message."""
        bonus = 0
        history = self.history[message.channel.id]
        name = message.guild.me.display_name
        try:
            async with message.channel.typing():
                await self.backfill(message)
                in_chat_channel = message.channel.id == CHAT_CHANNEL_ID
                chatter = self.chatter if in_chat_channel else (self.mention_chatter or self.chatter)
                images = await images_in(message) if chatter.sees_images else []
                elsewhere = [(ch, said) for cid, ch, said in self.recent_replies if cid != message.channel.id][-8:]
                text, tone = await chatter.chat_and_tone(
                    name, PERSONA, list(history)[-CHAT_CONTEXT_MESSAGES:], speaker(message.author),
                    images=images, elsewhere=elsewhere, quotes=self.store.quotes_by(message.guild.id, message.author.id),
                    opinion=rep.opinion(self.store, message.author), rate_tone=rate,
                )
            if rate:
                bonus = self.react_to(message, message.clean_content, tone)
            if not text:
                if bonus:
                    await self.thank(message, bonus)
                return
            where = "chat channel" if in_chat_channel else "mention"
            log.info("Reply (%s) by %s: %r", where, chatter.last_model, text[:100])
            seen = f" (looked at {len(images)} image{'s' if len(images) > 1 else ''})" if images else ""
            self.log_event(
                message.guild, message.channel, "reply", message.author, content=text[:500], model=chatter.last_model,
                reason=where + seen, detail=f"replying to: {message.clean_content[:300]}",
            )
            # No pings from the AI, ever (it can't @everyone or mention people).
            sent = f"{text[:1985]} {aura.aura_text(bonus)}" if bonus else text[:2000]
            await message.reply(sent, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Couldn't send a chat reply in #%s: %s", getattr(message.channel, "name", "?"), e)
            return
        history.append((name, text))
        self.recent_replies.append((message.channel.id, getattr(message.channel, "name", "?"), text))

    async def backfill(self, message: discord.Message) -> None:
        """After a restart the bot remembers nothing, so the first time it chats in a channel it reads that
        channel's recent messages from Discord."""
        channel = message.channel
        if channel.id in self.backfilled:
            return
        self.backfilled.add(channel.id)
        try:
            older = [m async for m in channel.history(limit=CHAT_CONTEXT_MESSAGES, before=message)]
        except discord.HTTPException as e:
            log.warning("Couldn't read earlier messages in #%s: %s", getattr(channel, "name", "?"), e)
            return
        history = self.history[channel.id]
        current = history_line(message)
        history.clear()
        history.extend(history_line(m) for m in reversed(older))
        history.append(current)

    async def check_contribution(
        self, message: discord.Message, context: list[tuple[str, str]], text: str, exempt: bool = False,
    ) -> None:
        verdict = await self.judge.judge(speaker(message.author), context, text, PERSONA)
        if verdict is None:
            return
        member = message.author
        details = [f"first check: {verdict.first}"] if verdict.first else []
        if verdict.ok and verdict.aura != "none" and (gave := await aura.award(self, message, verdict, PERSONA)):
            details.append(gave)
        if exempt:
            self.log_event(message.guild, message.channel, "check", member, detail="; ".join(details) or None,
                           content=text[:500], verdict=verdict.verdict, reason=verdict.reason, model=verdict.model,
                           escalated=verdict.escalated, overruled=verdict.overruled)
            return
        record = dict(
            content=text[:500], verdict=verdict.verdict, reason=verdict.reason, model=verdict.model,
            escalated=verdict.escalated, overruled=verdict.overruled,
        )
        if verdict.ok:
            if self.strikes.count(member.id):
                details.append(f"cleared {self.strikes.count(member.id)} strike(s) in a row")
            self.strikes.reset(member.id)  # strikes only add up when they come in a row
            self.log_event(message.guild, message.channel, "check", member, detail="; ".join(details) or None, **record)
            return
        log.info("Strike for %s (%s, by %s): %s | %r", member, verdict.verdict, verdict.model, verdict.reason, text[:100])
        strikes = self.strikes.add(member.id, f"{verdict.reason}\n“{text[:150]}”", time.monotonic())
        details.append("strike 3 of 3 in a row: offense" if strikes else f"strike {self.strikes.count(member.id)} of {AI_STRIKES} in a row")
        self.log_event(message.guild, message.channel, "check", member, detail="; ".join(details), **record)
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
        invite = await invite_channel.create_invite(max_age=24 * 3600, max_uses=5, unique=True, reason="Ben's Butt Spreader kick")
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
            await member.kick(reason="Rejoined before the kick ended")
            return
        role_ids = self.saved_roles.pop((guild.id, member.id), None)
        if role_ids:
            roles = [r for r in map(guild.get_role, role_ids) if r and r < guild.me.top_role]
            if roles:
                await member.add_roles(*roles, reason="Restoring roles after kick")

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
    name="spreader",
    description="Ben's Butt Spreader moderation",
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
    bot.reset_offenses(interaction.guild_id, user.id)
    if user.is_timed_out():
        await user.timeout(None, reason=f"Pardoned by {interaction.user}")
    await interaction.response.send_message(f"Pardoned {user.mention}; their offenses are reset.", ephemeral=True)


@spamguard_commands.command(name="reset", description="Reset a member's offense count to zero (doesn't end a current timeout)")
async def reset_cmd(interaction: discord.Interaction, user: discord.Member) -> None:
    bot: SpamGuard = interaction.client
    bot.reset_offenses(interaction.guild_id, user.id)
    next_step = step_for(PUNISHMENTS, 1).describe()
    await interaction.response.send_message(f"Reset {user.mention}'s offenses. Their next punishment would be a {next_step}.", ephemeral=True)


@spamguard_commands.command(name="markets", description="Post today's prediction markets now (if they didn't go up)")
async def markets_cmd(interaction: discord.Interaction) -> None:
    bot: SpamGuard = interaction.client
    await interaction.response.defer(ephemeral=True, thinking=True)  # a web search can take a few seconds
    await interaction.followup.send(await bot.predictions.post_now(), ephemeral=True)


@spamguard_commands.command(name="resetall", description="Reset everyone's offense count to zero")
async def resetall_cmd(interaction: discord.Interaction) -> None:
    bot: SpamGuard = interaction.client
    bot.reset_offenses(interaction.guild_id, None)
    await interaction.response.send_message("Reset everyone's offenses in this server.", ephemeral=True)


# ---- commands anyone can use -------------------------------------------------

def shutup_blocker(author: discord.Member, target: discord.Member) -> str | None:
    """Why `author` can't /shutup `target`, or None if they can."""
    guild = target.guild
    if target.id == author.id:
        return "You can't /shutup yourself."
    if target.bot:
        return "Bots can't be shut up."
    if target.id == guild.owner_id or target.guild_permissions.administrator or target.top_role >= guild.me.top_role:
        return f"I can't time out {target.mention}: they're an admin or their role is above mine."
    if target.is_timed_out():
        return f"{target.mention} is already timed out."
    return None


def purge_filter(user: discord.abc.User | None, contains: str | None, bots_only: bool):
    """Which messages /purge deletes: never pinned ones, and only those matching the options given."""
    needle = (contains or "").lower()

    def keep(message: discord.Message) -> bool:
        if message.pinned:
            return False
        if user is not None and message.author.id != user.id:
            return False
        if bots_only and not message.author.bot:
            return False
        return not needle or needle in message.content.lower()
    return keep


@app_commands.command(name="purge", description="Delete recent messages in this channel (mods only)")
@app_commands.describe(
    amount="How many recent messages to look through (1-200)", user="Only this person's messages",
    contains="Only messages containing this text", bots_only="Only messages from bots",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.cooldown(1, 5, key=lambda i: i.channel_id)
async def purge_cmd(
    interaction: discord.Interaction, amount: app_commands.Range[int, 1, 200], user: Optional[discord.Member] = None,
    contains: Optional[str] = None, bots_only: bool = False,
) -> None:
    channel = interaction.channel
    if not interaction.permissions.manage_messages:
        await interaction.response.send_message("You need Manage Messages to purge.", ephemeral=True)
        return
    if not channel.permissions_for(interaction.guild.me).manage_messages:
        await interaction.response.send_message("I need the Manage Messages permission in this channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        # Messages under 14 days old go in bulk; older ones one at a time (slower, Discord's rule).
        deleted = await channel.purge(limit=amount, check=purge_filter(user, contains, bots_only),
                                      reason=f"/purge by {interaction.user}")
    except discord.HTTPException as e:
        await interaction.followup.send(f"Couldn't delete them: {e.text or e}", ephemeral=True)
        return
    bot: SpamGuard = interaction.client
    which = " ".join(part for part in (
        f"from {user.display_name}" if user else "", f'containing "{contains}"' if contains else "", "from bots" if bots_only else "",
    ) if part)
    log.info("%s purged %d message(s) in #%s %s", interaction.user, len(deleted), getattr(channel, "name", "?"), which)
    bot.log_event(interaction.guild, channel, "purge", interaction.user,
                  detail=f"deleted {len(deleted)} of the last {amount} messages {which}".strip())
    await interaction.followup.send(f"Deleted **{len(deleted)}** message{'s' if len(deleted) != 1 else ''}"
                                    f"{' ' + which if which else ''}.", ephemeral=True)


@app_commands.command(name="shutup", description=f"Time someone out for {SHUTUP_SECONDS} seconds ({SHUTUP_PER_WEEK} uses per week, more in /shop)")
@app_commands.describe(user="Who needs to shut up", long="Use a 1-minute /shutup you bought in /shop")
@app_commands.guild_only()
async def shutup_cmd(interaction: discord.Interaction, user: discord.Member, long: bool = False) -> None:
    bot: SpamGuard = interaction.client
    store, author, guild_id = bot.store, interaction.user, interaction.guild_id
    now = time.time()
    used = store.shutups_used(guild_id, author.id, now, SHUTUP_WINDOW)
    owned = store.items(guild_id, author.id)
    weekly_left = SHUTUP_PER_WEEK - len(used)
    if long and not owned.get("longshutup"):
        await interaction.response.send_message("You don't have a 1-minute /shutup. Buy one with `/buy longshutup`.", ephemeral=True)
        return
    if not long and weekly_left <= 0 and not owned.get("shutup"):
        await interaction.response.send_message(
            f"You've used all {SHUTUP_PER_WEEK} of your /shutups this week. Your next one comes back "
            f"<t:{int(used[0] + SHUTUP_WINDOW)}:R>, or buy one with `/buy shutup`.",
            ephemeral=True,
        )
        return
    if problem := shutup_blocker(author, user):
        await interaction.response.send_message(problem, ephemeral=True)
        return
    if (shielded := shop.shield_until(store, guild_id, user.id)) > now:
        await interaction.response.send_message(
            f"🛡️ {user.display_name} has a shutup shield until <t:{int(shielded)}:t>.", ephemeral=True,
        )
        return

    seconds = shop.LONG_SHUTUP_SECONDS if long else SHUTUP_SECONDS
    reason = f"/shutup from {author}"
    try:
        await user.timeout(timedelta(seconds=seconds), reason=reason)
    except discord.Forbidden:
        await interaction.response.send_message(f"I don't have permission to time out {user.mention}.", ephemeral=True)
        return
    # Use the weekly ones first, then bought ones.
    if long:
        store.use_item(guild_id, author.id, "longshutup")
    elif weekly_left > 0:
        store.record_shutup(guild_id, author.id, user.id, now)
        weekly_left -= 1
    else:
        store.use_item(guild_id, author.id, "shutup")
    store.record(
        guild_id=guild_id, user_id=user.id, user_name=str(user),
        channel_name=getattr(interaction.channel, "name", None), action="timeout", duration_seconds=seconds,
        offense=0, category="shutup", reason=reason,
    )
    bought = store.items(guild_id, author.id).get("shutup", 0)
    extra = f" + {bought} bought" if bought else ""
    embed = card(author, "🤫 Shut up", f"**{author.display_name}** told **{user.display_name}** to shut up.", color=LOSS)
    embed.add_field(name="Timeout", value=format_duration(seconds), inline=True)
    embed.add_field(name="Left this week", value=f"{weekly_left}{extra}", inline=True)
    await interaction.response.send_message(user.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=[user]))


@app_commands.command(name="settleargument", description="The AI reads the argument in this channel and decides who's right")
@app_commands.describe(
    comment="Anything you want to say to the bot about it, e.g. 'tell Dave he's wrong'",
    messages="How many recent messages to read (default 50)",
)
@app_commands.guild_only()
@app_commands.checks.cooldown(1, SETTLE_COOLDOWN_SECONDS, key=lambda i: i.channel_id)
async def settle_cmd(
    interaction: discord.Interaction,
    comment: Optional[str] = None,
    messages: app_commands.Range[int, 5, 100] = 50,
) -> None:
    bot: SpamGuard = interaction.client
    if bot.settler is None:
        await interaction.response.send_message("/settleargument isn't set up: its AI provider has no API key.", ephemeral=True)
        return
    if not bot.chat_budget.take(time.monotonic()):
        await interaction.response.send_message("The AI is busy right now, try again in a minute.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)

    transcript = [
        (speaker(m.author), m.clean_content or "[attachment]")
        async for m in interaction.channel.history(limit=messages)
        if not m.author.bot
    ]
    transcript.reverse()
    if not transcript:
        await interaction.followup.send("There's nothing here to settle.")
        return
    ruling = await bot.settler.settle(transcript, speaker(interaction.user), comment, PERSONA)
    if ruling:
        log.info("Settled %r by %s: winner %s", ruling.topic[:60], bot.settler.last_model, ruling.winner)
        bot.log_event(
            interaction.guild, interaction.channel, "ruling", interaction.user, content=ruling.verdict[:500],
            reason=ruling.reasoning[:1000], model=bot.settler.last_model,
            detail=f"topic: {ruling.topic[:200]} · winner: {ruling.winner[:100]}" + (f" · comment: {comment[:200]}" if comment else ""),
        )
    if ruling is None:
        await interaction.followup.send("The AI couldn't be reached. Try again in a minute.")
        return

    embed = discord.Embed(title=f"⚖️ {ruling.topic}"[:256], description=f"**{ruling.verdict}**"[:4096], color=0x5865F2)
    for side in ruling.sides[:6]:
        embed.add_field(name=(side["who"] or "?")[:256], value=(side["position"] or "–")[:1024], inline=False)
    embed.add_field(name="Winner", value=(ruling.winner or "Nobody")[:1024], inline=False)
    embed.add_field(name="Why", value=(ruling.reasoning or "–")[:1024], inline=False)
    if ruling.facts:
        embed.add_field(name="Facts", value="\n".join(f"• {fact}" for fact in ruling.facts[:4])[:1024], inline=False)
    if ruling.sources:
        embed.add_field(name="Sources", value=debate.source_list(ruling.sources), inline=False)
    embed.set_footer(text=f"Read the last {len(transcript)} messages · {bot.settler.last_model} · AI can be wrong")
    said = f"> {comment[:300]}\n" if comment else ""
    await interaction.followup.send(
        f"{said}{ruling.reply}"[:2000], embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


def optional_judge(provider: str, models: str | None, feature: str, effort: str | None = None) -> Judge | None:
    """The AI (or list of AIs) for a feature, skipping providers without an API key; None turns the feature off."""
    def has_key(name: str) -> bool:
        if os.getenv(API_KEYS[name]):
            return True
        log.warning("No %s set, so %s skips %s models", API_KEYS[name], feature, name)
        return False

    judge = make_judges(models, provider, available=has_key, effort=effort)
    if judge is None:
        log.warning("%s is off: none of its AI providers have an API key", feature)
    return judge


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example)")
    providers = (
        ("AI_PROVIDER", AI_PROVIDER), ("AI_UNSURE_PROVIDER", AI_UNSURE_PROVIDER), ("SETTLE_PROVIDER", SETTLE_PROVIDER),
        ("CHAT_PROVIDER", CHAT_PROVIDER), ("MENTION_PROVIDER", MENTION_PROVIDER),
    )
    for setting, provider in providers:
        if provider not in DEFAULT_MODELS:
            raise SystemExit(f"{setting} must be one of: {', '.join(DEFAULT_MODELS)}")
    key_name = API_KEYS[AI_PROVIDER]
    if AI_ENABLED and not os.getenv(key_name):
        raise SystemExit(f"Set {key_name} in .env, or AI_ENABLED=false to run without the AI check")
    try:
        SpamGuard().run(TOKEN, root_logger=True)
    except discord.PrivilegedIntentsRequired:
        log.error(
            "The Presence Intent is off in the Discord Developer Portal (Bot → Privileged Gateway Intents), so the "
            "grass check can't see who's online. Starting without it; turn it on and restart to use the grass check."
        )
        SpamGuard(presences=False).run(TOKEN, root_logger=True)


if __name__ == "__main__":
    main()
