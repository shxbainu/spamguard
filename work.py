"""Free aura without betting, and without any AI:

/work: a shift of four random mini-tasks out of twelve (type a phrase like "i love shxbainu and this server", unscramble a
word, quick maths, count emojis, type a word backwards, remember a code or words in order before they disappear, find
the missing number, spot the odd emoji, fill in the blanks, type only the capitals, count a letter), paid only if every task is right.
A few shifts a day.

/beg: a random stranger gives you a little aura (or walks past), every 30 minutes.

Aura drops: now and then, when the chat is active, the bot posts one of those tasks in the drop channel and the first
person to get it right wins.

Copy-paste doesn't work: phrases are shown with invisible characters mixed in, so pasted text gives itself away, and
typing faster than a person can counts as pasting too. Messages that are part of a shift or a drop skip moderation
(typing the same phrase isn't spam) and the bot's chat replies.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time

import discord
from discord import app_commands
from dotenv import load_dotenv

from cards import PLAYING, WIN, card
from casino import balance, fmt, welcome

load_dotenv()

log = logging.getLogger("spamguard.work")

WORK_SHIFTS_PER_DAY = int(os.getenv("WORK_SHIFTS_PER_DAY") or 3)
WORK_COOLDOWN_MINUTES = int(os.getenv("WORK_COOLDOWN_MINUTES") or 20)
WORK_PAY = (8, 15)           # per task, paid only if the whole shift is done
WORK_BONUS = 10              # for a whole shift
WORK_PHRASES = 4             # tasks in a shift
WORK_SECONDS = 20            # for each task
TYPE_SECONDS = 35            # for the long typing ones
CAUGHT_COOLDOWN_MINUTES = 60  # after being caught copy-pasting

DROPS_ENABLED = (os.getenv("DROPS_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")
DROP_CHANNEL_ID = int(os.getenv("DROP_CHANNEL_ID") or os.getenv("CHAT_CHANNEL_ID") or 0)
DROP_CHANCE = float(os.getenv("DROP_CHANCE") or 0.03)  # per message in the drop channel
DROP_GAP_MINUTES = int(os.getenv("DROP_GAP_MINUTES") or 20)
DROP_PAY = (15, 30)
DROP_SECONDS = 30

PHRASES = [
    "i love shxbainu and this server",
    "this server is the greatest place on earth",
    "ben's butt spreader is the best bot ever made",
    "i will touch grass today i promise",
    "my aura is immeasurable and i will not be humbled",
    "i am not mid i am built different",
    "the casino always wins and i accept that",
    "i owe everything to this server",
    "i would never ever copy and paste this",
    "please let me keep my aura i am begging",
    "i solemnly swear i am up to no good",
    "shxbainu is the goat and i will not argue",
    "i have read the rules and i agree with all of them",
    "i will stop yapping for at least five minutes",
]
WORDS = [
    "grass", "aura", "casino", "pizza", "rocket", "planet", "guitar", "dragon", "castle", "jungle", "pirate", "zombie",
    "wizard", "banana", "orange", "cookie", "hockey", "soccer", "goblin", "turtle", "monkey", "rabbit", "falcon",
    "shadow", "thunder", "volcano", "diamond", "crystal", "galaxy", "laptop", "camera", "kitchen", "blanket", "popcorn",
]

INVISIBLE = "​‌‍⁠"
INVISIBLE_RE = re.compile(f"[{INVISIBLE}﻿]")
MAX_CHARS_PER_SECOND = 14  # about 170 words a minute; faster than that is pasting


def disguise(phrase: str, rng: random.Random = random) -> str:
    """The phrase with invisible characters inside its words, so copying it copies them too."""
    out = []
    for word in phrase.split(" "):
        if len(word) > 2:
            cut = rng.randint(1, len(word) - 1)
            word = word[:cut] + rng.choice(INVISIBLE) + word[cut:]
        out.append(word)
    return " ".join(out)


def normalize(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return " ".join(text.split())


def check_typed(typed: str, phrase: str, seconds: float) -> str:
    """"ok", "copied" (it has the invisible characters, or came faster than anyone types) or "wrong"."""
    if INVISIBLE_RE.search(typed):
        return "copied"
    if normalize(typed) != normalize(phrase):
        return "wrong"
    if seconds < len(phrase) / MAX_CHARS_PER_SECOND:
        return "copied"
    return "ok"


def scramble(word: str, rng: random.Random = random) -> str:
    letters = list(word)
    while True:
        rng.shuffle(letters)
        if "".join(letters) != word:
            return " ".join(letters).upper()


EMOJIS = ["🍎", "🍅", "🍒", "🍓", "🔴"]  # lookalikes, so counting takes a careful look
LONG_WORDS = [
    "chocolate", "spaghetti", "pineapple", "astronaut", "trampoline", "dinosaur", "hurricane", "submarine",
    "volleyball", "skateboard", "microwave", "telescope", "butterfly", "lighthouse", "parachute", "basketball",
    "strawberry", "headphones", "cheeseburger", "grandmother", "playground", "watermelon", "snowboard", "detective",
]
TASK_KINDS = ["type", "unscramble", "math", "count", "backwards", "memory",
              "sequence", "missing", "oddone", "blanks", "capitals", "letters"]
TASK_NAMES = {"type": "Type it", "unscramble": "Unscramble", "math": "Quick maths", "count": "Count",
              "backwards": "Backwards", "memory": "Memory", "sequence": "Remember the order", "missing": "Missing number",
              "oddone": "Odd one out", "blanks": "Fill the blanks", "capitals": "Capitals only", "letters": "Letter count"}
PRIVATE_KINDS = {"memory", "sequence"}  # they hide the prompt after a moment, so they can't be drops
LOOKALIKES = [("🍎", "🍅"), ("😀", "😃"), ("🐶", "🐕"), ("🌑", "🌚"), ("🟥", "🟧"), ("💙", "💜"), ("🍊", "🍑")]


def make_task(kind: str | None = None, rng: random.Random = random) -> dict:
    """A mini-task: "prompt" to show, "answer" to type, and for memory tasks, "hidden" (shown once the code is gone).
    Typing tasks are checked for copy-paste."""
    kind = kind or rng.choice(TASK_KINDS)
    if kind == "type":
        first, second = rng.sample(PHRASES, 2)
        phrase = f"{first} and {second}"
        return {"kind": kind, "prompt": f"Type this:\n**{disguise(phrase, rng)}**", "answer": phrase, "seconds": TYPE_SECONDS}
    if kind == "unscramble":
        word = rng.choice(LONG_WORDS)
        return {"kind": kind, "prompt": f"Unscramble: **{scramble(word, rng)}**", "answer": word}
    if kind == "math":
        a, b, c, d = rng.randint(12, 25), rng.randint(3, 9), rng.randint(10, 99), rng.randint(5, 50)
        return {"kind": kind, "prompt": f"Quick maths: **{a} × {b} + {c} − {d}**", "answer": str(a * b + c - d)}
    if kind == "count":
        target = rng.choice(EMOJIS)
        row = [rng.choice(EMOJIS) for _ in range(rng.randint(20, 30))]
        return {"kind": kind, "prompt": f"How many {target} are here?\n{''.join(row)}", "answer": str(row.count(target))}
    if kind == "backwards":
        word = rng.choice(LONG_WORDS)
        return {"kind": kind, "prompt": f"Type this backwards: **{disguise(word, rng)}**", "answer": word[::-1]}
    if kind == "sequence":
        words = rng.sample(WORDS, 5)
        return {"kind": kind, "prompt": f"Remember these, in order: **{' '.join(words)}**",
                "hidden": "Now type the 5 words in the same order.", "answer": " ".join(words), "show": 3}
    if kind == "missing":
        start, step, length = rng.randint(2, 30), rng.randint(3, 13), 6
        if rng.random() < 0.3:
            numbers = [start * 2 ** i for i in range(length)] if start < 10 else [3 * 2 ** i for i in range(length)]
        else:
            numbers = [start + step * i for i in range(length)]
        gap = rng.randint(1, length - 2)
        shown = ", ".join("__" if i == gap else str(n) for i, n in enumerate(numbers))
        return {"kind": kind, "prompt": f"What's missing? **{shown}**", "answer": str(numbers[gap])}
    if kind == "oddone":
        same, odd = rng.choice(LOOKALIKES)
        if rng.random() < 0.5:
            same, odd = odd, same
        row = [same] * rng.randint(10, 14)
        spot = rng.randint(0, len(row) - 1)
        row[spot] = odd
        return {"kind": kind, "prompt": f"Which one is different? Type its position (1 is the first).\n{''.join(row)}",
                "answer": str(spot + 1)}
    if kind == "blanks":
        word = rng.choice(LONG_WORDS)
        shown = "".join("_" if ch in "aeiou" else ch for ch in word)
        return {"kind": kind, "prompt": f"Fill the blanks: **{shown.upper()}**", "answer": word}
    if kind == "capitals":
        word = rng.choice(LONG_WORDS)
        while True:
            mixed = "".join(ch.upper() if rng.random() < 0.45 else ch for ch in word)
            caps = "".join(ch for ch in mixed if ch.isupper())
            if 3 <= len(caps) < len(word):
                break
        return {"kind": kind, "prompt": f"Type only the capital letters: **{disguise(mixed, rng)}**", "answer": caps}
    if kind == "letters":
        sentence = rng.choice(PHRASES)
        letter = rng.choice([ch for ch in "aeinorst" if sentence.count(ch) >= 2] or ["e"])
        return {"kind": kind, "prompt": f"How many **{letter.upper()}**s are in this?\n*{disguise(sentence, rng)}*",
                "answer": str(sentence.count(letter))}
    code = "".join(rng.choice("0123456789") for _ in range(8))
    return {"kind": "memory", "prompt": f"Remember this code: **{' '.join(code)}**", "hidden": "Now type the code from memory.",
            "answer": code}


def check_answer(task: dict, typed: str, seconds: float) -> str:
    """"ok", "copied" or "wrong" for an answer to a task."""
    if task["kind"] == "type":
        return check_typed(typed, task["answer"], seconds)
    if INVISIBLE_RE.search(typed):
        return "copied"
    return "ok" if normalize(typed).replace(" ", "") == normalize(task["answer"]).replace(" ", "") else "wrong"


def make_drop(rng: random.Random = random) -> dict:
    """A drop challenge (any task but the memory ones, which need a private message)."""
    return make_task(rng.choice([k for k in TASK_KINDS if k not in PRIVATE_KINDS]), rng)


BEGGARS = [
    ("a sweaty gym bro", "here's some aura, lock in"), ("ben's grandma", "go buy yourself something nice"),
    ("a guy who's been in the casino for 9 hours", "take it before i lose it"), ("a lost tourist", "is this the bathroom"),
    ("Kim John-Un", "the supreme leader is generous today"), ("the bot itself", "don't tell anyone"),
    ("a random npc", "..."), ("a kid with a lemonade stand", "that's all my profits"),
]
BEG_COOLDOWN_MINUTES = int(os.getenv("BEG_COOLDOWN_MINUTES") or 60)


def beg(rng: random.Random = random) -> tuple[int, str]:
    """(aura, what happened): usually a little, sometimes nothing, now and then a lot."""
    who, line = rng.choice(BEGGARS)
    roll = rng.random()
    if roll < 0.35:
        return 0, f"**{who}** walked right past you."
    amount = rng.randint(15, 25) if roll > 0.92 else rng.randint(2, 10)
    return amount, f"**{who}** gave you {fmt(amount)}. \"{line}\""


class Work:
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.workers: set[tuple[int, int]] = set()  # (channel, user) in a shift
        self.drop_channels: set[int] = set()         # channels with a drop going
        self.last_drop: dict[int, float] = {}         # guild -> monotonic time

    def busy(self, message: discord.Message) -> bool:
        """Whether this message is part of a shift or a drop (so it skips moderation and chat replies)."""
        return (message.channel.id, message.author.id) in self.workers or message.channel.id in self.drop_channels

    # ---- /work ----

    def shift_problem(self, member: discord.Member, now: float) -> str | None:
        key = f"work:{member.guild.id}:{member.id}"
        raw = (self.store.get_state(key) or "0:0:")
        last, count, day = raw.split(":")
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        count = int(count) if day == today else 0
        wait = float(last) - now
        if wait > 0:
            return f"You can work again <t:{int(float(last))}:R>."
        if count >= WORK_SHIFTS_PER_DAY:
            return f"You've done your {WORK_SHIFTS_PER_DAY} shifts today. Come back tomorrow."
        return None

    def end_shift(self, member: discord.Member, now: float, cooldown_minutes: int) -> None:
        key = f"work:{member.guild.id}:{member.id}"
        raw = (self.store.get_state(key) or "0:0:")
        _, count, day = raw.split(":")
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        count = (int(count) if day == today else 0) + 1
        self.store.set_state(key, f"{now + cooldown_minutes * 60}:{count}:{today}")

    async def shift(self, interaction: discord.Interaction) -> None:
        member, channel = interaction.user, interaction.channel
        now = time.time()
        if (channel.id, member.id) in self.workers:
            await interaction.response.send_message("Finish the shift you're on first.", ephemeral=True)
            return
        if problem := self.shift_problem(member, now):
            await interaction.response.send_message(problem, ephemeral=True)
            return
        welcome(self.store, member)
        tasks = [make_task(kind) for kind in random.sample(TASK_KINDS, WORK_PHRASES)]
        key = (channel.id, member.id)
        self.workers.add(key)
        earned, done, caught = 0, 0, False
        try:
            for i, task in enumerate(tasks, 1):
                text = (f"🧹 **Shift started:** {WORK_PHRASES} tasks, and you only get paid if you get them **all** right. "
                        f"Type each answer in this channel (only you see these, and your answers get cleaned up). "
                        f"Copy-paste gets caught.\n\n"
                        if i == 1 else "") + f"**{i}/{WORK_PHRASES} · {TASK_NAMES[task['kind']]}** ({task.get('seconds', WORK_SECONDS)}s): {task['prompt']}"
                if i == 1:
                    await interaction.response.send_message(text, ephemeral=True)
                    shown_message = await interaction.original_response()
                else:
                    shown_message = await interaction.followup.send(text, ephemeral=True, wait=True)
                if task.get("hidden"):
                    await asyncio.sleep(task.get("show", 2))
                    try:
                        await shown_message.edit(content=text.replace(task["prompt"], task["hidden"]))
                    except discord.HTTPException:
                        pass
                shown = time.monotonic()
                try:
                    typed = await self.bot.wait_for(
                        "message", timeout=task.get("seconds", WORK_SECONDS),
                        check=lambda m: m.channel.id == channel.id and m.author.id == member.id,
                    )
                except asyncio.TimeoutError:
                    await interaction.followup.send("⏰ Too slow. Shift over.", ephemeral=True)
                    break
                verdict = check_answer(task, typed.content, time.monotonic() - shown)
                try:
                    await typed.delete()  # keep the channel clean
                except discord.HTTPException:
                    pass
                if verdict == "copied":
                    caught = True
                    await interaction.followup.send(
                        f"🚨 Caught copy-pasting. No pay for this shift, and no work for {CAUGHT_COOLDOWN_MINUTES} minutes.", ephemeral=True)
                    earned = 0
                    break
                if verdict == "wrong":
                    await interaction.followup.send(f"❌ That's not it (it was **{task['answer']}**). Shift over.", ephemeral=True)
                    break
                earned += random.randint(*WORK_PAY)
                done += 1
        finally:
            self.workers.discard(key)
        self.end_shift(member, time.time(), CAUGHT_COOLDOWN_MINUTES if caught else WORK_COOLDOWN_MINUTES)
        if done == WORK_PHRASES:
            earned += WORK_BONUS
            self.store.add_aura(member.guild.id, member.id, member.display_name, earned, "work", f"did all {WORK_PHRASES} tasks")
            await interaction.followup.send(
                f"✅ Shift done: **+{fmt(earned)}**. Balance {fmt(balance(self.store, member.guild.id, member.id))}.", ephemeral=True)
        else:
            earned = 0
            if not caught:
                await interaction.followup.send(f"No pay: you finished {done} of {WORK_PHRASES}. It's all or nothing.", ephemeral=True)
        log.info("Work: %s typed %d/%d%s, +%d", member, done, WORK_PHRASES, " (caught pasting)" if caught else "", earned)

    # ---- aura drops ----

    def maybe_drop(self, message: discord.Message) -> bool:
        """Starts a drop now and then when people are chatting in the drop channel; True if it did."""
        if not DROPS_ENABLED or message.channel.id != DROP_CHANNEL_ID or message.channel.id in self.drop_channels:
            return False
        now = time.monotonic()
        if now - self.last_drop.get(message.guild.id, -1e9) < DROP_GAP_MINUTES * 60 or random.random() >= DROP_CHANCE:
            return False
        self.last_drop[message.guild.id] = now
        self.bot.spawn(self.drop(message.channel))
        return True

    async def drop(self, channel, challenge: dict | None = None) -> discord.Member | None:
        challenge = challenge or make_drop()
        prize = random.randint(*DROP_PAY)
        self.drop_channels.add(channel.id)
        try:
            embed = card(channel.guild.me, f"💸 Aura drop · {TASK_NAMES[challenge['kind']]}",
                         f"{challenge['prompt']}\n\nFirst to get it wins **{fmt(prize)}**.", color=PLAYING)
            embed.set_footer(text=f"{DROP_SECONDS} seconds · copy-paste doesn't count")
            post = await channel.send(embed=embed)
            started = time.monotonic()

            def right(m: discord.Message) -> bool:
                if m.channel.id != channel.id or m.author.bot:
                    return False
                return check_answer(challenge, m.content, time.monotonic() - started) == "ok"
            try:
                winner_message = await self.bot.wait_for("message", timeout=DROP_SECONDS, check=right)
            except asyncio.TimeoutError:
                await post.edit(embed=card(channel.guild.me, "💸 Aura drop", f"{challenge['prompt']}\n\nNobody got it. "
                                           f"It was **{challenge['answer']}**."))
                return None
        finally:
            self.drop_channels.discard(channel.id)
        winner = winner_message.author
        welcome(self.store, winner)
        self.store.add_aura(channel.guild.id, winner.id, winner.display_name, prize, "drop", f"won a {challenge['kind']} drop")
        await post.edit(embed=card(winner, "💸 Aura drop", f"{challenge['prompt']}\n\n**{winner.display_name}** got it: "
                                   f"**+{fmt(prize)}**", color=WIN))
        try:
            await winner_message.add_reaction("💸")
        except discord.HTTPException:
            pass
        log.info("Drop: %s won %d (%s)", winner, prize, challenge["kind"])
        return winner


@app_commands.command(name="work", description="Type a few phrases for free aura (a few shifts a day, no copy-paste)")
@app_commands.guild_only()
async def work_cmd(interaction: discord.Interaction) -> None:
    await interaction.client.work.shift(interaction)


@app_commands.command(name="beg", description="Beg strangers for a little aura (every 30 minutes)")
@app_commands.guild_only()
async def beg_cmd(interaction: discord.Interaction) -> None:
    store, member, now = interaction.client.store, interaction.user, time.time()
    key = f"beg:{member.guild.id}:{member.id}"
    last = float(store.get_state(key) or 0)
    if now - last < BEG_COOLDOWN_MINUTES * 60:
        await interaction.response.send_message(f"Nobody's falling for it again yet. Try <t:{int(last + BEG_COOLDOWN_MINUTES * 60)}:R>.", ephemeral=True)
        return
    store.set_state(key, str(now))
    welcome(store, member)
    amount, what = beg()
    if amount:
        store.add_aura(member.guild.id, member.id, member.display_name, amount, "beg", "begged")
    embed = card(member, "🥺 Begging", what, color=WIN if amount else PLAYING)
    embed.set_footer(text=f"Balance {fmt(balance(store, member.guild.id, member.id))}")
    await interaction.response.send_message(embed=embed)


COMMANDS = [work_cmd, beg_cmd]
