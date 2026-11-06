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


class TestAGoalNeverGoesBackwards(LifecycleCase):
    """The service refuses the moves that run against a goal's life, not only the ones out of a terminal status.

    Only terminal statuses were guarded, so a stale write could do anything between the live ones: send a started goal
    back to waiting for approval, plan it again, or finish a paused one. The audit of 2026-11-06 could not construct such
    a race (the check and the write run synchronously on one event loop), so this is a guard on the service, not the fix
    for an observed fault. The rule is deliberately narrow. Running the whole suite with `update_status` recorded showed
    that tests and the benchmark harness approve a goal straight from `PLANNING` to `RUNNING` (the `start` route refuses
    that in production), so that shortcut is left alone and only the moves nothing legitimate makes are refused.
    """

    STATUSES = ("PLANNING", "PENDING", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED")
    TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
    #: Written out here, not imported, so the test is the rule's second statement and not a copy of the first.
    BACKWARD = {("RUNNING", "PENDING"), ("PAUSED", "COMPLETED")}

    def allowed(self, current: str, target: str) -> bool:
        if current == target:
            return True
        if current in self.TERMINAL:
            return current in ("COMPLETED", "FAILED") and target == "RUNNING"
        if target == "PLANNING":
            return False
        return (current, target) not in self.BACKWARD

    def put_in(self, status: str) -> None:
        # Fixture setup: the point under test is the move out of `status`, not how the goal got there.
        self.conn.execute("UPDATE goals SET status=? WHERE id=?", (status, self.goal.id))
        self.conn.commit()

    def test_every_move_is_allowed_or_refused_exactly_as_the_rule_says(self) -> None:
        for current in self.STATUSES:
            for target in self.STATUSES:
                with self.subTest(move=f"{current} -> {target}"):
                    self.put_in(current)
                    if self.allowed(current, target):
                        self.move(target)
                        self.assertEqual(target, self.status())
                    else:
                        before = self.events()
                        with self.assertRaises(ApiError) as caught:
                            self.move(target)
                        self.assertEqual("illegal_status", caught.exception.code)
                        self.assertEqual(current, self.status(), "a refused move changed the status")
                        self.assertEqual(before, self.events(), "a refused move published an event")

    def test_a_started_goal_is_not_sent_back_to_waiting_for_approval(self) -> None:
        self.put_in("RUNNING")
        with self.assertRaises(ApiError):
            self.move("PENDING")
        self.assertEqual("RUNNING", self.status())

    def test_no_goal_is_planned_again(self) -> None:
        for status in ("PENDING", "RUNNING", "PAUSED"):
            with self.subTest(status=status):
                self.put_in(status)
                with self.assertRaises(ApiError):
                    self.move("PLANNING")
                self.assertEqual(status, self.status())

    def test_a_paused_goal_is_not_finished_by_a_status_write(self) -> None:
        self.put_in("PAUSED")
        with self.assertRaises(ApiError):
            self.move("COMPLETED")
        self.assertEqual("PAUSED", self.status())

    def test_the_moves_the_engine_really_makes_still_work(self) -> None:
        """Every move engine code made across the whole suite (the observation this rule was built from)."""
        real = [
            ("PLANNING", "PENDING"), ("PLANNING", "FAILED"), ("PLANNING", "CANCELLED"), ("PLANNING", "COMPLETED"),
            ("PENDING", "PENDING"), ("PENDING", "RUNNING"), ("PENDING", "CANCELLED"),
            ("RUNNING", "PAUSED"), ("RUNNING", "COMPLETED"), ("RUNNING", "FAILED"), ("RUNNING", "CANCELLED"),
            ("PAUSED", "RUNNING"), ("PAUSED", "CANCELLED"), ("PAUSED", "PENDING"), ("PAUSED", "FAILED"),
            ("COMPLETED", "RUNNING"), ("FAILED", "RUNNING"), ("FAILED", "FAILED"),
        ]
        for current, target in real:
            with self.subTest(move=f"{current} -> {target}"):
                self.put_in(current)
                self.move(target)
                self.assertEqual(target, self.status())

    def test_the_runners_write_after_a_pause_landed_is_refused_and_leaves_the_pause(self) -> None:
        """The race the audit could not stage: the person pauses, then the runner writes COMPLETED over it."""
        from engine.executor import ExecutorService

        self.put_in("RUNNING")
        runner = ExecutorService.__new__(ExecutorService)
        runner.goals = self.goals
        runner.settings = None
        runner.orchestrator = types.SimpleNamespace(tracer=None)  # type: ignore[assignment]
        runner._consolidate = lambda goal_id: 0  # type: ignore[method-assign]

        self.move("PAUSED")  # the person's Pause, landing before the runner's write
        with self.assertRaises(ApiError) as caught:
            runner._set_status(self.goal.id, "COMPLETED", None)

        self.assertEqual("illegal_status", caught.exception.code)
        self.assertEqual("PAUSED", self.status(), "the runner's COMPLETED overwrote the person's Pause")


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


class TestSpawnedWorkIsNotLeftToTheGarbageCollector(unittest.IsolatedAsyncioTestCase):
    """L4: `_spawn` discarded its `create_task` result, and the event loop keeps only a weak reference
    to a task — a documented CPython hazard. A goal's driver that nothing else holds can be collected
    while it is still running, and the goal simply stops, with no error and no event."""

    async def test_a_spawned_task_is_held_until_it_finishes_and_then_released(self) -> None:
        from engine.app import _BACKGROUND_TASKS, _spawn

        gate = asyncio.Event()
        before = set(_BACKGROUND_TASKS)

        async def work() -> None:
            await gate.wait()

        _spawn(types.SimpleNamespace(), work())  # type: ignore[arg-type]
        held = set(_BACKGROUND_TASKS) - before
        self.assertEqual(1, len(held), "nothing holds the task that was just started")

        gate.set()
        for _ in range(50):
            if not set(_BACKGROUND_TASKS) - before:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(set(), set(_BACKGROUND_TASKS) - before, "a finished task is never released")

    async def test_a_spawned_failure_still_fails_the_goal_rather_than_vanishing(self) -> None:
        from engine.app import _spawn

        failed: list[tuple[str, str]] = []

        class Executor:
            def _fail(self, goal_id: str, step_id: Any, code: str, message: str) -> None:
                failed.append((goal_id, code))

            # `_task_done` asks this once the goal has nothing left running; without it the done-callback raised
            # `AttributeError`, which asyncio printed into a passing run's output.
            def forget_in_flight(self, goal_id: str) -> None:
                pass

        async def boom() -> None:
            raise RuntimeError("the driver crashed")

        _spawn(types.SimpleNamespace(state=types.SimpleNamespace(executor=Executor())), boom(), "g1")  # type: ignore[arg-type]
        for _ in range(50):
            if failed:
                break
            await asyncio.sleep(0.01)

        self.assertEqual([("g1", "internal_error")], failed)


if __name__ == "__main__":
    unittest.main()
