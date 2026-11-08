"""The shared look of the bot's aura, casino and shop cards."""
from __future__ import annotations

import discord

AURA = 0x9B59B6
WIN = 0x57F287
LOSS = 0xED4245
EVEN = 0x99AAB5
PLAYING = 0x5865F2
STARTING_REASON = "casino starting aura"


def card(member: discord.abc.User, title: str | None = None, description: str | None = None, color: int = AURA) -> discord.Embed:
    """An embed headed with the person's avatar and name."""
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    return embed


def signed(points: int) -> str:
    return f"{points:+,}"


def add_result(embed: discord.Embed, bet: int, payout: int, balance: int) -> discord.Embed:
    """Colors a game card by how it went and adds the Bet / Won / Balance columns."""
    net = payout - bet
    embed.color = WIN if net > 0 else LOSS if net < 0 else EVEN
    embed.add_field(name="Bet", value=f"{bet:,}", inline=True)
    if net:
        embed.add_field(name="Won" if net > 0 else "Lost", value=signed(net), inline=True)
    else:
        embed.add_field(name="Result", value="Money back", inline=True)
    embed.add_field(name="Balance", value=f"{balance:,} aura", inline=True)
    return embed
