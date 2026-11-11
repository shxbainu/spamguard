"""/duel: two people bet the same aura and play a quick head-to-head game for the pot: a quick draw, stopping a
hidden clock closest to 10 seconds, split or steal, or a plain coin flip."""
from __future__ import annotations

import asyncio
import random
import time

import discord
from discord import app_commands

from cards import EVEN, LOSS, PLAYING, WIN, card
from casino import balance, fmt, game_cooldown, in_casino, parse_bet, pay, welcome

GAME_NAMES = {
    "quickdraw": "🤠 Quick draw",
    "tenseconds": "⏱️ 10 seconds",
    "splitsteal": "🤝 Split or steal",
    "luck": "🎲 Pure luck",
}
RULES = {
    "quickdraw": "Wait for **DRAW!**, then hit the button first. Click early and you lose.",
    "tenseconds": "The clock starts now, but you can't see it. Hit **Stop** when you think exactly 10 seconds have "
                  "passed. Closest wins.",
    "splitsteal": "Talk it out, then pick in secret. Both split: you each get your bet back **plus 25%**. One steals: "
                  "they get +2x their bet. Both steal: you both lose it all.",
    "luck": "A coin flip decides it.",
}
SKILL_GAMES = ["quickdraw", "tenseconds", "splitsteal"]

TARGET_SECONDS = 10.0
SPLIT_BONUS = 1.25
WIN_MULTIPLIER = 3  # the winner gets their bet back plus double it (+2x); the loser loses their bet
CLICK_WINDOW = 0.6  # seconds to wait for the other quick-draw click, so the earlier one by Discord's clock wins


def stamp(when) -> float | None:
    """A Discord timestamp (a click's or an edit's) in seconds, or None."""
    return when.timestamp() if when is not None else None


def split_steal_payouts(a: str, b: str, bet: int) -> tuple[int, int, str]:
    """(what A gets, what B gets, what happened) for picks of "split" or "steal"."""
    if a == b == "split":
        each = int(bet * SPLIT_BONUS)
        return each, each, "Both split."
    if a == b == "steal":
        return 0, 0, "Both stole, so nobody gets anything."
    return (bet * WIN_MULTIPLIER, 0, "") if a == "steal" else (0, bet * WIN_MULTIPLIER, "")


class DuelGame(discord.ui.View):
    """One duel game after both bets are in."""

    game = "luck"

    def __init__(self, challenger: discord.Member, target: discord.Member, bet: int, store, timeout: float | None = 60):
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
        embed.add_field(name="Winner gets", value=f"+{fmt(self.bet * (WIN_MULTIPLIER - 1))}", inline=True)
        return embed

    def settle(self, payouts: dict[int, int], headline: str, how: str, lead: discord.Member | None = None) -> discord.Embed:
        """Pays everyone what they get back and returns the result card (green if someone came out ahead)."""
        self.done = True
        self.stop()
        guild_id = self.challenger.guild.id
        for member in self.players:
            pay(self.store, member, payouts.get(member.id, 0), "duel", f"vs {self.other(member).display_name}")
        up = any(payouts.get(m.id, 0) > self.bet for m in self.players)
        nothing = not any(payouts.values())
        embed = card(lead or self.challenger, f"⚔️ {GAME_NAMES[self.game]}", f"## {headline}\n{how}".strip(),
                     color=WIN if up else LOSS if nothing else EVEN)
        for member in self.players:
            net = payouts.get(member.id, 0) - self.bet
            embed.add_field(name=member.display_name, value=f"{net:+,} · {fmt(balance(self.store, guild_id, member.id))}", inline=True)
        return embed

    def finish(self, winner: discord.Member, how: str) -> discord.Embed:
        return self.settle({winner.id: self.bet * WIN_MULTIPLIER}, f"{winner.display_name} wins", how, lead=winner)

    def refund(self, why: str) -> discord.Embed:
        return self.settle({m.id: self.bet for m in self.players}, "Nobody wins", f"{why} Both bets went back.")

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
    """Timed with Discord's own clock: when Discord received each click versus when the DRAW! edit landed, so
    neither player's lag to the bot counts, and two near-simultaneous clicks go to whoever was really first."""

    game = "quickdraw"

    def __init__(self, *args):
        super().__init__(*args, timeout=30)
        self.drawn_at: float | None = None  # Discord's time of the DRAW! edit
        self.drawn = asyncio.Event()
        self.drawing = False
        self.clicks: dict[int, float] = {}  # user -> when Discord got their click
        self.judging = False

    async def start(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=None, embed=self.playing(f"{RULES[self.game]}\n## Wait for it…"), view=self)
        self.message = interaction.message
        asyncio.get_running_loop().create_task(self.draw_later(random.uniform(2.5, 7.0)))

    async def draw_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.done:
            return
        self.drawing = True
        self.trigger.label, self.trigger.style = "DRAW!", discord.ButtonStyle.danger
        sent = None
        if self.message:
            try:
                sent = await self.message.edit(content=None, embed=self.playing("## 🔫 DRAW!"), view=self)
            except discord.HTTPException:
                pass
        self.drawn_at = stamp(getattr(sent, "edited_at", None)) or time.time()
        self.drawn.set()

    @discord.ui.button(label="Wait…", style=discord.ButtonStyle.secondary, emoji="🤠")
    async def trigger(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        clicked = stamp(interaction.created_at) or time.time()
        if self.drawing and not self.drawn.is_set():
            await self.drawn.wait()  # the DRAW! edit is on its way; compare once its time is known
        if self.drawn_at is None or clicked < self.drawn_at:
            embed = self.finish(self.other(interaction.user), f"**{interaction.user.display_name}** drew too early.")
            await interaction.response.edit_message(embed=embed, view=None)
            return
        if interaction.user.id in self.clicks:
            await interaction.response.defer()
            return
        self.clicks[interaction.user.id] = clicked
        await interaction.response.defer()
        if self.judging:
            return
        self.judging = True
        await asyncio.sleep(CLICK_WINDOW)  # let a near-simultaneous click arrive
        if self.done:
            return
        first = min(self.clicks, key=self.clicks.get)
        winner = next(m for m in self.players if m.id == first)
        ms = round((self.clicks[first] - self.drawn_at) * 1000)
        how = f"Drew in **{ms:,} ms**."
        if len(self.clicks) == 2:
            loser = self.other(winner)
            how += f" {loser.display_name} took {round((self.clicks[loser.id] - self.drawn_at) * 1000):,} ms."
        await self.show(self.finish(winner, how))


def ten_seconds_result(times: dict[int, float]) -> tuple[int | None, dict[int, float]]:
    """(who was closest to 10 seconds, or None on an exact tie, and how far off each person was)."""
    off = {who: abs(seconds - TARGET_SECONDS) for who, seconds in times.items()}
    ranked = sorted(off, key=off.get)
    if len(ranked) == 2 and abs(off[ranked[0]] - off[ranked[1]]) < 0.001:
        return None, off
    return ranked[0], off


class TenSeconds(DuelGame):
    game = "tenseconds"

    def __init__(self, *args):
        super().__init__(*args, timeout=25)
        self.started_at = 0.0
        self.times: dict[int, float] = {}

    def status(self) -> discord.Embed:
        stopped = [m.display_name for m in self.players if m.id in self.times]
        line = f"\n**{' and '.join(stopped)}** stopped." if stopped else ""
        return self.playing(f"{RULES[self.game]}\n## ⏱️ …{line}")

    async def start(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=None, embed=self.status(), view=self)
        self.message = interaction.message
        # Timed with Discord's clock: from when the edit landed to when Discord got each click, so nobody's lag to
        # the bot counts.
        self.started_at = time.time()
        try:
            shown = await interaction.original_response()
            self.started_at = stamp(getattr(shown, "edited_at", None)) or self.started_at
        except discord.HTTPException:
            pass

    def result(self) -> discord.Embed:
        lines = []
        for member in self.players:
            if member.id in self.times:
                seconds = self.times[member.id]
                lines.append(f"**{member.display_name}**: {seconds:.2f}s ({seconds - TARGET_SECONDS:+.2f})")
            else:
                lines.append(f"**{member.display_name}**: never stopped")
        shown = "\n".join(lines)
        if not self.times:
            return self.refund("Nobody stopped the clock.")
        if len(self.times) == 1:
            return self.finish(next(m for m in self.players if m.id in self.times), shown)
        winner, _ = ten_seconds_result(self.times)
        if winner is None:
            return self.refund(f"{shown}\nExact tie.")
        return self.finish(next(m for m in self.players if m.id == winner), shown)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.primary, emoji="⏱️")
    async def stop_clock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id in self.times:
            await interaction.response.send_message("You already stopped.", ephemeral=True)
            return
        self.times[interaction.user.id] = (stamp(interaction.created_at) or time.time()) - self.started_at
        if len(self.times) == 2:
            await interaction.response.edit_message(embed=self.result(), view=None)
        else:
            await interaction.response.edit_message(embed=self.status(), view=self)

    async def on_timeout(self) -> None:
        if not self.done:
            await self.show(self.result())


class SplitOrSteal(DuelGame):
    game = "splitsteal"

    def __init__(self, *args):
        super().__init__(*args, timeout=60)
        self.picks: dict[int, str] = {}
        for choice, emoji, style in (("split", "🤝", discord.ButtonStyle.success), ("steal", "🔪", discord.ButtonStyle.danger)):
            button = discord.ui.Button(label=choice.title(), emoji=emoji, style=style)
            button.callback = self.picker(choice)
            self.add_item(button)

    def status(self) -> discord.Embed:
        waiting = [m.display_name for m in self.players if m.id not in self.picks]
        line = f"Waiting on **{' and '.join(waiting)}**. You have a minute." if waiting else ""
        return self.playing(f"{RULES[self.game]}\n{line}")

    async def start(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=None, embed=self.status(), view=self)
        self.message = interaction.message

    def reveal(self) -> discord.Embed:
        a = self.picks.get(self.challenger.id, "split")
        b = self.picks.get(self.target.id, "split")
        got_a, got_b, what = split_steal_payouts(a, b, self.bet)
        icon = {"split": "🤝", "steal": "🔪"}
        shown = f"{self.challenger.display_name} {icon[a]} {a} · {icon[b]} {b} {self.target.display_name}"
        missing = [m.display_name for m in self.players if m.id not in self.picks]
        if missing:
            shown += f"\n({' and '.join(missing)} didn't pick, so that counts as split.)"
        if got_a > got_b:
            headline, lead = f"{self.challenger.display_name} stole it", self.challenger
        elif got_b > got_a:
            headline, lead = f"{self.target.display_name} stole it", self.target
        else:
            headline, lead = ("Both split" if got_a else "Both stole"), None
        return self.settle({self.challenger.id: got_a, self.target.id: got_b}, headline, f"{shown}\n{what}".strip(), lead=lead)

    def picker(self, choice: str):
        async def pick(interaction: discord.Interaction) -> None:
            if interaction.user.id in self.picks:
                await interaction.response.send_message(f"You're locked in on {self.picks[interaction.user.id]}.", ephemeral=True)
                return
            self.picks[interaction.user.id] = choice
            await interaction.response.send_message(f"Locked in: **{choice}**. Nobody else can see it.", ephemeral=True)
            await self.show(self.reveal() if len(self.picks) == 2 else self.status(), None if len(self.picks) == 2 else self)
        return pick

    async def on_timeout(self) -> None:
        if not self.done:
            await self.show(self.reveal())


GAMES: dict[str, type[DuelGame]] = {
    "quickdraw": QuickDraw, "tenseconds": TenSeconds, "splitsteal": SplitOrSteal, "luck": DuelGame,
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


@app_commands.command(name="duel", description="Challenge someone: you both bet the same aura and play a game for it")
@app_commands.describe(user="Who to challenge", bet="How much aura each (a number, half or all)", game="Which game (default: a random one)")
@app_commands.choices(game=[
    app_commands.Choice(name="Random", value="random"),
    app_commands.Choice(name="Quick draw: fastest reaction", value="quickdraw"),
    app_commands.Choice(name="10 seconds: stop the hidden clock closest to 10s", value="tenseconds"),
    app_commands.Choice(name="Split or steal", value="splitsteal"),
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
    which = GAME_NAMES.get(game, "🎮 A random game")
    rules = f"\n{RULES[game]}" if game in RULES else ""
    embed = card(member, "⚔️ Duel challenge", f"**{member.display_name}** wants to duel **{user.display_name}**.\n**{which}**{rules}", color=PLAYING)
    embed.add_field(name="Bet each", value=fmt(amount), inline=True)
    embed.add_field(name="Winner gets", value=f"+{fmt(amount * (WIN_MULTIPLIER - 1))}", inline=True)
    embed.set_footer(text=f"{user.display_name} has 60 seconds to answer")
    await interaction.response.send_message(
        user.mention, embed=embed, view=view, allowed_mentions=discord.AllowedMentions(users=[user]),
    )
    view.message = await interaction.original_response()


COMMANDS = [duel_cmd]
