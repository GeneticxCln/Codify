"""Regression tests for AI agent runtime audit — Batch 4 (M4–M7, L1–L3)."""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from engine.agent_orchestrator import AgentOrchestrator
from engine.app import SQLITE_INT_MAX, ws_goal
from engine.db import connect
from engine.executor import ExecutorService
from engine.fs import FileSystemService
from engine.laya import _extract_json as laya_extract_json
from engine.models import AgentConfig, GoalCreate, PlanStep, WorkspaceCreate
from engine.providers import BaseProvider, Keychain, OpenAICompatProvider, ProviderError, ProviderFactory
from engine.replay import _ReplayFactory, _gate_verdict
from engine.replies import extract_json
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService
from engine.trace import ReplayProvider, prompt_digest


class _StaticProvider(BaseProvider):
    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int, **kwargs: Any,
    ) -> str:
        return self._reply


class TestBatch4RuntimeAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ws_dir = self.root / "ws"
        self.ws_dir.mkdir()
        self.conn = connect(self.root / "codify.db")
        self.addCleanup(self.conn.close)
        self.keychain = Keychain(secrets_path=self.root / "secrets.json")
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.sandbox = SandboxService()

    # ── M4: extract_json handles comments containing apostrophes/braces ──────

    def test_m4_extract_json_comments_with_apostrophe_and_brace(self) -> None:
        raw_line_comment = '{\n  // don\'t remove this entry\n  "files": [{"path": "a.py", "action": "create", "content": "x = 1\\n"}]\n}'
        parsed = extract_json(raw_line_comment, ("files",), repair_truncation=False)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["files"][0]["path"], "a.py")

        raw_block_comment = '{\n  /* unmatched { and \'quote */\n  "files": []\n}'
        parsed2 = extract_json(raw_block_comment, ("files",), repair_truncation=False)
        self.assertEqual(parsed2, {"files": []})

    # ── M5: Laya _extract_json strips reasoning tags before finding braces ───

    def test_m5_laya_extract_json_strips_think_blocks_with_braces(self) -> None:
        raw = (
            '<think>Let me weigh {"intent": "ops_command"} vs question.</think>\n'
            '{"intent": "question", "risk": 0.1, "needs_clarification": 0.0}'
        )
        parsed = laya_extract_json(raw)
        self.assertEqual(parsed.get("intent"), "question")
        self.assertEqual(parsed.get("risk"), 0.1)

    # ── M6: run_agent derives expect from custom json_schema, not REPLY_KEYS ─

    async def test_m6_run_agent_custom_schema_overrides_role_expect_keys(self) -> None:
        # The real lens JSON appears first, followed by a prose example quoting critic's default keys.
        # Under the old bug (`expect = REPLY_KEYS["critic"]`), extract_json chose the critic-shaped dict!
        reply = (
            '{"findings": [{"severity": "high", "title": "bug", "detail": "x"}]}\n'
            'Note: unlike a standard critic reply such as {"decision": "approve", "reasons": []}, '
            'this lens only returns findings.'
        )
        factory = ProviderFactory(self.keychain)
        registry = AgentRegistryService(self.conn, factory, self.keychain)
        self.conn.execute("UPDATE agent_configs SET model_name = 'qwen3' WHERE role = 'critic'")
        self.conn.commit()
        ws = self.workspaces.create(WorkspaceCreate(name="ws", root_path=str(self.ws_dir)))
        goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="lens", description=""))
        orch = AgentOrchestrator(registry, self.goals)
        lens_schema = {
            "type": "object",
            "properties": {"findings": {"type": "array"}},
            "required": ["findings"],
        }
        with mock.patch.object(registry, "build_provider", return_value=_StaticProvider(reply)):
            out = await orch.run_agent(
                "critic", goal.id, None, "inspect diff",
                system="You are a deep-review lens.", json_schema=lens_schema,
            )
        self.assertIn("findings", out)
        self.assertNotIn("decision", out)

    # ── M7: _critic accumulates all inspection rounds instead of overwriting ─

    async def test_m7_critic_accumulates_inspection_rounds(self) -> None:
        registry = AgentRegistryService(self.conn, ProviderFactory(self.keychain), self.keychain)
        executor = ExecutorService(self.goals, self.workspaces, registry, self.sandbox)
        ws = self.workspaces.create(WorkspaceCreate(name="ws", root_path=str(self.ws_dir)))
        goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="critic", description=""))
        step = PlanStep(
            id="s1", goal_id=goal.id, ordinal=0, title="step", description="desc",
            suggested_paths=[], status="IN_PROGRESS",
        )
        self.conn.execute(
            "INSERT INTO plan_steps (id, goal_id, ordinal, title, description, suggested_paths, status) "
            "VALUES ('s1', ?, 0, 'step', 'desc', '[]', 'IN_PROGRESS')",
            (goal.id,),
        )
        self.conn.commit()

        prompts_seen: list[str] = []
        replies = iter([
            {"decision": None, "reasons": [], "run_command": ["ls"]},
            {"decision": None, "reasons": [], "run_command": ["wc", "-l", "a.py"]},
            {"decision": "approve", "reasons": []},
        ])

        async def fake_run_agent(role: str, gid: str, sid: str | None, prompt: str, **kwargs: Any) -> Any:
            prompts_seen.append(prompt)
            return next(replies)

        outputs = iter([
            {"exit_code": 0, "stdout": "ROUND1_MARKER", "stderr": ""},
            {"exit_code": 0, "stdout": "ROUND2_MARKER", "stderr": ""},
        ])

        def fake_run_cmd(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return next(outputs)

        with (
            mock.patch.object(executor.orchestrator, "run_agent", side_effect=fake_run_agent),
            mock.patch.object(executor.sandbox, "run_command", side_effect=fake_run_cmd),
        ):
            await executor._critic(
                goal.id, step, FileSystemService(str(self.ws_dir)),
                diffs=[], ws_root=str(self.ws_dir),
            )

        self.assertEqual(len(prompts_seen), 3)
        self.assertIn("ROUND1_MARKER", prompts_seen[2])
        self.assertIn("ROUND2_MARKER", prompts_seen[2])

    # ── L1: Replay _gate_verdict preserves answers/intent & resets _conductor_seen

    async def test_l1_gate_verdict_restores_fields_and_replay_resets_conductor_seen(self) -> None:
        ev = types.SimpleNamespace(
            type="laya_decision",
            payload={
                "engine": "llm-fallback",
                "answers": {"intent": {"choice": "code_change"}},
                "routing": {"skip_design": True},
                "blocked": False,
                "block_reason": None,
                "warnings": ["careful"],
                "skipped_reason": None,
                "unavailable": False,
                "provider": "ollama",
                "model": "qwen3",
            },
        )
        decision = _gate_verdict([ev])
        self.assertEqual(decision.intent, "code_change")
        self.assertEqual(decision.routing, {"skip_design": True})
        self.assertEqual(decision.warnings, ["careful"])
        self.assertEqual(decision.provider, "ollama")
        self.assertEqual(decision.model, "qwen3")

        # Two separate conductor loops in one replay: second loop starts with 1 message.
        sys_prompt = "sys"
        m1 = [{"role": "user", "content": "loop1"}]
        m2 = [{"role": "user", "content": "loop2"}]
        reply_json = json.dumps({"text": "done", "tool_calls": [], "raw_tool_calls": None})
        calls = [
            {
                "role": "conductor",
                "prompt_hash": prompt_digest(sys_prompt, json.dumps(m1)),
                "response": reply_json,
            },
            {
                "role": "conductor",
                "prompt_hash": prompt_digest(sys_prompt, json.dumps(m2)),
                "response": reply_json,
            },
        ]
        rp = ReplayProvider(calls)
        r1 = await rp.complete_with_tools(sys_prompt, m1, [], "m", 0.0, 100)
        self.assertEqual(r1.text, "done")
        self.assertEqual(rp._conductor_seen, 2)
        # Second loop passes a shorter messages list (len=1 < _conductor_seen=2).
        r2 = await rp.complete_with_tools(sys_prompt, m2, [], "m", 0.0, 100)
        self.assertEqual(r2.text, "done")

    # ── L2: ws_goal clamps after to SQLITE_INT_MAX and swallows RuntimeError ─

    async def test_l2_ws_goal_clamps_after_and_handles_send_runtime_error(self) -> None:
        ws = self.workspaces.create(WorkspaceCreate(name="ws", root_path=str(self.ws_dir)))
        goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="ws", description=""))
        seen_after: list[int] = []
        orig_events_after = self.goals.events_after

        def spy_events_after(gid: str, after: int, limit: int = 500) -> Any:
            seen_after.append(after)
            return orig_events_after(gid, after, limit=limit)

        class _FakeWS:
            def __init__(self, goals: GoalService) -> None:
                self.app = types.SimpleNamespace(state=types.SimpleNamespace(goals=goals))

            async def receive_text(self) -> str:
                raise RuntimeError("socket closed")

            async def send_text(self, text: str) -> None:
                raise RuntimeError("broken pipe")

            async def close(self, code: int = 1000) -> None:
                pass

        with (
            mock.patch("engine.app._authenticate_ws", return_value=True),
            mock.patch.object(self.goals, "events_after", side_effect=spy_events_after),
        ):
            await ws_goal(_FakeWS(self.goals), goal.id, after=SQLITE_INT_MAX + 999999)  # type: ignore[arg-type]

        self.assertEqual(seen_after, [SQLITE_INT_MAX])

    # ── L3: OpenAICompatProvider streaming fallback & _ReplayFactory isolation

    async def test_l3_openai_compat_streaming_fallback_preserves_on_delta_and_replay_factory_isolates(self) -> None:
        provider = OpenAICompatProvider(base_url="http://127.0.0.1:1/v1", api_key="k")
        cb_marker = lambda _delta: None  # noqa: E731
        provider.on_delta = cb_marker

        async def reject_stream(*_a: Any, **_k: Any) -> str:
            raise ProviderError("provider_http", "streaming unsupported", status=400)

        observed_on_delta: list[Any] = []

        async def fake_post_json(*_a: Any, **_k: Any) -> dict[str, Any]:
            observed_on_delta.append(provider.on_delta)
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

        with (
            mock.patch.object(provider, "_complete_streaming", side_effect=reject_stream),
            mock.patch("engine.providers.post_json", side_effect=fake_post_json),
        ):
            out = await provider.complete("sys", "usr", "m", 0.0, 100)

        self.assertEqual(out, '{"ok": true}')
        self.assertIs(observed_on_delta[0], cb_marker)
        self.assertIs(provider.on_delta, cb_marker)

        # _ReplayFactory hands each role an isolated wrapper instance.
        rp = ReplayProvider([
            {"role": "fixer", "prompt_hash": prompt_digest("s", "u1"), "response": "r1"},
            {"role": "critic", "prompt_hash": prompt_digest("s", "u2"), "response": "r2"},
        ])
        rf = _ReplayFactory(rp, self.keychain)
        now = time.time()
        p_fixer = rf.build(AgentConfig(
            role="fixer", display_name="Fixer", provider="ollama", protocol="ollama", model_name="m", updated_at=now,
        ))
        p_critic = rf.build(AgentConfig(
            role="critic", display_name="Critic", provider="ollama", protocol="ollama", model_name="m", updated_at=now,
        ))
        self.assertIsNot(p_fixer, p_critic)
        # Building critic must NOT clobber fixer's bound role!
        self.assertEqual(await p_fixer.complete("s", "u1", "m", 0.0, 100), "r1")
        self.assertEqual(await p_critic.complete("s", "u2", "m", 0.0, 100), "r2")


if __name__ == "__main__":
    unittest.main()
