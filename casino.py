"""The casino: aura is the currency. Everyone gets a one-time starting stack, earns more in chat, and can
bet it on /coinflip, /slots, /roulette, /blackjack, /higherlower or a /duel against someone, give it away with
/give, or spend it in the /shop. Bets can be a number, "half" or "all" (also "1k", "2.5k")."""
from __future__ import annotations

import os
import random
import time
from typing import Literal, Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

START_AURA = int(os.getenv("CASINO_START_AURA") or 500)
# If set, games only work in this channel (the shop and /give work anywhere).
CASINO_CHANNEL_ID = int(os.getenv("CASINO_CHANNEL_ID") or 0)
GAME_TIMEOUT = 120  # seconds before an unfinished blackjack or higher/lower game ends itself

# ---- money helpers ------------------------------------------------------------


def fmt(amount: int) -> str:
    return f"{amount:,} aura"


def balance(store, guild_id: int, user_id: int) -> int:
    return store.aura_total(guild_id, user_id)


def welcome(store, member: discord.Member) -> int:
    """Gives someone their one-time starting aura the first time they use the casino; returns how much."""
    key = f"casino_start:{member.guild.id}:{member.id}"
    if START_AURA <= 0 or store.get_state(key):
        return 0
    store.set_state(key, "1")
    store.add_aura(member.guild.id, member.id, member.display_name, START_AURA, "gift", "casino starting aura")
    return START_AURA


def parse_bet(text: str, available: int) -> int | str:
    """The bet in aura, or why it isn't valid. Accepts 250, 1,000, 1k, 2.5k, half and all."""
    text = text.strip().lower().replace(",", "").replace(" ", "")
    if text in ("all", "max", "allin"):
        amount = available
    elif text == "half":
        amount = available // 2
    else:
        try:
            amount = int(float(text[:-1]) * 1000) if text.endswith("k") else int(text)
        except ValueError:
            return "Bet a number, like 100, 1k, half or all."
    if available <= 0:
        return f"You're broke ({fmt(available)}). Earn some aura first."
    if amount < 1:
        return "Bet at least 1 aura."
    if amount > available:
        return f"You only have {fmt(available)}."
    return amount


def in_casino(interaction: discord.Interaction) -> str | None:
    if CASINO_CHANNEL_ID and interaction.channel_id != CASINO_CHANNEL_ID:
        return f"Games only work in <#{CASINO_CHANNEL_ID}>."
    return None


async def place_bet(interaction: discord.Interaction, bet: str, game: str) -> int | None:
    """Takes the bet (sending the reason privately and returning None if it can't)."""
    store, member = interaction.client.store, interaction.user
    problem = in_casino(interaction)
    if problem is None:
        welcome(store, member)
        amount = parse_bet(bet, balance(store, member.guild.id, member.id))
        if isinstance(amount, str):
            problem = amount
        elif not store.spend_aura(member.guild.id, member.id, member.display_name, amount, game, "bet"):
            problem = "You don't have enough aura."
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return None
    return amount


def pay(store, member: discord.Member, amount: int, game: str, reason: str = "win") -> None:
    if amount > 0:
        store.add_aura(member.guild.id, member.id, member.display_name, amount, game, reason)


def result_line(store, member: discord.Member, bet: int, payout: int) -> str:
    net = payout - bet
    if net > 0:
        outcome = f"**+{fmt(net)}**"
    elif net < 0:
        outcome = f"**-{fmt(-net)}**"
    else:
        outcome = "**money back**"
    return f"{outcome} · balance {fmt(balance(store, member.guild.id, member.id))}"


game_cooldown = app_commands.checks.cooldown(1, 3, key=lambda i: i.user.id)

# ---- simple games ---------------------------------------------------------------


@app_commands.command(name="coinflip", description="Flip a coin: call it right and double your bet")
@app_commands.describe(bet="How much aura (a number, half or all)", call="Heads or tails")
@app_commands.guild_only()
@game_cooldown
async def coinflip_cmd(interaction: discord.Interaction, bet: str, call: Literal["heads", "tails"] = "heads") -> None:
    amount = await place_bet(interaction, bet, "coinflip")
    if amount is None:
        return
    store, member = interaction.client.store, interaction.user
    side = random.choice(["heads", "tails"])
    payout = amount * 2 if side == call else 0
    pay(store, member, payout, "coinflip")
    await interaction.response.send_message(
        f"🪙 **{side.title()}!** {member.display_name} called {call}. {result_line(store, member, amount, payout)}"
    )


SLOT_SYMBOLS = ["🍒", "🍋", "🔔", "💎", "7️⃣", "🍑"]
JACKPOT = "🍑"


def slot_payout(reels: list[str], bet: int) -> tuple[int, str]:
    """(payout, what happened). Three 🍑 = 25x, any other three = 10x, a pair = 1.5x (about 97% paid back)."""
    counts = {symbol: reels.count(symbol) for symbol in reels}
    best = max(counts.values())
    if best == 3:
        return (bet * 25, "JACKPOT") if reels[0] == JACKPOT else (bet * 10, "three of a kind")
    if best == 2:
        return bet * 3 // 2, "pair"
    return 0, "nothing"


@app_commands.command(name="slots", description="Spin the slots: pair 1.5x, three of a kind 10x, three 🍑 25x")
@app_commands.describe(bet="How much aura (a number, half or all)")
@app_commands.guild_only()
@game_cooldown
async def slots_cmd(interaction: discord.Interaction, bet: str) -> None:
    amount = await place_bet(interaction, bet, "slots")
    if amount is None:
        return
    store, member = interaction.client.store, interaction.user
    reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
    payout, what = slot_payout(reels, amount)
    pay(store, member, payout, "slots")
    await interaction.response.send_message(
        f"🎰 **[ {' '.join(reels)} ]** {what}. {result_line(store, member, amount, payout)}"
    )


RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def roulette_payout(choice: str, number: int, bet: int) -> int | None:
    """Payout for a European roulette bet, or None if the choice isn't a valid bet."""
    choice = choice.strip().lower()
    if choice.isdigit():
        if not 0 <= int(choice) <= 36:
            return None
        return bet * 36 if int(choice) == number else 0
    if choice == "green":
        return bet * 36 if number == 0 else 0
    wins = {
        "red": number in RED, "black": number != 0 and number not in RED,
        "odd": number % 2 == 1, "even": number != 0 and number % 2 == 0,
        "low": 1 <= number <= 18, "high": 19 <= number <= 36,
    }
    if choice not in wins:
        return None
    return bet * 2 if wins[choice] else 0


@app_commands.command(name="roulette", description="Bet on red, black, odd, even, low, high, green or a number 0-36")
@app_commands.describe(bet="How much aura (a number, half or all)", on="red, black, odd, even, low (1-18), high (19-36), green (0) or a number")
@app_commands.guild_only()
@game_cooldown
async def roulette_cmd(interaction: discord.Interaction, bet: str, on: str) -> None:
    if roulette_payout(on, 0, 1) is None:
        await interaction.response.send_message("Bet on red, black, odd, even, low, high, green or a number from 0 to 36.", ephemeral=True)
        return
    amount = await place_bet(interaction, bet, "roulette")
    if amount is None:
        return
    store, member = interaction.client.store, interaction.user
    number = random.randint(0, 36)
    payout = roulette_payout(on, number, amount)
    color = "🟢" if number == 0 else ("🔴" if number in RED else "⚫")
    pay(store, member, payout, "roulette")
    await interaction.response.send_message(
        f"🎡 The ball lands on **{color} {number}** ({member.display_name} bet on {on.lower()}). "
        f"{result_line(store, member, amount, payout)}"
    )


# ---- cards ------------------------------------------------------------------------

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["♠", "♥", "♦", "♣"]


def draw() -> tuple[str, str]:
    return random.choice(RANKS), random.choice(SUITS)


def show(cards: list[tuple[str, str]]) -> str:
    return " ".join(f"`{rank}{suit}`" for rank, suit in cards)


def hand_value(cards: list[tuple[str, str]]) -> int:
    total = sum(11 if rank == "A" else 10 if rank in "JQK" or rank == "10" else int(rank) for rank, _ in cards)
    aces = sum(rank == "A" for rank, _ in cards)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(cards: list[tuple[str, str]]) -> bool:
    return len(cards) == 2 and hand_value(cards) == 21


def blackjack_payout(player: list, dealer: list, bet: int) -> tuple[int, str]:
    """(payout, result) once both hands are finished."""
    mine, theirs = hand_value(player), hand_value(dealer)
    if mine > 21:
        return 0, "bust"
    if is_blackjack(player) and not is_blackjack(dealer):
        return bet * 5 // 2, "blackjack!"
    if is_blackjack(dealer) and not is_blackjack(player):
        return 0, "dealer blackjack"
    if theirs > 21:
        return bet * 2, "dealer busts"
    if mine > theirs:
        return bet * 2, "you win"
    if mine == theirs:
        return bet, "push"
    return 0, "dealer wins"


class Blackjack(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, bet: int):
        super().__init__(timeout=GAME_TIMEOUT)
        self.store, self.member, self.bet = interaction.client.store, interaction.user, bet
        self.player = [draw(), draw()]
        self.dealer = [draw(), draw()]
        self.finished = False
        self.result = ""
        self.message: discord.Message | None = None

    def embed(self) -> discord.Embed:
        dealer = show(self.dealer) + f" ({hand_value(self.dealer)})" if self.finished else f"{show(self.dealer[:1])} `??`"
        embed = discord.Embed(title=f"🃏 Blackjack: {self.member.display_name} bet {fmt(self.bet)}", color=0x2C3E50)
        embed.add_field(name="Your hand", value=f"{show(self.player)} ({hand_value(self.player)})", inline=False)
        embed.add_field(name="Dealer", value=dealer, inline=False)
        if self.finished:
            embed.add_field(name="Result", value=self.result, inline=False)
        return embed

    def finish(self) -> None:
        if hand_value(self.player) <= 21:
            while hand_value(self.dealer) < 17:
                self.dealer.append(draw())
        payout, what = blackjack_payout(self.player, self.dealer, self.bet)
        pay(self.store, self.member, payout, "blackjack")
        self.finished = True
        self.result = f"{what}: {result_line(self.store, self.member, self.bet, payout)}"
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("This isn't your game. Start one with /blackjack.", ephemeral=True)
            return False
        return True

    async def update(self, interaction: discord.Interaction) -> None:
        self.double.disabled = True
        await interaction.response.edit_message(embed=self.embed(), view=None if self.finished else self)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.player.append(draw())
        if hand_value(self.player) >= 21:
            self.finish()
        await self.update(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.finish()
        await self.update(interaction)

    @discord.ui.button(label="Double down", style=discord.ButtonStyle.success)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = self.member
        if not self.store.spend_aura(member.guild.id, member.id, member.display_name, self.bet, "blackjack", "double down"):
            await interaction.response.send_message(f"You need another {fmt(self.bet)} to double down.", ephemeral=True)
            return
        self.bet *= 2
        self.player.append(draw())
        self.finish()
        await self.update(interaction)

    async def on_timeout(self) -> None:
        if not self.finished:
            self.finish()
            if self.message:
                try:
                    await self.message.edit(embed=self.embed(), view=None)
                except discord.HTTPException:
                    pass


@app_commands.command(name="blackjack", description="Play blackjack against the dealer (blackjack pays 3:2)")
@app_commands.describe(bet="How much aura (a number, half or all)")
@app_commands.guild_only()
@game_cooldown
async def blackjack_cmd(interaction: discord.Interaction, bet: str) -> None:
    amount = await place_bet(interaction, bet, "blackjack")
    if amount is None:
        return
    game = Blackjack(interaction, amount)
    if is_blackjack(game.player) or is_blackjack(game.dealer):
        game.finish()  # naturals settle right away
    await interaction.response.send_message(embed=game.embed(), view=None if game.finished else game)
    game.message = await interaction.original_response()


CARD_NAMES = {1: "A", 11: "J", 12: "Q", 13: "K"}
HOUSE_EDGE = 0.97


def card_name(value: int) -> str:
    return CARD_NAMES.get(value, str(value))


def higher_lower_odds(card: int, guess: str) -> float:
    """Chance the next card (ties don't count) is higher or lower than `card` (A=1 ... K=13)."""
    return (13 - card) / 12 if guess == "higher" else (card - 1) / 12


class HigherLower(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, bet: int):
        super().__init__(timeout=GAME_TIMEOUT)
        self.store, self.member, self.bet = interaction.client.store, interaction.user, bet
        self.card = random.randint(1, 13)
        self.multiplier = 1.0
        self.streak = 0
        self.log = ""
        self.finished = False
        self.message: discord.Message | None = None
        self.refresh()

    @property
    def pot(self) -> int:
        return int(self.bet * self.multiplier)

    def refresh(self) -> None:
        self.higher.disabled = self.card == 13
        self.lower.disabled = self.card == 1
        self.cash_out.disabled = self.streak == 0
        self.cash_out.label = f"Cash out ({self.pot:,})"

    def embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"🔼🔽 Higher or lower: {self.member.display_name} bet {fmt(self.bet)}", color=0x16A085)
        embed.add_field(name="Card", value=f"`{card_name(self.card)}`", inline=True)
        embed.add_field(name="Streak", value=str(self.streak), inline=True)
        embed.add_field(name="Pot", value=f"{fmt(self.pot)} ({self.multiplier:.2f}x)", inline=True)
        if self.log:
            embed.add_field(name="Last", value=self.log, inline=False)
        if not self.finished:
            embed.set_footer(text="Guess whether the next card is higher or lower. Same card = free redo.")
        return embed

    def end(self, payout: int, what: str) -> None:
        pay(self.store, self.member, payout, "higherlower")
        self.finished = True
        self.log = f"{what}: {result_line(self.store, self.member, self.bet, payout)}"
        self.stop()

    async def guess(self, interaction: discord.Interaction, guess: str) -> None:
        old, new = self.card, random.randint(1, 13)
        self.card = new
        if new == old:
            self.log = f"`{card_name(old)}` → `{card_name(new)}`: same card, guess again"
        elif (new > old) == (guess == "higher"):
            self.multiplier *= HOUSE_EDGE / higher_lower_odds(old, guess)
            self.streak += 1
            self.log = f"`{card_name(old)}` → `{card_name(new)}`: correct"
        else:
            self.multiplier = 0
            self.end(0, f"`{card_name(old)}` → `{card_name(new)}`: wrong")
        if not self.finished:
            self.refresh()
        await interaction.response.edit_message(embed=self.embed(), view=None if self.finished else self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("This isn't your game. Start one with /higherlower.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Higher", style=discord.ButtonStyle.primary, emoji="🔼")
    async def higher(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.guess(interaction, "higher")

    @discord.ui.button(label="Lower", style=discord.ButtonStyle.primary, emoji="🔽")
    async def lower(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.guess(interaction, "lower")

    @discord.ui.button(label="Cash out", style=discord.ButtonStyle.success)
    async def cash_out(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.end(self.pot, "cashed out")
        await interaction.response.edit_message(embed=self.embed(), view=None)

    async def on_timeout(self) -> None:
        if not self.finished:
            self.end(self.pot, "timed out, cashed out")
            if self.message:
                try:
                    await self.message.edit(embed=self.embed(), view=None)
                except discord.HTTPException:
                    pass


@app_commands.command(name="higherlower", description="Guess if the next card is higher or lower; cash out whenever")
@app_commands.describe(bet="How much aura (a number, half or all)")
@app_commands.guild_only()
@game_cooldown
async def higherlower_cmd(interaction: discord.Interaction, bet: str) -> None:
    amount = await place_bet(interaction, bet, "higherlower")
    if amount is None:
        return
    game = HigherLower(interaction, amount)
    await interaction.response.send_message(embed=game.embed(), view=game)
    game.message = await interaction.original_response()


# ---- against each other -----------------------------------------------------------


class Duel(discord.ui.View):
    def __init__(self, challenger: discord.Member, target: discord.Member, bet: int, store):
        super().__init__(timeout=60)
        self.challenger, self.target, self.bet, self.store = challenger, target, bet, store
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
            await interaction.response.edit_message(content=f"{self.challenger.display_name} can't cover the bet anymore. Duel off.", view=None)
            self.stop()
            return
        if not store.spend_aura(guild_id, self.target.id, self.target.display_name, bet, "duel", "bet"):
            await interaction.response.send_message(f"You need {fmt(bet)} to accept.", ephemeral=True)
            return
        store.spend_aura(guild_id, self.challenger.id, self.challenger.display_name, bet, "duel", "bet")
        winner, loser = random.sample([self.challenger, self.target], 2)
        pay(store, winner, bet * 2, "duel", f"beat {loser.display_name}")
        self.stop()
        await interaction.response.edit_message(
            content=f"⚔️ **{winner.display_name}** beat {loser.display_name} and takes **{fmt(bet * 2)}**.", view=None,
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content=f"{self.target.display_name} declined the duel.", view=None)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content=f"{self.target.display_name} didn't answer. Duel off.", view=None)
            except discord.HTTPException:
                pass


@app_commands.command(name="duel", description="Challenge someone: you both bet the same aura, winner takes it all")
@app_commands.describe(user="Who to challenge", bet="How much aura each (a number, half or all)")
@app_commands.guild_only()
@game_cooldown
async def duel_cmd(interaction: discord.Interaction, user: discord.Member, bet: str) -> None:
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
    view = Duel(member, user, amount, store)
    await interaction.response.send_message(
        f"⚔️ {member.mention} challenges {user.mention} to a duel for **{fmt(amount)}** each. Winner takes all. "
        f"{user.display_name}, you have 60 seconds.",
        view=view, allowed_mentions=discord.AllowedMentions(users=[user]),
    )
    view.message = await interaction.original_response()


# ---- getting aura -------------------------------------------------------------------


@app_commands.command(name="give", description="Give some of your aura to someone")
@app_commands.describe(user="Who gets it", amount="How much (a number, half or all)")
@app_commands.guild_only()
async def give_cmd(interaction: discord.Interaction, user: discord.Member, amount: str) -> None:
    store, member = interaction.client.store, interaction.user
    if user.bot or user.id == member.id:
        await interaction.response.send_message("Pick someone else.", ephemeral=True)
        return
    welcome(store, member)
    value = parse_bet(amount, balance(store, member.guild.id, member.id))
    if isinstance(value, str):
        await interaction.response.send_message(value.replace("Bet", "Give"), ephemeral=True)
        return
    store.spend_aura(member.guild.id, member.id, member.display_name, value, "gift", f"gave to {user.display_name}")
    store.add_aura(member.guild.id, user.id, user.display_name, value, "gift", f"from {member.display_name}")
    await interaction.response.send_message(
        f"🎁 {member.display_name} gave {user.mention} **{fmt(value)}**.", allowed_mentions=discord.AllowedMentions(users=[user]),
    )


COMMANDS = [coinflip_cmd, slots_cmd, roulette_cmd, blackjack_cmd, higherlower_cmd, duel_cmd, give_cmd]
