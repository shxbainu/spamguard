"""The aura shop: /shop lists what's for sale, /buy spends aura on it.

Items: an extra /shutup, a 1-minute /shutup, a 24-hour /shutup shield, renaming someone for an hour, a roast from
the bot, and setting the bot's status for an hour."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Literal, Optional

import discord
from discord import app_commands

from casino import balance, fmt, welcome

log = logging.getLogger("spamguard.shop")

# item -> (price, name, what it does)
ITEMS = {
    "shutup": (250, "Extra /shutup", "One more /shutup on top of your weekly ones"),
    "longshutup": (600, "1-minute /shutup", "A /shutup that lasts 60 seconds (use /shutup with long: True)"),
    "shield": (400, "Shutup shield", "Nobody can /shutup you for 24 hours"),
    "nickname": (500, "Rename someone", "Change someone's nickname for 1 hour"),
    "roast": (150, "Roast", "The bot roasts someone of your choice"),
    "status": (750, "Bot status", "Set the bot's status for 1 hour"),
}
SHIELD_SECONDS = 24 * 3600
NICKNAME_SECONDS = 3600
STATUS_SECONDS = 3600
LONG_SHUTUP_SECONDS = 60


def shield_until(store, guild_id: int, user_id: int) -> float:
    """When someone's /shutup shield runs out (0 or in the past if they don't have one)."""
    return float(store.get_state(f"shield:{guild_id}:{user_id}") or 0)


def status_override(store) -> str | None:
    """A bought bot status that's still running, or None."""
    data = json.loads(store.get_state("status_override") or "{}")
    return data.get("text") if data.get("until", 0) > time.time() else None


def clean_text(text: str, limit: int) -> str:
    """No pings, links or line breaks in text people pay to show off."""
    text = re.sub(r"<[@#&!][^>]*>|@(everyone|here)|https?://\S+", "", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


@app_commands.command(name="shop", description="See what you can buy with aura")
@app_commands.guild_only()
async def shop_cmd(interaction: discord.Interaction) -> None:
    store, member = interaction.client.store, interaction.user
    welcome(store, member)
    lines = [f"**{name}** ({fmt(price)}): {about}  ·  `/buy {item}`" for item, (price, name, about) in ITEMS.items()]
    embed = discord.Embed(title="🛒 Aura shop", description="\n".join(lines), color=0x9B59B6)
    embed.set_footer(text=f"You have {fmt(balance(store, member.guild.id, member.id))}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.command(name="buy", description="Spend aura in the shop")
@app_commands.describe(
    item="What to buy (see /shop)", user="Who it's for (rename and roast)", text="The new nickname or bot status",
)
@app_commands.guild_only()
async def buy_cmd(
    interaction: discord.Interaction,
    item: Literal["shutup", "longshutup", "shield", "nickname", "roast", "status"],
    user: Optional[discord.Member] = None,
    text: Optional[str] = None,
) -> None:
    bot, member = interaction.client, interaction.user
    store, guild = bot.store, interaction.guild
    price, name, _ = ITEMS[item]
    welcome(store, member)

    # Check everything that could go wrong before taking the aura.
    problem = None
    if item in ("nickname", "roast") and user is None:
        problem = "Pick who it's for with `user`."
    elif item in ("nickname", "roast") and user.bot:
        problem = "Pick a person, not a bot."
    elif item in ("nickname", "status") and not (text and clean_text(text, 32 if item == "nickname" else 60)):
        problem = "Say what it should be with `text`."
    elif item == "nickname" and (user.id == guild.owner_id or user.top_role >= guild.me.top_role):
        problem = f"I can't rename {user.display_name}: they're the owner or their role is above mine."
    elif item == "roast" and (bot.mention_chatter or bot.chatter) is None:
        problem = "Roasts need the AI chat, which isn't set up."
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    if not store.spend_aura(guild.id, member.id, member.display_name, price, "shop", name):
        await interaction.response.send_message(
            f"{name} costs {fmt(price)} and you have {fmt(balance(store, guild.id, member.id))}.", ephemeral=True,
        )
        return

    def refund(why: str) -> str:
        store.add_aura(guild.id, member.id, member.display_name, price, "shop", f"refund: {name}")
        return f"{why} You got your {fmt(price)} back."

    left = f"Balance {fmt(balance(store, guild.id, member.id))}."
    if item in ("shutup", "longshutup"):
        store.add_item(guild.id, member.id, item)
        count = store.items(guild.id, member.id).get(item, 0)
        how = "/shutup with long: True" if item == "longshutup" else "/shutup after your weekly ones run out"
        await interaction.response.send_message(f"Bought a {name}. You have {count}; use it with {how}. {left}", ephemeral=True)
    elif item == "shield":
        until = max(time.time(), shield_until(store, guild.id, member.id)) + SHIELD_SECONDS
        store.set_state(f"shield:{guild.id}:{member.id}", str(until))
        await interaction.response.send_message(f"🛡️ {member.display_name} can't be /shutup'd until <t:{int(until)}:t>.")
    elif item == "nickname":
        nick = clean_text(text, 32)
        key = f"nick:{guild.id}:{user.id}"
        saved = json.loads(store.get_state(key) or "{}")
        old = saved.get("old", user.nick) if saved else user.nick
        try:
            await user.edit(nick=nick, reason=f"Bought by {member} in the aura shop")
        except discord.HTTPException:
            await interaction.response.send_message(refund(f"I couldn't rename {user.display_name} (I need Manage Nicknames)."), ephemeral=True)
            return
        store.set_state(key, json.dumps({"old": old, "until": time.time() + NICKNAME_SECONDS}))
        await interaction.response.send_message(
            f"🏷️ {member.display_name} renamed {user.mention} to **{nick}** for an hour.",
            allowed_mentions=discord.AllowedMentions(users=[user]),
        )
    elif item == "roast":
        await interaction.response.defer(thinking=True)
        talker = bot.mention_chatter or bot.chatter
        line = await talker.say(
            guild.me.display_name, bot.persona,
            f"{member.display_name} paid you to roast {user.display_name}. Roast {user.display_name} in one or two short lines.",
        )
        if not line:
            await interaction.followup.send(refund("The AI couldn't be reached."), ephemeral=True)
            return
        await interaction.followup.send(f"{user.mention} {line}", allowed_mentions=discord.AllowedMentions(users=[user]))
    elif item == "status":
        status = clean_text(text, 60)
        store.set_state("status_override", json.dumps({"text": status, "until": time.time() + STATUS_SECONDS, "by": str(member)}))
        try:
            await bot.show_status(status)
        except discord.HTTPException:
            pass
        await interaction.response.send_message(f"{member.display_name} set my status to **{status}** for an hour.")


async def undo_expired(bot) -> None:
    """Every minute, gives people their old nicknames back when a bought rename runs out."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for key, value in bot.store.states("nick:").items():
            try:
                data = json.loads(value)
                if data.get("until", 0) > now:
                    continue
                _, guild_id, user_id = key.split(":")
                guild = bot.get_guild(int(guild_id))
                member = guild.get_member(int(user_id)) if guild else None
                if member is not None:
                    await member.edit(nick=data.get("old"), reason="Bought nickname ran out")
            except (discord.HTTPException, ValueError) as e:
                log.warning("Couldn't restore a nickname (%s): %s", key, e)
            bot.store.delete_state(key)


COMMANDS = [shop_cmd, buy_cmd]
