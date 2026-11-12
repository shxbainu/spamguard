"""Tests for /work, aura drops and /beg."""
import asyncio
import random
import unittest
import unittest.mock
from types import SimpleNamespace

import work
from test_features import temp_store
from test_games import person


class TaskTests(unittest.TestCase):
    def test_disguised_phrases_catch_copy_paste(self):
        phrase = "i love shxbainu and this server"
        shown = work.disguise(phrase, random.Random(1))
        self.assertNotEqual(shown, phrase, "invisible characters were mixed in")
        self.assertEqual(work.INVISIBLE_RE.sub("", shown), phrase, "but it reads the same")
        self.assertEqual(work.check_typed(shown, phrase, 10), "copied", "pasted text carries the invisible characters")
        self.assertEqual(work.check_typed(phrase, phrase, 0.5), "copied", "faster than anyone types")
        self.assertEqual(work.check_typed("I love Shxbainu and this server!", phrase, 6), "ok", "case and punctuation don't matter")
        self.assertEqual(work.check_typed("i love ben and this server", phrase, 6), "wrong")

    def test_every_task_has_a_checkable_answer(self):
        for seed in range(50):
            for kind in work.TASK_KINDS:
                task = work.make_task(kind, random.Random(seed))
                self.assertEqual(task["kind"], kind)
                seconds = 60 if kind == "type" else 2
                self.assertEqual(work.check_answer(task, task["answer"], seconds), "ok", task)
                self.assertEqual(work.check_answer(task, task["answer"] + "x", seconds), "wrong", task)

    def test_task_details(self):
        count = work.make_task("count", random.Random(3))
        target = count["prompt"].split("How many ")[1].split(" ")[0]
        row = count["prompt"].split("\n")[1]
        self.assertEqual(int(count["answer"]), row.count(target))
        backwards = work.make_task("backwards", random.Random(4))
        word = work.INVISIBLE_RE.sub("", backwards["prompt"].split("**")[1])
        self.assertEqual(backwards["answer"], word[::-1])
        self.assertEqual(work.check_answer(backwards, work.disguise(backwards["answer"]) if len(word) > 2 else "x", 5), "copied")
        memory = work.make_task("memory", random.Random(5))
        self.assertIn("hidden", memory)
        self.assertEqual(work.check_answer(memory, " ".join(memory["answer"]), 5), "ok", "spaces between digits are fine")
        self.assertNotEqual(work.make_drop(random.Random(6))["kind"], "memory")

    def test_new_tasks(self):
        rng = random.Random(11)
        missing = work.make_task("missing", rng)
        numbers = [n.strip() for n in missing["prompt"].split("**")[1].split(",")]
        self.assertIn("__", numbers)
        odd = work.make_task("oddone", rng)
        row = odd["prompt"].split("\n")[1]
        self.assertEqual(len(set(row)), 2, "one lookalike in a row of the same emoji")
        caps = work.make_task("capitals", rng)
        shown = work.INVISIBLE_RE.sub("", caps["prompt"].split("**")[1])
        self.assertEqual(caps["answer"], "".join(ch for ch in shown if ch.isupper()))
        blanks = work.make_task("blanks", rng)
        self.assertEqual(len(blanks["prompt"].split("**")[1]), len(blanks["answer"]))
        sequence = work.make_task("sequence", rng)
        self.assertIn("hidden", sequence)
        self.assertEqual(work.check_answer(sequence, " ".join(reversed(sequence["answer"].split())), 5), "wrong", "order matters")
        self.assertTrue(all(work.make_drop(random.Random(i))["kind"] not in work.PRIVATE_KINDS for i in range(100)))
        self.assertGreaterEqual(len(work.TASK_KINDS), 8)

    def test_beg(self):
        results = [work.beg(random.Random(i)) for i in range(400)]
        nothing = sum(1 for amount, _ in results if amount == 0)
        self.assertTrue(100 < nothing < 180, nothing)
        self.assertTrue(all(0 <= amount <= 25 for amount, _ in results))
        self.assertTrue(any(amount >= 15 for amount, _ in results), "now and then more")
        usual = [amount for amount, _ in results if 0 < amount < 15]
        self.assertLessEqual(max(usual), 10)


class Recorder:
    """A fake interaction for /work: records what's sent, and runs `on_view` for each task message with buttons."""

    def __init__(self, user, on_view=None):
        self.user, self.channel = user, SimpleNamespace(id=7)
        self.sent, self.on_view = [], on_view
        outer = self

        class Message:
            async def edit(self, content=None, **kwargs):
                outer.sent.append(content)
                return SimpleNamespace(edited_at=None)

        async def send_message(text=None, **kwargs):
            outer.sent.append(text)

        async def followup(text=None, view=None, wait=False, **kwargs):
            outer.sent.append(text)
            if view is not None and outer.on_view:
                asyncio.get_running_loop().create_task(outer.on_view(view))
            return Message()
        self.response = SimpleNamespace(send_message=send_message)
        self.followup = SimpleNamespace(send=followup)

    def said(self, text):
        return any(text in t for t in self.sent if t)


def tap(user):
    """A fake button press by `user`."""
    edits = []

    async def edit_message(**kwargs):
        edits.append(kwargs)

    async def defer():
        pass

    async def send_message(text=None, **kwargs):
        edits.append({"content": text})
    return SimpleNamespace(user=user, created_at=None, edits=edits,
                           response=SimpleNamespace(edit_message=edit_message, defer=defer, send_message=send_message))


async def solve(view):
    """Plays a task perfectly."""
    import work_tasks as wt
    await asyncio.sleep(0)
    who = view.member
    if isinstance(view, wt.FindIt):
        await view.children[view.spot].callback(tap(who))
    elif isinstance(view, wt.TapCount):
        for _ in range(view.target):
            await view.plus.callback(tap(who))
        await view.done.callback(tap(who))
    elif isinstance(view, wt.PickAll):
        view.menu._values = list(view.right)
        await view.picked(tap(who))


class ShiftTests(unittest.TestCase):
    def setUp(self):
        self.store = temp_store()
        self.ben = person(5, "ben")

    def run_shift(self, pool, typed=(), on_view=solve):
        typed = list(typed)
        bot = SimpleNamespace(store=self.store)
        worker = work.Work(bot)
        deleted = []

        async def wait_for(event, timeout, check):
            if not typed:
                raise asyncio.TimeoutError
            answer = typed.pop(0)
            content = answer(worker.current) if callable(answer) else answer

            async def delete():
                deleted.append(content)
            message = SimpleNamespace(channel=SimpleNamespace(id=7), author=self.ben, content=content, delete=delete)
            assert check(message)
            return message
        bot.wait_for = wait_for
        original = work.make_task

        def remember(kind=None, rng=random):
            worker.current = original(kind, rng)
            return worker.current
        interaction = Recorder(self.ben, on_view)
        with unittest.mock.patch.object(work, "WORK_POOL", pool), unittest.mock.patch.object(work, "make_task", remember), \
                unittest.mock.patch.object(work, "MAX_CHARS_PER_SECOND", 1e9):  # the speed check has its own test
            asyncio.run(worker.shift(interaction))
        return worker, interaction, deleted

    def test_interactive_shift_pays(self):
        worker, interaction, _ = self.run_shift(["findit", "tapcount", "pickall", "type"], typed=[lambda t: t["answer"]])
        total = self.store.aura_total(1, 5) - 500  # the casino's starting aura
        n = work.WORK_PHRASES
        self.assertTrue(n * work.WORK_PAY[0] + work.WORK_BONUS <= total <= n * work.WORK_PAY[1] + work.WORK_BONUS, total)
        self.assertTrue(interaction.said("Shift done"))
        self.assertEqual(worker.workers, set())
        self.assertIn("work again", worker.shift_problem(self.ben, __import__("time").time()), "cooldown")

    def test_failing_a_button_task_means_no_pay(self):
        async def wrong(view):
            await asyncio.sleep(0)
            await view.children[(view.spot + 1) % 20].callback(tap(view.member))
        self.run_shift(["findit"] * 4, on_view=wrong)
        self.assertEqual(self.store.aura_total(1, 5), 500, "all or nothing")

    def test_copy_paste_ends_the_shift_with_nothing(self):
        right = lambda task: task["answer"]
        pasted = lambda task: work.disguise(task["answer"])
        worker, interaction, deleted = self.run_shift(["type"] * 4, typed=[right, pasted])
        self.assertEqual(self.store.aura_total(1, 5), 500)
        self.assertTrue(interaction.said("Caught copy-pasting"))
        self.assertEqual(len(deleted), 2, "typed answers are deleted to keep the channel clean")

    def test_running_out_of_time_means_no_pay(self):
        right = lambda task: task["answer"]
        worker, interaction, _ = self.run_shift(["type"] * 4, typed=[right, right, right])
        self.assertEqual(self.store.aura_total(1, 5), 500)
        self.assertTrue(interaction.said("all or nothing"))

    def test_daily_limit(self):
        worker = work.Work(SimpleNamespace(store=self.store))
        now = __import__("time").time()
        for _ in range(work.WORK_SHIFTS_PER_DAY):
            worker.end_shift(self.ben, now - 5, 0)
        self.assertIn("shifts today", worker.shift_problem(self.ben, now))


class InteractiveTaskTests(unittest.TestCase):
    def play(self, cls, steps, seed=1):
        import work_tasks as wt
        ben = person(5, "ben")

        async def go():
            view = cls(ben, random.Random(seed))
            view.message = SimpleNamespace(edits=[])

            async def edit(content=None, **kwargs):
                view.message.edits.append(content)
                return SimpleNamespace(edited_at=None)
            view.message.edit = edit
            await steps(view, ben)
            return view, (view.result.result() if view.result.done() else None)
        with unittest.mock.patch.object(wt.asyncio, "sleep", fast_sleep):
            return asyncio.run(go())

    def test_find_it(self):
        import work_tasks as wt
        view, result = self.play(wt.FindIt, lambda v, b: v.children[v.spot].callback(tap(b)))
        self.assertEqual(result[0], True)
        self.assertEqual(len(view.children), 20)
        self.assertEqual(sum(1 for c in view.children if str(c.emoji) == view.odd_emoji), 1)
        _, result = self.play(wt.FindIt, lambda v, b: v.children[(v.spot + 3) % 20].callback(tap(b)))
        self.assertFalse(result[0])

    def test_tap_count(self):
        import work_tasks as wt

        async def off_by_one(view, who):
            for _ in range(view.target + 1):
                await view.plus.callback(tap(who))
            await view.done.callback(tap(who))
        _, result = self.play(wt.TapCount, off_by_one)
        self.assertFalse(result[0])

    def test_pick_all(self):
        import work_tasks as wt

        async def pick_one_too_many(view, who):
            extra = next(o.label for o in view.menu.options if o.label not in view.right)
            view.menu._values = [*view.right, extra]
            await view.picked(tap(who))
        _, result = self.play(wt.PickAll, pick_one_too_many)
        self.assertFalse(result[0])

    def test_simon(self):
        import work_tasks as wt

        async def repeat(view, who):
            buttons = {c: b for c, b in zip(wt.COLORS, view.children)}
            await buttons[view.sequence[0]].callback(tap(who))
            self.assertEqual(view.position, 0, "can't tap while it's showing")
            await view.begin()
            for color in view.sequence:
                await buttons[color].callback(tap(who))
        _, result = self.play(wt.Simon, repeat)
        self.assertTrue(result[0])

        async def mistake(view, who):
            await view.begin()
            wrong = next(c for c in wt.COLORS if c != view.sequence[0])
            await dict(zip(wt.COLORS, view.children))[wrong].callback(tap(who))
        _, result = self.play(wt.Simon, mistake)
        self.assertFalse(result[0])

    def test_green_light(self):
        import work_tasks as wt

        async def early(view, who):
            await view.light.callback(tap(who))
        _, result = self.play(wt.GreenLight, early)
        self.assertEqual(result, (False, "Too early."))

        async def on_time(view, who):
            await view.begin()
            await view.light.callback(tap(who))
        _, result = self.play(wt.GreenLight, on_time)
        self.assertTrue(result[0])

    def test_whack(self):
        import work_tasks as wt

        async def hit_them_all(view, who):
            async def whack():
                for _ in range(view.ROUNDS):
                    while view.mole is None and not view.result.done():
                        await real_sleep(0)
                    if view.result.done():
                        return
                    await view.holes[view.mole].callback(tap(who))
            await asyncio.gather(view.begin(), whack())
        view, result = self.play(wt.Whack, hit_them_all)
        self.assertTrue(result[0], result)
        self.assertEqual(view.hits, view.ROUNDS)

        async def flail(view, who):
            for i in range(view.MAX_MISSES + 1):
                await view.holes[i].callback(tap(who))
        _, result = self.play(wt.Whack, flail)
        self.assertEqual(result, (False, "Too many misses."))

    def test_pairs(self):
        import work_tasks as wt

        async def perfect(view, who):
            spots = {}
            for i, emoji in enumerate(view.cards):
                spots.setdefault(emoji, []).append(i)
            for a, b in spots.values():
                await view.buttons[a].callback(tap(who))
                await view.buttons[b].callback(tap(who))
        view, result = self.play(wt.Pairs, perfect)
        self.assertTrue(result[0])
        self.assertEqual(view.misses, 0)

        async def guess_badly(view, who):
            wrong = [(a, b) for a in range(12) for b in range(12) if a < b and view.cards[a] != view.cards[b]]
            for a, b in wrong[: view.MAX_MISSES + 1]:
                await view.buttons[a].callback(tap(who))
                await view.buttons[b].callback(tap(who))
        _, result = self.play(wt.Pairs, guess_badly)
        self.assertEqual(result, (False, "Too many misses."))

    def test_only_the_worker_can_tap(self):
        import work_tasks as wt

        async def stranger(view, who):
            other = tap(person(6, "kai"))
            self.assertFalse(await view.interaction_check(other))
            self.assertIn("isn't your shift", other.edits[0]["content"])
        self.play(wt.FindIt, stranger)


real_sleep = asyncio.sleep


async def fast_sleep(seconds=0, *args):
    await real_sleep(0)


class DropTests(unittest.TestCase):
    def test_first_right_answer_wins(self):
        store = temp_store()
        channel = SimpleNamespace(id=7, guild=SimpleNamespace(id=1, me=person(99, "bot")))
        posts = []

        class Post:
            async def edit(self, embed=None, **kwargs):
                posts.append(embed)

        async def send(embed=None, **kwargs):
            posts.append(embed)
            return Post()
        channel.send = send
        challenge = work.make_task("unscramble", random.Random(1))
        ben, kai = person(5, "ben"), person(6, "kai")

        async def wait_for(event, timeout, check):
            guesses = [("kai", kai, "wrong"), ("ben", ben, challenge["answer"].upper())]
            for _, who, text in guesses:
                async def react(e):
                    pass
                message = SimpleNamespace(channel=channel, author=who, content=text, add_reaction=react)
                if check(message):
                    self.assertIn(7, worker.drop_channels, "guesses skip moderation while it's on")
                    return message
            raise asyncio.TimeoutError
        worker = work.Work(SimpleNamespace(store=store, wait_for=wait_for))
        winner = asyncio.run(worker.drop(channel, challenge))
        self.assertEqual(winner.id, 5)
        self.assertGreaterEqual(store.aura_total(1, 5) - 500, work.DROP_PAY[0])
        self.assertIn("ben", posts[-1].description)
        self.assertEqual(worker.drop_channels, set())

    def test_drops_are_occasional(self):
        spawned = []
        worker = work.Work(SimpleNamespace(store=temp_store(), spawn=lambda c: (spawned.append(c), c.close())))
        message = SimpleNamespace(channel=SimpleNamespace(id=work.DROP_CHANNEL_ID or 7), guild=SimpleNamespace(id=1))
        with unittest.mock.patch.object(work, "DROP_CHANNEL_ID", message.channel.id), unittest.mock.patch.object(work.random, "random", lambda: 0):
            self.assertTrue(worker.maybe_drop(message))
            self.assertFalse(worker.maybe_drop(message), "not again for a while")
        self.assertEqual(len(spawned), 1)


if __name__ == "__main__":
    unittest.main()
