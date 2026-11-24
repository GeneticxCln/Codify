"""Regression tests for `POST /goals/{id}/trace/replay` (`engine/replay.py`, Item 8)."""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — isolated CODIFY_HOME

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

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

    async def test_replay_handles_dangling_and_external_symlinks_without_dereferencing(self) -> None:
        # Preserve the original v1.9.0 failure: a workspace with a dangling symlink caused
        # `POST /goals/{id}/trace/replay` to crash with 500 (`shutil.Error`), and an external
        # symlink had its target copied verbatim into the scratch directory.
        secret_outside = self.root / "outside-secret.txt"
        secret_outside.write_text("TOP_SECRET_DO_NOT_COPY\n")
        os.symlink("/nonexistent/codify-dangling-target", self.ws_dir / "dangling_link")
        os.symlink(secret_outside, self.ws_dir / "external_link")

        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="plan symlink repo", description="", plan_only=True, trace=True)
        )
        await self._executor().run_planning(goal.id)

        r = await self.client.post(f"/goals/{goal.id}/trace/replay", headers=self.headers)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body["matched"], body)

        # Also verify directly that scratch copy preserves symlinks rather than materializing external targets.
        from engine.replay import replay

        scratch_into = self.root / "kept-scratch"
        report = await replay(self.conn, goal.id, source=self.ws_dir, into=scratch_into, cleanup_scratch=False)
        self.assertTrue(report["matched"], report)
        copied_external = scratch_into / "external_link"
        self.assertTrue(copied_external.is_symlink(), "external symlink must not be dereferenced into a copied file")
        copied_dangling = scratch_into / "dangling_link"
        self.assertTrue(copied_dangling.is_symlink(), "dangling symlink must be preserved without failing copytree")

    async def test_replay_copies_off_event_loop_and_uses_scratch_keychain(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="plan isolation", description="", plan_only=True, trace=True)
        )
        await self._executor().run_planning(goal.id)

        seen_secrets_paths: list[Path | None] = []
        copy_threads: list[int] = []
        loop_thread = threading.get_ident()
        orig_keychain = Keychain
        from engine import replay as replay_mod
        orig_copy = replay_mod._copy_workspace_tree

        def spy_keychain(secrets_path: Path | None = None) -> Keychain:
            seen_secrets_paths.append(secrets_path)
            return orig_keychain(secrets_path=secrets_path)

        def spy_copy(src: Path, dst: Path) -> None:
            copy_threads.append(threading.get_ident())
            orig_copy(src, dst)

        with (
            patch("engine.replay.Keychain", side_effect=spy_keychain),
            patch("engine.replay._copy_workspace_tree", side_effect=spy_copy),
        ):
            r = await self.client.post(f"/goals/{goal.id}/trace/replay", headers=self.headers)
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(seen_secrets_paths, "expected replay to construct a Keychain")
        self.assertTrue(
            all(p is not None for p in seen_secrets_paths),
            f"replay must pass an explicit scratch secrets_path instead of touching host keyring: {seen_secrets_paths}",
        )
        self.assertEqual(1, len(copy_threads))
        self.assertNotEqual(loop_thread, copy_threads[0], "workspace tree copy must run off the event loop")

    async def test_replay_copy_failure_returns_structured_422_instead_of_500(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="plan copy err", description="", plan_only=True, trace=True)
        )
        await self._executor().run_planning(goal.id)

        with patch("engine.replay.shutil.copytree", side_effect=shutil.Error([("a", "b", "permission denied")])):
            r = await self.client.post(f"/goals/{goal.id}/trace/replay", headers=self.headers)
        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual("replay_copy_failed", r.json()["code"])


if __name__ == "__main__":
    unittest.main()
