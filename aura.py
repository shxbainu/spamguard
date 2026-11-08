"""Aura points. The moderation check already reads every message; when it (and the smarter second-opinion model)
agree a message is a standout moment, the bot replies with one curt line and the points: a real comeback, calling
out something false, or saying something blatantly false. Most messages never get any.

/aura shows someone's aura and what they got it for; /aura leaderboard ranks everyone."""
from __future__ import annotations

import os
import random
import time
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

AURA_ENABLED = (os.getenv("AURA_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")
# A member can gain or lose aura from a message at most once per this many minutes, so it stays rare.
AURA_COOLDOWN_MINUTES = int(os.getenv("AURA_COOLDOWN_MINUTES") or 10)

POINTS = {"comeback": 100, "callout": 50, "called_out": -50, "false_claim": -100, "debate": 100}
WHAT = {
    "comeback": "comeback", "callout": "called someone out", "called_out": "got called out",
    "false_claim": "said something false", "debate": "won a debate", "daily": "daily", "gift": "gift",
    "shop": "shop",
}
# What the bot is reacting to, for the one-liner it writes.
SITUATIONS = {
    "comeback": "{name} just hit someone with a genuinely good comeback ({reason}). Give them their props",
    "callout": "{name} just called out {target} for saying something false ({reason}). React to {target} getting exposed or {name} being smart",
    "false_claim": "{name} just said something blatantly false ({reason}). Call it out like they're dumb",
}
# Used when the AI can't be reached.
FALLBACK = {
    "comeback": ["oh he cooked", "nah that was tuff", "ok that was actually good"],
    "callout": ["too smart??", "exposed lmao", "caught in 4k"],
    "false_claim": ["damn ur stupid", "holy shit this guy is dumb", "thats just not true bro"],
}


def aura_text(points: int) -> str:
    return f"{points:+d} aura"


def find_member(name: str, candidates: list[discord.Member]) -> discord.Member | None:
    """The member a name from the chat refers to: "Benny (@empireben)", "Benny" or "empireben"."""
    name = name.strip().lower().lstrip("@")
    if not name:
        return None
    for member in candidates:
        labels = {member.display_name.lower(), member.name.lower(), f"{member.display_name} (@{member.name})".lower()}
        if name in labels:
            return member
    return None


def called_out_member(bot, message: discord.Message, target: str) -> discord.Member | None:
    """Who a callout is aimed at: the person being replied to, or someone named who spoke in the channel lately."""
    ref = message.reference
    if ref is not None and isinstance(ref.resolved, discord.Message) and isinstance(ref.resolved.author, discord.Member):
        if ref.resolved.author.id != message.author.id and not ref.resolved.author.bot:
            return ref.resolved.author
    recent = [m.author for m in list(bot.cached_messages)[-200:] if m.channel.id == message.channel.id]
    unique = {m.id: m for m in recent}.values()
    members = [m for m in unique if isinstance(m, discord.Member) and not m.bot and m.id != message.author.id]
    return find_member(target, members)


async def award(bot, message: discord.Message, verdict, persona: str) -> str | None:
    """Gives or takes aura for a standout message and replies with a short line; returns what happened (for the
    log), or None if nothing did."""
    if not AURA_ENABLED or verdict.aura not in SITUATIONS:
        return None
    guild, member = message.guild, message.author
    now = time.time()
    if now - bot.store.last_aura(guild.id, member.id) < AURA_COOLDOWN_MINUTES * 60:
        return None
    points = POINTS[verdict.aura]
    snippet = message.clean_content[:300]
    bot.store.add_aura(guild.id, member.id, member.display_name, points, verdict.aura, verdict.aura_reason, snippet, now)
    target = called_out_member(bot, message, verdict.aura_target) if verdict.aura == "callout" else None
    if target is not None:
        bot.store.add_aura(guild.id, target.id, target.display_name, POINTS["called_out"], "called_out",
                           f"called out by {member.display_name}: {verdict.aura_reason}", snippet, now)

    line = None
    talker = getattr(bot, "chatter", None)
    if talker is not None and bot.chat_budget.take(time.monotonic()):
        situation = SITUATIONS[verdict.aura].format(
            name=member.display_name, target=target.display_name if target else "someone", reason=verdict.aura_reason,
        )
        line = await talker.say(guild.me.display_name, persona, situation + " in 2 to 6 words. Don't mention aura or points.")
    line = (line or random.choice(FALLBACK[verdict.aura])).rstrip(" .")
    text = f"{line} {aura_text(points)}"
    if target is not None:
        text += f" ({target.display_name} {aura_text(POINTS['called_out'])})"
    try:
        await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass
    return f"aura: {verdict.aura} {aura_text(points)}" + (f", {target.display_name} {aura_text(POINTS['called_out'])}" if target else "")


def give_debate_aura(store, guild_id: int, winners: list[discord.Member], motion: str) -> None:
    if AURA_ENABLED:
        for member in winners:
            store.add_aura(guild_id, member.id, member.display_name, POINTS["debate"], "debate", f"won: {motion[:150]}")


aura_commands = app_commands.Group(name="aura", description="Aura points", guild_only=True)


@aura_commands.command(name="check", description="See someone's aura and what they got it for")
@app_commands.describe(user="Whose aura (default: yours)")
async def check_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
    store = interaction.client.store
    user = user or interaction.user
    total = store.aura_total(interaction.guild_id, user.id)
    lines = [f"**{user.display_name}** has **{total:,} aura**"]
    for row in store.aura_history(interaction.guild_id, user.id):
        why = f": {row['reason']}" if row["reason"] else ""
        lines.append(f"`{aura_text(row['points']):>10}` {WHAT.get(row['kind'], row['kind'])}{why} <t:{int(row['created_at'])}:R>")
    if len(lines) == 1:
        lines.append("Nothing yet.")
    extras = []
    net = store.game_net(interaction.guild_id, user.id)
    if net:
        extras.append(f"casino: {'+' if net > 0 else ''}{net:,}")
    owned = store.items(interaction.guild_id, user.id)
    if owned:
        extras.append("items: " + ", ".join(f"{count}x {item}" for item, count in owned.items()))
    from shop import shield_until
    if (until := shield_until(store, interaction.guild_id, user.id)) > time.time():
        extras.append(f"🛡️ shielded until <t:{int(until)}:t>")
    if extras:
        lines.append(" · ".join(extras))
    await interaction.response.send_message("\n".join(lines)[:2000], allowed_mentions=discord.AllowedMentions.none())


@aura_commands.command(name="leaderboard", description="Who has the most aura")
async def leaderboard_cmd(interaction: discord.Interaction) -> None:
    rows = interaction.client.store.aura_leaderboard(interaction.guild_id)
    if not rows:
        await interaction.response.send_message("Nobody has any aura yet.", ephemeral=True)
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`{i + 1}.`'} **{row['user_name']}**: {row['points']:,}" for i, row in enumerate(rows)
    ]
    embed = discord.Embed(title="Aura leaderboard", description="\n".join(lines), color=0x9B59B6)
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())
