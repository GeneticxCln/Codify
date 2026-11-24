"""Per-project usage: `/stats/overview` and `/stats/failures` narrowed to one workspace (docs/21 item 6).

Tokens and calls only: there is no price table (docs/21 decision A). What this file holds:

1. **A project's numbers are that project's.** Two projects with different goals, token spends and failures: the
   narrowed reads count only the chosen one's, and the all-projects read still counts both.
2. **The filter happens before the caps.** An event carries no project, so the narrowing goes through its goal, in
   SQL; filtering after a capped read would count only the share of the newest rows that happened to match.
3. **A narrowed read never writes the all-projects history.** Frozen daily snapshots are documents about every
   project; a project view freezing one would put a single project's day into everyone's trend for good.
4. **An unknown project is a 404, not zeros.** Zeros would read as "this project did nothing".
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

from engine import app as engine_app
from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.models import Event, WorkspaceCreate
from engine.services import GoalService, SettingsService, WorkspaceService
from engine.stats_history import StatsSnapshotService
from engine.trace import TraceService


class PerProjectStats(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "test.db")
        app.state.conn = self.conn
        app.state.workspaces = WorkspaceService(self.conn)
        app.state.goals = GoalService(self.conn)
        app.state.settings = SettingsService(self.conn)
        app.state.stats_snapshots = StatsSnapshotService(self.conn)
        app.state.traces = TraceService(self.conn)
        app.state.token = BOOT_TOKEN
        self.client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.conn.close()
        self.temp_dir.cleanup()

    def _project(self, name: str) -> str:
        root = self.root / name
        root.mkdir()
        workspaces: WorkspaceService = app.state.workspaces
        return workspaces.create(WorkspaceCreate(name=name, root_path=str(root))).id

    def _goal(self, workspace_id: str, *, tokens: int, failures: int = 0, at: float | None = None) -> str:
        """One finished goal in `workspace_id` that spent `tokens` in one call and failed `failures` calls."""
        now = at if at is not None else time.time()
        goal_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO goals (id, workspace_id, title, description, status, created_at, updated_at) "
            "VALUES (?, ?, 'g', 'g', 'COMPLETED', ?, ?)",
            (goal_id, workspace_id, now, now),
        )
        self.conn.commit()
        goals: GoalService = app.state.goals
        goals.publish(Event(
            id=str(uuid.uuid4()), goal_id=goal_id, step_id=None, type="usage",
            payload={"role": "fixer", "provider": "p", "model": "m",
                     "input_tokens": tokens - 1, "output_tokens": 1, "total_tokens": tokens, "duration_ms": 10},
            timestamp=now, sequence=goals.next_sequence(goal_id),
        ))
        for _ in range(failures):
            goals.publish(Event(
                id=str(uuid.uuid4()), goal_id=goal_id, step_id=None, type="agent_call_failed",
                payload={"role": "fixer", "code": "provider_timeout", "message": "slow"},
                timestamp=now, sequence=goals.next_sequence(goal_id),
            ))
        if at is not None:
            self.conn.execute("UPDATE events SET timestamp = ? WHERE goal_id = ?", (at, goal_id))
            self.conn.commit()
        return goal_id

    async def _get(self, path: str) -> dict[str, Any]:
        r = await self.client.get(path, headers=self.headers)
        self.assertEqual(200, r.status_code, r.text)
        body: dict[str, Any] = r.json()
        return body

    async def test_a_project_view_counts_only_that_projects_goals_tokens_and_failures(self) -> None:
        alpha, beta = self._project("alpha"), self._project("beta")
        self._goal(alpha, tokens=100, failures=1)
        self._goal(alpha, tokens=50)
        self._goal(beta, tokens=7, failures=2)

        everyone = await self._get("/stats/overview?window=0")
        self.assertEqual(3, everyone["goals"]["goals"])
        self.assertEqual(157, everyone["usage"]["total_tokens"])
        self.assertIsNone(everyone["workspace_id"])

        only_alpha = await self._get(f"/stats/overview?window=0&workspace_id={alpha}")
        self.assertEqual(2, only_alpha["goals"]["goals"], "another project's goal was counted")
        self.assertEqual(150, only_alpha["usage"]["total_tokens"], "another project's tokens were counted")
        self.assertEqual(alpha, only_alpha["workspace_id"])
        only_beta = await self._get(f"/stats/overview?window=0&workspace_id={beta}")
        self.assertEqual((1, 7), (only_beta["goals"]["goals"], only_beta["usage"]["total_tokens"]))

        self.assertEqual(3, (await self._get("/stats/failures?window=0"))["total"])
        self.assertEqual(1, (await self._get(f"/stats/failures?window=0&workspace_id={alpha}"))["total"])
        self.assertEqual(2, (await self._get(f"/stats/failures?window=0&workspace_id={beta}"))["total"])

        # An empty parameter is "every project", as a picker's first option sends it.
        self.assertEqual(3, (await self._get("/stats/overview?window=0&workspace_id="))["goals"]["goals"])

    async def test_the_filter_is_applied_before_the_cap_not_after(self) -> None:
        # With a cap of 2 events, the newest two belong to beta. Filtering afterwards would leave alpha nothing.
        alpha, beta = self._project("alpha"), self._project("beta")
        old = time.time() - 3600
        self._goal(alpha, tokens=40, at=old)
        self._goal(beta, tokens=1)
        self._goal(beta, tokens=1)
        original = engine_app._STATS_EVENT_LIMIT
        engine_app._STATS_EVENT_LIMIT = 2
        try:
            only_alpha = await self._get(f"/stats/overview?window=0&workspace_id={alpha}")
        finally:
            engine_app._STATS_EVENT_LIMIT = original
        self.assertEqual(40, only_alpha["usage"]["total_tokens"], "the project's own usage fell behind the cap")

    async def test_a_project_view_never_writes_the_all_projects_history(self) -> None:
        # A goal two days ago is a past day the all-projects read would freeze. The project read must not.
        alpha = self._project("alpha")
        self._goal(alpha, tokens=30, at=time.time() - 2 * 86400)
        await self._get(f"/stats/overview?window=0&workspace_id={alpha}")
        frozen = self.conn.execute("SELECT count(*) FROM stats_snapshots").fetchone()[0]
        self.assertEqual(0, frozen, "a one-project read froze a day into everyone's history")
        # The control: the all-projects read is what freezes it.
        await self._get("/stats/overview?window=0")
        self.assertGreater(self.conn.execute("SELECT count(*) FROM stats_snapshots").fetchone()[0], 0)

    async def test_an_unknown_project_is_refused_not_answered_with_zeros(self) -> None:
        for path in ("/stats/overview", "/stats/failures"):
            r = await self.client.get(f"{path}?window=0&workspace_id=no-such-project", headers=self.headers)
            self.assertEqual(404, r.status_code, path)
            self.assertEqual("unknown_workspace", r.json()["code"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
