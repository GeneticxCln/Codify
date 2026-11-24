"""Regression tests for Item 10: App-wide audit view (`engine/audit_feed.py`, `GET /audit/changes`)."""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — isolated CODIFY_HOME

import tempfile
import unittest
from pathlib import Path

import httpx
from httpx import ASGITransport

from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.models import Event, GoalCreate, WorkspaceCreate
from engine.services import GoalService, WorkspaceService


class TestAuditFeed(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.conn = connect(self.root / "audit.db")
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)

        self.dir_a = self.root / "alpha"
        self.dir_b = self.root / "beta"
        self.dir_a.mkdir()
        self.dir_b.mkdir()
        self.ws_a = self.workspaces.create(WorkspaceCreate(name="Alpha", root_path=str(self.dir_a)))
        self.ws_b = self.workspaces.create(WorkspaceCreate(name="Beta", root_path=str(self.dir_b)))

        app.state.conn = self.conn
        app.state.goals = self.goals
        app.state.workspaces = self.workspaces
        app.state.token = BOOT_TOKEN
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}
        self.client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.conn.close()
        self.tmp.cleanup()

    def _emit(
        self, goal_id: str, step_id: str | None, ev_type: str, payload: dict[str, object], ts: float
    ) -> None:
        seq = self.goals.next_sequence(goal_id)
        self.goals.publish(
            Event(
                id=f"ev-{goal_id}-{seq}",
                goal_id=goal_id,
                step_id=step_id,
                type=ev_type,
                payload=payload,
                timestamp=ts,
                sequence=seq,
            )
        )

    async def test_aggregates_commits_and_files_across_projects_and_filters_by_workspace(self) -> None:
        # Goal 1 in Alpha: touched 2 files and committed once.
        g_a = self.goals.create(GoalCreate(workspace_id=self.ws_a.id, title="Add banner", description=""))
        self.conn.execute(
            "INSERT INTO plan_steps (id, goal_id, ordinal, title, description, suggested_paths, status, commit_message) "
            "VALUES (?, ?, 1, ?, '', '[]', 'COMPLETED', ?)",
            ("step-a1", g_a.id, "Write banner", "feat: add banner"),
        )
        self.conn.commit()
        self._emit(
            g_a.id, "step-a1", "file_change_summary",
            {"paths": ["banner.txt", "README.md"], "dry_run": False, "unchanged": []},
            100.0,
        )
        self._emit(
            g_a.id, "step-a1", "log",
            {"level": "info", "message": "git committed 1a2b3c4: feat: add banner"},
            101.0,
        )

        # Goal 2 in Beta: diff event only (no commit).
        g_b = self.goals.create(GoalCreate(workspace_id=self.ws_b.id, title="Tweak config", description=""))
        self._emit(
            g_b.id, None, "diff",
            {"path": "config.toml", "unified_diff": "+key = 1\n"},
            200.0,
        )

        # Goal 3 in Alpha: question only, changed nothing -> must be excluded.
        self.goals.create(GoalCreate(workspace_id=self.ws_a.id, title="Just asking", description=""))

        # App-wide feed (all projects).
        r_all = await self.client.get("/audit/changes", headers=self.headers)
        self.assertEqual(200, r_all.status_code, r_all.text)
        body = r_all.json()
        self.assertIsNone(body["workspace_id"])
        self.assertEqual(2, body["total_goals"])
        self.assertEqual(1, body["total_commits"])
        self.assertEqual(3, body["total_files"])

        by_goal = {e["goal_id"]: e for e in body["entries"]}
        self.assertIn(g_a.id, by_goal)
        self.assertIn(g_b.id, by_goal)
        self.assertEqual("Alpha", by_goal[g_a.id]["workspace_name"])
        self.assertEqual(["banner.txt", "README.md"], by_goal[g_a.id]["files"])
        self.assertEqual("1a2b3c4", by_goal[g_a.id]["commits"][0]["sha"])
        self.assertEqual("feat: add banner", by_goal[g_a.id]["commits"][0]["message"])
        self.assertEqual("Write banner", by_goal[g_a.id]["commits"][0]["step_title"])
        self.assertEqual(["config.toml"], by_goal[g_b.id]["files"])

        # Filtered to Beta only.
        r_beta = await self.client.get(
            f"/audit/changes?workspace_id={self.ws_b.id}", headers=self.headers
        )
        self.assertEqual(200, r_beta.status_code, r_beta.text)
        beta = r_beta.json()
        self.assertEqual(self.ws_b.id, beta["workspace_id"])
        self.assertEqual(1, beta["total_goals"])
        self.assertEqual(g_b.id, beta["entries"][0]["goal_id"])

        # Unknown workspace -> 404 unknown_workspace.
        r_missing = await self.client.get("/audit/changes?workspace_id=no-such-ws", headers=self.headers)
        self.assertEqual(404, r_missing.status_code, r_missing.text)
        self.assertEqual("unknown_workspace", r_missing.json()["code"])


if __name__ == "__main__":
    unittest.main()
