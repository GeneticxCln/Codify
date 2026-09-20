import asyncio
import os
import tempfile
import unittest
from pathlib import Path

import httpx
from httpx import ASGITransport

from engine.app import app, BOOT_TOKEN
from engine.db import connect
from engine.executor import ExecutorService
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService


class TestApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.db_path = self.root / "test.db"

        # Override app state with isolated test db
        conn = connect(self.db_path)
        keychain = Keychain()
        factory = ProviderFactory(keychain)
        app.state.conn = conn
        app.state.registry = AgentRegistryService(conn, factory, keychain)
        app.state.workspaces = WorkspaceService(conn)
        app.state.goals = GoalService(conn)
        app.state.sandbox = SandboxService()
        app.state.executor = ExecutorService(app.state.goals, app.state.workspaces, app.state.registry, app.state.sandbox)
        # Mock run_planning so background planning does not call live LLM providers in API tests
        async def mock_run_planning(goal_id: str) -> None:
            pass
        app.state.executor.run_planning = mock_run_planning
        app.state.token = BOOT_TOKEN

        self.transport = ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}

    async def asyncTearDown(self):
        await self.client.aclose()
        app.state.conn.close()
        self.temp_dir.cleanup()

    async def test_auth_middleware(self):
        # No token -> 401
        r = await self.client.get("/health")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "unauthorized")

        # Wrong token -> 401
        r = await self.client.get("/health", headers={"Authorization": "Bearer bad-token"})
        self.assertEqual(r.status_code, 401)

        # Correct token -> 200
        r = await self.client.get("/health", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True})

    async def test_settings_endpoints(self):
        # GET /settings/providers
        r = await self.client.get("/settings/providers", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("builtins", data)

        # GET /settings/agents
        r = await self.client.get("/settings/agents", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        agents = r.json()
        self.assertEqual(len(agents), 5)

        # PUT /settings/agents/planner
        r = await self.client.put(
            "/settings/agents/planner",
            headers=self.headers,
            json={"display_name": "API Planner", "temperature": 0.4},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["display_name"], "API Planner")

    async def test_workspaces_and_goals_lifecycle(self):
        ws_dir = self.root / "ws1"
        ws_dir.mkdir()

        # Create workspace
        r = await self.client.post(
            "/workspaces",
            headers=self.headers,
            json={"name": "WS1", "root_path": str(ws_dir)},
        )
        self.assertEqual(r.status_code, 200)
        ws_id = r.json()["id"]

        # Create goal
        r = await self.client.post(
            "/goals",
            headers=self.headers,
            json={"workspace_id": ws_id, "title": "My Goal", "description": "Goal desc"},
        )
        self.assertEqual(r.status_code, 200)
        goal = r.json()
        self.assertEqual(goal["status"], "PLANNING")
        goal_id = goal["id"]

        # Reject starting while PLANNING
        r = await self.client.post(
            f"/goals/{goal_id}/start",
            headers=self.headers,
            json={"expected_version": 0},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "illegal_status")

        # Manually transition to PENDING (simulating planner success) and insert a step
        app.state.goals.update_status(goal_id, 0, "PENDING")
        app.state.executor._insert_steps(goal_id, [{"title": "Step 1", "description": "desc", "suggested_paths": []}])

        async def mock_run_step(g_id: str, s_id: str) -> None:
            await asyncio.sleep(1.0)
        app.state.executor.run_step = mock_run_step

        # Start with invalid expected_version -> 409 version_conflict
        r = await self.client.post(
            f"/goals/{goal_id}/start",
            headers=self.headers,
            json={"expected_version": 999},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "version_conflict")

        # Start with correct version -> RUNNING
        r = await self.client.post(
            f"/goals/{goal_id}/start",
            headers=self.headers,
            json={"expected_version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "RUNNING")
        current_version = r.json()["version"]

        # Pause goal
        r = await self.client.post(
            f"/goals/{goal_id}/pause",
            headers=self.headers,
            json={"expected_version": current_version},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "PAUSED")
        current_version = r.json()["version"]

        # Cancel goal
        r = await self.client.post(
            f"/goals/{goal_id}/cancel",
            headers=self.headers,
            json={"expected_version": current_version},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "CANCELLED")


if __name__ == "__main__":
    unittest.main()
