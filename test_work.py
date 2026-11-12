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


class FakeInteraction:
    def __init__(self, user, channel_id=7):
        self.user, self.channel = user, SimpleNamespace(id=channel_id)
        self.sent = []
        outer = self

        class Message:
            async def edit(self, content=None, **kwargs):
                outer.sent.append(("edit", content))

        async def send_message(text=None, **kwargs):
            outer.sent.append(("send", text))

        async def followup(text=None, wait=False, **kwargs):
            outer.sent.append(("send", text))
            return Message()

        async def original_response():
            return Message()
        self.response = SimpleNamespace(send_message=send_message)
        self.followup = SimpleNamespace(send=followup)
        self.original_response = original_response

    def texts(self):
        return [text for _, text in self.sent]


def answering(worker, answers):
    """A fake bot whose wait_for hands back `answers` in turn (a function of the shown task, or text)."""
    deleted = []

    async def wait_for(event, timeout, check):
        if not answers:
            raise asyncio.TimeoutError
        answer = answers.pop(0)
        content = answer(worker.current) if callable(answer) else answer

        async def delete():
            deleted.append(content)
        message = SimpleNamespace(channel=SimpleNamespace(id=7), author=worker.member, content=content, delete=delete)
        assert check(message)
        return message
    return wait_for, deleted


class ShiftTests(unittest.TestCase):
    def setUp(self):
        self.store = temp_store()
        self.ben = person(5, "ben")

    def run_shift(self, answers, now=None):
        tasks = []
        original = work.make_task

        def remember(kind=None, rng=random):
            task = original(kind, rng)
            tasks.append(task)
            worker.current = task
            return task

        bot = SimpleNamespace(store=self.store)
        worker = work.Work(bot)
        worker.member = self.ben
        worker.current = None
        wait_for, deleted = answering(worker, answers)
        bot.wait_for = wait_for
        interaction = FakeInteraction(self.ben)
        real_sleep = asyncio.sleep

        async def no_wait(seconds):
            await real_sleep(0)

        async def go():
            with unittest.mock.patch.object(work, "make_task", remember), unittest.mock.patch.object(work.asyncio, "sleep", no_wait):
                # tasks are made up front, so answer them in order
                await worker.shift(interaction)
        # answers refer to worker.current; make it point at the task being asked
        real_wait = bot.wait_for

        async def wait_in_order(event, timeout, check):
            worker.current = tasks[len(deleted)] if len(deleted) < len(tasks) else None
            return await real_wait(event, timeout, check)
        bot.wait_for = wait_in_order
        with unittest.mock.patch.object(work, "MAX_CHARS_PER_SECOND", 1e9):  # the speed check has its own test
            asyncio.run(go())
        return worker, interaction, deleted

    def test_full_shift_pays_and_cleans_up(self):
        right = lambda task: task["answer"]
        worker, interaction, deleted = self.run_shift([right] * work.WORK_PHRASES)
        total = self.store.aura_total(1, 5) - 500  # the casino's starting aura
        n = work.WORK_PHRASES
        self.assertTrue(n * work.WORK_PAY[0] + work.WORK_BONUS <= total <= n * work.WORK_PAY[1] + work.WORK_BONUS, total)
        self.assertEqual(len(deleted), n, "answers are deleted to keep the channel clean")
        self.assertIn("Shift done", interaction.texts()[-1])
        self.assertEqual(worker.workers, set())
        self.assertIn("work again", worker.shift_problem(self.ben, __import__("time").time()), "cooldown")

    def test_copy_paste_ends_the_shift_with_nothing(self):
        right = lambda task: task["answer"]
        pasted = lambda task: work.disguise(task["answer"] + " pasted text")
        worker, interaction, _ = self.run_shift([right, pasted, right])
        self.assertEqual(self.store.aura_total(1, 5), 500, "no pay, even for the first one")
        self.assertTrue(any("Caught copy-pasting" in t for t in interaction.texts() if t))

    def test_one_mistake_means_no_pay(self):
        right = lambda task: task["answer"]
        worker, interaction, _ = self.run_shift([right, right, "nope"])
        self.assertEqual(self.store.aura_total(1, 5), 500, "all or nothing")
        self.assertTrue(any("That's not it" in t for t in interaction.texts() if t))
        self.assertTrue(any("all or nothing" in t for t in interaction.texts() if t))

    def test_running_out_of_time_means_no_pay(self):
        right = lambda task: task["answer"]
        self.run_shift([right, right, right])  # the 4th never comes
        self.assertEqual(self.store.aura_total(1, 5), 500)

    def test_daily_limit(self):
        worker = work.Work(SimpleNamespace(store=self.store))
        now = __import__("time").time()
        for _ in range(work.WORK_SHIFTS_PER_DAY):
            worker.end_shift(self.ben, now - 5, 0)
        self.assertIn("shifts today", worker.shift_problem(self.ben, now))


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
