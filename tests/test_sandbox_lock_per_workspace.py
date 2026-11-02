"""The sandbox lock is per workspace tree, not one for the whole engine.

A command in a workspace must not run while another command is measuring that same tree: a parallel step's write
moving under a read-only probe is what the lock exists to stop. It is a rule about one directory, so a long
`pytest` in one workspace used to hold up every other workspace's commands for no reason. These pin both halves:
the same tree still serialises, and two trees do not wait on each other.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.db import connect
from engine.executor import ExecutorService
from engine.providers import Keychain
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService
from tests.test_executor import MockFactory, MockProvider


class TestSandboxLockIsPerTree(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.conn = connect(self.root / "t.db")
        registry = AgentRegistryService(self.conn, MockFactory(MockProvider({})), Keychain())
        self.executor = ExecutorService(GoalService(self.conn), WorkspaceService(self.conn), registry, SandboxService())
        self.a = self.root / "a"
        self.b = self.root / "b"
        self.a.mkdir()
        self.b.mkdir()

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def test_one_tree_is_one_lock_however_it_is_spelled(self) -> None:
        link = self.root / "link-to-a"
        link.symlink_to(self.a)

        self.assertIs(self.executor.sandbox_lock(self.a), self.executor.sandbox_lock(str(self.a)))
        self.assertIs(self.executor.sandbox_lock(self.a), self.executor.sandbox_lock(link))
        self.assertIs(self.executor.sandbox_lock(self.a), self.executor.sandbox_lock(self.a / "." / "../a"))

    def test_two_trees_are_two_locks(self) -> None:
        self.assertIsNot(self.executor.sandbox_lock(self.a), self.executor.sandbox_lock(self.b))

    async def test_a_command_in_one_workspace_does_not_wait_on_another_workspaces(self) -> None:
        async with self.executor.sandbox_lock(self.a):
            # A holds its tree. B's lock is free, so this returns at once instead of timing out.
            await asyncio.wait_for(self.executor.sandbox_lock(self.b).acquire(), 1)
            self.executor.sandbox_lock(self.b).release()

    async def test_a_second_command_in_the_same_workspace_still_waits(self) -> None:
        async with self.executor.sandbox_lock(self.a):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(self.executor.sandbox_lock(self.a).acquire(), 0.2)

    def test_there_is_no_engine_wide_sandbox_lock_left_to_reach_for(self) -> None:
        self.assertFalse(hasattr(self.executor, "_sandbox_lock"))
