"""A parallel step that fails does not strand its healthy sibling half-done.

`docs/04` section 1.2 says a goal that is `FAILED` because a parallel sibling failed has not had its approval taken
back, and the batch lets its healthy steps finish. The step runner did the opposite after the sibling's `_fail` had
marked the goal `FAILED`: its `_cancelled` check read any status outside `RUNNING` and `PAUSED` as a cancel, so the
healthy step's fixer wrote, its verifier passed, and then the step was abandoned there with "cancelled — skipping review
and commit for this step". It stayed `IN_PROGRESS` with its file on disk, no critic, no summary and no commit, in a
log that blamed a cancel nobody pressed.

The decision (docs/23, O2) is to follow the document: the healthy step goes through its critic and its commit. A real
cancel still stops it before the commit, and the log now says what actually stopped it.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from engine.executor import ExecutorService
from engine.models import Goal
from engine.services import GoalService
from tests import test_executor


def _borrow(name: str) -> Any:
    """A member of the parallel-execution harness. `Any`, because it is typed against the class it came from, and a
    function and not a module-level class name, because the test loader collects every TestCase class it can see."""
    return getattr(test_executor.TestParallelStepExecution, name)



class TestAHealthySiblingFinishes(unittest.IsolatedAsyncioTestCase):
    # The parallel-execution harness's setup, scripted provider and driver, borrowed rather than inherited: a subclass
    # would collect and re-run all of that class's own tests as well.
    asyncSetUp = _borrow("asyncSetUp")
    asyncTearDown = _borrow("asyncTearDown")
    make_parallel = _borrow("make_parallel")
    _script_parallel = _borrow("_script_parallel")
    _drive = _borrow("_drive")

    # What that setup leaves on the instance, declared so the checker can read it.
    root: Path
    conn: Connection
    goals: GoalService
    goal: Goal
    executor: ExecutorService
    provider: Any

    def _steps(self) -> dict[str, str]:
        return {s.title: s.status for s in self.goals.steps(self.goal.id)}

    def _stages_of(self, title: str) -> list[tuple[str, str]]:
        ids = {s.id for s in self.goals.steps(self.goal.id) if s.title == title}
        return [
            (e.payload["stage"], e.payload["outcome"])
            for e in self.goals.events_after(self.goal.id, 0)
            if e.type == "stage_result" and e.step_id in ids
        ]

    def _logs(self) -> list[str]:
        return [e.payload.get("message", "") for e in self.goals.events_after(self.goal.id, 0) if e.type == "log"]

    async def test_the_healthy_step_is_reviewed_and_committed_when_its_sibling_fails(self) -> None:
        self.make_parallel()
        self._script_parallel(delays={"a": 0.4}, fail_on="b")  # b fails at once; a is still writing
        await self.executor.run_planning(self.goal.id)

        await self._drive()

        self.assertEqual("FAILED", self.goals.get(self.goal.id).status, "the failed sibling still fails the goal")
        self.assertEqual({"a": "COMPLETED", "b": "FAILED"}, self._steps())
        self.assertTrue((self.root / "a.txt").exists())
        stages = dict(self._stages_of("a"))
        self.assertEqual("approve", stages.get("critic"), f"the healthy step was never reviewed: {stages}")
        self.assertIn("scribe", stages, f"the healthy step was never summarised: {stages}")

    async def test_a_critic_that_asks_for_changes_does_not_turn_into_an_illegal_pause(self) -> None:
        # The goal is already FAILED when the healthy step's critic answers, and a FAILED goal cannot be paused. The step
        # keeps the critic's notes and stays open for a Retry, and no `illegal_status` error is published for it.
        self.make_parallel()
        self._script_parallel(delays={"a": 0.3}, fail_on="b")
        inner = self.provider.complete

        async def complete(system_prompt: str, user_prompt: str, *args: Any, **kwargs: Any) -> str:
            if "You are Codify Critic" in system_prompt:
                return json.dumps({"decision": "request-changes", "reasons": ["use a better name"]})
            return str(await inner(system_prompt, user_prompt, *args, **kwargs))

        self.provider.complete = complete
        await self.executor.run_planning(self.goal.id)

        await self._drive()

        self.assertEqual({"a": "IN_PROGRESS", "b": "FAILED"}, self._steps())
        a = next(s for s in self.goals.steps(self.goal.id) if s.title == "a")
        self.assertEqual("use a better name", a.review_notes)
        codes = [e.payload.get("code") for e in self.goals.events_after(self.goal.id, 0) if e.type == "error"]
        self.assertEqual(["provider_unreachable"], codes, "the rejection was reported as a failure of its own")

    async def test_nothing_is_blamed_on_a_cancel_nobody_pressed(self) -> None:
        self.make_parallel()
        self._script_parallel(delays={"a": 0.4}, fail_on="b")
        await self.executor.run_planning(self.goal.id)

        await self._drive()

        blamed = [m for m in self._logs() if "cancel" in m.lower()]
        self.assertEqual([], blamed, "a sibling's failure was reported as a cancel")

    async def test_a_cancel_pressed_while_the_healthy_step_is_verified_still_stops_it_before_the_commit(self) -> None:
        # The control: the person's Cancel is what the check exists for, and it still wins. It is pressed after the
        # fixer has written (a cancel during the fixer is the write's own check) and before the critic and the commit.
        self.make_parallel()
        self._script_parallel(delays={"a": 0.1, "b": 0.1})
        inner = self.provider.complete
        pressed: list[bool] = []

        async def complete(system_prompt: str, user_prompt: str, *args: Any, **kwargs: Any) -> str:
            if "You are Codify Verifier" in system_prompt and not pressed:
                pressed.append(True)
                self.goals.update_status(self.goal.id, self.goals.get(self.goal.id).version, "CANCELLED")
            return str(await inner(system_prompt, user_prompt, *args, **kwargs))

        self.provider.complete = complete
        await self.executor.run_planning(self.goal.id)

        await self._drive()

        self.assertEqual("CANCELLED", self.goals.get(self.goal.id).status)
        reviewed = [role for role, _ in self.provider.calls if role == "critic"]
        self.assertEqual([], reviewed, "a cancelled goal's steps were still reviewed")
        said = [m for m in self._logs() if "skipping review and commit" in m]
        self.assertTrue(said, self._logs())
        self.assertTrue(all("the goal is cancelled" in m for m in said), said)


if __name__ == "__main__":
    unittest.main()
