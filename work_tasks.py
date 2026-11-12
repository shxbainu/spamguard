"""The interactive /work tasks: each is a private message with buttons or a menu, played by tapping rather than
typing. A task resolves `result` with (passed, a short note) and takes its buttons away when it's over."""
from __future__ import annotations

import asyncio
import random
import time

import discord

LOOKALIKES = [("🍎", "🍅"), ("😀", "😃"), ("🐶", "🐕"), ("🌑", "🌚"), ("🟥", "🟧"), ("💙", "💜"), ("🍊", "🍑"), ("🐱", "🐈")]
COLORS = {"red": ("🟥", discord.ButtonStyle.danger), "green": ("🟩", discord.ButtonStyle.success),
          "blue": ("🟦", discord.ButtonStyle.primary), "grey": ("⬜", discord.ButtonStyle.secondary)}
PAIR_EMOJIS = ["🍕", "🐸", "🚀", "🎸", "🦆", "🍩", "⚽", "👑", "🧃", "🌵"]
CATEGORIES = {
    "animals": ["Dog", "Penguin", "Shark", "Owl", "Horse", "Frog", "Tiger", "Rabbit"],
    "foods": ["Pizza", "Taco", "Sushi", "Burger", "Pancake", "Noodles", "Waffle", "Burrito"],
    "sports": ["Soccer", "Hockey", "Tennis", "Boxing", "Golf", "Rugby", "Cricket", "Bowling"],
    "colors": ["Purple", "Orange", "Yellow", "Maroon", "Teal", "Crimson", "Indigo", "Beige"],
}
REACTION_LIMIT_MS = 1000


def stamp(when) -> float | None:
    return when.timestamp() if when is not None else None


class TaskView(discord.ui.View):
    name = "Task"
    seconds = 20

    def __init__(self, member: discord.abc.User, rng: random.Random = random):
        super().__init__(timeout=self.seconds)
        self.member, self.rng = member, rng
        self.result: asyncio.Future = asyncio.get_running_loop().create_future()
        self.message = None  # the private message it's shown in

    def prompt(self) -> str:
        raise NotImplementedError

    def finish(self, passed: bool, note: str = "") -> None:
        if not self.result.done():
            self.result.set_result((passed, note))
        self.stop()

    async def begin(self) -> None:
        """Runs once the message is showing (for tasks that change over time)."""

    async def edit(self, content: str | None = None, view: discord.ui.View | None | bool = True) -> object | None:
        """Edits the task's message; `view` True keeps this view, None takes the buttons away."""
        if self.message is None:
            return None
        try:
            return await self.message.edit(content=content, view=self if view is True else view)
        except discord.HTTPException:
            return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("This isn't your shift.", ephemeral=True)
            return False
        if self.result.done():
            await interaction.response.defer()
            return False
        return True

    async def on_timeout(self) -> None:
        if not self.result.done():
            self.finish(False, "Too slow.")
            await self.edit(f"{self.prompt()}\n⏰ Too slow.", None)


class FindIt(TaskView):
    """A grid of the same emoji with one lookalike; tap the odd one."""
    name, seconds = "🔍 Find it", 15

    def __init__(self, member, rng=random):
        super().__init__(member, rng)
        same, odd = rng.choice(LOOKALIKES)
        if rng.random() < 0.5:
            same, odd = odd, same
        self.odd_emoji, self.spot = odd, rng.randrange(20)
        for i in range(20):
            button = discord.ui.Button(emoji=odd if i == self.spot else same, style=discord.ButtonStyle.secondary, row=i // 5)
            button.callback = self.tap(i)
            self.add_item(button)

    def prompt(self) -> str:
        return "One of these is different. Tap it."

    def tap(self, i: int):
        async def callback(interaction: discord.Interaction) -> None:
            passed = i == self.spot
            self.finish(passed, "" if passed else "Wrong one.")
            await interaction.response.edit_message(content=f"{self.prompt()}\n{'✅ Got it.' if passed else '❌ Wrong one.'}", view=None)
        return callback


class TapCount(TaskView):
    """Press +1 exactly N times without seeing the count, then Done."""
    name, seconds = "👆 Tap count", 20

    def __init__(self, member, rng=random):
        super().__init__(member, rng)
        self.target, self.count = rng.randint(8, 15), 0

    def prompt(self) -> str:
        return f"Press **+1** exactly **{self.target}** times, then **Done**. The counter is hidden, so count in your head."

    @discord.ui.button(label="+1", style=discord.ButtonStyle.primary)
    async def plus(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.count += 1
        await interaction.response.defer()

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        passed = self.count == self.target
        self.finish(passed, "" if passed else f"You pressed {self.count}.")
        await interaction.response.edit_message(
            content=f"{self.prompt()}\n{'✅' if passed else '❌'} You pressed it **{self.count}** times.", view=None)


class PickAll(TaskView):
    """A dropdown of 8; select exactly the ones in a category."""
    name, seconds = "✅ Pick them all", 20

    def __init__(self, member, rng=random):
        super().__init__(member, rng)
        self.category = rng.choice(list(CATEGORIES))
        others = [item for name, items in CATEGORIES.items() if name != self.category for item in items]
        self.right = set(rng.sample(CATEGORIES[self.category], rng.randint(2, 4)))
        options = list(self.right) + rng.sample(others, 8 - len(self.right))
        rng.shuffle(options)
        menu = discord.ui.Select(placeholder="Select all that fit", min_values=1, max_values=len(options),
                                 options=[discord.SelectOption(label=o) for o in options])
        menu.callback = self.picked
        self.menu = menu
        self.add_item(menu)

    def prompt(self) -> str:
        return f"Select **all** the {self.category}, and nothing else."

    async def picked(self, interaction: discord.Interaction) -> None:
        chosen = set(self.menu.values)
        passed = chosen == self.right
        self.finish(passed, "" if passed else f"It was {', '.join(sorted(self.right))}.")
        await interaction.response.edit_message(
            content=f"{self.prompt()}\n{'✅ All of them.' if passed else '❌ Not quite: ' + ', '.join(sorted(self.right))}", view=None)


class Simon(TaskView):
    """Watch a sequence of colours, then tap it back."""
    name, seconds = "🎨 Simon says", 30
    SHOW = 3.0

    def __init__(self, member, rng=random):
        super().__init__(member, rng)
        self.sequence = [rng.choice(list(COLORS)) for _ in range(5)]
        self.position, self.watching = 0, True
        for color, (emoji, style) in COLORS.items():
            button = discord.ui.Button(emoji=emoji, style=style, disabled=True)
            button.callback = self.tap(color)
            self.add_item(button)

    def shown(self) -> str:
        return "".join(COLORS[c][0] for c in self.sequence)

    def prompt(self) -> str:
        return f"Remember this: **{self.shown()}**" if self.watching else "Now tap it back in order."

    def progress(self) -> str:
        return "✅" * self.position + "⬜" * (len(self.sequence) - self.position)

    async def begin(self) -> None:
        await asyncio.sleep(self.SHOW)
        self.watching = False
        for item in self.children:
            item.disabled = False
        if not self.result.done():
            await self.edit(f"{self.prompt()}\n{self.progress()}")

    def tap(self, color: str):
        async def callback(interaction: discord.Interaction) -> None:
            if self.watching:
                await interaction.response.defer()
                return
            if color != self.sequence[self.position]:
                self.finish(False, "Wrong colour.")
                await interaction.response.edit_message(content=f"❌ Wrong colour. It was **{self.shown()}**", view=None)
                return
            self.position += 1
            if self.position == len(self.sequence):
                self.finish(True)
                await interaction.response.edit_message(content=f"✅ {self.shown()}", view=None)
            else:
                await interaction.response.edit_message(content=f"{self.prompt()}\n{self.progress()}", view=self)
        return callback


class GreenLight(TaskView):
    """Wait for green, then tap within a second (timed with Discord's clock); tapping early fails."""
    name, seconds = "🚦 Green light", 15

    def __init__(self, member, rng=random):
        super().__init__(member, rng)
        self.lit_at: float | None = None

    def prompt(self) -> str:
        return f"Tap the button the moment it turns **green** (within {REACTION_LIMIT_MS / 1000:g}s). Too early fails."

    async def begin(self) -> None:
        await asyncio.sleep(self.rng.uniform(1.5, 5.0))
        if self.result.done():
            return
        self.light.label, self.light.style = "NOW!", discord.ButtonStyle.success
        sent = await self.edit(f"{self.prompt()}\n## 🟢")
        self.lit_at = stamp(getattr(sent, "edited_at", None)) or time.time()

    @discord.ui.button(label="Wait…", style=discord.ButtonStyle.danger)
    async def light(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        clicked = stamp(interaction.created_at) or time.time()
        if self.lit_at is None or clicked < self.lit_at:
            self.finish(False, "Too early.")
            await interaction.response.edit_message(content="❌ Too early.", view=None)
            return
        ms = round((clicked - self.lit_at) * 1000)
        passed = ms <= REACTION_LIMIT_MS
        self.finish(passed, "" if passed else f"{ms:,} ms is too slow.")
        await interaction.response.edit_message(content=f"{'✅' if passed else '❌'} {ms:,} ms", view=None)


class Whack(TaskView):
    """A mole pops up 6 times in a 3x3 grid; hit at least 4, without hitting empty holes more than twice."""
    name, seconds = "🔨 Whack-a-mole", 30
    ROUNDS, NEEDED, MAX_MISSES, UP = 6, 4, 2, 1.6  # seconds a mole stays up, with room for lag

    def __init__(self, member, rng=random):
        super().__init__(member, rng)
        self.mole: int | None = None
        self.hits = self.misses = 0
        self.hit = asyncio.Event()
        self.holes = []
        for i in range(9):
            button = discord.ui.Button(emoji="🕳️", style=discord.ButtonStyle.secondary, row=i // 3)
            button.callback = self.tap(i)
            self.holes.append(button)
            self.add_item(button)

    def prompt(self) -> str:
        return f"Hit the 🐹 when it pops up: {self.NEEDED} of {self.ROUNDS}, and don't hit empty holes more than {self.MAX_MISSES} times."

    def score(self) -> str:
        return f"Hits **{self.hits}** · misses **{self.misses}**"

    def show_mole(self, spot: int | None) -> None:
        self.mole = spot
        for i, button in enumerate(self.holes):
            button.emoji = "🐹" if i == spot else "🕳️"

    async def begin(self) -> None:
        await asyncio.sleep(1)
        for _ in range(self.ROUNDS):
            if self.result.done():
                return
            self.hit.clear()
            self.show_mole(self.rng.randrange(9))
            await self.edit(f"{self.prompt()}\n{self.score()}")
            try:
                await asyncio.wait_for(self.hit.wait(), self.UP)
            except asyncio.TimeoutError:
                pass
        self.show_mole(None)
        passed = self.hits >= self.NEEDED
        self.finish(passed, "" if passed else f"Only {self.hits} hits.")
        await self.edit(f"{'✅' if passed else '❌'} {self.score()}", None)

    def tap(self, i: int):
        async def callback(interaction: discord.Interaction) -> None:
            if i == self.mole and not self.hit.is_set():
                self.hits += 1
                self.hit.set()
                self.show_mole(None)
            else:
                self.misses += 1
                if self.misses > self.MAX_MISSES:
                    self.finish(False, "Too many misses.")
                    await interaction.response.edit_message(content=f"❌ Too many misses. {self.score()}", view=None)
                    return
            await interaction.response.edit_message(content=f"{self.prompt()}\n{self.score()}", view=self)
        return callback


class Pairs(TaskView):
    """Flip cards two at a time to find all 6 pairs, with at most 6 misses."""
    name, seconds = "🃏 Memory pairs", 60
    MAX_MISSES = 6

    def __init__(self, member, rng=random):
        super().__init__(member, rng)
        emojis = rng.sample(PAIR_EMOJIS, 6) * 2
        rng.shuffle(emojis)
        self.cards, self.found, self.first, self.misses, self.busy = emojis, set(), None, 0, False
        self.buttons = []
        for i in range(12):
            button = discord.ui.Button(label="?", style=discord.ButtonStyle.secondary, row=i // 4)
            button.callback = self.flip(i)
            self.buttons.append(button)
            self.add_item(button)

    def prompt(self) -> str:
        return f"Find all 6 pairs. Misses: **{self.misses}/{self.MAX_MISSES}**"

    def show(self, *face_up: int) -> None:
        for i, button in enumerate(self.buttons):
            up = i in self.found or i in face_up
            button.label, button.emoji = (None, self.cards[i]) if up else ("?", None)
            button.style = discord.ButtonStyle.success if i in self.found else discord.ButtonStyle.secondary

    def flip(self, i: int):
        async def callback(interaction: discord.Interaction) -> None:
            if self.busy or i in self.found or i == self.first:
                await interaction.response.defer()
                return
            if self.first is None:
                self.first = i
                self.show(i)
                await interaction.response.edit_message(content=self.prompt(), view=self)
                return
            first, self.first = self.first, None
            if self.cards[first] == self.cards[i]:
                self.found |= {first, i}
                self.show()
                if len(self.found) == 12:
                    self.finish(True)
                    await interaction.response.edit_message(content=f"✅ All pairs, {self.misses} misses.", view=None)
                else:
                    await interaction.response.edit_message(content=self.prompt(), view=self)
                return
            self.misses += 1
            if self.misses > self.MAX_MISSES:
                self.show(first, i)
                self.finish(False, "Too many misses.")
                await interaction.response.edit_message(content="❌ Too many misses.", view=None)
                return
            self.show(first, i)
            self.busy = True
            await interaction.response.edit_message(content=self.prompt(), view=self)
            await asyncio.sleep(0.9)  # long enough to see them, then they flip back
            self.busy = False
            self.show()
            if not self.result.done():
                await self.edit(self.prompt())
        return callback


TASKS: dict[str, type[TaskView]] = {
    "findit": FindIt, "whack": Whack, "simon": Simon, "greenlight": GreenLight, "pairs": Pairs, "pickall": PickAll,
    "tapcount": TapCount,
}
