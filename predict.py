"""The daily prediction: one yes/no question a day that people can bet aura on, settled that night. Most questions
are about this server (how busy the chat gets, whether anyone gets timed out, big aura moments, casino losses) and
are settled from the bot's own records with no AI at all. When the settle model can search the web (Gemini), some
days get a sports or news question instead: one search to write it and one to check the result. If a result can't
be confirmed, everyone gets their bet back."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Literal

import discord
from discord import app_commands
from dotenv import load_dotenv

from cards import EVEN, LOSS, PLAYING, WIN, card
from casino import balance, fmt, parse_bet, welcome

load_dotenv()

log = logging.getLogger("spamguard.predict")

PREDICT_ENABLED = (os.getenv("PREDICT_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")
PREDICT_CHANNEL_ID = int(os.getenv("PREDICT_CHANNEL_ID") or os.getenv("CASINO_CHANNEL_ID") or os.getenv("CHAT_CHANNEL_ID") or 0)
PREDICT_OPEN = (os.getenv("PREDICT_OPEN") or "10:00").strip()      # the question goes up
PREDICT_CLOSE = (os.getenv("PREDICT_CLOSE") or "18:00").strip()    # no more bets
PREDICT_SETTLE = (os.getenv("PREDICT_SETTLE") or "23:30").strip()  # the answer and payouts
PREDICT_MAX_BET = int(os.getenv("PREDICT_MAX_BET") or 500)
# How often the question is about sports or news (needs a settle model that can search the web).
PREDICT_WORLD_CHANCE = float(os.getenv("PREDICT_WORLD_CHANCE") or 0.5)

WORLD_PROMPT = """You write the daily yes/no prediction question for a Discord server, where people bet on it.
Search the web for one sports game or one big scheduled event that happens today, {date}, and whose result will be
clearly reported online by {settle} ({zone}) tonight. Popular sports are best (NBA, NFL, NHL, MLB, Premier League,
Champions League, UFC), or something like an award show or a product launch. The outcome should be close to a coin
flip, not obvious. Nothing about deaths, disasters, crime, elections or anything upsetting.
Answer in JSON: "question" is the question (under 20 words, about one specific thing, naming the teams or event),
"check" is what to search tonight to find the result (under 15 words)."""
WORLD_SCHEMA = {
    "type": "object",
    "properties": {"question": {"type": "string"}, "check": {"type": "string"}},
    "required": ["question", "check"],
}
SETTLE_PROMPT = """Search the web to answer a yes/no question about something that was due to happen on {date}.
Answer "yes" or "no" only if the result is clearly reported; answer "unknown" if it isn't yet, if it was postponed or
cancelled, or if you aren't sure. Answer in JSON: "answer" is "yes", "no" or "unknown", "reason" is under 20 words."""
SETTLE_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["yes", "no", "unknown"]}, "reason": {"type": "string"}},
    "required": ["answer", "reason"],
}


def at(now: datetime, clock: str) -> datetime:
    hour, _, minute = clock.partition(":")
    return now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)


HISTORY_DAYS = 14
# Only ask what's a real toss-up here: a question that came out "yes" on fewer or more of the past days than this
# is skipped.
FAIR = (0.2, 0.8)
AURA_STEPS = [25, 50, 100, 150, 200, 250, 300, 400, 500, 750, 1000, 1500, 2000, 2500, 5000, 10000]


def round_count(count: int) -> int:
    """A tidy over/under line near a typical day's message count."""
    step = 10 if count < 200 else 25 if count < 1000 else 100
    return max(step, round(count / step) * step)


def nice_aura(amount: float) -> int:
    return min(AURA_STEPS, key=lambda step: abs(step - amount))


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def chat_questions(history: dict[str, list[int]]) -> list[dict]:
    """Questions about this server that the bot can settle itself, with lines set from what really happened on
    recent days, so they're realistic for this server right now. `history` has one number per past day for:
    "messages" (messages sent), "timeout" (timeouts), "moment" (the biggest aura gain or loss from one message) and
    "casino" (the most anyone lost in the casino). Each question says how often it would have been "yes" ("rate"),
    and only ones that were a toss-up are kept."""
    questions = []

    def add(kind: str, line: int, question: str, hit) -> None:
        days = history.get(kind) or []
        if len(days) < 3:
            return  # not enough to go on yet
        rate = sum(1 for value in days if hit(value)) / len(days)
        if FAIR[0] <= rate <= FAIR[1]:
            questions.append({"kind": kind, "line": line, "question": question, "rate": round(rate, 2)})

    busy = [n for n in history.get("messages", []) if n >= 50]
    if busy:
        line = round_count(median(busy))
        add("messages", line, f"Will this server send more than {line:,} messages today?", lambda n: n > line)
    add("timeout", 1, "Will the bot time anyone out today?", lambda n: n >= 1)
    moments = [n for n in history.get("moment", []) if n]
    if moments:
        line = nice_aura(median(moments))
        add("moment", line, f"Will anyone gain or lose {line:,}+ aura from a single message today?", lambda n: n >= line)
    losses = [n for n in history.get("casino", []) if n]
    if losses:
        line = nice_aura(median(losses))
        add("casino", line, f"Will anyone lose {line:,}+ aura in the casino today?", lambda n: n >= line)
    return questions


def settle_chat(store, guild_id: int, question: dict, span: tuple[float, float], messages_that_day: int) -> tuple[str, str]:
    """("yes" or "no", why) for a question about the server, from the bot's own records for that day."""
    kind = question["kind"]
    line = question.get("line") or {"timeout": 1, "moment": 100, "casino": 1000}.get(kind, 0)
    if kind == "messages":
        return ("yes" if messages_that_day > line else "no"), f"{messages_that_day:,} messages today"
    if kind == "timeout":
        count = store.punishments_between(guild_id, *span)
        return ("yes" if count >= line else "no"), f"{count} timeout{'s' if count != 1 else ''} today"
    if kind == "moment":
        biggest = store.biggest_moment_between(guild_id, *span)
        return ("yes" if abs(biggest) >= line else "no"), (f"biggest was {biggest:+,}" if biggest else "no aura moments today")
    worst = store.worst_casino_day_between(guild_id, *span)
    return ("yes" if -worst >= line else "no"), (f"worst casino day was {worst:+,}" if worst < 0 else "nobody lost aura in the casino")


def payouts(bets: dict[str, list], answer: str) -> dict[str, int]:
    """What each bettor gets back: double on the right side, nothing on the wrong one, the bet if it's void."""
    if answer not in ("yes", "no"):
        return {uid: amount for uid, (side, amount, _) in bets.items()}
    return {uid: amount * 2 for uid, (side, amount, _) in bets.items() if side == answer}


class Predictions:
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.zone = bot.grass.zone
        self.task: asyncio.Task | None = None
        self.messages: dict[tuple[int, str], int] = {}  # (guild, date) -> messages seen

    def start(self) -> None:
        if not PREDICT_ENABLED or self.task:
            return
        if self.channel() is None:
            log.warning("Daily prediction: no PREDICT_CHANNEL_ID, CASINO_CHANNEL_ID or CHAT_CHANNEL_ID to post in, so it's off")
            return
        self.task = asyncio.create_task(self.loop())
        log.info("Daily prediction: question at %s, bets close %s, settled %s", PREDICT_OPEN, PREDICT_CLOSE, PREDICT_SETTLE)

    def stop(self) -> None:
        if self.task:
            self.task.cancel()
        self.save_counts()

    def channel(self):
        return self.bot.get_channel(PREDICT_CHANNEL_ID) if PREDICT_CHANNEL_ID else None

    def today(self) -> datetime:
        return datetime.now(self.zone)

    # ---- message counting (for the "how busy" question) ----

    def count(self, message: discord.Message) -> None:
        key = (message.guild.id, self.today().date().isoformat())
        if key not in self.messages:
            self.messages[key] = int(self.store.get_state(f"messages:{key[0]}:{key[1]}") or 0)
        self.messages[key] += 1

    def messages_on(self, guild_id: int, date: str) -> int:
        return self.messages.get((guild_id, date)) or int(self.store.get_state(f"messages:{guild_id}:{date}") or 0)

    def save_counts(self) -> None:
        for (guild_id, date), count in self.messages.items():
            self.store.set_state(f"messages:{guild_id}:{date}", str(count))

    # ---- the day's question ----

    def key(self, guild_id: int, date: str) -> str:
        return f"predict:{guild_id}:{date}"

    def load(self, guild_id: int, date: str) -> dict | None:
        raw = self.store.get_state(self.key(guild_id, date))
        return json.loads(raw) if raw else None

    def save(self, guild_id: int, date: str, data: dict) -> None:
        self.store.set_state(self.key(guild_id, date), json.dumps(data))

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                self.save_counts()
                await self.tick()
            except Exception:
                log.exception("Daily prediction failed")

    async def tick(self) -> None:
        channel = self.channel()
        if channel is None:
            return
        guild_id, now = channel.guild.id, self.today()
        # Yesterday's too, in case the bot was offline when it was due.
        for day in (now - timedelta(days=1), now):
            date = day.date().isoformat()
            data = self.load(guild_id, date)
            if data and data["status"] == "open" and now >= at(day, PREDICT_SETTLE):
                await self.settle(channel, data, day)
                self.save(guild_id, date, data)
        date = now.date().isoformat()
        if self.load(guild_id, date) is None and at(now, PREDICT_OPEN) <= now < at(now, PREDICT_CLOSE):
            data = await self.make_question(guild_id, now)
            self.save(guild_id, date, data)
            if data["status"] == "open":
                await channel.send(embed=self.question_card(channel.guild, data))

    def history(self, guild_id: int, now: datetime) -> dict[str, list[int]]:
        """What happened on each of the past HISTORY_DAYS days (most recent first), for setting realistic lines.
        Days before the bot started counting messages are left out of "messages"."""
        days = {"messages": [], "timeout": [], "moment": [], "casino": []}
        midnight = at(now, "00:00")
        for back in range(1, HISTORY_DAYS + 1):
            start = midnight - timedelta(days=back)
            span = (start.timestamp(), (start + timedelta(days=1)).timestamp())
            if count := self.messages_on(guild_id, start.date().isoformat()):
                days["messages"].append(count)
            days["timeout"].append(self.store.punishments_between(guild_id, *span))
            days["moment"].append(abs(self.store.biggest_moment_between(guild_id, *span)))
            days["casino"].append(-self.store.worst_casino_day_between(guild_id, *span))
        return days

    async def world_question(self, now: datetime) -> dict | None:
        settler = self.bot.settler
        if settler is None or not settler.can_search:
            return None
        prompt = WORLD_PROMPT.format(date=now.strftime("%A %B %d, %Y"), settle=PREDICT_SETTLE, zone=self.zone.key)
        found, sources = await settler.research(prompt, "Write today's question.", WORLD_SCHEMA, 1024)
        # Only with real search results: without them the model would be guessing what's on today.
        if found and sources and found.get("question") and found.get("check"):
            log.info("Daily prediction (world): %s", found["question"])
            return {"kind": "world", "question": found["question"][:200], "check": found["check"][:150]}
        return None

    async def make_question(self, guild_id: int, now: datetime) -> dict:
        """Today's question: sports/news some days, otherwise a realistic one about the server. If nothing fits
        (a new or quiet server), there's no question today rather than one that's basically a sure thing."""
        tried_world = False
        question = None
        if random.random() < PREDICT_WORLD_CHANCE:
            tried_world, question = True, await self.world_question(now)
        if question is None and (options := chat_questions(self.history(guild_id, now))):
            question = random.choice(options)
            log.info("Daily prediction (chat): %s (yes on %d%% of recent days)", question["question"], question["rate"] * 100)
        if question is None and not tried_world:
            question = await self.world_question(now)
        if question is None:
            log.info("Daily prediction: nothing realistic to ask today, so no question")
            return {"status": "skipped", "bets": {}}
        return {**question, "status": "open", "bets": {}}

    def question_card(self, guild: discord.Guild, data: dict) -> discord.Embed:
        embed = card(guild.me, "🔮 Daily prediction", f"## {data['question']}", color=PLAYING)
        yes = sum(amount for side, amount, _ in data["bets"].values() if side == "yes")
        no = sum(amount for side, amount, _ in data["bets"].values() if side == "no")
        embed.add_field(name="✅ Yes", value=fmt(yes), inline=True)
        embed.add_field(name="❌ No", value=fmt(no), inline=True)
        embed.add_field(name="Pays", value="2x", inline=True)
        now = self.today()
        embed.set_footer(text=f"/predict yes or no with a bet (up to {PREDICT_MAX_BET:,}) · bets close {PREDICT_CLOSE}, "
                              f"answer at {PREDICT_SETTLE} ({now.tzname()})")
        return embed

    async def settle(self, channel, data: dict, day: datetime) -> None:
        guild = channel.guild
        since = at(day, "00:00")
        span = (since.timestamp(), (since + timedelta(days=1)).timestamp())
        if data["kind"] == "world":
            answer, why = await self.check_world(data, day)
        else:
            answer, why = settle_chat(self.store, guild.id, data, span, self.messages_on(guild.id, day.date().isoformat()))
        data["status"], data["answer"], data["why"] = "settled", answer, why
        paid = payouts(data["bets"], answer)
        for uid, amount in paid.items():
            name = data["bets"][uid][2]
            self.store.add_aura(guild.id, int(uid), name, amount, "predict", "refund" if answer == "unknown" else "win")
        log.info("Daily prediction settled: %s (%s); paid %d people", answer, why, len(paid))
        title = "## Couldn't confirm it, so every bet went back" if answer == "unknown" else f"## {answer.title()}"
        winners = [f"**{data['bets'][uid][2]}** +{fmt(amount // 2)}" for uid, amount in paid.items()] if answer != "unknown" else []
        color = EVEN if answer == "unknown" else WIN if winners else LOSS
        embed = card(guild.me, "🔮 Daily prediction result", f"{data['question']}\n{title}\n{why}", color=color)
        if data["bets"]:
            embed.add_field(name="Winners", value=("\n".join(winners) or "Nobody")[:1024], inline=False)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def check_world(self, data: dict, now: datetime) -> tuple[str, str]:
        settler = self.bot.settler
        if settler is None or not settler.can_search:
            return "unknown", "The AI that checks results is off."
        found, sources = await settler.research(
            SETTLE_PROMPT.format(date=now.strftime("%A %B %d, %Y")),
            f"Question: {data['question']}\nSearch for: {data['check']}", SETTLE_SCHEMA, 1024,
        )
        if not found or not sources or found.get("answer") not in ("yes", "no"):
            return "unknown", (found or {}).get("reason") or "The result wasn't clearly reported."
        return found["answer"], (found.get("reason") or "").strip()[:200]

    def place(self, member: discord.Member, side: str, bet: str) -> str | discord.Embed:
        """Takes a bet on today's question; returns the updated card, or why it can't."""
        channel = self.channel()
        if not PREDICT_ENABLED or channel is None or channel.guild.id != member.guild.id:
            return "The daily prediction is off."
        now = self.today()
        date = now.date().isoformat()
        data = self.load(member.guild.id, date)
        if data is None:
            return f"Today's question goes up at {PREDICT_OPEN}."
        if data["status"] == "skipped":
            return "There's no question today."
        if data["status"] != "open" or now >= at(now, PREDICT_CLOSE):
            return f"Bets closed at {PREDICT_CLOSE}. The answer comes at {PREDICT_SETTLE}."
        if str(member.id) in data["bets"]:
            side_was, amount, _ = data["bets"][str(member.id)]
            return f"You already bet {fmt(amount)} on {side_was}."
        welcome(self.store, member)
        amount = parse_bet(bet, min(PREDICT_MAX_BET, balance(self.store, member.guild.id, member.id)))
        if isinstance(amount, str):
            return amount.replace("You only have", f"The most you can bet is {PREDICT_MAX_BET:,}, and you have") \
                if "only have" in amount else amount
        if not self.store.spend_aura(member.guild.id, member.id, member.display_name, amount, "predict", "bet"):
            return "You don't have enough aura."
        data["bets"][str(member.id)] = [side, amount, member.display_name]
        self.save(member.guild.id, date, data)
        return self.question_card(member.guild, data)


@app_commands.command(name="predict", description="Bet on today's yes/no prediction (pays 2x), or see it")
@app_commands.describe(side="Yes or no (leave empty to just see the question)", bet="How much aura (a number, half or all)")
@app_commands.guild_only()
async def predict_cmd(interaction: discord.Interaction, side: Literal["yes", "no"] | None = None, bet: str | None = None) -> None:
    predictions: Predictions = interaction.client.predictions
    member = interaction.user
    if side is None:
        data = predictions.load(member.guild.id, predictions.today().date().isoformat())
        if data is None or data["status"] == "skipped":
            await interaction.response.send_message(
                "There's no question today." if data else f"Today's question goes up at {PREDICT_OPEN}.", ephemeral=True)
        else:
            await interaction.response.send_message(embed=predictions.question_card(member.guild, data), ephemeral=True)
        return
    result = predictions.place(member, side, bet or "100")
    if isinstance(result, str):
        await interaction.response.send_message(result, ephemeral=True)
        return
    await interaction.response.send_message(
        f"**{member.display_name}** bet on **{side}**.", embed=result, allowed_mentions=discord.AllowedMentions.none(),
    )


COMMANDS = [predict_cmd]
