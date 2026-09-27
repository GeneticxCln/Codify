"""What the engine does *not* do at startup.

The engine used to auto-create a workspace from its own working directory when
the database had none. For anyone running the engine from a source checkout that
directory is this repository, so a fresh install came up with a workspace
pointing at Codify's own source tree — one prompt away from the app editing
itself — and it chose that target without the user picking anything.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.app import app, lifespan
from engine.models import GoalCreate, WorkspaceCreate
from engine.services import WorkspaceService


class TestFreshStartup(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_creates_no_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODIFY_DB": str(Path(tmp) / "fresh.db"),
                "CODIFY_SECRETS": str(Path(tmp) / "secrets.json"),
            }
            with patch.dict(os.environ, env):
                # The lifespan wires these onto the module-level app; put back
                # whatever was there so other suites are unaffected.
                saved = {
                    name: getattr(app.state, name, None)
                    for name in ("conn", "workspaces", "token", "registry", "goals")
                }
                try:
                    async with lifespan(app):
                        self.assertEqual(
                            app.state.workspaces.list_workspaces(),
                            [],
                            "a fresh engine must not invent a workspace",
                        )
                finally:
                    for name, value in saved.items():
                        setattr(app.state, name, value)

    async def test_a_workspace_must_be_chosen_before_it_exists(self) -> None:
        """The store itself stays empty until something asks for a workspace."""
        with tempfile.TemporaryDirectory() as tmp:
            from engine.db import connect

            conn = connect(Path(tmp) / "fresh.db")
            try:
                self.assertEqual(WorkspaceService(conn).list_workspaces(), [])
            finally:
                conn.close()

    async def test_a_goal_running_at_shutdown_is_marked_failed(self) -> None:
        """A graceful shutdown records the run it interrupted.

        Without this, a goal that was RUNNING when the window closed stayed RUNNING
        in the database until the *next* boot rewrote it — a post-mortem, not a
        record, and one that leaves the app showing a run that is not happening.
        The desktop shell asks this process to stop rather than killing it
        (`docs/09` §5.4.1) precisely so this block is reached; a hard exit skips it
        and the boot-time rescue is the only thing left.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODIFY_DB": str(Path(tmp) / "shutdown.db"),
                "CODIFY_SECRETS": str(Path(tmp) / "secrets.json"),
            }
            with patch.dict(os.environ, env):
                saved = {
                    name: getattr(app.state, name, None)
                    for name in ("conn", "workspaces", "token", "registry", "goals")
                }
                try:
                    async with lifespan(app):
                        ws = app.state.workspaces.create(
                            WorkspaceCreate(name="WS", root_path=tmp)
                        )
                        running = app.state.goals.create(
                            GoalCreate(workspace_id=ws.id, title="R", description="")
                        )
                        app.state.goals.update_status(running.id, running.version, "RUNNING")
                        goal_id = running.id

                    # Read it back through a *new* connection, the way the next
                    # process would: the shutdown closed the engine's own handle,
                    # and a record only counts if it was committed.
                    from engine.db import connect
                    from engine.services import GoalService

                    conn = connect(Path(tmp) / "shutdown.db")
                    try:
                        after = GoalService(conn).get(goal_id)
                    finally:
                        conn.close()
                    self.assertEqual(
                        "FAILED", after.status,
                        "a goal whose driver is gone must not still claim to be running",
                    )
                finally:
                    for name, value in saved.items():
                        setattr(app.state, name, value)


if __name__ == "__main__":
    unittest.main()
