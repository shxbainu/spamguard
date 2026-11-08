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

from cards import AURA, LOSS, STARTING_REASON, card, signed

load_dotenv()

AURA_ENABLED = (os.getenv("AURA_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")
# A member can gain or lose aura from a message at most once per this many minutes, so it stays rare.
AURA_COOLDOWN_MINUTES = int(os.getenv("AURA_COOLDOWN_MINUTES") or 10)

# Points by how big the moment is (level 1 to 5, decided by the AI). The person called out loses what the caller gains.
LEVEL_POINTS = {
    "comeback": [25, 50, 100, 250, 500],
    "callout": [25, 50, 100, 200, 300],
    "false_claim": [-25, -50, -100, -250, -500],
    "bad_take": [-25, -50, -100, -250, -500],
}
DEBATE_POINTS = 100
WHAT = {
    "comeback": "comeback", "callout": "called someone out", "called_out": "got called out",
    "false_claim": "said something false", "bad_take": "had a terrible take", "debate": "won a debate", "gift": "gift", "shop": "shop",
}
# What the bot is reacting to, for the one-liner it writes; {size} is how big the moment was.
SITUATIONS = {
    "comeback": "{name} just hit someone with a {size} comeback ({reason}). Give them props that match how good it was",
    "callout": "{name} just called out {target} ({size}) for saying something false ({reason}). React to {target} getting exposed or {name} being smart, matching how big it was",
    "false_claim": "{name} just said something {size} false ({reason}). Call it out like they're dumb, matching how bad it was",
    "bad_take": "{name} just had a {size} take that basically nobody would agree with ({reason}). Clown them for it, matching how bad it was",
}
SIZES = {
    "comeback": {1: "decent", 2: "good", 3: "really good", 4: "brutal", 5: "legendary, all-time"},
    "callout": {1: "small", 2: "clear", 3: "big", 4: "brutal", 5: "total"},
    "false_claim": {1: "slightly", 2: "plainly", 3: "confidently", 4: "wildly", 5: "absurdly, spectacularly"},
    "bad_take": {1: "weird", 2: "bad", 3: "terrible", 4: "awful", 5: "historically bad"},
}
# Used when the AI can't be reached: small moments (levels 1-2) and big ones (3-5).
FALLBACK = {
    "comeback": (["ok that was decent", "lowk good"], ["oh he cooked", "nah that was tuff", "BRO 😭"]),
    "callout": (["fair point", "true"], ["too smart??", "exposed lmao", "caught in 4k"]),
    "false_claim": (["thats not true", "nah"], ["damn ur stupid", "holy shit this guy is dumb", "what are u on"]),
    "bad_take": (["weird take", "nah thats crazy"], ["worst take ive ever seen", "who hurt you", "get help"]),
}


def points_for(kind: str, level: int) -> int:
    return LEVEL_POINTS[kind][min(5, max(1, level)) - 1]


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
    level = verdict.aura_level or 2
    points = points_for(verdict.aura, level)
    lost = -points_for("callout", level)
    snippet = message.clean_content[:300]
    size = SIZES[verdict.aura][level]
    reason = f"{size}: {verdict.aura_reason}" if verdict.aura_reason else size
    bot.store.add_aura(guild.id, member.id, member.display_name, points, verdict.aura, reason, snippet, now)
    target = called_out_member(bot, message, verdict.aura_target) if verdict.aura == "callout" else None
    if target is not None:
        bot.store.add_aura(guild.id, target.id, target.display_name, lost, "called_out",
                           f"called out by {member.display_name}: {verdict.aura_reason}", snippet, now)

    line = None
    talker = getattr(bot, "chatter", None)
    if talker is not None and bot.chat_budget.take(time.monotonic()):
        situation = SITUATIONS[verdict.aura].format(
            name=member.display_name, target=target.display_name if target else "someone", reason=verdict.aura_reason,
            size=size,
        )
        line = await talker.say(guild.me.display_name, persona, situation + " in 2 to 6 words. Don't mention aura or points.")
    line = (line or random.choice(FALLBACK[verdict.aura][level >= 3])).rstrip(" .")
    text = f"{line} {aura_text(points)}"
    if target is not None:
        text += f" ({target.display_name} {aura_text(lost)})"
    try:
        await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass
    return f"aura: {verdict.aura} level {level} {aura_text(points)}" + (f", {target.display_name} {aura_text(lost)}" if target else "")


def give_debate_aura(store, guild_id: int, winners: list[discord.Member], motion: str) -> None:
    if AURA_ENABLED:
        for member in winners:
            store.add_aura(guild_id, member.id, member.display_name, DEBATE_POINTS, "debate", f"won: {motion[:150]}")


aura_commands = app_commands.Group(name="aura", description="Aura points", guild_only=True)


LABELS = {
    "comeback": "Comeback", "callout": "Called someone out", "called_out": "Got called out",
    "false_claim": "False claim", "bad_take": "Bad take", "debate": "Won a debate", "gift": "Gift", "shop": "Shop",
}


def history_line(row: dict) -> str:
    reason = (row["reason"] or "").strip()
    if len(reason) > 70:
        reason = reason[:69].rstrip() + "…"
    detail = f" · {reason}" if reason else ""
    return f"`{signed(row['points']):>6}` **{LABELS.get(row['kind'], row['kind'].title())}**{detail} · <t:{int(row['created_at'])}:R>"


def aura_card(store, guild_id: int, user: discord.abc.User, now: float | None = None) -> discord.Embed:
    from shop import ITEMS, shield_until

    total = store.aura_total(guild_id, user.id)
    place, people = store.aura_rank(guild_id, user.id)
    standing = f"Rank **#{place}** of {people}" if place else "Not on the leaderboard yet"
    embed = card(user, description=f"## ✨ {total:,} aura\n{standing}", color=LOSS if total < 0 else AURA)

    moments, net, played = store.aura_from_moments(guild_id, user.id), store.game_net(guild_id, user.id), store.games_played(guild_id, user.id)
    embed.add_field(name="From chat", value=signed(moments) if moments else "0", inline=True)
    embed.add_field(
        name="Casino", value=(f"{signed(net) if net else 'Even'} · {played:,} game{'' if played == 1 else 's'}" if played else "No games yet"), inline=True,
    )

    recent = [row for row in store.aura_history(guild_id, user.id, limit=6) if row["reason"] != STARTING_REASON][:5]
    empty = "Nothing yet. Good comebacks, callouts and false claims show up here."
    embed.add_field(name="Recent", value="\n".join(map(history_line, recent))[:1024] or empty, inline=False)

    owned = [f"{count}× {ITEMS[item][1] if item in ITEMS else item}" for item, count in store.items(guild_id, user.id).items()]
    until = shield_until(store, guild_id, user.id)
    if until > (now or time.time()):
        owned.append(f"🛡️ Shield until <t:{int(until)}:t>")
    if owned:
        embed.add_field(name="Items", value="\n".join(owned)[:1024], inline=False)
    return embed


@aura_commands.command(name="check", description="See someone's aura and what they got it for")
@app_commands.describe(user="Whose aura (default: yours)")
async def check_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
    embed = aura_card(interaction.client.store, interaction.guild_id, user or interaction.user)
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


def leaderboard_card(rows: list[dict], viewer_id: int, place: int, total: int) -> discord.Embed:
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for index, row in enumerate(rows):
        marker = medals[index] if index < 3 else f"`{index + 1}.`"
        you = " ← you" if row["user_id"] == viewer_id else ""
        lines.append(f"{marker} **{row['user_name']}** · {row['points']:,}{you}")
    embed = discord.Embed(title="🏆 Aura leaderboard", description="\n".join(lines), color=AURA)
    if place and all(row["user_id"] != viewer_id for row in rows):
        embed.set_footer(text=f"You: #{place} · {total:,} aura")
    return embed


@aura_commands.command(name="leaderboard", description="Who has the most aura")
async def leaderboard_cmd(interaction: discord.Interaction) -> None:
    store, guild_id, viewer = interaction.client.store, interaction.guild_id, interaction.user.id
    rows = store.aura_leaderboard(guild_id)
    if not rows:
        await interaction.response.send_message("Nobody has any aura yet.", ephemeral=True)
        return
    embed = leaderboard_card(rows, viewer, store.aura_rank(guild_id, viewer)[0], store.aura_total(guild_id, viewer))
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())
