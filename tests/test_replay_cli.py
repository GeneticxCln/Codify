from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from engine.models import GoalCreate
from engine.services import ApiError

from scripts.replay_trace import EXIT_DIVERGED, EXIT_OK, EXIT_UNUSABLE, main, replay
from tests.test_trace import RESPONSES, ScriptedProvider, TraceHarness


class ReplaySurfaceCase(TraceHarness):
    """The CLI is the only way a recording is reachable outside a test, so
    these are the claims it makes: it replays, it never writes to the tree it
    read from, and it refuses when the tree is not the one the recording ran
    against."""

    async def _recorded(self) -> tuple[str, Path]:
        """Record a run, keeping a pristine copy of the tree it started from.

        The copy is the whole precondition: the prompts were built from the
        workspace *before* the fixer wrote to it, so a replay that wants to
        match has to be handed that tree rather than the one that is left.
        """
        pristine = self._clone_of(self.ws, folder="ws-pristine")
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        await self._run(self._engine(ScriptedProvider(dict(RESPONSES))), goal.id)
        self.assertTrue(self.traces.count(goal.id), "the run recorded something")
        return goal.id, Path(pristine.root_path)

    def _listing(self, root: Path) -> list[str]:
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    async def test_a_replay_serves_every_recorded_call_in_order(self) -> None:
        goal_id, pristine = await self._recorded()
        into = Path(tempfile.mkdtemp()) / "scratch"

        report = await replay(self.conn, goal_id, source=pristine, into=into)

        self.assertTrue(
            report["matched"],
            f"replay diverged: {report['diverged']!r} "
            f"({report['served_calls']}/{report['recorded_calls']} served)",
        )
        self.assertIsNone(report["diverged"])
        self.assertEqual(report["served_calls"], report["recorded_calls"])
        self.assertGreater(len(report["stages"]), 1)
        self.assertIn(
            ("planner", "plan"),
            report["stages"],
            "the replay took the same stages as a real run",
        )

    async def test_a_replay_never_writes_to_the_tree_it_read_from(self) -> None:
        """The whole reason the copy exists. A replay that fixed files in the
        user's checkout would be worse than no replay at all."""
        goal_id, pristine = await self._recorded()
        before = self._listing(pristine)
        into = Path(tempfile.mkdtemp()) / "scratch"

        await replay(self.conn, goal_id, source=pristine, into=into)

        self.assertEqual(
            self._listing(pristine), before, "the source tree was modified"
        )
        self.assertTrue(into.is_dir(), "the scratch tree is where work went")

    async def test_a_tree_that_moved_refuses_instead_of_answering_the_wrong_call(self) -> None:
        """The guard, exercised from the surface a user actually reaches: a
        directory that is not the one the recording ran against."""
        goal_id, pristine = await self._recorded()
        drifted = Path(tempfile.mkdtemp()) / "drifted"
        shutil.copytree(pristine, drifted)
        (drifted / "added-after-the-run.txt").write_text("later\n")
        into = Path(tempfile.mkdtemp()) / "scratch"

        report = await replay(self.conn, goal_id, source=drifted, into=into)

        self.assertFalse(report["matched"], "a drifted tree matched the recording")
        self.assertIsNotNone(report["diverged"])
        self.assertIn(
            "librarian", str(report["diverged"]).lower(),
            "the first call that saw the changed tree is the one named",
        )

    async def test_a_goal_that_recorded_nothing_cannot_be_replayed(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=False)
        )
        with self.assertRaises(ApiError) as ctx:
            await replay(self.conn, goal.id, into=Path(tempfile.mkdtemp()) / "s")
        self.assertEqual(ctx.exception.code, "nothing_recorded")

    async def test_an_unknown_goal_is_a_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            await replay(self.conn, "no-such-goal")
        self.assertEqual(ctx.exception.status, 404)

    async def test_a_source_that_is_not_a_directory_says_so(self) -> None:
        goal_id, _pristine = await self._recorded()
        with self.assertRaises(ApiError) as ctx:
            await replay(
                self.conn, goal_id, source=Path("/definitely/not/here"),
                into=Path(tempfile.mkdtemp()) / "s",
            )
        self.assertEqual(ctx.exception.code, "source_missing")

    async def test_the_command_reports_a_match_with_a_zero_exit(self) -> None:
        goal_id, pristine = await self._recorded()
        into = Path(tempfile.mkdtemp()) / "scratch"
        db = Path(self.temp_dir.name) / "t.db"

        code = await asyncio.to_thread(main, [
            "--goal", goal_id,
            "--db", str(db),
            "--from", str(pristine),
            "--into", str(into),
        ])

        self.assertEqual(code, EXIT_OK)

    async def test_the_command_exits_two_when_the_recording_is_unreadable(self) -> None:
        db = Path(self.temp_dir.name) / "t.db"
        code = await asyncio.to_thread(main, ["--goal", "nope", "--db", str(db)])
        self.assertEqual(code, EXIT_UNUSABLE)

    async def test_the_command_exits_one_when_the_run_diverged(self) -> None:
        goal_id, pristine = await self._recorded()
        drifted = Path(tempfile.mkdtemp()) / "drifted"
        shutil.copytree(pristine, drifted)
        (drifted / "added-after-the-run.txt").write_text("later\n")
        db = Path(self.temp_dir.name) / "t.db"

        code = await asyncio.to_thread(main, [
            "--goal", goal_id,
            "--db", str(db),
            "--from", str(drifted),
            "--into", str(Path(tempfile.mkdtemp()) / "scratch"),
        ])

        self.assertEqual(code, EXIT_DIVERGED)

    async def test_a_missing_database_is_not_a_stack_trace(self) -> None:
        missing = Path(self.temp_dir.name) / "never-created.db"
        code = await asyncio.to_thread(
            main, ["--goal", "x", "--db", str(missing)]
        )
        self.assertEqual(code, EXIT_UNUSABLE)


class GateReplayCase(unittest.TestCase):
    """The gate's verdict is replayed rather than recomputed, because a fresh
    decision would change the run before its first model call."""

    def test_a_blocked_run_replays_as_blocked(self) -> None:
        from engine.models import Event
        from scripts.replay_trace import _gate_verdict

        blocked = Event(
            id="e1", goal_id="g", step_id=None, type="laya_decision",
            payload={"engine": "sdk", "blocked": True, "block_reason": "injection 0.99"},
            timestamp=1.0, sequence=1,
        )
        verdict = _gate_verdict([blocked])
        self.assertTrue(verdict.blocked)
        self.assertEqual(verdict.block_reason, "injection 0.99")

    def test_a_run_whose_gate_never_ran_replays_as_skipped(self) -> None:
        from scripts.replay_trace import _gate_verdict

        verdict = _gate_verdict([])
        self.assertEqual(verdict.engine, "skipped")
        self.assertFalse(verdict.blocked)

    def test_the_newest_gate_event_wins(self) -> None:
        from engine.models import Event
        from scripts.replay_trace import _gate_verdict

        events: list[Any] = [
            Event(id="e1", goal_id="g", step_id=None, type="laya_decision",
                  payload={"engine": "sdk", "blocked": True, "block_reason": "old"},
                  timestamp=1.0, sequence=1),
            Event(id="e2", goal_id="g", step_id=None, type="laya_decision",
                  payload={"engine": "sdk", "blocked": False}, timestamp=2.0, sequence=2),
        ]
        self.assertFalse(_gate_verdict(events).blocked)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
