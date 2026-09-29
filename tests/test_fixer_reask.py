"""A fixer reply the engine cannot apply is asked for again — once — with the reason (audit of 2026-09-29, 3.3).

Found by running Qwen2.5-1.5B against the benchmark's `repo-rename-greeting`: the model answered
`{"action": "edit", "edits": [{"old_text": "greet", "new_text": "welcome", "count": 1}]}` for a file
in which `greet` appears three times. The engine's answer — `old_text appears 2 time(s), expected 1` — is
precise and the model can act on it, but it went to the goal as a failure: the same-model re-ask (H4) only
covered replies that were not JSON, so the one case where the engine knows exactly what to say was the one
case where it said it to a person instead of to the model.

Rules pinned here: a reply that parsed but could not be *applied* (an edit that matches the wrong number of
times or not at all, an entry the contract refuses) gets one re-ask of the same fixer, carrying the original
task, the reason, and the reply it refers to; nothing is written by the reply that failed; a reply that
applied is never re-asked; and after the second failure the step fails exactly as it did before.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import GateCall, LayaDecision, LayaService
from engine.models import ROLES, AgentConfig, AgentConfigUpdate, GoalCreate, WorkspaceCreate
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService

SOURCE = 'def greet(name):\n    return f"hello {name}"\n\n\ndef shout(name):\n    return greet(name).upper()\n'
AMBIGUOUS = {"files": [{"path": "a.py", "action": "edit",
                        "edits": [{"old_text": "greet", "new_text": "welcome", "count": 1}]}]}
CORRECTED = {"files": [{"path": "a.py", "action": "edit",
                        "edits": [{"old_text": "greet", "new_text": "welcome", "count": 0}]}]}


class ScriptedProvider(BaseProvider):
    """Answers each role from a list (the last entry repeats), and remembers every fixer prompt."""

    def __init__(self, fixer_replies: list[Any]) -> None:
        self.fixer_replies = list(fixer_replies)
        self.fixer_prompts: list[str] = []
        self.roles = {
            "librarian": {"summary": "s", "files": [], "conventions": [], "enough": True},
            "design": {"applies": False},
            "planner": {"steps": [{"title": "Rename", "description": "rename greet", "suggested_paths": ["a.py"]}]},
            "verifier": {"argv": None, "verdict": "pass", "explanation": "nothing to run"},
            "critic": {"decision": "approve", "reasons": []},
            "scribe": {"summary": "did it", "commit_message": "feat: rename"},
        }

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        role = next((r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown")
        if role == "fixer":
            self.fixer_prompts.append(user_prompt)
            step = self.fixer_replies.pop(0) if len(self.fixer_replies) > 1 else self.fixer_replies[0]
            return step if isinstance(step, str) else json.dumps(step)
        return json.dumps(self.roles.get(role, {}))


class Factory(ProviderFactory):
    def __init__(self, provider: BaseProvider) -> None:
        super().__init__(Keychain())
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        return self.provider


class Gate(LayaService):
    async def decide(self, state: dict[str, Any], on_call: GateCall | None = None) -> LayaDecision:
        return LayaDecision(engine="skipped", skipped_reason="test double")


class ReAskCase(unittest.IsolatedAsyncioTestCase):
    def build(self, *fixer_replies: Any) -> ScriptedProvider:
        self.provider = ScriptedProvider(list(fixer_replies))
        registry = AgentRegistryService(self.conn, Factory(self.provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="test-model"))
        self.executor = ExecutorService(self.goals, self.workspaces, registry, SandboxService(), laya=Gate())
        return self.provider

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "a.py").write_text(SOURCE, encoding="utf-8")
        self.conn = connect(self.root / "t.db")
        self.addCleanup(self.conn.close)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def run_the_step(self) -> str:
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        self.executor._insert_steps(
            self.goal.id, [{"title": "Rename", "description": "rename greet", "suggested_paths": ["a.py"]}]
        )
        step = self.goals.steps(self.goal.id)[0]
        await self.executor.run_step(self.goal.id, step.id)
        return self.goals.steps(self.goal.id)[0].status

    def events(self, type_: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == type_]

    @property
    def file(self) -> str:
        return (self.root / "a.py").read_text(encoding="utf-8")


class TestAnEditThatCouldNotBeApplied(ReAskCase):
    async def test_the_reason_goes_back_to_the_model_and_the_corrected_reply_is_used(self) -> None:
        provider = self.build(AMBIGUOUS, CORRECTED)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts), "the same fixer should have been asked exactly twice")
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual(SOURCE.replace("greet", "welcome"), self.file)

    async def test_the_second_prompt_carries_the_task_the_reason_and_the_reply_it_refers_to(self) -> None:
        provider = self.build(AMBIGUOUS, CORRECTED)

        await self.run_the_step()

        first, second = provider.fixer_prompts
        self.assertTrue(second.startswith(first), "the repair must carry the original task unchanged")
        self.assertIn("appears 2 time(s), expected 1", second, "the engine's own reason was not passed on")
        self.assertIn('"old_text": "greet"', second, "the model was not shown the reply the reason is about")

    async def test_nothing_is_written_by_the_reply_that_failed(self) -> None:
        # The second reply also fails, so whatever is on disk afterwards came from a failed reply.
        self.build(AMBIGUOUS)

        await self.run_the_step()

        self.assertEqual(SOURCE, self.file)

    async def test_the_first_failure_is_recorded_as_a_failed_call_that_was_retried(self) -> None:
        self.build(AMBIGUOUS, CORRECTED)

        await self.run_the_step()

        failed = [e for e in self.events("agent_call_failed") if e["role"] == "fixer"]
        self.assertEqual(1, len(failed))
        self.assertEqual("agent_output_invalid", failed[0]["code"])
        self.assertTrue(failed[0]["retrying"])
        self.assertIn("appears 2 time(s)", failed[0]["message"])

    async def test_a_reply_that_applied_is_never_asked_for_twice(self) -> None:
        provider = self.build(CORRECTED)

        await self.run_the_step()

        self.assertEqual(1, len(provider.fixer_prompts))
        self.assertEqual([], [e for e in self.events("agent_call_failed") if e["role"] == "fixer"])

    async def test_after_the_second_failure_the_step_fails_as_it_always_did(self) -> None:
        provider = self.build(AMBIGUOUS)

        status = await self.run_the_step()

        self.assertEqual("FAILED", status)
        self.assertEqual(2, len(provider.fixer_prompts), "asked more than the one repair")
        errors = self.events("error")
        self.assertEqual("agent_output_invalid", errors[-1]["code"])
        self.assertEqual("fixer", errors[-1]["role"])
        self.assertIn("appears 2 time(s), expected 1", errors[-1]["message"])
        self.assertIn("after one repair attempt", errors[-1]["message"])


class TestAnEntryTheContractRefuses(ReAskCase):
    async def test_a_file_entry_with_an_unknown_action_is_asked_for_again(self) -> None:
        bad = {"files": [{"path": "a.py", "action": "rewrite", "content": "x = 1\n"}]}
        good = {"files": [{"path": "a.py", "action": "update", "content": "x = 1\n"}]}
        provider = self.build(bad, good)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual("x = 1\n", self.file)
        self.assertIn("fixer file entry invalid", provider.fixer_prompts[1])


if __name__ == "__main__":
    unittest.main()
