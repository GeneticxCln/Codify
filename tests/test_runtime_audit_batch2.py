"""Regression tests for AI agent runtime audit — Batch 2 (H1, H5, M1, M9)."""

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
from unittest import mock

from engine.app import _run_steps_locked, app
from engine.conductor_tools import ConductorTools
from engine.db import connect
from engine.executor import ExecutorService
from engine.fs import FileMovedUnderProposalError, FileSystemService
from engine.git import GitService
from engine.library import LibraryService
from engine.models import Event, GoalCreate, WorkspaceCreate
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import CANCELLED_EXIT_CODE, SandboxService
from engine.services import AgentRegistryService, GoalService, SettingsService, WorkspaceService
from engine.skills import load_skills


class TestBatch2RuntimeAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ws_dir = self.root / "ws"
        self.ws_dir.mkdir()
        self.conn = connect(self.root / "codify.db")
        self.addCleanup(self.conn.close)
        self.keychain = Keychain(secrets_path=self.root / "secrets.json")
        self.registry = AgentRegistryService(self.conn, ProviderFactory(self.keychain), self.keychain)
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.sandbox = SandboxService()
        self.settings = SettingsService(self.conn)
        self.executor = ExecutorService(self.goals, self.workspaces, self.registry, self.sandbox)
        self.executor.settings = self.settings
        self.ws = self.workspaces.create(WorkspaceCreate(name="ws", root_path=str(self.ws_dir)))

    # ── H1: Resuming a paused dry-run Apply replays stored proposals ─────────

    async def test_h1_resume_paused_apply_replays_stored_proposals_without_live_llm(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="dry apply", description="", dry_run=True),
        )
        now = time.time()
        # Step 1 already completed during Apply; Step 2 paused before replay.
        self.conn.execute(
            "INSERT INTO plan_steps (id, goal_id, ordinal, title, description, suggested_paths, status) "
            "VALUES ('s1', ?, 0, 'step 1', 'd1', '[\"a.txt\"]', 'COMPLETED'), "
            "('s2', ?, 1, 'step 2', 'd2', '[\"b.txt\"]', 'PENDING')",
            (goal.id, goal.id),
        )
        self.conn.execute(
            "INSERT INTO proposed_files (goal_id, step_id, path, action, content, base_version, created_at) "
            "VALUES (?, 's2', 'b.txt', 'create', 'reviewed content\n', '', ?)",
            (goal.id, now),
        )
        # Apply cleared dry_run=False and later paused, then user hit Start -> RUNNING.
        self.conn.execute(
            "UPDATE goals SET dry_run = 0, status = 'RUNNING', updated_at = ? WHERE id = ?",
            (now, goal.id),
        )
        self.conn.commit()

        app.state.goals = self.goals
        app.state.executor = self.executor

        replayed: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        async def fake_drive_replay(
            gid: str, by_step: dict[str, Any], by_base: dict[str, Any],
        ) -> Any:
            replayed.append((gid, by_step, by_base))
            return self.goals.get(gid)

        async def forbidden_conductor(gid: str, **kwargs: Any) -> Any:
            raise AssertionError("resuming a paused Apply must not invoke the live conductor")

        async def forbidden_run_step(gid: str, sid: str, **kwargs: Any) -> Any:
            raise AssertionError("resuming a paused Apply must not fall through to run_step(stored_files=None)")

        with (
            mock.patch.object(self.executor, "_drive_replay_steps", side_effect=fake_drive_replay),
            mock.patch.object(self.executor, "conductor_can_drive", return_value=True),
            mock.patch.object(self.executor, "run_conductor_resume", side_effect=forbidden_conductor),
            mock.patch.object(self.executor, "run_step", side_effect=forbidden_run_step),
        ):
            await _run_steps_locked(app, goal.id)

        self.assertEqual(len(replayed), 1)
        gid, by_step, by_base = replayed[0]
        self.assertEqual(gid, goal.id)
        self.assertEqual(by_step["s2"], [{"path": "b.txt", "action": "create", "content": "reviewed content\n"}])
        self.assertEqual(by_base["s2"], {"b.txt": ""})

    # ── H5: Pre-cancelled sandbox, git cancel, library cancel, lock drain ────

    def test_h5_sandbox_does_not_spawn_when_cancel_already_set(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with mock.patch("engine.sandbox.subprocess.Popen", side_effect=AssertionError("must not spawn Popen")):
            res = self.sandbox.run_command(str(self.ws_dir), ["ls"], mode="read_only", cancel=cancel)
        self.assertEqual(res["exit_code"], CANCELLED_EXIT_CODE)
        self.assertIn("cancelled", res["stderr"])

    def test_h5_git_read_only_and_commit_honour_cancel(self) -> None:
        git = GitService()
        subprocess.run(["git", "init", "-q"], cwd=str(self.ws_dir), check=True)
        (self.ws_dir / "note.txt").write_text("hello\n", encoding="utf-8")

        cancel = threading.Event()
        cancel.set()
        out = git.read_only(str(self.ws_dir), ["status"], cancel=cancel)
        self.assertIn("cancelled", out)

        commit_hash, reason = git.commit_with_reason(
            str(self.ws_dir), "feat: should not commit", ["note.txt"], cancel=cancel,
        )
        self.assertIsNone(commit_hash)
        self.assertEqual(reason, "cancelled")
        # Verify no commit landed.
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=str(self.ws_dir), capture_output=True, text=True,
        )
        self.assertNotEqual(head.returncode, 0)

    async def test_h5_librarian_requests_pass_cancel_signal(self) -> None:
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="lib cancel", description=""))
        seen_cancels: list[threading.Event | None] = []

        class _SpySandbox(SandboxService):
            def run_command(
                self, root_path: str, argv: list[str], timeout_s: int = 120, mode: str = "test",
                cancel: threading.Event | None = None, network: bool | None = None,
            ) -> dict[str, Any]:
                seen_cancels.append(cancel)
                return {"argv": argv, "exit_code": 0, "stdout": "ok", "stderr": ""}

        lib = LibraryService(str(self.ws_dir), sandbox=_SpySandbox())
        (self.ws_dir / "a.py").write_text("needle = 1\n", encoding="utf-8")
        await self.executor._serve_library_requests(
            goal.id, lib, [("run", ["ls"]), ("searches", "needle")],
        )
        self.assertEqual(seen_cancels, [self.executor.cancel_signal(goal.id)])

    async def test_h5_to_thread_drain_holds_lock_until_worker_exits(self) -> None:
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="drain", description=""))
        worker_started = threading.Event()
        worker_exited = threading.Event()
        cancel = self.executor.cancel_signal(goal.id)

        def slow_worker() -> str:
            worker_started.set()
            assert cancel.wait(timeout=5.0), "cancel signal was not set by _to_thread_drain"
            time.sleep(0.15)
            worker_exited.set()
            return "done"

        async def holder() -> None:
            async with self.executor.sandbox_lock(str(self.ws_dir)):
                await self.executor._to_thread_drain(goal.id, slow_worker)

        task = asyncio.create_task(holder())
        while not worker_started.is_set():
            await asyncio.sleep(0.01)

        task.cancel()
        # While the worker is tearing down, the lock must remain held.
        await asyncio.sleep(0.05)
        self.assertTrue(self.executor.sandbox_lock(str(self.ws_dir)).locked())
        self.assertFalse(worker_exited.is_set())

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(worker_exited.is_set(), "lock/task unwound before worker exited")
        self.assertFalse(self.executor.sandbox_lock(str(self.ws_dir)).locked())

    # ── M1: Pause/resume + second write preserves run 1's diff ───────────────

    def test_m1_resume_second_write_preserves_run1_diff(self) -> None:
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="m1", description=""))
        step_id = "step-m1"
        run1_diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        self.goals.publish(
            Event(
                id="ev-m1-1",
                goal_id=goal.id,
                step_id=step_id,
                sequence=self.goals.next_sequence(goal.id),
                type="diff",
                payload={"path": "app.py", "unified_diff": run1_diff},
                timestamp=time.time(),
            )
        )
        # Disk already has post-run-1 content before run 2 begins.
        target = self.ws_dir / "app.py"
        target.write_text("x = 2\n", encoding="utf-8")

        tools = ConductorTools(self.executor, goal.id, goal, str(self.ws_dir), load_skills(str(self.ws_dir)))
        state = tools._state_of(step_id)
        # Simulate _fixer recording baseline from post-run-1 disk and writing run 2's edit.
        fs = FileSystemService(str(self.ws_dir))
        state.baselines["app.py"] = fs.read_text_or_none("app.py")
        summaries = fs.apply(
            [{"path": "app.py", "action": "update", "content": "x = 2\ny = 3\n"}],
            dry_run=False,
        )
        state.wrote(summaries, fs=fs, dry_run=False)

        combined = state.files["app.py"]["unified_diff"]
        self.assertIn("-x = 1", combined)
        self.assertIn("+x = 2", combined)
        self.assertIn("+y = 3", combined)
        self.assertTrue(state.files["app.py"]["changed"])

    # ── M9: expected_versions[path] = None skips comparison ──────────────────

    def test_m9_expected_versions_none_skips_comparison_while_empty_string_guards_absent(self) -> None:
        fs = FileSystemService(str(self.ws_dir))
        (self.ws_dir / "existing.txt").write_text("before\n", encoding="utf-8")

        # None means "version unknown (e.g. OSError during context read)" -> must succeed.
        summaries = fs.apply(
            [{"path": "existing.txt", "action": "update", "content": "after\n"}],
            dry_run=False,
            expected_versions={"existing.txt": None},
        )
        self.assertEqual((self.ws_dir / "existing.txt").read_text(encoding="utf-8"), "after\n")
        self.assertTrue(summaries[0]["changed"])

        # "" means "file was absent" -> must still reject existing file.
        with self.assertRaises(FileMovedUnderProposalError):
            fs.apply(
                [{"path": "existing.txt", "action": "create", "content": "nope\n"}],
                dry_run=False,
                expected_versions={"existing.txt": ""},
            )


if __name__ == "__main__":
    unittest.main()
