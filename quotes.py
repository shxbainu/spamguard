"""The quote book: right-click a message → Apps → Quote this to save it, then /quote random to pull one back up.
The bot also knows a few of each person's saved quotes when it chats with them, and can throw one back at them."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands

IMAGE_TYPES = ("image/",)


def jump_url(row: dict) -> str:
    return f"https://discord.com/channels/{row['guild_id']}/{row['channel_id']}/{row['message_id']}"


async def fresh_image(guild: discord.Guild, row: dict) -> str | None:
    """Discord picture links expire, so a quoted picture is looked up again from the original message."""
    if not row["has_image"]:
        return None
    channel = guild.get_channel_or_thread(row["channel_id"])
    if channel is None:
        return None
    try:
        message = await channel.fetch_message(row["message_id"])
    except discord.HTTPException:
        return None
    return next((a.url for a in message.attachments if (a.content_type or "").startswith(IMAGE_TYPES)), None)


async def quote_embed(guild: discord.Guild, row: dict) -> discord.Embed:
    text = f"“{row['content']}”" if row["content"] else "*(a picture)*"
    embed = discord.Embed(
        description=f"{text}\n\n[Jump to message]({jump_url(row)})"[:4096],
        color=0xF1C40F,
        timestamp=datetime.fromtimestamp(row["said_at"], timezone.utc),
    )
    author = guild.get_member(row["author_id"])
    embed.set_author(name=row["author_name"], icon_url=author.display_avatar.url if author else None)
    embed.set_footer(text=f"Quote #{row['id']} · saved by {row['saved_by_name']}")
    if image := await fresh_image(guild, row):
        embed.set_image(url=image)
    return embed


@app_commands.context_menu(name="Quote this")
@app_commands.guild_only()
async def quote_menu(interaction: discord.Interaction, message: discord.Message) -> None:
    store = interaction.client.store
    if message.author.id == interaction.user.id:
        await interaction.response.send_message("You can't quote yourself.", ephemeral=True)
        return
    content = message.clean_content.strip()
    has_image = any((a.content_type or "").startswith(IMAGE_TYPES) for a in message.attachments)
    if not content and not has_image:
        await interaction.response.send_message("There's nothing in that message to quote.", ephemeral=True)
        return
    quote_id = store.add_quote(
        guild_id=interaction.guild_id, channel_id=message.channel.id, message_id=message.id,
        said_at=message.created_at.timestamp(), author_id=message.author.id, author_name=message.author.display_name,
        content=content[:1500], has_image=has_image, saved_by_id=interaction.user.id,
        saved_by_name=interaction.user.display_name,
    )
    if quote_id is None:
        await interaction.response.send_message("That one's already in the quote book.", ephemeral=True)
        return
    embed = await quote_embed(interaction.guild, store.quote(interaction.guild_id, quote_id))
    await interaction.response.send_message(
        f"📖 Saved to the quote book as #{quote_id}", embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


quote_commands = app_commands.Group(name="quote", description="The server's quote book", guild_only=True)


@quote_commands.command(name="random", description="Show a random saved quote")
@app_commands.describe(user="Only quotes from this person", about="Only quotes containing this word or phrase")
async def random_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None, about: Optional[str] = None) -> None:
    store = interaction.client.store
    row = store.random_quote(interaction.guild_id, user.id if user else None, about)
    if row is None:
        if store.count_quotes(interaction.guild_id) == 0:
            reason = "The quote book is empty. Right-click a message → Apps → Quote this to save one."
        else:
            reason = "No saved quotes match that."
        await interaction.response.send_message(reason, ephemeral=True)
        return
    await interaction.response.defer()
    await interaction.followup.send(embed=await quote_embed(interaction.guild, row), allowed_mentions=discord.AllowedMentions.none())


@quote_commands.command(name="show", description="Show a saved quote by its number")
async def show_cmd(interaction: discord.Interaction, number: int) -> None:
    row = interaction.client.store.quote(interaction.guild_id, number)
    if row is None:
        await interaction.response.send_message(f"There's no quote #{number}.", ephemeral=True)
        return
    await interaction.response.defer()
    await interaction.followup.send(embed=await quote_embed(interaction.guild, row), allowed_mentions=discord.AllowedMentions.none())


@quote_commands.command(name="delete", description="Remove a quote (the person quoted, whoever saved it, or a mod)")
async def delete_cmd(interaction: discord.Interaction, number: int) -> None:
    store = interaction.client.store
    row = store.quote(interaction.guild_id, number)
    if row is None:
        await interaction.response.send_message(f"There's no quote #{number}.", ephemeral=True)
        return
    user = interaction.user
    if user.id not in (row["author_id"], row["saved_by_id"]) and not user.guild_permissions.manage_messages:
        await interaction.response.send_message("Only the person quoted, whoever saved it, or a mod can remove it.", ephemeral=True)
        return
    store.delete_quote(interaction.guild_id, number)
    await interaction.response.send_message(f"Removed quote #{number}.", ephemeral=True)
