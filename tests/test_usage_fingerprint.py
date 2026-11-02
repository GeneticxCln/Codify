"""The `usage` event carries a content-free fingerprint of the prompt it paid for (docs/21 P3).

Pinned here: the fingerprint changes when, and only when, the system prompt, the tool set, the model or the provider
changes; it is the same for two different user messages under one system prompt, which is what a prefix cache is keyed
on; and nothing on the event holds the prompt text, however distinctive.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.test_executor import _SkippedGate, configure_every_role

from engine.conductor import ToolCallRequest
from engine.db import connect
from engine.executor import ExecutorService
from engine.models import AgentConfig, GoalCreate, WorkspaceCreate
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService

MARKER = "DISTINCTIVE-PROMPT-TEXT-4471"


class _SinkProvider(BaseProvider):
    """Answers a critic's JSON and reports usage the way a real provider does."""

    async def complete(self, system_prompt: str, user_prompt: str, model: str, temperature: float,
                       max_tokens: int, **_: Any) -> str:
        if self.usage_sink is not None:
            self.usage_sink({"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
        return json.dumps({"decision": "approve", "reasons": []})


class _Factory(ProviderFactory):
    def __init__(self, provider: _SinkProvider) -> None:
        super().__init__(Keychain())
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        return self.provider


class TestUsageFingerprint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name).resolve() / "work"
        root.mkdir()
        self.conn = connect(Path(self.temp_dir.name) / "t.db")
        self.provider = _SinkProvider()
        registry = AgentRegistryService(self.conn, _Factory(self.provider), Keychain())
        configure_every_role(registry)
        workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.executor = ExecutorService(self.goals, workspaces, registry, SandboxService(), laya=_SkippedGate())
        ws = workspaces.create(WorkspaceCreate(name="WS", root_path=str(root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="G", description=""))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def usage(self) -> list[dict[str, Any]]:
        return [e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == "usage"]

    async def ask(self, user: str, system: str | None = None) -> None:
        await self.executor.orchestrator.run_agent("critic", self.goal.id, None, user, system=system)

    async def test_the_same_system_prompt_gives_the_same_fingerprint_whatever_the_user_said(self) -> None:
        await self.ask("first question")
        await self.ask("a quite different and longer second question")
        first, second = self.usage()
        self.assertEqual(first["prompt_fingerprint"], second["prompt_fingerprint"])
        self.assertEqual(first["prompt_parts"], second["prompt_parts"])
        self.assertNotEqual(first["prompt_chars"], second["prompt_chars"], "the length is the whole prompt's")

    async def test_a_different_system_prompt_changes_the_fingerprint_and_names_the_part(self) -> None:
        await self.ask("q")
        await self.ask("q", system="You are a different reader.")
        first, second = self.usage()
        self.assertNotEqual(first["prompt_fingerprint"], second["prompt_fingerprint"])
        self.assertNotEqual(first["prompt_parts"]["system"], second["prompt_parts"]["system"])
        self.assertEqual(first["prompt_parts"]["model"], second["prompt_parts"]["model"])
        self.assertEqual(first["prompt_parts"]["tools"], second["prompt_parts"]["tools"])

    async def test_nothing_on_the_event_holds_the_prompt(self) -> None:
        await self.ask(f"the user wrote {MARKER}", system=f"You are the {MARKER} reader.")
        shown = json.dumps(self.usage())
        self.assertNotIn(MARKER, shown)
        self.assertNotIn("reader", shown)
        self.assertEqual({"system", "tools", "model", "protocol"}, set(self.usage()[0]["prompt_parts"]))

    async def test_the_conductor_call_fingerprints_its_system_prompt_and_tool_set(self) -> None:
        cfg = SimpleNamespace(provider="anthropic")
        ledger = self.executor.orchestrator.tool_call_ledger(
            self.goal.id, None, [(self.provider, "m1", cfg)],  # type: ignore[list-item]
        )

        def book(tools: tuple[str, ...], system: str = "conductor system") -> dict[str, Any]:
            books = ledger.open(ToolCallRequest(
                self.provider, "m1", system, [{"role": "user", "content": MARKER}], 0.2, 100, tools,
            ))
            assert self.provider.usage_sink is not None
            self.provider.usage_sink({"input_tokens": 5, "output_tokens": 1, "total_tokens": 6})
            books.close()
            return self.usage()[-1]

        a = book(("read_file", "write"))
        b = book(("write", "read_file"))
        c = book(("read_file",))
        self.assertEqual(a["prompt_fingerprint"], b["prompt_fingerprint"], "tool order is not a different set")
        self.assertNotEqual(a["prompt_fingerprint"], c["prompt_fingerprint"])
        self.assertEqual("anthropic", a["prompt_parts"]["protocol"])
        self.assertNotIn(MARKER, json.dumps(self.usage()))


if __name__ == "__main__":
    unittest.main()
