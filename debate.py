"""Simple parliamentary-style debates.

1. /debate start motion:"..." posts the motion with For / Against buttons; people pick a side.
2. Whoever started it presses Start. The bot opens a thread and calls speakers one at a time, alternating sides.
   Only the person speaking can type in the thread; everyone else's messages are removed until it's their turn.
3. After the last speech everyone else votes for a minute, then the AI judge (the impartial /settleargument one)
   writes a formal decision: the winning side, the reasoning, facts it checked (with web sources when it can
   search) and the best speaker. Winners get aura.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import discord
from discord import app_commands

log = logging.getLogger("spamguard.debate")

SIDES = {"for": "For", "against": "Against"}
ROUND_NAMES = {1: ["Speech"], 2: ["Opening", "Closing"], 3: ["Opening", "Rebuttal", "Closing"]}
VOTE_SECONDS = 60
WARNING_SECONDS = 30

# Debates running right now, by the channel they were started in and by their thread.
by_channel: dict[int, "Debate"] = {}
by_thread: dict[int, "Debate"] = {}


def source_list(sources: list[tuple[str, str]]) -> str:
    """Numbered markdown links that fit in one embed field."""
    lines, size = [], 0
    for number, (title, url) in enumerate(sources, 1):
        line = f"{number}. [{title[:80]}]({url})"
        if size + len(line) + 1 > 1024:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines) or "–"


def quote_list(quotes: list[tuple[str, str, str]]) -> str:
    """Each exact quote with a link to the article it's from, fitting in one embed field."""
    blocks, size = [], 0
    for title, url, quote in quotes:
        text = quote if len(quote) <= 260 else quote[:257].rsplit(" ", 1)[0] + "…"
        block = f"> “{text}”\n— [{title[:60]}]({url})"
        if size + len(block) + 2 > 1024:
            break
        blocks.append(block)
        size += len(block) + 2
    return "\n\n".join(blocks) or "–"


def sources_field(quotes: list[tuple[str, str, str]], sources: list[tuple[str, str]]) -> str | None:
    """Exact quotes when there are any, otherwise plain links; None when there's nothing to show."""
    if quotes:
        return quote_list(quotes)
    if sources:
        return source_list(sources)
    return None


def is_mod(member: discord.Member) -> bool:
    return member.guild_permissions.manage_messages or member.guild_permissions.administrator


def speaking_order(sides: dict[str, list], rounds: int) -> list[tuple[str, str, object]]:
    """(round name, side, member) for every speech: For then Against each round, taking turns within a side."""
    order = []
    for index, round_name in enumerate(ROUND_NAMES[rounds]):
        for side in ("for", "against"):
            members = sides[side]
            order.append((round_name, side, members[index % len(members)]))
    return order


def tally(votes: dict[int, str]) -> dict[str, int]:
    counts = {"for": 0, "against": 0}
    for side in votes.values():
        counts[side] += 1
    return counts


@dataclass
class Debate:
    motion: str
    host: discord.Member
    channel: discord.abc.Messageable
    minutes: int
    rounds: int
    sides: dict[str, list[discord.Member]] = field(default_factory=lambda: {"for": [], "against": []})
    stage: str = "joining"  # joining, speaking, voting, judging, done
    message: discord.Message | None = None
    thread: discord.Thread | None = None
    speaker: discord.Member | None = None
    said: list[str] = field(default_factory=list)  # the current speech so far
    speeches: list[tuple[str, str, str, str]] = field(default_factory=list)  # (round, side, name, text)
    votes: dict[int, str] = field(default_factory=dict)
    turn_over: asyncio.Event = field(default_factory=asyncio.Event)
    warned: set[int] = field(default_factory=set)  # people told not to talk during this speech
    task: asyncio.Task | None = None

    def can_run(self, member: discord.Member) -> bool:
        return member.id == self.host.id or is_mod(member)

    def debaters(self) -> set[int]:
        return {m.id for side in self.sides.values() for m in side}

    def lobby_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🏛️ Debate", description=f"**Motion:** {self.motion}", color=0xE67E22)
        for side, label in SIDES.items():
            names = "\n".join(m.mention for m in self.sides[side]) or "*nobody yet*"
            embed.add_field(name=f"{label} ({len(self.sides[side])})", value=names, inline=True)
        speeches = len(ROUND_NAMES[self.rounds]) * 2
        embed.add_field(
            name="How it works",
            value=(
                f"Pick a side. When {self.host.display_name} presses **Start**, a thread opens and each side takes turns "
                f"giving {speeches} speeches of {self.minutes} min. Only the speaker can type. Then everyone else "
                f"votes and the AI judge decides who won."
            ),
            inline=False,
        )
        return embed

    def finish(self) -> None:
        self.stage = "done"
        by_channel.pop(getattr(self.channel, "id", 0), None)
        if self.thread:
            by_thread.pop(self.thread.id, None)


class LobbyView(discord.ui.View):
    def __init__(self, debate: Debate):
        super().__init__(timeout=1800)
        self.debate = debate

    async def join(self, interaction: discord.Interaction, side: str) -> None:
        debate = self.debate
        other = "against" if side == "for" else "for"
        debate.sides[other] = [m for m in debate.sides[other] if m.id != interaction.user.id]
        if all(m.id != interaction.user.id for m in debate.sides[side]):
            debate.sides[side].append(interaction.user)
        await interaction.response.edit_message(embed=debate.lobby_embed(), view=self)

    @discord.ui.button(label="Join For", style=discord.ButtonStyle.success)
    async def join_for(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.join(interaction, "for")

    @discord.ui.button(label="Join Against", style=discord.ButtonStyle.danger)
    async def join_against(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.join(interaction, "against")

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        debate = self.debate
        for side in debate.sides:
            debate.sides[side] = [m for m in debate.sides[side] if m.id != interaction.user.id]
        await interaction.response.edit_message(embed=debate.lobby_embed(), view=self)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        debate = self.debate
        if not debate.can_run(interaction.user):
            await interaction.response.send_message(f"Only {debate.host.display_name} or a mod can start it.", ephemeral=True)
            return
        if not debate.sides["for"] or not debate.sides["against"]:
            await interaction.response.send_message("Each side needs at least one person first.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        debate.task = asyncio.create_task(run(interaction.client, debate))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        debate = self.debate
        if not debate.can_run(interaction.user):
            await interaction.response.send_message(f"Only {debate.host.display_name} or a mod can cancel it.", ephemeral=True)
            return
        self.stop()
        debate.finish()
        await interaction.response.edit_message(content="Debate cancelled.", embed=debate.lobby_embed(), view=None)

    async def on_timeout(self) -> None:
        if self.debate.stage == "joining":
            self.debate.finish()
            if self.debate.message:
                try:
                    await self.debate.message.edit(content="Debate expired (nobody started it).", view=None)
                except discord.HTTPException:
                    pass


class TurnView(discord.ui.View):
    def __init__(self, debate: Debate):
        super().__init__(timeout=None)
        self.debate = debate

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        debate = self.debate
        if debate.speaker is None or interaction.user.id != debate.speaker.id:
            await interaction.response.send_message("Only the speaker can end their speech.", ephemeral=True)
            return
        await interaction.response.defer()
        debate.turn_over.set()

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        debate = self.debate
        if not debate.can_run(interaction.user):
            await interaction.response.send_message(f"Only {debate.host.display_name} or a mod can skip a speaker.", ephemeral=True)
            return
        await interaction.response.defer()
        debate.turn_over.set()


class VoteView(discord.ui.View):
    def __init__(self, debate: Debate):
        super().__init__(timeout=VOTE_SECONDS + 5)
        self.debate = debate

    async def vote(self, interaction: discord.Interaction, side: str) -> None:
        debate = self.debate
        if interaction.user.id in debate.debaters():
            await interaction.response.send_message("Debaters can't vote.", ephemeral=True)
            return
        if debate.stage != "voting":
            await interaction.response.send_message("Voting is closed.", ephemeral=True)
            return
        debate.votes[interaction.user.id] = side
        await interaction.response.send_message(f"Voted {SIDES[side]}.", ephemeral=True)

    @discord.ui.button(label="Vote For", style=discord.ButtonStyle.success)
    async def vote_for(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.vote(interaction, "for")

    @discord.ui.button(label="Vote Against", style=discord.ButtonStyle.danger)
    async def vote_against(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.vote(interaction, "against")


async def give_speech(debate: Debate, round_name: str, side: str, member: discord.Member) -> None:
    debate.speaker, debate.said, debate.warned = member, [], set()
    debate.turn_over.clear()
    view = TurnView(debate)
    prompt = await debate.thread.send(
        f"🎤 **{round_name} speech, {SIDES[side]}:** {member.mention}, you have **{debate.minutes} min**. "
        f"Type your argument here, then press **Done**.",
        view=view, allowed_mentions=discord.AllowedMentions(users=[member]),
    )
    seconds = debate.minutes * 60
    try:
        await asyncio.wait_for(debate.turn_over.wait(), timeout=seconds - WARNING_SECONDS)
    except asyncio.TimeoutError:
        await debate.thread.send(f"⏳ {WARNING_SECONDS} seconds left, {member.display_name}.", allowed_mentions=discord.AllowedMentions.none())
        try:
            await asyncio.wait_for(debate.turn_over.wait(), timeout=WARNING_SECONDS)
        except asyncio.TimeoutError:
            await debate.thread.send("⏱️ Time's up.")
    view.stop()
    try:
        await prompt.edit(view=None)
    except discord.HTTPException:
        pass
    debate.speeches.append((round_name, SIDES[side], member.display_name, "\n".join(debate.said)))
    debate.speaker = None


async def run(bot, debate: Debate) -> None:
    try:
        debate.thread = await debate.message.create_thread(name=f"Debate: {debate.motion}"[:100], auto_archive_duration=1440)
    except discord.HTTPException as e:
        log.warning("Couldn't open a debate thread: %s", e)
        await debate.channel.send("I couldn't open a thread for the debate (I need the Create Public Threads permission).")
        debate.finish()
        return
    by_thread[debate.thread.id] = debate
    debate.stage = "speaking"
    try:
        await debate.thread.send(
            f"**Motion:** {debate.motion}\n"
            f"**For:** {', '.join(m.display_name for m in debate.sides['for'])}\n"
            f"**Against:** {', '.join(m.display_name for m in debate.sides['against'])}\n\n"
            "Only the person speaking can type here. Everyone else: watch, then vote at the end.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        for round_name, side, member in speaking_order(debate.sides, debate.rounds):
            if debate.stage == "done":
                return
            await give_speech(debate, round_name, side, member)

        debate.stage = "voting"
        vote_view = VoteView(debate)
        ballot = await debate.thread.send(f"🗳️ **Vote!** Everyone except the debaters has {VOTE_SECONDS} seconds.", view=vote_view)
        await asyncio.sleep(VOTE_SECONDS)
        debate.stage = "judging"
        vote_view.stop()
        try:
            await ballot.edit(content="🗳️ Voting closed.", view=None)
        except discord.HTTPException:
            pass
        await announce(bot, debate)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Debate failed")
        await debate.thread.send("Something went wrong running the debate.")
    finally:
        debate.finish()


async def announce(bot, debate: Debate) -> None:
    from aura import give_debate_aura

    votes = tally(debate.votes)
    ruling = None
    judge = getattr(bot, "settler", None)
    if judge is not None:
        async with debate.thread.typing():
            ruling = await judge.adjudicate(debate.motion, debate.speeches, bot.persona)
    vote_line = f"For {votes['for']} · Against {votes['against']}"
    if ruling is None:
        winner = max(votes, key=votes.get) if votes["for"] != votes["against"] else "draw"
        embed = discord.Embed(title=f"🏛️ Result: {debate.motion}"[:256], color=0xE67E22,
                              description="The AI judge couldn't be reached, so the audience vote decides.")
    else:
        winner = ruling.winner
        embed = discord.Embed(title=f"🏛️ Result: {debate.motion}"[:256], description=f"**{ruling.verdict}**"[:4096], color=0xE67E22)
        embed.add_field(name="Reasoning", value=(ruling.reasoning or "–")[:1024], inline=False)
        if ruling.facts:
            embed.add_field(name="Facts checked", value="\n".join(f"• {fact}" for fact in ruling.facts)[:1024], inline=False)
        if shown := sources_field(ruling.quotes, ruling.sources):
            embed.add_field(name="Sources", value=shown, inline=False)
        embed.add_field(name="Best speaker", value=(ruling.best_speaker or "–")[:1024], inline=True)
        embed.set_footer(text=f"Adjudicated by {judge.last_model} · AI can make mistakes")
    winners = debate.sides.get(winner, [])
    title = f"{SIDES[winner]} wins ({', '.join(m.display_name for m in winners)})" if winners else "It's a draw"
    embed.insert_field_at(0, name="Winner", value=title[:1024], inline=False)
    embed.add_field(name="Audience vote", value=vote_line, inline=True)
    await debate.thread.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    if debate.message:
        try:
            await debate.message.edit(content=f"Debate over: **{title}**", view=None)
        except discord.HTTPException:
            pass
    give_debate_aura(bot.store, debate.thread.guild.id, winners, debate.motion)
    if ruling is not None:
        bot.log_event(
            debate.thread.guild, debate.channel, "ruling", debate.host, content=ruling.verdict[:500],
            reason=ruling.reasoning[:1000], model=judge.last_model,
            detail=f"debate: {debate.motion[:200]} · winner: {winner} · audience {vote_line}",
        )


async def on_thread_message(debate: Debate, message: discord.Message) -> None:
    """During a speech only the speaker talks; anyone else's message is removed (with one short notice)."""
    if debate.stage != "speaking" or debate.speaker is None:
        return
    if message.author.id == debate.speaker.id:
        debate.said.append(message.clean_content)
        return
    try:
        await message.delete()
    except discord.HTTPException:
        return
    if message.author.id not in debate.warned:
        debate.warned.add(message.author.id)
        await message.channel.send(
            f"{message.author.mention} only {debate.speaker.display_name} can talk right now.",
            delete_after=6, allowed_mentions=discord.AllowedMentions(users=[message.author]),
        )


debate_commands = app_commands.Group(name="debate", description="Parliamentary-style debates", guild_only=True)


@debate_commands.command(name="start", description="Start a debate: people pick a side, then take turns giving speeches")
@app_commands.describe(
    motion="What's being debated, e.g. 'This house would ban homework'",
    minutes="Minutes per speech (default 2)",
    rounds="1 = one speech each, 2 = opening + closing, 3 = opening + rebuttal + closing (default 2)",
)
async def start_cmd(
    interaction: discord.Interaction, motion: app_commands.Range[str, 3, 200],
    minutes: app_commands.Range[int, 1, 5] = 2, rounds: app_commands.Range[int, 1, 3] = 2,
) -> None:
    if isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("Start debates in a normal channel, not a thread.", ephemeral=True)
        return
    if interaction.channel_id in by_channel:
        await interaction.response.send_message("There's already a debate going in this channel.", ephemeral=True)
        return
    debate = Debate(motion=motion, host=interaction.user, channel=interaction.channel, minutes=minutes, rounds=rounds)
    by_channel[interaction.channel_id] = debate
    view = LobbyView(debate)
    await interaction.response.send_message(embed=debate.lobby_embed(), view=view)
    debate.message = await interaction.original_response()


@debate_commands.command(name="stop", description="End the debate in this channel (whoever started it, or a mod)")
async def stop_cmd(interaction: discord.Interaction) -> None:
    debate = by_thread.get(interaction.channel_id) or by_channel.get(interaction.channel_id)
    if debate is None:
        await interaction.response.send_message("There's no debate running here.", ephemeral=True)
        return
    if not debate.can_run(interaction.user):
        await interaction.response.send_message(f"Only {debate.host.display_name} or a mod can stop it.", ephemeral=True)
        return
    if debate.task:
        debate.task.cancel()
    debate.finish()
    await interaction.response.send_message("Debate stopped.")
