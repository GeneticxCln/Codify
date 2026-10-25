"""What the engine does on its way out: stop the work it was driving, and record that it did.

A goal that was running a moment ago must not be left claiming to run, and the code that records
it must be reached even when something earlier in the shutdown went wrong.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import asyncio
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import patch

from starlette.testclient import TestClient

from engine.app import _spawn, app
from engine.catalog_watch import CatalogWatch
from engine.models import GoalCreate, WorkspaceCreate
from engine.services import GoalService


class TestShutdown(unittest.TestCase):
    def test_a_watcher_that_crashed_does_not_stop_the_goals_being_recorded(self) -> None:
        """An unexpected exception ends the catalogue watcher loudly, by design. Awaiting it at shutdown
        re-raised that exception and skipped the block that marks the interrupted goals failed."""

        async def crash(self: CatalogWatch) -> None:
            raise RuntimeError("a bug in a sweep")

        seen: list[str] = []
        real = GoalService.fail_orphaned_active_goals

        def recording(self: GoalService, why: str = "the engine restarted") -> Any:
            seen.append(why)
            return real(self, why)

        with patch.object(CatalogWatch, "run", crash), patch.object(
            GoalService, "fail_orphaned_active_goals", recording
        ):
            with TestClient(app):
                pass

        self.assertIn("the engine is shutting down", seen)

    def test_a_goal_that_is_still_being_driven_is_stopped_before_it_is_recorded_failed(self) -> None:
        """Marking a goal FAILED while its coroutine and commands keep running leaves both going until the
        process is killed, with the record already saying they stopped."""
        started = threading.Event()
        stopped = threading.Event()
        stopped_when_recorded: list[bool] = []
        real = GoalService.fail_orphaned_active_goals

        async def driver() -> None:
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()

        def recording(self: GoalService, why: str = "the engine restarted") -> Any:
            stopped_when_recorded.append(stopped.wait(2) if started.is_set() else False)
            return real(self, why)

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.object(GoalService, "fail_orphaned_active_goals", recording):
            with TestClient(app) as client:
                ws = app.state.workspaces.create(WorkspaceCreate(name="W", root_path=folder.name))
                goal = app.state.goals.create(GoalCreate(workspace_id=ws.id, title="t", description=""))

                async def launch() -> None:
                    _spawn(app, driver(), goal.id)

                client.portal.call(launch)  # type: ignore[union-attr]
                self.assertTrue(started.wait(5), "the stand-in driver never started")
        # The first call is the boot-time rescue, before the driver exists; the last is the shutdown's.
        self.assertTrue(stopped_when_recorded[-1], "the goal was recorded as failed while its driver still ran")


if __name__ == "__main__":
    unittest.main()
