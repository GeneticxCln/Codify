"""Regression tests for AI agent runtime audit Batch 1 (conductor & provider bugs)."""
from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from engine.conductor import CALL_MCP, Conductor
from engine.db import connect
from engine.executor import ExecutorService
from engine.models import BUILTIN_PROVIDERS, ConversationCreate, GoalCreate, WorkspaceCreate
from engine.providers import (
    ENV_KEY_MAP,
    AnthropicProvider,
    BaseProvider,
    GoogleProvider,
    Keychain,
    ProviderError,
    ProviderFactory,
    _thinking_budget,
)
from engine.compaction import CompactionService
from engine.sandbox import SandboxService
from engine.services import (
    AgentRegistryService,
    ConversationService,
    GoalService,
    SettingsService,
    WorkspaceService,
)
from engine.toolcall import ToolCall, ToolReply, ToolSpec, coerce_arguments, schema_problems


class TestBatch1ConductorAndProviders(unittest.IsolatedAsyncioTestCase):
    def test_nvidia_is_registered_in_env_key_map(self) -> None:
        self.assertIn("nvidia", ENV_KEY_MAP)
        self.assertIn("NVIDIA_API_KEY", ENV_KEY_MAP["nvidia"])
        for slug, meta in BUILTIN_PROVIDERS.items():
            if meta.get("needs_key"):
                self.assertIn(slug, ENV_KEY_MAP, f"keyed built-in provider {slug!r} missing from ENV_KEY_MAP")

    def test_thinking_budget_returns_none_for_invalid_or_empty_effort(self) -> None:
        self.assertIsNone(_thinking_budget(None, 4096))
        self.assertIsNone(_thinking_budget("", 4096))
        self.assertIsNone(_thinking_budget("ultra", 4096))
        self.assertEqual(_thinking_budget("low", 4096), 1024)

    def test_call_mcp_google_schema_has_no_empty_nested_object_and_coerces_json_string(self) -> None:
        self.assertEqual(schema_problems(CALL_MCP, "google"), [])
        g = CALL_MCP.to_google()
        args_prop = g["parameters"]["properties"]["arguments"]
        self.assertEqual(args_prop["type"], "STRING")
        self.assertIn("JSON", args_prop["description"])
        coerced = coerce_arguments(CALL_MCP, {"server": "gh", "tool": "get_issue", "arguments": '{"id": 7}'})
        self.assertEqual(coerced["arguments"], {"id": 7})

    async def test_anthropic_fallback_skips_thinking_when_prior_tool_turns_lack_reasoning(self) -> None:
        bodies: list[dict[str, Any]] = []

        def answer(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Recovered."}], "usage": {}})

        real = httpx.AsyncClient

        def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(answer)
            return real(*args, **kwargs)

        class _PrimaryFailsSecondCall(BaseProvider):
            supports_tools = True

            def __init__(self) -> None:
                self.n = 0

            async def complete(self, *args: Any, **kwargs: Any) -> str:
                return ""

            async def complete_with_tools(self, *args: Any, **kwargs: Any) -> ToolReply:
                self.n += 1
                if self.n == 1:
                    return ToolReply(tool_calls=[ToolCall("c1", "read_file", {"path": "a.py"})])
                raise ProviderError("provider_http", "rate limited", status=429)

        read_spec = ToolSpec(
            name="read_file",
            description="Read a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        )

        async def do_read(_args: dict[str, Any]) -> str:
            return "x = 1"

        fallback = AnthropicProvider("sk-ant-test", "https://api.anthropic.com")
        conductor = Conductor(
            _PrimaryFailsSecondCall(), "gpt-4.1", "/tmp",
            system_prompt="sys", tools=[read_spec], dispatch={"read_file": do_read},
            fallback=(fallback, "claude-sonnet-4-5"), thinking_effort="medium",
        )
        with mock.patch.object(httpx, "AsyncClient", client):
            reply = await conductor.run("read a.py")
        self.assertEqual(reply, "Recovered.")
        self.assertNotIn("thinking", bodies[0], "Anthropic rejects thinking when prior tool_use lacks thinking blocks")

    async def test_google_fallback_skips_thinking_when_prior_tool_turns_lack_signature(self) -> None:
        bodies: list[dict[str, Any]] = []

        def answer(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"role": "model", "parts": [{"text": "Recovered."}]}}]},
            )

        real = httpx.AsyncClient

        def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(answer)
            return real(*args, **kwargs)

        class _PrimaryFailsSecondCall(BaseProvider):
            supports_tools = True

            def __init__(self) -> None:
                self.n = 0

            async def complete(self, *args: Any, **kwargs: Any) -> str:
                return ""

            async def complete_with_tools(self, *args: Any, **kwargs: Any) -> ToolReply:
                self.n += 1
                if self.n == 1:
                    return ToolReply(tool_calls=[ToolCall("c1", "read_file", {"path": "a.py"})])
                raise ProviderError("provider_http", "rate limited", status=429)

        read_spec = ToolSpec(
            name="read_file",
            description="Read a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        )

        async def do_read(_args: dict[str, Any]) -> str:
            return "x = 1"

        fallback = GoogleProvider("g-test", "https://generativelanguage.googleapis.com/v1beta")
        conductor = Conductor(
            _PrimaryFailsSecondCall(), "gpt-4.1", "/tmp",
            system_prompt="sys", tools=[read_spec], dispatch={"read_file": do_read},
            fallback=(fallback, "gemini-3-flash"), thinking_effort="medium",
        )
        with mock.patch.object(httpx, "AsyncClient", client):
            reply = await conductor.run("read a.py")
        self.assertEqual(reply, "Recovered.")
        self.assertNotIn(
            "thinkingConfig",
            bodies[0]["generationConfig"],
            "Google fallback must omit thinkingConfig when prior tool_calls have no thoughtSignature",
        )

    async def test_turn_reply_and_summarise_turns_use_conductor_provider_and_model(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = connect(Path(tmp.name) / "test.db")
        self.addCleanup(conn.close)
        kc = Keychain(Path(tmp.name) / "secrets.json")
        registry = AgentRegistryService(conn, ProviderFactory(kc), kc)
        settings = SettingsService(conn)
        workspaces = WorkspaceService(conn)
        goals = GoalService(conn)
        conversations = ConversationService(conn)
        compactions = CompactionService(conn, goals)

        # Leave `scribe` unconfigured while configuring the conductor pair in engine settings.
        settings.set_str("conductor_provider", "openai")
        settings.set_str("conductor_model", "gpt-4.1")
        ws = workspaces.create(WorkspaceCreate(name="demo", root_path=""))
        conv = conversations.create(ConversationCreate(workspace_id=ws.id, title="test"))
        goal = goals.create(
            GoalCreate(workspace_id=ws.id, title="say hi", description="say hi", conversation_id=conv.id)
        )

        seen_primaries: list[Any] = []

        async def fake_run_agent(
            role: str, goal_id: str, step_id: str | None, prompt: str, **kwargs: Any
        ) -> Any:
            seen_primaries.append(kwargs.get("primary"))
            return "Plain prose reply about the workspace."

        executor = ExecutorService(goals, workspaces, registry, SandboxService())
        executor.settings = settings
        executor.compactions = compactions
        with mock.patch.object(executor.orchestrator, "run_agent", side_effect=fake_run_agent):
            reply = await executor._turn_reply(goal.id, goal)
            self.assertIn("Plain prose reply", reply)
            self.assertEqual(seen_primaries[-1].provider, "openai")
            self.assertEqual(seen_primaries[-1].model_name, "gpt-4.1")

            from engine.compaction import CompactionPlan
            plan = CompactionPlan(
                older=[{"prompt": "hi", "reply": "hello", "created_at": 1.0}],
                recent=[],
                previous=None,
                keep_turns=2,
            )
            summary = await executor._summarise_turns(goal, plan)
            self.assertIn("Plain prose reply", summary)
            self.assertEqual(seen_primaries[-1].provider, "openai")
            self.assertEqual(seen_primaries[-1].model_name, "gpt-4.1")

    async def test_plan_steps_passes_allow_consult_false_after_budget_exhausted(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = connect(Path(tmp.name) / "test.db")
        self.addCleanup(conn.close)
        kc = Keychain(Path(tmp.name) / "secrets.json")
        registry = AgentRegistryService(conn, ProviderFactory(kc), kc)
        workspaces = WorkspaceService(conn)
        goals = GoalService(conn)
        repo = Path(tmp.name) / "repo"
        repo.mkdir()
        ws = workspaces.create(WorkspaceCreate(name="demo", root_path=str(repo)))
        goal = goals.create(GoalCreate(workspace_id=ws.id, title="add feature", description="add feature"))

        from engine.executor_support import AgentOutputInvalid

        calls = 0
        accept_errors: list[str | None] = []

        async def fake_run_agent(
            role: str, goal_id: str, step_id: str | None, prompt: str, **kwargs: Any
        ) -> Any:
            nonlocal calls
            calls += 1
            accept = kwargs.get("accept")
            if calls == 1:
                consult_reply = {"consult": {"reads": ["README.md"]}}
                try:
                    if accept:
                        accept(consult_reply)
                    accept_errors.append(None)
                except AgentOutputInvalid as exc:
                    accept_errors.append(str(exc))
                return consult_reply
            second_consult = {"consult": {"reads": ["OTHER.md"]}}
            try:
                if accept:
                    accept(second_consult)
                accept_errors.append(None)
            except AgentOutputInvalid as exc:
                accept_errors.append(str(exc))
            return {"steps": [{"title": "Do it", "description": "Implement", "suggested_paths": ["a.py"]}]}

        executor = ExecutorService(goals, workspaces, registry, SandboxService())
        with (
            mock.patch.object(executor.orchestrator, "run_agent", side_effect=fake_run_agent),
            mock.patch.object(executor, "_serve_library_requests", new=mock.AsyncMock(return_value=("ctx", set(), [], 0))),
        ):
            await executor._plan_steps(goal.id, goal, ws, {}, {}, fail_goal=False)
        self.assertIsNone(accept_errors[0], "first consult must be accepted")
        self.assertIsNotNone(accept_errors[1], "second consult after budget exhaustion must raise AgentOutputInvalid in accept")


if __name__ == "__main__":
    unittest.main()
