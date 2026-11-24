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

        def recording(
            self: GoalService, why: str = "the engine restarted", only: set[str] | None = None,
        ) -> Any:
            seen.append(why)
            return real(self, why, only)

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

        def recording(
            self: GoalService, why: str = "the engine restarted", only: set[str] | None = None,
        ) -> Any:
            stopped_when_recorded.append(stopped.wait(2) if started.is_set() else False)
            return real(self, why, only)

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.object(GoalService, "fail_orphaned_active_goals", recording):
            with TestClient(app) as client:
                ws = app.state.workspaces.create(WorkspaceCreate(name="W", root_path=folder.name))
                goal = app.state.goals.create(GoalCreate(workspace_id=ws.id, title="t", description=""))

                async def launch() -> None:
                    _spawn(app, driver(), goal.id)

                portal = client.portal
                assert portal is not None
                portal.call(launch)
                self.assertTrue(started.wait(5), "the stand-in driver never started")
        # The first call is the boot-time rescue, before the driver exists; the last is the shutdown's.
        self.assertTrue(stopped_when_recorded[-1], "the goal was recorded as failed while its driver still ran")

    def test_close_wakes_active_bridge_and_surface_long_polls_immediately(self) -> None:
        """An active 20s `/bridge/next` or `/surfaces/next` poll must return None as soon as
        shutdown begins instead of stalling uvicorn for its entire graceful-shutdown timeout."""
        from engine.surfaces import SurfaceBridge
        from engine.webview_bridge import WebviewBridge

        async def _check() -> None:
            surfaces = SurfaceBridge()
            bridge = WebviewBridge()
            t_surf = asyncio.create_task(surfaces.next_request(wait_s=20.0))
            t_bridge = asyncio.create_task(bridge.next_request(wait_s=20.0))
            await asyncio.sleep(0.02)
            surfaces.close()
            bridge.close()
            res_surf, res_bridge = await asyncio.wait_for(asyncio.gather(t_surf, t_bridge), timeout=1.0)
            self.assertIsNone(res_surf)
            self.assertIsNone(res_bridge)
            # Subsequent polls after close also return immediately.
            self.assertIsNone(await asyncio.wait_for(surfaces.next_request(wait_s=20.0), timeout=0.5))
            self.assertIsNone(await asyncio.wait_for(bridge.next_request(wait_s=20.0), timeout=0.5))

        asyncio.run(_check())


if __name__ == "__main__":
    unittest.main()
