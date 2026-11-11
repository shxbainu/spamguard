"""Reputation with the bot: a score from -100 to 100 for how the bot feels about each person. Being nice to it
(thanks, compliments) raises it, being rude to it lowers it, and the bot's chat replies get warmer or colder to
match. When the bot replies, the chat AI reads the tone in the same call (with the personality and slang in front
of it); otherwise the wording decides. Either way it costs no extra AI calls. /rep shows anyone's score."""
from __future__ import annotations

import os
import re
import time
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

from aura import is_nice
from cards import LOSS, WIN, EVEN, card

load_dotenv()

REP_ENABLED = (os.getenv("REP_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")
NICE_REP, RUDE_REP = 3, -5
# At most one change per person in this many seconds, so spamming either way doesn't swing it.
REP_COOLDOWN_SECONDS = 120

# Fallback for when the chat AI isn't answering (normally it reads the tone itself, with the personality and slang
# in front of it). Includes the group's slang: mid, cooked, chopped, bum, larp, no aura and the like.
YOU = r"(?:you'?re|youre|ur|u r|you are|u|you)"
RUDE = re.compile(
    r"\b(?:stfu|shut (?:the fuck |tf )?up|clanker|(?:dumb|stupid|bad|trash|useless|worst|mid|cooked|chopped|l) (?:ass )?bot|"
    rf"{YOU} (?:so |really |actually |lowk |deadass |lowkey |hella )?"
    r"(?:dumb|stupid|trash|garbage|mid|annoying|useless|bad|ass|cooked|chopped|a clown|an idiot|a bum|bum|an npc|npc|"
    r"slow|not funny|not tuff|lame|weird|corny|washed|fat|a fatass|fatass|ugly|a loser|loser|a bitch|a dick|an asshole|"
    r"irrelevant|a nerd|a joke|pathetic|cringe|broke|short|bald|a waste|so bad)|"
    rf"{YOU} (?:got|have|has) (?:no|negative|zero|0) aura|no aura|negative aura|"
    rf"i mog (?:you|u)|stop larp(?:ing)?|{YOU} larp(?:ing)?|"
    r"i hate (?:you|u)|deadass i hate|nobody asked|who asked|kys|fuck (?:you|u|off)|screw (?:you|u)|go away|get lost|"
    r"ratio|not funny)\b|(?<!\w)-\s?\d+\s?aura\b",
    re.IGNORECASE,
)

LEVELS = [  # (lowest score, how the bot feels, what /rep says)
    (60, "you genuinely like them; be warm and on their side", "💜 Fw them heavy"),
    (20, "you like them; be a bit friendlier", "🙂 Likes them"),
    (-19, "neutral", "😐 Neutral"),
    (-59, "they've annoyed you; be a bit colder and shorter with them", "😒 Annoyed"),
    (-100, "you can't stand them; be cold, short and unimpressed with them", "🙄 Can't stand them"),
]


def is_rude(text: str) -> bool:
    return bool(RUDE.search(text))


def level(score: int) -> tuple[str, str]:
    """(how the bot feels, the /rep label) for a score."""
    for lowest, feeling, label in LEVELS:
        if score >= lowest:
            return feeling, label
    return LEVELS[-1][1], LEVELS[-1][2]


def classify(text: str, tone: str | None = None) -> str | None:
    """"nice", "rude" or None for a message to the bot. The group's slang list goes first, since it's more
    reliable on what it knows; the chat AI's read (`tone`) covers wording it doesn't."""
    if is_rude(text):
        return "rude"
    if is_nice(text):
        return "nice"
    return tone if tone in ("nice", "rude") else None


def react(store, member: discord.Member, text: str, now: float | None = None, tone: str | None = None) -> int:
    """Moves the bot's opinion of someone who's talking to it, if they were nice or rude; returns the change.
    `tone` is the chat AI's read of the message ("nice", "rude" or "neutral"); without one the wording decides."""
    if not REP_ENABLED:
        return 0
    change = {"nice": NICE_REP, "rude": RUDE_REP}.get(classify(text, tone), 0)
    if not change:
        return 0
    now = time.time() if now is None else now
    last = store.rep_history(member.guild.id, member.id, limit=1)
    if last and now - last[0]["created_at"] < REP_COOLDOWN_SECONDS:
        return 0
    store.add_rep(member.guild.id, member.id, change, "nice" if change > 0 else "rude", text[:300], now)
    return change


def opinion(store, member: discord.Member) -> str | None:
    """A line for the chat prompt on how the bot feels about this person, or None if it's neutral."""
    if not REP_ENABLED:
        return None
    score = store.rep_score(member.guild.id, member.id)
    feeling, _ = level(score)
    if feeling == "neutral":
        return None
    note = f"How you feel about {member.display_name} right now: {feeling}. Never mention a score."
    if score < 0 and (rude := store.rep_history(member.guild.id, member.id, limit=1, reason="rude")):
        note += f' The last thing they said that bugged you: "{rude[0]["message"][:150]}". Bring it up only rarely, when it fits.'
    return note


def bar(score: int) -> str:
    filled = round((score + 100) / 20)
    return "▰" * filled + "▱" * (10 - filled)


@app_commands.command(name="rep", description="See how the bot feels about someone (nice to it: up, rude: down)")
@app_commands.describe(user="Whose reputation (default: yours)")
@app_commands.guild_only()
async def rep_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
    store, user = interaction.client.store, user or interaction.user
    score = store.rep_score(user.guild.id, user.id)
    _, label = level(score)
    color = WIN if score >= 20 else LOSS if score <= -20 else EVEN
    embed = card(user, description=f"## {score:+d} rep\n{bar(score)}\n**{label}**", color=color)
    history = store.rep_history(user.guild.id, user.id, limit=5)
    if history:
        lines = [
            f"`{row['change']:+d}` “{(row['message'] or '')[:60]}” · <t:{int(row['created_at'])}:R>" for row in history
        ]
        embed.add_field(name="Recently", value="\n".join(lines)[:1024], inline=False)
    ranking = [uid for uid, _ in store.rep_ranking(user.guild.id)]
    if user.id in ranking:
        embed.set_footer(text=f"#{ranking.index(user.id) + 1} of {len(ranking)} with the bot · -100 to 100")
    else:
        embed.set_footer(text="Be nice to the bot to raise it; being rude lowers it · -100 to 100")
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


COMMANDS = [rep_cmd]
