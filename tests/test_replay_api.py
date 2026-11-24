"""Regression tests for `POST /goals/{id}/trace/replay` (`engine/replay.py`, Item 8)."""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — isolated CODIFY_HOME

import tempfile
import unittest
from pathlib import Path

import httpx
from httpx import ASGITransport

from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.executor import ExecutorService
from engine.models import ROLES, AgentConfigUpdate, GoalCreate, WorkspaceCreate
from engine.providers import Keychain
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService
from engine.trace import TraceService
from tests.test_design_role import SkippedGate
from tests.test_trace import RESPONSES, ScriptedFactory, ScriptedProvider


class TestReplayApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.conn = connect(self.root / "replay.db")
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.traces = TraceService(self.conn)
        self.ws_dir = self.root / "repo"
        self.ws_dir.mkdir()
        (self.ws_dir / "README.md").write_text("# fixture\n")
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.ws_dir)))

        app.state.conn = self.conn
        app.state.goals = self.goals
        app.state.workspaces = self.workspaces
        app.state.traces = self.traces
        app.state.token = BOOT_TOKEN
        self.client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.conn.close()
        self.tmp.cleanup()

    def _executor(self) -> ExecutorService:
        provider = ScriptedProvider(dict(RESPONSES))
        registry = AgentRegistryService(self.conn, ScriptedFactory(provider, Keychain()), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(provider="ollama", model_name="m"))
        return ExecutorService(
            self.goals, self.workspaces, registry, SandboxService(),
            laya=SkippedGate(), tracer=self.traces,
        )

    async def test_replay_route_matches_and_detects_workspace_divergence(self) -> None:
        # Plan-only run writes nothing to the workspace, so replaying against the same tree matches.
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="plan it", description="", plan_only=True, trace=True)
        )
        await self._executor().run_planning(goal.id)

        r = await self.client.post(f"/goals/{goal.id}/trace/replay", headers=self.headers)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body["matched"], body)
        self.assertIsNone(body["diverged"])
        self.assertIsNone(body["scratch"], "in-app replay cleans up its scratch directory")
        self.assertEqual(body["served_calls"], body["recorded_calls"])

        # Mutating the workspace makes the evidence pack differ -> replay must report divergence.
        (self.ws_dir / "new.txt").write_text("changed\n")
        r2 = await self.client.post(f"/goals/{goal.id}/trace/replay", headers=self.headers)
        self.assertEqual(200, r2.status_code, r2.text)
        body2 = r2.json()
        self.assertFalse(body2["matched"])
        self.assertTrue(body2["diverged"])

    async def test_unrecorded_and_unknown_goals_are_refused(self) -> None:
        unrecorded = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="nope", description=""))
        r1 = await self.client.post(f"/goals/{unrecorded.id}/trace/replay", headers=self.headers)
        self.assertEqual(409, r1.status_code, r1.text)
        self.assertEqual("nothing_recorded", r1.json()["code"])

        r2 = await self.client.post("/goals/no-such-goal/trace/replay", headers=self.headers)
        self.assertEqual(404, r2.status_code, r2.text)
        self.assertEqual("unknown_goal", r2.json()["code"])


if __name__ == "__main__":
    unittest.main()
