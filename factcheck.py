"""/factcheck: a formal fact-check with web sources (when the AI can search).

Right-click a message → Apps → Fact check checks what someone said, with the chat around it for context. If it
turns out false, they lose aura and whoever checked it gains some. /factcheck claim:"..." checks any claim."""
from __future__ import annotations

import time

import discord
from discord import app_commands

from debate import sources_field

RATINGS = {
    "true": ("✅ True", 0x2ECC71),
    "mostly_true": ("🟢 Mostly true", 0x27AE60),
    "misleading": ("⚠️ Misleading", 0xF39C12),
    "false": ("❌ False", 0xE74C3C),
    "unverifiable": ("❔ Unverifiable", 0x95A5A6),
    "opinion": ("💬 Opinion, not a factual claim", 0x95A5A6),
}
# Aura for a false claim that someone fact-checked.
CAUGHT_POINTS = -100
CHECKER_POINTS = 50
COOLDOWN_SECONDS = 30


def fact_embed(check, quoted_by: str | None, model: str) -> discord.Embed:
    label, color = RATINGS.get(check.rating, RATINGS["unverifiable"])
    embed = discord.Embed(title="Fact check", color=color)
    claim = f"“{check.claim}”" + (f"\n— {quoted_by}" if quoted_by else "")
    embed.add_field(name="Claim", value=claim[:1024], inline=False)
    embed.add_field(name="Rating", value=label, inline=False)
    embed.add_field(name="Conclusion", value=(check.summary or "–")[:1024], inline=False)
    if check.explanation:
        embed.add_field(name="Evidence", value=check.explanation[:1024], inline=False)
    if check.correction:
        embed.add_field(name="Correction", value=check.correction[:1024], inline=False)
    shown = sources_field(check.quotes, check.sources)
    embed.add_field(
        name="Sources", inline=False,
        value=shown or "No matching source was found, so this relies on the AI's general knowledge.",
    )
    embed.set_footer(text=f"Checked by {model} · AI can make mistakes; check the sources")
    return embed


def ready(interaction: discord.Interaction) -> str | None:
    """Why a fact-check can't run right now, or None if it can."""
    bot = interaction.client
    if getattr(bot, "settler", None) is None:
        return "Fact-checking isn't set up: its AI provider has no API key."
    last = bot.last_factcheck.get(interaction.user.id, 0)
    if time.monotonic() - last < COOLDOWN_SECONDS:
        return f"Wait {COOLDOWN_SECONDS - int(time.monotonic() - last)}s before your next fact-check."
    if not bot.chat_budget.take(time.monotonic()):
        return "The AI is busy right now, try again in a minute."
    bot.last_factcheck[interaction.user.id] = time.monotonic()
    return None


@app_commands.context_menu(name="Fact check")
@app_commands.guild_only()
async def factcheck_menu(interaction: discord.Interaction, message: discord.Message) -> None:
    bot = interaction.client
    claim = message.clean_content.strip()
    if not claim:
        await interaction.response.send_message("There's no text in that message to check.", ephemeral=True)
        return
    key = f"factchecked:{message.id}"
    if bot.store.get_state(key):
        await interaction.response.send_message("That message has already been fact-checked.", ephemeral=True)
        return
    if problem := ready(interaction):
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    try:
        context = [(m.author.display_name, m.clean_content) async for m in message.channel.history(limit=8, before=message)]
    except discord.HTTPException:
        context = []
    context.reverse()
    check = await bot.settler.factcheck(claim, context, message.author.display_name)
    if check is None:
        await interaction.followup.send("The AI couldn't be reached. Try again in a minute.")
        return
    bot.store.set_state(key, check.rating)
    note = ""
    author, checker = message.author, interaction.user
    from aura import AURA_ENABLED, aura_text
    if AURA_ENABLED and check.rating == "false" and not author.bot and author.id != checker.id:
        guild_id = interaction.guild_id
        bot.store.add_aura(guild_id, author.id, author.display_name, CAUGHT_POINTS, "false_claim",
                           f"fact-checked: {check.summary[:150]}", claim[:300])
        bot.store.add_aura(guild_id, checker.id, checker.display_name, CHECKER_POINTS, "callout",
                           f"fact-checked {author.display_name}", claim[:300])
        note = f"{author.display_name} {aura_text(CAUGHT_POINTS)} · {checker.display_name} {aura_text(CHECKER_POINTS)}"
    embed = fact_embed(check, author.display_name, bot.settler.last_model)
    if note:
        embed.add_field(name="Aura", value=note, inline=False)
    bot.log_event(interaction.guild, interaction.channel, "ruling", checker, content=check.summary[:500],
                  reason=check.explanation[:1000], model=bot.settler.last_model,
                  detail=f"fact check of {author.display_name}: {claim[:200]} · {check.rating}")
    await interaction.followup.send(f"[Message]({message.jump_url})", embed=embed, allowed_mentions=discord.AllowedMentions.none())


@app_commands.command(name="factcheck", description="Fact-check a claim, with sources")
@app_commands.describe(claim="The claim to check, e.g. 'the Great Wall of China is visible from space'")
@app_commands.guild_only()
async def factcheck_cmd(interaction: discord.Interaction, claim: app_commands.Range[str, 5, 500]) -> None:
    bot = interaction.client
    if problem := ready(interaction):
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    check = await bot.settler.factcheck(claim)
    if check is None:
        await interaction.followup.send("The AI couldn't be reached. Try again in a minute.")
        return
    bot.log_event(interaction.guild, interaction.channel, "ruling", interaction.user, content=check.summary[:500],
                  reason=check.explanation[:1000], model=bot.settler.last_model, detail=f"fact check: {claim[:200]} · {check.rating}")
    await interaction.followup.send(embed=fact_embed(check, None, bot.settler.last_model), allowed_mentions=discord.AllowedMentions.none())
