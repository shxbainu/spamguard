"""/duel: two people bet the same aura and play one of a few quick head-to-head games for the pot. Most of them
come down to skill (reactions, reading the other person, quick maths, spelling) rather than a coin flip."""
from __future__ import annotations

import asyncio
import random
import time

import discord
from discord import app_commands

from cards import EVEN, PLAYING, WIN, card
from casino import balance, fmt, game_cooldown, in_casino, parse_bet, pay, welcome

# (channel id, user id) of people racing in an unscramble duel, so their guesses skip the AI check.
busy: set[tuple[int, int]] = set()

GAME_NAMES = {
    "quickdraw": "🤠 Quick draw",
    "rps": "✂️ Rock paper scissors",
    "math": "🧮 Math race",
    "unscramble": "🔤 Unscramble race",
    "luck": "🎲 Pure luck",
}
RULES = {
    "quickdraw": "Wait for **DRAW!**, then hit the button first. Click early and you lose.",
    "rps": "Both pick in secret. Ties go again.",
    "math": "First to tap the right answer wins. Tap a wrong one and you lose.",
    "unscramble": "First to type the unscrambled word in this channel wins.",
    "luck": "A coin flip decides it.",
}
SKILL_GAMES = ["quickdraw", "rps", "math", "unscramble"]

WORDS = [
    "grass", "aura", "casino", "pizza", "rocket", "planet", "guitar", "dragon", "castle", "jungle", "pirate", "zombie",
    "wizard", "banana", "orange", "cookie", "hockey", "soccer", "tennis", "goblin", "turtle", "monkey", "rabbit",
    "spider", "falcon", "shadow", "thunder", "volcano", "diamond", "crystal", "galaxy", "laptop", "camera", "button",
    "basket", "candle", "garden", "island", "mirror", "pencil", "silver", "winter", "summer", "yellow", "purple",
    "kitchen", "blanket", "chicken", "popcorn", "noodle",
]

RPS = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


def rps_winner(a: str, b: str) -> int:
    """1 if a wins, -1 if b wins, 0 for a tie."""
    return 0 if a == b else 1 if BEATS[a] == b else -1


def math_problem(rng: random.Random = random) -> tuple[str, int, list[int]]:
    """(question, answer, four shuffled choices including the answer)."""
    kind = rng.choice(["mul_add", "add_sub", "mul"])
    if kind == "mul_add":
        a, b, c = rng.randint(3, 12), rng.randint(3, 12), rng.randint(5, 40)
        question, answer = f"{a} × {b} + {c}", a * b + c
    elif kind == "add_sub":
        a, b, c = rng.randint(20, 99), rng.randint(20, 99), rng.randint(5, 40)
        question, answer = f"{a} + {b} − {c}", a + b - c
    else:
        a, b = rng.randint(6, 19), rng.randint(6, 15)
        question, answer = f"{a} × {b}", a * b
    choices = {answer}
    while len(choices) < 4:
        wrong = answer + rng.choice([-1, 1]) * rng.choice([1, 2, 3, 5, 10, rng.randint(4, 12)])
        if wrong > 0:
            choices.add(wrong)
    choices = list(choices)
    rng.shuffle(choices)
    return question, answer, choices


def scramble(word: str, rng: random.Random = random) -> str:
    letters = list(word)
    while True:
        rng.shuffle(letters)
        if "".join(letters) != word:
            return " ".join(letters).upper()


class DuelGame(discord.ui.View):
    """One duel game after both bets are in. Pays the winner the pot, or gives both bets back if nobody wins."""

    game = "luck"

    def __init__(self, challenger: discord.Member, target: discord.Member, bet: int, store, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.challenger, self.target, self.bet, self.store = challenger, target, bet, store
        self.players = (challenger, target)
        self.message: discord.Message | None = None
        self.done = False

    def other(self, member) -> discord.Member:
        return self.target if member.id == self.challenger.id else self.challenger

    def playing(self, body: str) -> discord.Embed:
        embed = card(self.challenger, f"⚔️ {GAME_NAMES[self.game]}", body, color=PLAYING)
        embed.add_field(name="Players", value=f"{self.challenger.display_name} vs {self.target.display_name}", inline=True)
        embed.add_field(name="Pot", value=fmt(self.bet * 2), inline=True)
        return embed

    def finish(self, winner: discord.Member, how: str) -> discord.Embed:
        """Pays the winner and returns the result card."""
        self.done = True
        self.stop()
        loser = self.other(winner)
        pay(self.store, winner, self.bet * 2, "duel", f"beat {loser.display_name}")
        guild_id = winner.guild.id
        embed = card(winner, f"⚔️ {GAME_NAMES[self.game]}", f"## {winner.display_name} wins\n{how}", color=WIN)
        embed.add_field(name="Pot", value=fmt(self.bet * 2), inline=True)
        embed.add_field(name=winner.display_name, value=fmt(balance(self.store, guild_id, winner.id)), inline=True)
        embed.add_field(name=loser.display_name, value=fmt(balance(self.store, guild_id, loser.id)), inline=True)
        return embed

    def refund(self, why: str) -> discord.Embed:
        """Nobody won: both get their bet back."""
        self.done = True
        self.stop()
        for member in self.players:
            pay(self.store, member, self.bet, "duel", "refund")
        embed = card(self.challenger, f"⚔️ {GAME_NAMES[self.game]}", f"## Nobody wins\n{why} Both bets went back.", color=EVEN)
        embed.add_field(name="Players", value=f"{self.challenger.display_name} vs {self.target.display_name}", inline=True)
        return embed

    async def show(self, embed: discord.Embed, view: discord.ui.View | None = None) -> None:
        if self.message:
            try:
                await self.message.edit(content=None, embed=embed, view=view)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.challenger.id, self.target.id):
            await interaction.response.send_message("This duel isn't yours. Start one with /duel.", ephemeral=True)
            return False
        if self.done:
            await interaction.response.defer()
            return False
        return True

    async def start(self, interaction: discord.Interaction) -> None:
        """Called from the Accept button, which hasn't been answered yet."""
        winner = random.choice(self.players)
        await interaction.response.edit_message(content=None, embed=self.finish(winner, "The coin picked them."), view=None)

    async def on_timeout(self) -> None:
        if not self.done:
            await self.show(self.refund("Time ran out."))


class QuickDraw(DuelGame):
    game = "quickdraw"

    def __init__(self, *args):
        super().__init__(*args, timeout=30)
        self.drawn_at: float | None = None

    async def start(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=None, embed=self.playing(f"{RULES[self.game]}\n## Wait for it…"), view=self)
        self.message = interaction.message
        asyncio.get_running_loop().create_task(self.draw_later(random.uniform(2.5, 7.0)))

    async def draw_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.done:
            return
        self.drawn_at = time.monotonic()
        self.trigger.label, self.trigger.style = "DRAW!", discord.ButtonStyle.danger
        await self.show(self.playing("## 🔫 DRAW!"), self)

    @discord.ui.button(label="Wait…", style=discord.ButtonStyle.secondary, emoji="🤠")
    async def trigger(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.drawn_at is None:
            winner = self.other(interaction.user)
            embed = self.finish(winner, f"**{interaction.user.display_name}** drew too early.")
        else:
            ms = int((time.monotonic() - self.drawn_at) * 1000)
            embed = self.finish(interaction.user, f"Drew first, about {ms:,} ms after the signal.")
        await interaction.response.edit_message(embed=embed, view=None)


class RockPaperScissors(DuelGame):
    game = "rps"

    def __init__(self, *args):
        super().__init__(*args, timeout=60)
        self.picks: dict[int, str] = {}
        self.ties = 0
        for choice, emoji in RPS.items():
            button = discord.ui.Button(label=choice.title(), emoji=emoji, style=discord.ButtonStyle.primary)
            button.callback = self.picker(choice)
            self.add_item(button)

    def status(self, note: str = "") -> discord.Embed:
        waiting = [m.display_name for m in self.players if m.id not in self.picks]
        line = f"Waiting on **{' and '.join(waiting)}**." if waiting else ""
        return self.playing("\n".join(part for part in (RULES[self.game], note, line) if part))

    async def start(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=None, embed=self.status(), view=self)
        self.message = interaction.message

    def picker(self, choice: str):
        async def pick(interaction: discord.Interaction) -> None:
            if interaction.user.id in self.picks:
                await interaction.response.send_message(f"You already picked {RPS[self.picks[interaction.user.id]]}.", ephemeral=True)
                return
            self.picks[interaction.user.id] = choice
            await interaction.response.send_message(f"You picked {RPS[choice]} {choice}.", ephemeral=True)
            if len(self.picks) < 2:
                await self.show(self.status(), self)
                return
            a, b = self.picks[self.challenger.id], self.picks[self.target.id]
            result = rps_winner(a, b)
            shown = f"{self.challenger.display_name} {RPS[a]} vs {RPS[b]} {self.target.display_name}"
            if result == 0:
                self.picks.clear()
                self.ties += 1
                await self.show(self.status(f"**Tie:** {shown}. Go again."), self)
                return
            winner = self.challenger if result == 1 else self.target
            await self.show(self.finish(winner, shown), None)
        return pick

    async def on_timeout(self) -> None:
        if self.done:
            return
        if len(self.picks) == 1:
            picked = next(m for m in self.players if m.id in self.picks)
            await self.show(self.finish(picked, f"**{self.other(picked).display_name}** never picked."))
        else:
            await self.show(self.refund("Nobody picked in time."))


class MathRace(DuelGame):
    game = "math"

    def __init__(self, *args):
        super().__init__(*args, timeout=30)
        self.question, self.answer, choices = math_problem()
        for number in choices:
            button = discord.ui.Button(label=str(number), style=discord.ButtonStyle.secondary)
            button.callback = self.answerer(number)
            self.add_item(button)

    async def start(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=None, embed=self.playing(f"{RULES[self.game]}\n## {self.question} = ?"), view=self)
        self.message = interaction.message

    def answerer(self, number: int):
        async def answer(interaction: discord.Interaction) -> None:
            if number == self.answer:
                embed = self.finish(interaction.user, f"**{self.question} = {self.answer}**")
            else:
                embed = self.finish(self.other(interaction.user), f"**{interaction.user.display_name}** said {number}. It was **{self.question} = {self.answer}**.")
            await interaction.response.edit_message(embed=embed, view=None)
        return answer


class Unscramble(DuelGame):
    game = "unscramble"
    TIME = 45

    def __init__(self, *args):
        super().__init__(*args, timeout=None)
        self.word = random.choice(WORDS)
        self.scrambled = scramble(self.word)

    async def start(self, interaction: discord.Interaction) -> None:
        embed = self.playing(f"{RULES[self.game]}\n## {self.scrambled}")
        embed.set_footer(text=f"{self.TIME} seconds")
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        self.message = interaction.message
        channel_id = interaction.channel_id
        ids = {m.id for m in self.players}
        keys = {(channel_id, m.id) for m in self.players}
        busy.update(keys)
        try:
            guess = await interaction.client.wait_for(
                "message", timeout=self.TIME,
                check=lambda m: m.channel.id == channel_id and m.author.id in ids and m.content.strip().lower() == self.word,
            )
        except asyncio.TimeoutError:
            await self.show(self.refund(f"Nobody got it. The word was **{self.word}**."))
        else:
            winner = next(m for m in self.players if m.id == guess.author.id)
            await self.show(self.finish(winner, f"Got **{self.word}** first."))
            try:
                await guess.add_reaction("✅")
            except discord.HTTPException:
                pass
        finally:
            busy.difference_update(keys)


GAMES: dict[str, type[DuelGame]] = {
    "quickdraw": QuickDraw, "rps": RockPaperScissors, "math": MathRace, "unscramble": Unscramble, "luck": DuelGame,
}


class Challenge(discord.ui.View):
    def __init__(self, challenger: discord.Member, target: discord.Member, bet: int, store, game: str):
        super().__init__(timeout=60)
        self.challenger, self.target, self.bet, self.store, self.game = challenger, target, bet, store, game
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("This duel isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        store, bet = self.store, self.bet
        welcome(store, self.target)
        guild_id = self.target.guild.id
        if balance(store, guild_id, self.challenger.id) < bet:
            await interaction.response.edit_message(content=None, embed=discord.Embed(title="⚔️ Duel off", description=f"{self.challenger.display_name} can't cover the bet anymore."), view=None)
            self.stop()
            return
        if not store.spend_aura(guild_id, self.target.id, self.target.display_name, bet, "duel", "bet"):
            await interaction.response.send_message(f"You need {fmt(bet)} to accept.", ephemeral=True)
            return
        store.spend_aura(guild_id, self.challenger.id, self.challenger.display_name, bet, "duel", "bet")
        self.stop()
        game = self.game if self.game in GAMES else random.choice(SKILL_GAMES)
        await GAMES[game](self.challenger, self.target, bet, store).start(interaction)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content=None, embed=discord.Embed(title="⚔️ Duel declined", description=f"{self.target.display_name} didn't want it."), view=None)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content=None, embed=discord.Embed(title="⚔️ Duel off", description=f"{self.target.display_name} didn't answer."), view=None)
            except discord.HTTPException:
                pass


@app_commands.command(name="duel", description="Challenge someone: you both bet the same aura and play a quick game for it")
@app_commands.describe(user="Who to challenge", bet="How much aura each (a number, half or all)", game="Which game (default: a random skill game)")
@app_commands.choices(game=[
    app_commands.Choice(name="Random skill game", value="random"),
    app_commands.Choice(name="Quick draw: fastest reaction", value="quickdraw"),
    app_commands.Choice(name="Rock paper scissors", value="rps"),
    app_commands.Choice(name="Math race", value="math"),
    app_commands.Choice(name="Unscramble race: type the word", value="unscramble"),
    app_commands.Choice(name="Pure luck: 50/50", value="luck"),
])
@app_commands.guild_only()
@game_cooldown
async def duel_cmd(interaction: discord.Interaction, user: discord.Member, bet: str, game: str = "random") -> None:
    store, member = interaction.client.store, interaction.user
    problem = in_casino(interaction)
    if not problem and (user.bot or user.id == member.id):
        problem = "Pick someone else to duel."
    if not problem:
        welcome(store, member)
        amount = parse_bet(bet, balance(store, member.guild.id, member.id))
        problem = amount if isinstance(amount, str) else None
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    view = Challenge(member, user, amount, store, game)
    which = GAME_NAMES.get(game, "🎮 A random skill game")
    rules = f"\n{RULES[game]}" if game in RULES else ""
    embed = card(member, "⚔️ Duel challenge", f"**{member.display_name}** wants to duel **{user.display_name}**.\n**{which}**{rules}", color=PLAYING)
    embed.add_field(name="Bet each", value=fmt(amount), inline=True)
    embed.add_field(name="Pot", value=fmt(amount * 2), inline=True)
    embed.set_footer(text=f"{user.display_name} has 60 seconds to answer")
    await interaction.response.send_message(
        user.mention, embed=embed, view=view, allowed_mentions=discord.AllowedMentions(users=[user]),
    )
    view.message = await interaction.original_response()


COMMANDS = [duel_cmd]
