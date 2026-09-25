"""End-to-end coverage for the dry-run → apply flow through the HTTP API.

The dry run must not touch the workspace, the proposal must be stored, and
"Apply" must write exactly the proposed bytes *without* asking the fixer again —
a second LLM call could return something different from what the user approved.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import LayaDecision, LayaService
from engine.models import ROLES, AgentConfig, AgentConfigUpdate, Goal
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService
from tests.versioned import post_versioned_async

PROPOSED = "hello from a dry run\n"


class _StubProvider(BaseProvider):
    # The factory stamps the last-built role here (see engine's routing contract).
    current_role: str | None

    def __init__(self, **by_role: Any):
        self.by_role = by_role
        self.calls: list[dict[str, Any]] = []
        # Which role's config the factory last built a provider for. Routing on this
        # instead of scanning the system prompt for a role name: the prompts refer
        # to each other (the planner is given "the librarian's evidence pack"), so a
        # keyword scan answers the wrong role's script.
        self.current_role: str | None = None

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        if self.current_role in self.by_role:
            resp = self.by_role[self.current_role]
            return resp if isinstance(resp, str) else json.dumps(resp)
        for role, resp in self.by_role.items():
            if role in system_prompt.lower():
                return resp if isinstance(resp, str) else json.dumps(resp)
        return json.dumps({"status": "ok"})

    def fixer_calls(self) -> int:
        return len([c for c in self.calls if "codify fixer" in c["system_prompt"].lower()])


class _StubFactory(ProviderFactory):
    def __init__(self, provider: _StubProvider) -> None:
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        self.provider.current_role = config.role
        return self.provider


class _NoGate(LayaService):
    """Skip the pre-flight gate so the pipeline under test is the only variable."""

    async def decide(self, state: dict[str, Any]) -> LayaDecision:
        return LayaDecision(engine="skipped", skipped_reason="test double")


class TestApplyFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.ws_dir = self.root / "workspace"
        self.ws_dir.mkdir()

        conn = connect(self.root / "apply.db")
        self.provider = _StubProvider(
            planner={
                "steps": [
                    {"title": "Add banner", "description": "create it", "suggested_paths": ["banner.txt"]}
                ]
            },
            fixer={"files": [{"path": "banner.txt", "action": "create", "content": PROPOSED}]},
            verifier={"argv": None, "verdict": "pass", "explanation": "nothing to run"},
            critic={"decision": "approve", "reasons": []},
            scribe={"summary": "added banner", "commit_message": "feat: banner"},
        )
        self.goals = GoalService(conn)
        self.registry = AgentRegistryService(conn, _StubFactory(self.provider), Keychain())
        # Seeded roles carry no model id, so this suite states the one it runs with.
        for role in ROLES:
            self.registry.set_config(role, AgentConfigUpdate(model_name="stub-model"))
        app.state.conn = conn
        app.state.goals = self.goals
        app.state.registry = self.registry
        app.state.workspaces = WorkspaceService(conn)
        app.state.sandbox = SandboxService()
        app.state.executor = ExecutorService(
            self.goals, app.state.workspaces, self.registry, app.state.sandbox, laya=_NoGate()
        )
        app.state.token = BOOT_TOKEN

        self.transport = ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        app.state.conn.close()
        self.temp_dir.cleanup()

    async def _mk_workspace(self) -> str:
        r = await self.client.post(
            "/workspaces", json={"name": "WS", "root_path": str(self.ws_dir)}, headers=self.headers
        )
        self.assertEqual(r.status_code, 200, r.text)
        workspace_id: str = r.json()["id"]
        return workspace_id

    async def _mk_goal(self, ws_id: str, *, dry_run: bool) -> dict[str, Any]:
        r = await self.client.post(
            "/goals",
            json={"workspace_id": ws_id, "title": "Add banner", "description": "d", "dry_run": dry_run},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        goal: dict[str, Any] = r.json()
        return goal

    async def _wait_for_status(
        self, goal_id: str, wanted: set[str], timeout: float = 5.0
    ) -> Goal:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        g = self.goals.get(goal_id)
        while g.status not in wanted and loop.time() < deadline:
            await asyncio.sleep(0.02)
            g = self.goals.get(goal_id)
        self.assertIn(g.status, wanted, f"goal stuck in {g.status}")
        return g

    async def _run_dry_run(self) -> tuple[str, str]:
        ws_id = await self._mk_workspace()
        goal = await self._mk_goal(ws_id, dry_run=True)
        await self._wait_for_status(goal["id"], {"PENDING"})
        # The planner is a live background task here, so quoting a version read a
        # moment earlier is a race; the helper re-reads if it loses one.
        await post_versioned_async(self.client, goal["id"], "start", headers=self.headers)
        await self._wait_for_status(goal["id"], {"COMPLETED", "FAILED"})
        return ws_id, goal["id"]

    async def test_dry_run_stores_the_proposal_and_writes_nothing(self) -> None:
        _, goal_id = await self._run_dry_run()

        self.assertFalse((self.ws_dir / "banner.txt").exists(), "dry run must not write files")
        self.assertTrue(self.goals.has_proposed_files(goal_id))
        self.assertEqual(self.provider.fixer_calls(), 1)

        diffs = [e for e in self.goals.events_after(goal_id, 0) if e.type == "diff"]
        self.assertEqual(len(diffs), 1)
        self.assertIn(PROPOSED.strip(), diffs[0].payload["unified_diff"])

    async def test_apply_writes_the_exact_proposal_without_re_asking_the_fixer(self) -> None:
        _, goal_id = await self._run_dry_run()
        fixer_calls_before = self.provider.fixer_calls()

        applied = await post_versioned_async(
            self.client, goal_id, "apply", headers=self.headers
        )
        self.assertEqual(
            applied.response.json(), {"applied": True, "goal_id": goal_id}
        )

        goal = await self._wait_for_status(goal_id, {"COMPLETED", "FAILED"})
        self.assertEqual(goal.status, "COMPLETED")
        self.assertFalse(goal.dry_run, "the apply run is a real execution")

        self.assertEqual((self.ws_dir / "banner.txt").read_text(), PROPOSED)
        self.assertEqual(
            self.provider.fixer_calls(), fixer_calls_before, "apply must replay stored contents"
        )

        summaries = [e for e in self.goals.events_after(goal_id, 0) if e.type == "file_change_summary"]
        self.assertEqual(len(summaries), 2, "one dry run + one real run")
        self.assertTrue(summaries[0].payload["dry_run"])
        self.assertFalse(summaries[1].payload["dry_run"])

        # Every step ran for real: no step may be left PENDING.
        self.assertTrue(all(s.status == "COMPLETED" for s in self.goals.steps(goal_id)))

    async def test_apply_guards_answer_409_instead_of_faking_success(self) -> None:
        ws_id = await self._mk_workspace()

        # Not a dry run at all.
        real = await self._mk_goal(ws_id, dry_run=False)
        r = await self.client.post(
            f"/goals/{real['id']}/apply", headers=self.headers,
            json={"expected_version": 0},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "not_dry_run")

        # A dry run that has not finished yet cannot be applied.
        pending = await self._mk_goal(ws_id, dry_run=True)
        r = await self.client.post(
            f"/goals/{pending['id']}/apply", headers=self.headers,
            json={"expected_version": 0},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "illegal_status")

        # Terminal dry run with nothing proposed is refused, not silently a no-op.
        self.goals._db.execute(
            "UPDATE goals SET status='COMPLETED', version=version+1 WHERE id=?", (pending["id"],)
        )
        self.goals._db.commit()
        r = await self.client.post(
            f"/goals/{pending['id']}/apply", headers=self.headers,
            json={"expected_version": self.goals.get(pending["id"]).version},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "nothing_to_apply")

    async def test_a_failing_test_suite_is_reported_as_tests_failed(self) -> None:
        ws_id = await self._mk_workspace()
        self.provider.by_role["verifier"] = {"argv": None, "verdict": "fail", "explanation": "assertion error"}
        goal = await self._mk_goal(ws_id, dry_run=False)
        await self._wait_for_status(goal["id"], {"PENDING"})
        await post_versioned_async(self.client, goal["id"], "start", headers=self.headers)
        await self._wait_for_status(goal["id"], {"FAILED"})

        errors = [e for e in self.goals.events_after(goal["id"], 0) if e.type == "error"]
        self.assertEqual(errors[-1].payload["code"], "tests_failed")
        self.assertIn("assertion error", errors[-1].payload["message"])

    async def test_pause_and_cancel_emit_goal_status_events(self) -> None:
        ws_id = await self._mk_workspace()
        goal = await self._mk_goal(ws_id, dry_run=False)
        await self._wait_for_status(goal["id"], {"PENDING"})

        # /cancel goes through GoalService.update_status, which must announce the
        # change — an open chat should never have to poll to notice it.
        # Deliberately after the plan: /cancel is legal from PLANNING too, and this
        # test is about the event a cancel of a *planned* goal publishes.
        await post_versioned_async(self.client, goal["id"], "cancel", headers=self.headers)
        status_events = [e for e in self.goals.events_after(goal["id"], 0) if e.type == "goal_status"]
        self.assertEqual(status_events[-1].payload["status"], "CANCELLED")

        # Same for a status change made by the executor itself (pause mid-run).
        other = await self._mk_goal(ws_id, dry_run=False)
        await self._wait_for_status(other["id"], {"PENDING"})
        before = len([e for e in self.goals.events_after(other["id"], 0) if e.type == "goal_status"])
        self.goals.update_status(other["id"], self.goals.get(other["id"]).version, "PAUSED")
        paused = [e for e in self.goals.events_after(other["id"], 0) if e.type == "goal_status"]
        self.assertEqual(len(paused), before + 1)
        self.assertEqual(paused[-1].payload["status"], "PAUSED")
        self.assertEqual(paused[-1].payload["version"], self.goals.get(other["id"]).version)


if __name__ == "__main__":
    unittest.main()
