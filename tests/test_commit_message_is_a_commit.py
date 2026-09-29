"""A step shows a commit only if one happened (audit of 2026-09-29, second pass).

The scribe's `commit_message` was written onto the step *before* anything knew whether a commit would
follow, and the timeline renders any stored message as a green check and "Commit: <message>". So a workspace
that is not a git repository, a dry run, a cancelled step and a step whose files already matched the last
commit all showed a commit that does not exist — the one place in the product that told a person their
history had changed when it had not. Found by running the golden path in a plain folder.

The message is now stored when the commit lands. When it does not, the message the scribe wrote is put in
the step's log with the reason, so nothing the model produced is lost and nothing is claimed.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from tests.test_executor import _QueuedFactory, _QueuedProvider, _SkippedGate, configure_every_role

from engine.db import connect
from engine.executor import ExecutorService
from engine.git import GitService
from engine.providers import Keychain
from engine.models import GoalCreate, WorkspaceCreate
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService

MESSAGE = "feat: add banner"

SCRIPT: dict[str, Any] = {
    "planner": {"steps": [{"title": "S1", "description": "d", "suggested_paths": ["banner.txt"]}]},
    "fixer": {"files": [{"path": "banner.txt", "action": "create", "content": "hello\n"}]},
    "verifier": {"argv": None, "verdict": "pass", "explanation": "nothing to run"},
    "critic": {"decision": "approve", "reasons": []},
    "scribe": {"summary": "added a banner", "commit_message": MESSAGE},
}


class _Workspace(unittest.IsolatedAsyncioTestCase):
    """A workspace directory, optionally a git repository, driven through one real step."""

    is_repo = True

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve() / "work"
        self.root.mkdir()
        (self.root / "keep.txt").write_text("kept\n", encoding="utf-8")
        if self.is_repo:
            git = GitService()
            git.init_repo(str(self.root))
            git.commit(str(self.root), "chore: base", ["keep.txt"])
        self.conn = connect(Path(self.temp_dir.name) / "t.db")
        self.provider = _QueuedProvider()
        self.registry = AgentRegistryService(
            self.conn, _QueuedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def run_one_step(self, dry_run: bool = False) -> str:
        self.provider.by_role = {role: [value] for role, value in SCRIPT.items()}
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="T", description=""))
        await self.executor.run_planning(goal.id)
        if dry_run:
            self.goals.set_dry_run(goal.id, True)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, goal.version + 1, "RUNNING")
        await self.executor.run_step(goal.id, step.id)
        return goal.id

    def stored_message(self, goal_id: str) -> str | None:
        return self.goals.steps(goal_id)[0].commit_message

    def step_log(self, goal_id: str) -> str:
        return " | ".join(
            e.payload["message"] for e in self.goals.events_after(goal_id, 0) if e.type == "log"
        )

    def published_messages(self, goal_id: str) -> list[Any]:
        return [
            e.payload.get("commit_message") for e in self.goals.events_after(goal_id, 0)
            if e.type == "step_status" and "commit_message" in e.payload
        ]


class TestARealCommitIsShown(_Workspace):
    async def test_the_message_is_stored_once_the_commit_has_landed(self) -> None:
        goal_id = await self.run_one_step()

        self.assertEqual(MESSAGE, self.stored_message(goal_id))
        subject = subprocess.run(
            ["git", "log", "-1", "--format=%s"], cwd=self.root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(MESSAGE, subject, "what the step shows is what git holds")

    async def test_the_published_event_carries_the_message_only_with_the_commit(self) -> None:
        goal_id = await self.run_one_step()

        self.assertEqual([MESSAGE], self.published_messages(goal_id))


class TestNoRepositoryNoCommit(_Workspace):
    is_repo = False

    async def test_a_plain_folder_shows_no_commit(self) -> None:
        goal_id = await self.run_one_step()

        self.assertIsNone(self.stored_message(goal_id))
        self.assertEqual([], self.published_messages(goal_id))
        self.assertEqual("COMPLETED", self.goals.steps(goal_id)[0].status)
        self.assertTrue((self.root / "banner.txt").exists(), "the change itself still lands on disk")

    async def test_the_person_is_told_why_and_keeps_the_message(self) -> None:
        goal_id = await self.run_one_step()

        log = self.step_log(goal_id)
        self.assertIn("not a git repository", log)
        self.assertIn(MESSAGE, log, "the scribe's message is kept in the log, not thrown away")


class TestADryRunCommitsNothingAndSaysSo(_Workspace):
    async def test_a_dry_run_shows_no_commit(self) -> None:
        goal_id = await self.run_one_step(dry_run=True)

        self.assertIsNone(self.stored_message(goal_id))
        self.assertIn("dry run", self.step_log(goal_id))
        self.assertIn(MESSAGE, self.step_log(goal_id))


class TestNothingToCommitShowsNoCommit(_Workspace):
    async def test_files_that_already_match_the_last_commit_show_no_commit(self) -> None:
        # The fixer writes exactly what is already committed: git has nothing to record.
        (self.root / "banner.txt").write_text("hello\n", encoding="utf-8")
        GitService().commit(str(self.root), "chore: banner already here", ["banner.txt"])

        goal_id = await self.run_one_step()

        self.assertIsNone(self.stored_message(goal_id))
