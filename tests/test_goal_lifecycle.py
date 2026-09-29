"""A goal's status only moves along the paths the API documents (audit of 2026-09-29, M1 and M2).

`CANCELLED` is a decision a person made, and three things could undo it: `run_chat` ended with an
unconditional `COMPLETED`, `GoalService.update_status` had no transition table at all, and the step
driver started a conductor run for a goal that had been cancelled while a retried step was still
running. The rules live in one place now — the service refuses a move out of a terminal status
except the two documented ones (apply and retry re-open a finished goal by way of `RUNNING`) — and
the runner-side guards are what keep that refusal from surfacing as a crash inside a background task.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import _run_steps_locked
from engine.db import connect
from engine.models import GoalCreate, WorkspaceCreate
from engine.services import ApiError, GoalService, WorkspaceService


class LifecycleCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        (base / "ws").mkdir()
        self.conn: sqlite3.Connection = connect(base / "t.db")
        self.addCleanup(self.conn.close)
        self.goals = GoalService(self.conn)
        ws = WorkspaceService(self.conn).create(WorkspaceCreate(name="w", root_path=str(base / "ws")))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="g"))

    def move(self, *statuses: str) -> None:
        for status in statuses:
            self.goals.update_status(self.goal.id, self.goals.get(self.goal.id).version, status)

    def status(self) -> str:
        return self.goals.get(self.goal.id).status

    def events(self) -> int:
        return len(self.goals.events_after(self.goal.id, 0))


class TestOnlyDocumentedMovesOutOfATerminalStatus(LifecycleCase):
    def test_nothing_leaves_cancelled(self) -> None:
        self.move("PENDING", "CANCELLED")
        for target in ("COMPLETED", "FAILED", "RUNNING", "PENDING", "PAUSED", "PLANNING"):
            with self.subTest(target=target):
                before = self.events()
                with self.assertRaises(ApiError) as caught:
                    self.move(target)
                self.assertEqual(409, caught.exception.status)
                self.assertEqual("illegal_status", caught.exception.code)
                self.assertEqual("CANCELLED", self.status())
                self.assertEqual(before, self.events(), "a refused move was announced")

    def test_a_finished_goal_reopens_only_through_running(self) -> None:
        # Apply re-opens a COMPLETED dry run and retry/apply a FAILED goal, both as RUNNING.
        for terminal in ("COMPLETED", "FAILED"):
            with self.subTest(terminal=terminal):
                goal = self.goals.create(GoalCreate(workspace_id=self.goal.workspace_id, title=terminal))
                self.goals.update_status(goal.id, 0, "PENDING")
                self.goals.update_status(goal.id, 1, terminal)
                for target in ("PENDING", "PAUSED", "PLANNING", "CANCELLED"):
                    with self.assertRaises(ApiError):
                        self.goals.update_status(goal.id, self.goals.get(goal.id).version, target)
                reopened = self.goals.update_status(goal.id, self.goals.get(goal.id).version, "RUNNING")
                self.assertEqual("RUNNING", reopened.status)

    def test_a_non_terminal_goal_moves_as_it_always_did(self) -> None:
        self.move("PENDING", "RUNNING", "PAUSED", "RUNNING", "COMPLETED")
        self.assertEqual("COMPLETED", self.status())

    def test_repeating_the_status_it_already_has_is_not_a_refusal(self) -> None:
        # `_fail` after a `_fail`, or a second cancel: idempotent, and refusing would only
        # turn a harmless repeat into an error.
        self.move("PENDING", "CANCELLED", "CANCELLED")
        self.assertEqual("CANCELLED", self.status())


class TestTheStepDriverDoesNotStartAConductorForACancelledGoal(unittest.IsolatedAsyncioTestCase):
    async def test_a_goal_cancelled_during_a_retried_step_gets_no_conductor_run(self) -> None:
        """M2: `_run_steps_locked` never re-checked status before `run_conductor_resume`.

        The retry route awaited the whole retried step inside the request and then spawned the
        driver unconditionally, so a cancel that landed during the step was followed by a full
        conductor run: model spend, and (before H3) a command.
        """
        started: list[str] = []
        goal = types.SimpleNamespace(status="CANCELLED", parallel=False)

        class Executor:
            def conductor_can_drive(self, goal_id: str) -> bool:
                return True

            async def run_conductor_resume(self, goal_id: str) -> None:
                started.append(goal_id)

            async def run_step(self, goal_id: str, step_id: str) -> None:
                started.append(step_id)

            def _log(self, *args: Any) -> None:
                pass

            def _set_status(self, goal_id: str, status: str, step_id: str | None) -> None:
                started.append(f"status:{status}")

        class Goals:
            def get(self, goal_id: str) -> Any:
                return goal

            def steps(self, goal_id: str) -> list[Any]:
                return [types.SimpleNamespace(id="s1", status="PENDING")]

        app = types.SimpleNamespace(state=types.SimpleNamespace(executor=Executor(), goals=Goals()))

        await _run_steps_locked(app, "g1")  # type: ignore[arg-type]

        self.assertEqual([], started, "a cancelled goal was driven anyway")
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
