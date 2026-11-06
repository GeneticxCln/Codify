"""The recipe's step runner refuses to write for a goal nobody has approved.

Invariant 9 says the fixer writes only for an approved goal. The conductor's `write` move reads the goal's stored status
(`_write_allowed`: `RUNNING` only), but the recipe's `run_step` reached the same fixer through a check that asked a
narrower question (`_approval_withdrawn`: has a person *taken the approval back*), and that question is answered "no"
for a goal that was never approved. After `run_planning` a goal is `PENDING`, waiting for a person's Start, and calling
`run_step` on it wrote the step's files. Nothing called it that way, because every caller (`_run_steps_locked`,
`begin_retry`, `apply_goal`) makes the goal `RUNNING` first; the gate was the callers' habit, not the write's own check,
so a future caller would have lost it without a test failing.

A goal that is `FAILED` because a parallel sibling failed is still not "taken back" (docs/04 section 3.0), and the check
still lets it through; `tests/test_executor.py::test_parallel_failure_fails_the_goal_after_join` covers that case.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from sqlite3 import Connection

from engine.executor import ExecutorService
from engine.models import Goal
from engine.services import GoalService
from tests import test_executor


class TestTheStepRunnerWritesOnlyForARunningGoal(unittest.IsolatedAsyncioTestCase):
    # The parallel-execution harness's setup and scripted provider, borrowed rather than inherited: a subclass would
    # collect and re-run all of that class's own tests as well.
    asyncSetUp = test_executor.TestParallelStepExecution.asyncSetUp
    asyncTearDown = test_executor.TestParallelStepExecution.asyncTearDown
    _script_parallel = test_executor.TestParallelStepExecution._script_parallel

    # What that setup leaves on the instance, declared so the checker can read it.
    root: Path
    conn: Connection
    goals: GoalService
    goal: Goal
    executor: ExecutorService

    async def _write_for(self, status: str) -> tuple[bool, str]:
        """Plan the goal, put it in `status`, call the step runner directly: whether a.txt was written, and the status after."""
        self._script_parallel()  # type: ignore[misc]  # borrowed from the parallel-execution harness, see above
        await self.executor.run_planning(self.goal.id)
        self.assertEqual("PENDING", self.goals.get(self.goal.id).status, "planning leaves the goal waiting for Start")
        if status != "PENDING":
            self.conn.execute("UPDATE goals SET status = ? WHERE id = ?", (status, self.goal.id))
            self.conn.commit()
        first = self.goals.steps(self.goal.id)[0]

        await self.executor.run_step(self.goal.id, first.id)

        return (self.root / "a.txt").exists(), self.goals.get(self.goal.id).status

    async def test_a_pending_goal_is_not_written_for(self) -> None:
        # The probe: after planning the goal waits for a person's Start, and the step runner is not that person.
        written, status = await self._write_for("PENDING")

        self.assertFalse(written, "the recipe wrote for a goal nobody approved")
        self.assertEqual("PENDING", status, "refusing to write must not move the goal")

    async def test_a_planning_goal_is_not_written_for(self) -> None:
        written, status = await self._write_for("PLANNING")

        self.assertFalse(written, "the recipe wrote for a goal that has no plan approved yet")
        self.assertEqual("PLANNING", status)

    async def test_the_refusal_is_logged_not_raised(self) -> None:
        await self._write_for("PENDING")

        messages = [e.payload.get("message", "") for e in self.goals.events_after(self.goal.id, 0) if e.type == "log"]
        self.assertTrue(any("no plan has been approved to write" in m for m in messages), messages)

    async def test_a_running_goal_is_still_written_for(self) -> None:
        # The control: Start makes the goal RUNNING and the same call writes, so only the missing approval changed.
        written, _ = await self._write_for("RUNNING")

        self.assertTrue(written, "an approved goal could not be written for")


if __name__ == "__main__":
    unittest.main()
