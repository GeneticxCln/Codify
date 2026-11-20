"""Unit tests for core engine audit fixes (E1-E8, E16-E23, E40-E43)."""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from engine import watchdog
from engine.db import connect, migrate_agent_roles
from engine.executor import ExecutorService
from engine.models import (
    AgentConfigUpdate,
    ConversationCreate,
    Event,
    GoalCreate,
    TurnCreate,
    WorkspaceCreate,
)
from engine.providers import (
    AnthropicProvider,
    BaseProvider,
    GoogleProvider,
    Keychain,
    OllamaProvider,
    OpenAICompatProvider,
    ProviderError,
    ProviderFactory,
    _http_client,
)
from engine.sandbox import SandboxService
from engine.services import (
    AgentRegistryService,
    ConversationService,
    GoalService,
    SettingsService,
    WorkspaceService,
)
from engine.trace import ReplayProvider, TraceService, prompt_digest


class _FakeProvider(BaseProvider):
    def __init__(self, replies: list[Any] | None = None) -> None:
        self.replies = list(replies or [])
        self.seen_prompts: list[str] = []

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        *,
        num_ctx: int | None = None,
        keep_alive: str | None = None,
        json_schema: dict[str, Any] | None = None,
        thinking_effort: str | None = None,
    ) -> str:
        self.seen_prompts.append(user_prompt)
        if not self.replies:
            return "{}"
        nxt = self.replies.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt if isinstance(nxt, str) else json.dumps(nxt)


class TestCoreAuditFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "repo").mkdir()
        self.conn = connect(self.root / "test.db")
        self.addCleanup(self.conn.close)
        self.keychain = Keychain(secrets_path=self.root / "secrets.json")
        self.registry = AgentRegistryService(self.conn, ProviderFactory(self.keychain), self.keychain)
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.conversations = ConversationService(self.conn)
        self.settings = SettingsService(self.conn)
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root / "repo")))

    # ── E1: Critic inspection command timeout ─────────────────────────────

    async def test_e1_critic_inspection_timeout_is_caught_and_reported(self) -> None:
        provider = _FakeProvider([
            {"decision": None, "reasons": [], "run_command": ["ls", "-la"]},
            {"decision": "approve", "reasons": []},
        ])
        for cfg in self.registry.list_configs():
            self.registry.set_config(cfg.role, AgentConfigUpdate(provider="ollama", model_name="m"))
        self.registry.build_provider = lambda cfg: provider  # type: ignore[method-assign]
        sandbox = SandboxService()
        executor = ExecutorService(self.goals, self.workspaces, self.registry, sandbox)
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="t", description="d"))
        executor._insert_steps(goal.id, [{"title": "S1", "description": "d", "suggested_paths": ["a.py"]}])
        step = self.goals.steps(goal.id)[0]

        with mock.patch.object(
            sandbox, "run_command", side_effect=subprocess.TimeoutExpired(cmd=["ls", "-la"], timeout=10),
        ):
            await executor._critic(
                goal.id, step, mock.MagicMock(), [], {}, {"verdict": "pass", "ran": False},
                ws_root=self.ws.root_path,
            )

        self.assertEqual(len(provider.seen_prompts), 2)
        self.assertIn("Critic inspection command timed out.", provider.seen_prompts[1])

    # ── E2 & E41: Null-safe provider parsers & Google thinkingBudget clamp ─

    async def test_e2_all_four_providers_handle_null_and_malformed_json_shapes(self) -> None:
        payloads: dict[str, Any] = {
            "anthropic": {"content": [None, {"type": "text", "text": None}, {"type": "text", "text": "ok"}], "usage": None},
            "openai_compat": {"choices": [{"message": None}], "usage": None},
            "ollama": {"response": None, "message": None, "done": True},
            "google": {"candidates": [{"content": {"parts": [None, {"text": None}, {"text": "g-ok"}]}}], "usageMetadata": None},
        }
        stream_lines = [
            'data: {"choices": [{"delta": null}]}',
            'data: {"choices": [{"delta": {"content": null}}]}',
            'data: {"choices": [{"delta": {"content": "stream-ok"}, "finish_reason": "stop"}], "usage": null}',
            "data: [DONE]",
        ]
        kind = "anthropic"
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"data": None})
            body = json.loads(request.content or b"{}")
            if body.get("stream") and kind == "openai_compat":
                return httpx.Response(200, content="\n\n".join(stream_lines).encode("utf-8"))
            if body.get("stream") and kind == "ollama":
                ndjson = '{"response": null, "done": false}\n{"response": "ol-stream", "done": true}\n'
                return httpx.Response(200, content=ndjson.encode("utf-8"))
            return httpx.Response(200, json=payloads[kind])

        def fake_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        with mock.patch.object(httpx, "AsyncClient", fake_client):
            kind = "anthropic"
            ap = AnthropicProvider("sk-ant-test", "https://api.anthropic.com")
            self.assertEqual(await ap.complete("s", "u", "m", 0.0, 32), "ok")
            self.assertEqual((await ap.complete_with_tools("s", [], [], "m", 0.0, 32)).text, "ok")

            kind = "openai_compat"
            op = OpenAICompatProvider("sk-oa-test", "https://api.openai.com/v1")
            self.assertEqual(await op.complete("s", "u", "m", 0.0, 32), "")
            self.assertEqual((await op.complete_with_tools("s", [], [], "m", 0.0, 32)).text, "")
            deltas: list[str] = []
            op.on_delta = deltas.append
            self.assertEqual(await op.complete("s", "u", "m", 0.0, 32), "stream-ok")

            kind = "ollama"
            ol = OllamaProvider("http://127.0.0.1:11434")
            self.assertEqual(await ol.complete("s", "u", "m", 0.0, 32), "")
            self.assertEqual((await ol.complete_with_tools("s", [], [], "m", 0.0, 32)).text, "")
            ol.on_delta = deltas.append
            self.assertEqual(await ol.complete("s", "u", "m", 0.0, 32), "ol-stream")

            kind = "google"
            gp = GoogleProvider("g-key-test", "https://generativelanguage.googleapis.com/v1beta")
            self.assertEqual(await gp.complete("s", "u", "m", 0.0, 32), "g-ok")
            self.assertEqual((await gp.complete_with_tools("s", [], [], "m", 0.0, 32)).text, "g-ok")

    async def test_e41_google_provider_complete_clamps_thinking_budget(self) -> None:
        sent_bodies: list[dict[str, Any]] = []
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            sent_bodies.append(json.loads(request.content or b"{}"))
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

        def fake_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        with mock.patch.object(httpx, "AsyncClient", fake_client):
            gp = GoogleProvider("g-key-test", "https://generativelanguage.googleapis.com/v1beta")
            await gp.complete("s", "u", "gemini-2.5-pro", 0.0, 1500, thinking_effort="high")

        budget = sent_bodies[-1]["generationConfig"]["thinkingConfig"]["thinkingBudget"]
        self.assertEqual(budget, 750)

    # ── E3 & E8: GoalService.turn_history guards corrupt JSON & batches ───

    def test_e3_and_e8_turn_history_ignores_corrupt_log_rows_and_batches_queries(self) -> None:
        convo = self.conversations.create(ConversationCreate(workspace_id=self.ws.id, title="Chat"))
        t1 = self.goals.create_turn(convo.id, TurnCreate(prompt="first"))
        t2 = self.goals.create_turn(convo.id, TurnCreate(prompt="second"))

        # Insert corrupt JSON, non-dict JSON, and valid turn log rows.
        self.conn.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, NULL, 'log', ?, ?, ?)",
            ("bad-json", t1.id, "{not-valid-json", time.time(), 1),
        )
        self.conn.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, NULL, 'log', ?, ?, ?)",
            ("list-json", t1.id, "[1, 2, 3]", time.time(), 2),
        )
        self.conn.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, NULL, 'log', ?, ?, ?)",
            ("good-json", t1.id, json.dumps({"turn": True, "message": "first reply"}), time.time(), 3),
        )
        self.conn.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, NULL, 'log', ?, ?, ?)",
            ("good-json-2", t2.id, json.dumps({"turn": True, "message": "second reply"}), time.time(), 1),
        )
        self.conn.commit()

        queries: list[str] = []
        self.conn.set_trace_callback(queries.append)
        try:
            history = self.goals.turn_history(convo.id, limit=10)
        finally:
            self.conn.set_trace_callback(None)

        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["reply"], "first reply")
        self.assertEqual(history[1]["reply"], "second reply")
        event_selects = [q for q in queries if "FROM events" in q]
        self.assertEqual(len(event_selects), 1, f"expected 1 batched event query, got {event_selects}")

    # ── E4: Shared _json_capabilities cache & pooled httpx.AsyncClient ────

    async def test_e4_json_capabilities_and_async_client_persist_across_provider_instances(self) -> None:
        models_hits = 0

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                nonlocal models_hits
                models_hits += 1
                body = json.dumps({"data": [{"id": "m1", "capabilities": {"json_object": True}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                _ = self.rfile.read(length)
                body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base_url = f"http://127.0.0.1:{server.server_port}/v1"

        p1 = OpenAICompatProvider("k", base_url)
        p2 = OpenAICompatProvider("k", base_url)
        self.assertEqual(await p1.complete("s", "u", "m1", 0.0, 16), "ok")
        self.assertEqual(await p2.complete("s", "u", "m1", 0.0, 16), "ok")
        self.assertEqual(models_hits, 1, "second provider instance must reuse cached /models capabilities")

        async with _http_client(base_url, timeout=120) as c1:
            async with _http_client(base_url, timeout=120) as c2:
                self.assertIs(c1, c2, "pooled AsyncClient must be reused on the same event loop")

    # ── E6 & E42: _PerThreadConnection cleanup & migrate_agent_roles ──────

    def test_e6_per_thread_connection_releases_handle_when_worker_thread_exits(self) -> None:
        def worker() -> None:
            self.conn.execute("SELECT 1").fetchall()

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        # Trigger another worker or release check and verify dead thread handles are pruned.
        t2 = threading.Thread(target=worker)
        t2.start()
        t2.join()
        opened = getattr(self.conn, "_opened", {})
        alive_opened = [th for th, _c in opened.values() if not th.is_alive()]
        self.assertEqual(alive_opened, [])

    def test_e42_migrate_agent_roles_preserves_thinking_effort_num_ctx_keep_alive(self) -> None:
        self.conn.execute("DELETE FROM agent_configs WHERE role = 'fixer'")
        self.conn.execute(
            """INSERT INTO agent_configs
               (role, display_name, provider, protocol, model_name, temperature, max_tokens,
                thinking_effort, num_ctx, keep_alive, fallback_model_name, updated_at)
               VALUES ('coder', 'Coder', 'ollama', 'ollama', 'qwen2.5-coder:7b', 0.1, 4096,
                       'high', 32768, '30m', '', 100.0)"""
        )
        self.conn.commit()
        seed_sql = (
            "INSERT INTO agent_configs (role, display_name, provider, protocol, model_name, "
            "temperature, max_tokens, fallback_model_name, updated_at) "
            "VALUES ('fixer', 'Fixer', 'ollama', 'ollama', '', 0.2, 4096, '', 0.0)"
        )
        self.conn.execute(seed_sql)
        self.conn.commit()

        moved = migrate_agent_roles(self.conn)
        self.assertIn(("coder", "fixer"), moved)
        cfg = self.registry.get_config("fixer")
        self.assertEqual(cfg.model_name, "qwen2.5-coder:7b")
        self.assertEqual(cfg.thinking_effort, "high")
        self.assertEqual(cfg.num_ctx, 32768)
        self.assertEqual(cfg.keep_alive, "30m")

    # ── E16: events_after advances past corrupt rows ──────────────────────

    def test_e16_events_after_advances_cursor_past_corrupt_rows(self) -> None:
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="t", description="d"))
        self.goals.publish(Event(
            id="ev1", goal_id=goal.id, step_id=None, type="log",
            payload={"level": "info", "message": "ok"}, timestamp=1.0, sequence=1,
        ))
        self.conn.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, NULL, 'log', ?, ?, ?)",
            ("ev2-bad", goal.id, "{corrupt", 2.0, 2),
        )
        self.conn.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, NULL, 'unknown_type', ?, ?, ?)",
            ("ev3-bad", goal.id, "{}", 3.0, 3),
        )
        self.conn.commit()

        batch = self.goals.events_after(goal.id, 1, limit=500)
        self.assertEqual(batch, [])
        self.assertEqual(getattr(batch, "last_sequence", 0), 3)

    # ── E18 & E40: Watchdog grace window & broken pipe guard ──────────────

    def test_e18_and_e40_watchdog_grace_and_broken_stderr_on_arm(self) -> None:
        self.assertGreaterEqual(watchdog.HARD_EXIT_GRACE_S, 10)
        self.assertGreater(watchdog.HARD_EXIT_GRACE_S, watchdog.GRACEFUL_SHUTDOWN_S + 4)

        class BrokenStderr:
            def write(self, _s: str) -> int:
                raise BrokenPipeError("pipe closed")

            def flush(self) -> None:
                raise BrokenPipeError("pipe closed")

        stopped = threading.Event()
        with mock.patch.object(sys, "stderr", BrokenStderr()):
            thread = watchdog.start_parent_watchdog(
                999999,
                interval_s=0.01,
                on_parent_death=lambda _pid: stopped.set(),
                is_gone=lambda _pid: True,
            )
        self.assertIsNotNone(thread)
        self.assertTrue(stopped.wait(timeout=2.0))

    # ── E19: AgentOrchestrator clears provider.usage_sink in finally ──────

    async def test_e19_run_agent_clears_usage_sink_on_completion_and_failure(self) -> None:
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="t", description="d"))
        for cfg in self.registry.list_configs():
            self.registry.set_config(cfg.role, AgentConfigUpdate(provider="ollama", model_name="m"))

        provider = _FakeProvider([{"summary": "s", "commit_message": "feat: x"}])
        self.registry.build_provider = lambda cfg: provider  # type: ignore[method-assign]
        executor = ExecutorService(self.goals, self.workspaces, self.registry, SandboxService())

        await executor.orchestrator.run_agent("scribe", goal.id, None, "hello")
        self.assertIsNone(provider.usage_sink)

        failing_provider = _FakeProvider([ProviderError("provider_unreachable", "down")])
        self.registry.build_provider = lambda cfg: failing_provider  # type: ignore[method-assign]
        with self.assertRaises(ProviderError):
            await executor.orchestrator.run_agent("scribe", goal.id, None, "hello")
        self.assertIsNone(failing_provider.usage_sink)

    # ── E20: ReplayProvider serves identical retry calls by index ─────────

    async def test_e20_replay_provider_serves_duplicate_role_prompt_hashes_in_order(self) -> None:
        digest = prompt_digest("sys", "user")
        replay = ReplayProvider([
            {"seq": 1, "role": "fixer", "prompt_hash": digest, "response": "first-attempt"},
            {"seq": 2, "role": "fixer", "prompt_hash": digest, "response": "second-attempt"},
        ])
        replay.current_role = "fixer"
        first = await replay.complete("sys", "user", "m", 0.0, 32)
        second = await replay.complete("sys", "user", "m", 0.0, 32)
        self.assertEqual(first, "first-attempt")
        self.assertEqual(second, "second-attempt")

        traces = TraceService(self.conn)
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="t", description="d", trace=True))
        traces.record(
            goal_id=goal.id, step_id=None, role="fixer", provider="ollama", model="m",
            temperature=0.0, max_tokens=32,
            system_prompt="sys", user_prompt="user", response="resp",
            usage={"input_tokens": 10, "output_tokens": 5}, duration_ms=12,
        )
        summary = traces.summary(goal.id)
        self.assertEqual(summary["calls"], 1)

    # ── E21: record_observations handles concurrent/IntegrityError races ──

    def test_e21_record_observations_is_race_safe(self) -> None:
        obs_batch = [
            {
                "subject": "code:tests_failed",
                "lesson": "Tests failed before.",
                "example": "boom",
                "proof": 1,
                "evidence": ["ev-1"],
            }
        ]

        def worker(ev_id: str) -> None:
            self.goals.record_observations(
                self.ws.id,
                [{**obs_batch[0], "evidence": [ev_id]}],
            )

        threads = [threading.Thread(target=worker, args=(f"ev-{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = self.goals.observation_rows(self.ws.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "code:tests_failed")
        self.assertEqual(rows[0]["proof"], 6)

    # ── E23: _conduct cleans up _editor_edits on CancelledError ───────────

    async def test_e23_conduct_cleans_up_editor_edits_on_cancellation(self) -> None:
        for cfg in self.registry.list_configs():
            self.registry.set_config(cfg.role, AgentConfigUpdate(provider="ollama", model_name="m"))
        executor = ExecutorService(self.goals, self.workspaces, self.registry, SandboxService())
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="t", description="d"))

        async def cancel_during_run(*_args: Any, **_kwargs: Any) -> str:
            executor._editor_edits[goal.id] = 2
            raise asyncio.CancelledError()

        with (
            mock.patch("engine.executor_conduct.Conductor.run", side_effect=cancel_during_run),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await executor._conduct(goal.id, goal, self.ws.root_path)

        self.assertNotIn(goal.id, executor._editor_edits)


if __name__ == "__main__":
    unittest.main()
