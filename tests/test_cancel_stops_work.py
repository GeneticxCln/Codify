"""A Cancel stops the work, not only the record of it.

Before this, `POST /goals/{id}/cancel` flipped the stored status and nothing else. Every stage re-checks the status
before it writes (`ExecutorService._cancelled`, `_write_allowed`), so a cancelled goal wrote nothing more — but a model
call already in flight ran to its end, and so did a test command, which runs the repository's own code as the person
for up to its whole timeout after they asked for it to stop.

Three layers, each held here: the sandbox stops a command's whole process group when the goal's signal is set; the
executor hands that signal to every command a goal runs; and the route cancels the coroutines working for the goal.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

from engine import app as app_module
from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.executor import ExecutorService
from engine.models import WorkspaceCreate
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import CANCELLED_EXIT_CODE, SandboxService
from engine.services import AgentRegistryService, GoalService, SettingsService, WorkspaceService
from tests.test_conductor import ConductorTestCase, _ToolProvider

#: Appears in exactly one process's command line: the grandchild a cancelled command must not leave behind.
PROBE = "time.sleep(61)"


def _survivors() -> str:
    return subprocess.run(
        ["pgrep", "-f", r"time\.sleep\(61\)"], capture_output=True, text=True, check=False,
    ).stdout.strip()


class TestTheSandboxStopsWhenTheSignalIsSet(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "spawner.py").write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', 'import time; {PROBE}'])\n"
            "time.sleep(61)\n",
            encoding="utf-8",
        )

    def test_a_cancel_stops_the_whole_group_long_before_the_timeout(self) -> None:
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        started = time.monotonic()
        result = SandboxService().run_command(str(self.root), ["python3", "spawner.py"], timeout_s=60, cancel=cancel)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 15, f"a cancelled command ran {elapsed:.1f}s of its 60s timeout")
        self.assertEqual(result["exit_code"], CANCELLED_EXIT_CODE)
        self.assertIn("the goal was cancelled", result["stderr"])
        self.assertNotIn("timed out", result["stderr"], "a cancel was reported as a timeout")
        time.sleep(0.5)
        self.assertEqual(_survivors(), "", "the command's child outlived the cancel")

    def test_a_signal_that_is_never_set_changes_nothing(self) -> None:
        (self.root / "quick.py").write_text("print('done')\n", encoding="utf-8")
        result = SandboxService().run_command(
            str(self.root), ["python3", "quick.py"], timeout_s=30, cancel=threading.Event(),
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("done", result["stdout"])

    def test_a_timeout_is_still_a_timeout_with_a_signal_attached(self) -> None:
        result = SandboxService().run_command(
            str(self.root), ["python3", "spawner.py"], timeout_s=1, cancel=threading.Event(),
        )
        self.assertEqual(result["exit_code"], 124)
        self.assertIn("timed out after 1s", result["stderr"])
        time.sleep(0.5)
        self.assertEqual(_survivors(), "")


class _RecordingSandbox(SandboxService):
    """Runs nothing; remembers the cancel signal each command was handed."""

    def __init__(self) -> None:
        self.signals: list[threading.Event | None] = []

    def run_command(
        self, root_path: str, argv: list[str], timeout_s: int = 120, mode: str = "test",
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        self.signals.append(cancel)
        return {"argv": argv, "exit_code": 0, "stdout": "", "stderr": ""}


class TestTheExecutorHandsEveryCommandItsGoalsSignal(ConductorTestCase):
    async def test_the_conductors_run_command_carries_the_goals_signal(self) -> None:
        executor = self._executor(_ToolProvider())
        recording = _RecordingSandbox()
        executor.sandbox = recording
        from engine.skills import load_skills

        table = executor._conductor_dispatch(
            self.goal.id, self.goals.get(self.goal.id), str(self.repo), load_skills(str(self.repo)),
        )
        await table["run_command"]({"argv": ["ls"]})
        self.assertEqual(recording.signals, [executor.cancel_signal(self.goal.id)])

    async def test_stop_in_flight_sets_the_signal_and_forget_drops_it(self) -> None:
        executor = self._executor(_ToolProvider())
        signal = executor.cancel_signal(self.goal.id)
        self.assertFalse(signal.is_set())
        executor.stop_in_flight(self.goal.id)
        self.assertTrue(signal.is_set())
        executor.forget_in_flight(self.goal.id)
        self.assertIsNot(executor.cancel_signal(self.goal.id), signal, "a forgotten goal kept its old signal")


class TestTheRouteCancelsTheWork(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.conn = connect(self.root / "t.db")
        keychain = Keychain(secrets_path=self.root / "secrets.json")
        app.state.conn = self.conn
        app.state.keychain = keychain
        app.state.registry = AgentRegistryService(self.conn, ProviderFactory(keychain), keychain)
        app.state.workspaces = WorkspaceService(self.conn)
        app.state.goals = GoalService(self.conn)
        app.state.sandbox = SandboxService()
        app.state.settings = SettingsService(self.conn)
        app.state.executor = ExecutorService(
            app.state.goals, app.state.workspaces, app.state.registry, app.state.sandbox,
        )
        app.state.executor.settings = app.state.settings
        app.state.token = BOOT_TOKEN
        self.parked = asyncio.Event()
        self.outcome: list[str] = []

        async def a_planner_that_is_still_working(goal_id: str) -> None:
            self.parked.set()
            try:
                await asyncio.Event().wait()  # a model call that has not answered yet
            except asyncio.CancelledError:
                self.outcome.append("cancelled")
                raise
            self.outcome.append("finished")

        app.state.executor.run_planning = a_planner_that_is_still_working
        self.client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}
        self.ws = app.state.workspaces.create(WorkspaceCreate(name="ws", root_path=str(self.root)))

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.conn.close()
        self.tmp.cleanup()

    async def test_cancel_stops_a_planner_that_is_still_working(self) -> None:
        r = await self.client.post(
            "/goals", headers=self.headers, json={"workspace_id": self.ws.id, "title": "slow", "description": ""},
        )
        goal = r.json()
        await asyncio.wait_for(self.parked.wait(), timeout=5)
        signal = app.state.executor.cancel_signal(goal["id"])

        r = await self.client.post(
            f"/goals/{goal['id']}/cancel", headers=self.headers, json={"expected_version": goal["version"]},
        )
        self.assertEqual(r.json()["status"], "CANCELLED")
        for _ in range(100):
            if self.outcome:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.outcome, ["cancelled"], "the planner kept working after the goal was cancelled")
        self.assertTrue(signal.is_set(), "a command the goal was running would have run on")
        self.assertNotIn(goal["id"], app_module._GOAL_TASKS, "a finished goal's tasks were kept")

    async def test_a_cancel_that_is_refused_stops_nothing(self) -> None:
        r = await self.client.post(
            "/goals", headers=self.headers, json={"workspace_id": self.ws.id, "title": "slow", "description": ""},
        )
        goal = r.json()
        await asyncio.wait_for(self.parked.wait(), timeout=5)
        r = await self.client.post(
            f"/goals/{goal['id']}/cancel", headers=self.headers, json={"expected_version": goal["version"] + 7},
        )
        self.assertEqual(r.status_code, 409, r.text)
        await asyncio.sleep(0.05)
        self.assertEqual(self.outcome, [], "a refused cancel stopped the work anyway")
        # Tidy up: the planner is parked until cancelled.
        for task in list(app_module._GOAL_TASKS.get(goal["id"], ())):
            task.cancel()
        await asyncio.sleep(0.05)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
