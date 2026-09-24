from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
import threading
import unittest
from pathlib import Path
from typing import Any

from engine.db import connect
from engine.git import GitService
from engine.executor import MAX_LIBRARY_ROUNDS, MAX_REFUSED_TEST_COMMANDS, ExecutorService
from engine.laya import LayaDecision, LayaService
from engine.library import LibraryService
from engine.models import (
    ROLES,
    AgentConfig,
    AgentConfigUpdate,
    Event,
    Goal,
    GoalCreate,
    PlanStep,
    WorkspaceCreate,
)
from engine.providers import BaseProvider, Keychain, ProviderError, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService


class MockProvider(BaseProvider):
    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        # Which role's config the factory last built a provider for. Reply routing
        # uses this, not a scan of the system prompt: the prompts mention each
        # other (the planner is handed "the librarian's evidence pack", the scribe
        # is told never to claim a test "the verifier did not run"), so a keyword
        # scan hands back another role's script.
        self.current_role: str | None = None

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        self.calls.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "model": model,
            "role": self.current_role,
        })
        resp = self.responses.get(self.current_role) if self.current_role else None
        if resp is None:
            # Fall back to the old keyword scan so a suite that keys replies by
            # wording still works.
            for role, candidate in self.responses.items():
                if role in system_prompt.lower():
                    resp = candidate
                    break
        if resp is None:
            return json.dumps({"status": "ok"})
        return json.dumps(resp(user_prompt) if callable(resp) else resp)


class MockFactory(ProviderFactory):
    def __init__(self, mock_provider: MockProvider):
        self.mock_provider = mock_provider

    def build(self, config: AgentConfig) -> MockProvider:
        self.mock_provider.current_role = config.role
        return self.mock_provider


def configure_every_role(registry: AgentRegistryService, model: str = "test-model") -> None:
    """Give every role a model id.

    Seeded roles deliberately carry none (there is no hardcoded model list), and
    the executor refuses to call a role with an empty model — so a test that
    wants a pipeline to run has to say which model it runs with.
    """
    for role in ROLES:
        registry.set_config(role, AgentConfigUpdate(model_name=model))


class TestPerRoleConfig(unittest.IsolatedAsyncioTestCase):
    """The executor must honor each role's own AgentConfig (model, temperature,
    max_tokens, system prompt override) — command-bar style overrides must not
    flatten the five roles into one shared model."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.mock_provider = MockProvider({})
        self.registry = AgentRegistryService(self.conn, MockFactory(self.mock_provider), Keychain())
        configure_every_role(self.registry)
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.executor = ExecutorService(self.goals, self.workspaces, self.registry, SandboxService())
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_each_role_uses_its_own_config(self) -> None:
        # Configure planner and fixer with distinct models + prompt overrides.
        self.registry.set_config("planner", AgentConfigUpdate(
            model_name="planner-model", temperature=0.7, max_tokens=1234,
            system_prompt_override="PLANNER OVERRIDE",
        ))
        self.registry.set_config("fixer", AgentConfigUpdate(
            model_name="fixer-model", temperature=0.0, max_tokens=222,
        ))

        self.mock_responses = self.mock_provider.responses
        self.mock_responses["planner"] = {"steps": [
            {"title": "S1", "description": "d", "suggested_paths": []}
        ]}

        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="T", description=""))
        await self.executor.run_planning(goal.id)

        calls = self.mock_provider.calls
        planner_calls = [c for c in calls if "PLANNER OVERRIDE" in c["system_prompt"]]
        self.assertTrue(planner_calls, "planner system prompt override must be used")
        self.assertEqual(planner_calls[0]["model"], "planner-model")

        # Fixer call (made during run_step) must use the fixer config.
        self.mock_responses["fixer"] = {"files": []}
        self.mock_responses["verifier"] = {"argv": None, "verdict": "pass", "explanation": "x"}
        self.mock_responses["critic"] = {"decision": "approve", "reasons": []}
        self.mock_responses["scribe"] = {"summary": "s", "commit_message": "c: x"}
        step = self.goals.steps(goal.id)[0]
        # run_step guards its late stages on goal status (a cancel must be able
        # to stop a commit), so it is called the way every production caller
        # calls it: with the goal RUNNING.
        self.goals.update_status(goal.id, goal.version + 1, "RUNNING")
        await self.executor.run_step(goal.id, step.id)

        fixer_calls = [c for c in self.mock_provider.calls if "You are Codify Fixer" in c["system_prompt"]]
        self.assertTrue(fixer_calls, "fixer call must happen")
        self.assertEqual(fixer_calls[0]["model"], "fixer-model")

        # And the goal must have completed the step.
        self.assertEqual(self.goals.steps(goal.id)[0].status, "COMPLETED")


class TestCallLatencyRecording(unittest.IsolatedAsyncioTestCase):
    """The Settings screen's "last call / last error" reads the goal event log,
    so the log has to carry what happened: how long a completed call took, and
    that a failed one failed. Without the duration a card cannot tell a slow
    fixer from a fast one; without a failure record a role that only ever fails
    looks like it never ran."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(_FailingProvider("none"), Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_a_completed_call_records_its_duration_on_the_usage_event(self) -> None:
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="T", description=""))
        await self.executor.run_planning(goal.id)
        usage = [e.payload for e in self.goals.events_after(goal.id, 0) if e.type == "usage"]
        self.assertTrue(usage, "planning must produce at least one usage event")
        for p in usage:
            self.assertIn("duration_ms", p, "duration rides on the same event as the tokens")
            self.assertIsInstance(p["duration_ms"], int)
            self.assertGreaterEqual(p["duration_ms"], 0)
            self.assertIn("role", p)
            self.assertIn("provider", p)

    async def test_a_provider_failure_records_an_agent_call_failed_event(self) -> None:
        self.registry.set_config(
            "fixer", AgentConfigUpdate(model_name="fixer-model")
        )
        # Swap in a factory whose provider breaks the fixer with a 500.
        self.executor.orchestrator.registry._factory = _ScriptedFactory(
            _FailingProvider("http"), Keychain()
        )
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="T", description=""))
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, goal.version + 1, "RUNNING")
        await self.executor.run_step(goal.id, step.id)

        failures = [
            e.payload for e in self.goals.events_after(goal.id, 0)
            if e.type == "agent_call_failed"
        ]
        self.assertTrue(failures, "a failed provider call must leave its own record")
        fixer_failures = [p for p in failures if p["role"] == "fixer"]
        self.assertTrue(fixer_failures)
        p = fixer_failures[0]
        self.assertEqual(p["code"], "provider_http")
        self.assertEqual(p["target"], "primary")
        self.assertIn("duration_ms", p)
        self.assertIsInstance(p["duration_ms"], int)
        self.assertIn("model", p)
        self.assertIn("message", p)


class TestExecutorService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.db_path = self.root / "test.db"
        self.conn = connect(self.db_path)
        self.keychain = Keychain()

        self.mock_responses: dict[str, dict[str, Any]] = {}
        self.mock_provider = MockProvider(self.mock_responses)
        self.mock_provider = MockProvider(self.mock_responses)
        self.mock_factory = MockFactory(self.mock_provider)

        self.registry = AgentRegistryService(self.conn, self.mock_factory, self.keychain)
        configure_every_role(self.registry)
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.sandbox = SandboxService()
        self.executor = ExecutorService(self.goals, self.workspaces, self.registry, self.sandbox)

        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_planning_flow(self) -> None:
        self.mock_responses["planner"] = {
            "steps": [
                {"title": "Step 1", "description": "Create hello.py", "suggested_paths": ["hello.py"]}
            ]
        }

        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="Test", description="Desc"))
        self.assertEqual(goal.status, "PLANNING")

        await self.executor.run_planning(goal.id)

        refreshed = self.goals.get(goal.id)
        self.assertEqual(refreshed.status, "PENDING")

        steps = self.goals.steps(goal.id)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].title, "Step 1")
        self.assertEqual(steps[0].status, "PENDING")

    async def test_step_execution_success(self) -> None:
        self.mock_responses["planner"] = {
            "steps": [
                {"title": "Step 1", "description": "Create a.py", "suggested_paths": ["a.py"]}
            ]
        }
        self.mock_responses["fixer"] = {
            "files": [
                {"path": "a.py", "action": "create", "content": "print('hello world')\n"}
            ]
        }
        self.mock_responses["verifier"] = {
            "argv": None,
            "verdict": "pass",
            "explanation": "tests passed",
        }
        self.mock_responses["critic"] = {
            "decision": "approve",
            "reasons": [],
        }
        self.mock_responses["scribe"] = {
            "summary": "Added a.py",
            "commit_message": "feat: add a.py",
        }

        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="Create File", description=""))
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]

        # Update goal to RUNNING
        self.goals.update_status(goal.id, goal.version + 1, "RUNNING")

        await self.executor.run_step(goal.id, step.id)

        # File should be created
        self.assertTrue((self.root / "a.py").exists())
        self.assertEqual((self.root / "a.py").read_text(), "print('hello world')\n")

        # Step should be COMPLETED
        refreshed_step = self.goals.steps(goal.id)[0]
        self.assertEqual(refreshed_step.status, "COMPLETED")
        self.assertEqual(refreshed_step.commit_message, "feat: add a.py")

        # Check diff and test_result events were published
        events = self.goals.events_after(goal.id, 0)
        types = [e.type for e in events]
        self.assertIn("diff", types)
        self.assertIn("test_result", types)
        self.assertIn("file_change_summary", types)

    async def test_role_without_a_model_says_so_instead_of_calling_nothing(self) -> None:
        """Seeded roles carry no model id; the failure must name the fix.

        Without this guard the role was called with an empty model, the provider
        answered with a protocol error, and the goal failed as
        `agent_output_invalid` — pointing the user at a malformed model reply
        that never existed.
        """
        self.registry.set_config("planner", AgentConfigUpdate(model_name=""))
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="t", description="d"))
        await self.executor.run_planning(goal.id)

        events = self.goals.events_after(goal.id, 0)
        error = next(e for e in events if e.type == "error")
        self.assertEqual(error.payload["code"], "agent_not_configured")
        self.assertIn("planner", error.payload["message"])
        self.assertIn("Settings", error.payload["message"])
        # Named, so "why did this fail?" can open the right role's config.
        self.assertEqual(error.payload["role"], "planner")
        planner_calls = [
            c for c in self.mock_provider.calls if "planner" in c["system_prompt"].lower()
        ]
        self.assertEqual(planner_calls, [], "an unconfigured role must not be called")

    async def test_missing_credential_is_reported_as_a_setup_problem(self) -> None:
        """A role whose provider has no key is a configuration gap, not bad output."""

        class _NoCredentialFactory(ProviderFactory):
            def build(self, config: AgentConfig) -> BaseProvider:
                raise ProviderError("missing_api_key", "Anthropic API key is not set")

        self.registry = AgentRegistryService(self.conn, _NoCredentialFactory(Keychain()), Keychain())
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService()
        )
        self.registry.set_config("planner", AgentConfigUpdate(model_name="some-model"))

        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="t", description="d"))
        await self.executor.run_planning(goal.id)

        error = next(e for e in self.goals.events_after(goal.id, 0) if e.type == "error")
        self.assertEqual(error.payload["code"], "agent_not_configured")
        self.assertIn("Anthropic API key is not set", error.payload["message"])
        self.assertIn("Provider Keys", error.payload["message"])
        self.assertEqual(error.payload["role"], "planner")

    async def test_critic_rejection_and_retry(self) -> None:
        self.mock_responses["planner"] = {
            "steps": [{"title": "Step 1", "description": "Do something", "suggested_paths": []}]
        }
        self.mock_responses["fixer"] = {
            "files": [{"path": "b.py", "action": "create", "content": "x = 1\n"}]
        }
        self.mock_responses["verifier"] = {"argv": None, "verdict": "pass", "explanation": "ok"}
        # Critic rejects
        self.mock_responses["critic"] = {
            "decision": "request-changes",
            "reasons": ["Missing comments"],
        }

        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="Reject Test", description=""))
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, goal.version + 1, "RUNNING")

        await self.executor.run_step(goal.id, step.id)

        # Step is IN_PROGRESS with review notes; Goal is PAUSED
        step_after = self.goals.steps(goal.id)[0]
        self.assertEqual(step_after.status, "IN_PROGRESS")
        self.assertEqual(step_after.review_notes, "Missing comments")

        goal_after = self.goals.get(goal.id)
        self.assertEqual(goal_after.status, "PAUSED")

        # Now approve and retry
        self.mock_responses["critic"] = {"decision": "approve", "reasons": []}
        self.mock_responses["scribe"] = {"summary": "Done", "commit_message": "feat: b"}

        await self.executor.retry_step(goal.id, step.id, goal_after.version)

        step_final = self.goals.steps(goal.id)[0]
        self.assertEqual(step_final.status, "COMPLETED")

        # Retrying a COMPLETED step should raise ApiError
        from engine.services import ApiError
        with self.assertRaises(ApiError) as ctx:
            await self.executor.retry_step(goal.id, step.id, self.goals.get(goal.id).version)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "step_not_retryable")

    async def test_verifier_fail_marks_step_failed(self) -> None:
        self.mock_responses["planner"] = {
            "steps": [{"title": "Step 1", "description": "Test failure", "suggested_paths": []}]
        }
        self.mock_responses["fixer"] = {
            "files": [{"path": "fail.py", "action": "create", "content": "assert False\n"}]
        }
        self.mock_responses["verifier"] = {
            "argv": None,
            "verdict": "fail",
            "explanation": "assertion error",
        }

        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="Fail Test", description=""))
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, goal.version + 1, "RUNNING")

        await self.executor.run_step(goal.id, step.id)

        step_after = self.goals.steps(goal.id)[0]
        self.assertEqual(step_after.status, "FAILED")
        goal_after = self.goals.get(goal.id)
        self.assertEqual(goal_after.status, "FAILED")


class _ScriptedProvider(BaseProvider):
    """Verifier answers from a script; every other role answers from a map."""

    def __init__(self, others: dict[str, Any], verifier_replies: list[Any] | None = None):
        self.others = others
        self.verifier_replies = list(verifier_replies or [])
        self.calls: list[tuple[str, str]] = []
        # Prompts whose scripted reply was served, in reply order — reply k
        # corresponds to verifier_prompt_seq[k].
        self.calls_seq: list[str] = []

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
        role = next(
            (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
        )
        self.calls.append((role, user_prompt))
        if role == "verifier" and self.verifier_replies:
            self.calls_seq.append(user_prompt)
            return json.dumps(self.verifier_replies.pop(0))
        return json.dumps(self.others.get(role, {}))

    def verifier_calls(self) -> list[str]:
        return [p for role, p in self.calls if role == "verifier"]

    @property
    def verifier_prompt_seq(self) -> list[str]:
        """Verifier prompts whose scripted reply was served, in reply order."""
        return list(self.calls_seq)


class _ScriptedFactory(ProviderFactory):
    def __init__(self, provider: BaseProvider, keychain: Keychain) -> None:
        super().__init__(keychain)
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        return self.provider


class _SkippedGate(LayaService):
    """The gate is a separate concern; these tests are about the verifier."""

    async def decide(self, state: dict[str, Any]) -> LayaDecision:
        return LayaDecision(engine="skipped", skipped_reason="test double")


class TestSandboxRefusalRecovery(unittest.IsolatedAsyncioTestCase):
    """A refused test command must not end the goal.

    Real case: the verifier proposed `touch …` to create a file, the sandbox
    refused the binary, and the step — along with a change the fixer had already
    written to disk — died with `command_not_allowed`. The refusal is something
    the verifier can act on, so it is fed back like command output.
    """

    REFUSED = {"argv": ["touch", "x"], "verdict": None, "explanation": "create a file"}

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.provider = _ScriptedProvider(
            {
                "planner": {
                    "steps": [{"title": "Step 1", "description": "create x.txt", "suggested_paths": ["x.txt"]}]
                },
                "fixer": {"files": [{"path": "x.txt", "action": "create", "content": "hi\n"}]},
                "critic": {"decision": "approve", "reasons": []},
                "scribe": {"summary": "did it", "commit_message": "feat: x"},
            }
        )
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def _plan_and_run(self) -> None:
        await self.executor.run_planning(self.goal.id)
        step = self.goals.steps(self.goal.id)[0]
        self.goals.update_status(self.goal.id, self.goal.version + 1, "RUNNING")
        await self.executor.run_step(self.goal.id, step.id)

    def _test_results(self) -> list[dict[str, Any]]:
        return [
            e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == "test_result"
        ]

    async def test_refused_command_is_replaced_and_the_step_completes(self) -> None:
        self.provider.verifier_replies = [
            self.REFUSED,
            {"argv": ["git", "status"], "verdict": None, "explanation": "an allowed runner"},
            {"argv": None, "verdict": "pass", "explanation": "nothing to run; working tree checked"},
        ]
        await self._plan_and_run()

        step = self.goals.steps(self.goal.id)[0]
        self.assertEqual(step.status, "COMPLETED", "a refused command must not end the step")
        self.assertEqual(self.goals.get(self.goal.id).status, "RUNNING", "nor the goal")

        result = self._test_results()[0]
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["argv"], ["git", "status"], "the command that actually ran")
        self.assertTrue(result["ran"])
        self.assertEqual(result["refused"], ["touch x — binary not allowed: touch"])

        # The refusal reached the model as actionable feedback, not as a crash.
        prompts = self.provider.verifier_calls()
        self.assertIn("was NOT run", prompts[1])
        self.assertIn("binary not allowed: touch", prompts[1])
        self.assertIn("Command ran.", prompts[2])  # the run feedback is unchanged

        levels = [
            e.payload.get("level") for e in self.goals.events_after(self.goal.id, 0) if e.type == "log"
        ]
        self.assertIn("warn", levels, "the refusal has to be visible, not silent")
        self.assertTrue((self.root / "x.txt").exists(), "the fixer's work is kept")

    async def test_nothing_runnable_ends_as_a_skip_not_a_dead_goal(self) -> None:
        """A refusal with no permitted alternative is a verdict, not a failure."""
        self.provider.verifier_replies = [
            self.REFUSED,
            {
                "argv": None,
                "verdict": "skip",
                "explanation": "no permitted test command exists in this project",
            },
        ]
        await self._plan_and_run()

        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "COMPLETED")
        self.assertEqual(self.goals.get(self.goal.id).status, "RUNNING")
        result = self._test_results()[0]
        self.assertEqual(result["verdict"], "skip")
        self.assertFalse(result["ran"], "no command ran, and the event says so")
        self.assertIsNone(result["argv"])
        self.assertEqual(len(result["refused"]), 1)

    async def test_a_refusal_prompt_keeps_the_step_context(self) -> None:
        """The verifier's calls are stateless — the retry prompt after a refusal
        must still name the step it is ruling on, not just the refusal. The
        old feedback REPLACED the prompt, dropping title/description/test
        command, so the verifier re-decided from one sentence."""
        self.provider.verifier_replies = [
            self.REFUSED,
            {"argv": ["git", "status"], "verdict": None, "explanation": "an allowed runner"},
            {"argv": None, "verdict": "pass", "explanation": "nothing to run; working tree checked"},
        ]
        await self._plan_and_run()

        prompts = self.provider.verifier_prompt_seq
        self.assertEqual(len(prompts), 3)
        for prompt in prompts:
            self.assertIn("Step 1", prompt, "every verifier call carries the step title")
            self.assertIn("create x.txt", prompt, "and the step description")
        # The refusal feedback is appended, not substituted.
        self.assertIn("was NOT run", prompts[1])
        self.assertIn("binary not allowed: touch", prompts[1])
        # The run feedback rides on the context too.
        self.assertIn("Command output", prompts[2])
        self.assertIn("Command ran.", prompts[2])

    async def test_endless_refusals_are_capped(self) -> None:
        """Retrying is bounded — the model cannot loop forever on refusals."""
        self.provider.verifier_replies = [self.REFUSED] * 8
        await self._plan_and_run()

        goal = self.goals.get(self.goal.id)
        self.assertEqual(goal.status, "FAILED")
        error = next(e for e in self.goals.events_after(self.goal.id, 0) if e.type == "error")
        self.assertEqual(error.payload["code"], "agent_output_invalid")
        self.assertIn("kept proposing commands the sandbox refused", error.payload["message"])

        # 1 initial proposal + MAX_REFUSED_TEST_COMMANDS retries + the one call
        # that must be a verdict; nothing beyond that.
        self.assertEqual(
            len(self.provider.verifier_calls()),
            1 + MAX_REFUSED_TEST_COMMANDS + 1,
        )

    async def test_proposing_another_command_after_a_run_is_still_rejected(self) -> None:
        """The single-execution rule survives the retry path."""
        self.provider.verifier_replies = [
            {"argv": ["git", "status"], "verdict": None, "explanation": "run it"},
            {"argv": ["git", "diff"], "verdict": None, "explanation": "a second command"},
        ]
        await self._plan_and_run()

        self.assertEqual(self.goals.get(self.goal.id).status, "FAILED")
        error = next(e for e in self.goals.events_after(self.goal.id, 0) if e.type == "error")
        self.assertIn("argv null", error.payload["message"])


class TestEvidencePackPathHandling(unittest.TestCase):
    """Cited paths survive normalization. The old `lstrip("./")` stripped the
    *characters* '.' and '/', mangling dotfiles (.gitignore → gitignore) so
    the engine dropped evidence it had actually been shown."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.registry = AgentRegistryService(self.conn, ProviderFactory(Keychain()), Keychain())
        self.executor = ExecutorService(
            GoalService(self.conn), WorkspaceService(self.conn), self.registry,
            SandboxService(), laya=_SkippedGate(),
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_a_cited_dotfile_survives_path_normalization(self) -> None:
        pack = self.executor._evidence_pack(
            "g1",
            {
                "files": [{"path": ".gitignore", "why": "ignore rules"}],
                "symbols": [{"name": "X", "path": "./src/x.py"}],
            },
            opened={".gitignore", "src/x.py"},
            matched=set(),
            listed={".gitignore", "src/x.py"},
            rounds_used=1,
        )
        self.assertEqual([f["path"] for f in pack["files"]], [".gitignore"])
        self.assertEqual(pack["files"][0]["evidence"], "opened")
        self.assertEqual(
            [s["path"] for s in pack["symbols"]], ["src/x.py"],
            "a true ./ prefix is stripped; the file's own leading dots are not",
        )
        self.assertEqual(pack["dropped_paths"], [])

    def test_the_rendered_symbol_line_names_each_symbol_and_its_path(self) -> None:
        """The Symbols line is what the planner and the fixer actually read.

        Nothing asserted it, which is how a Python 3.12-only f-string sat in the
        renderer unnoticed — it parsed on the developer's interpreter and would
        have been a SyntaxError on the 3.10 minimum (`tests/test_min_python_syntax.py`).
        A symbol without a path is still listed, and one with a path says so.
        """
        text = self.executor._evidence_text(
            {"symbols": [{"name": "greet", "path": "src/foo.py"}, {"name": "banner"}]}
        )
        self.assertIn("Symbols: greet (src/foo.py), banner", text)


class _HangingSandbox(SandboxService):
    """Every command hangs until the timeout — no real process, no real wait."""

    last_timeout: int

    def run_command(
        self, root_path: str, argv: list[str], timeout_s: int = 120, mode: str = "test"
    ) -> dict[str, Any]:
        self.calls = getattr(self, "calls", 0) + 1
        self.last_timeout = timeout_s
        raise subprocess.TimeoutExpired(argv, timeout_s)


class TestHungTestCommand(unittest.IsolatedAsyncioTestCase):
    """A test command that hangs must not wedge the goal.

    The sandbox subprocess used to block forever, leaving the step IN_PROGRESS
    (no review notes, so unretryable) and the goal stuck, surfaced as an
    unlabelled internal error. The executor now applies timeout(1)-style
    semantics itself: exit 124, output handed back, verdict still owed.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.provider = _ScriptedProvider(
            {
                "planner": {
                    "steps": [{"title": "Step 1", "description": "make x", "suggested_paths": ["x.txt"]}]
                },
                "fixer": {"files": [{"path": "x.txt", "action": "create", "content": "hi\n"}]},
                "critic": {"decision": "approve", "reasons": []},
                "scribe": {"summary": "did it", "commit_message": "feat: x"},
            }
        )
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, _HangingSandbox(), laya=_SkippedGate()
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_a_hang_reports_exit_124_and_the_goal_reaches_a_verdict(self) -> None:
        self.provider.verifier_replies = [
            {"argv": ["pytest", "-q"], "verdict": None, "explanation": "run the suite"},
            {"argv": None, "verdict": "fail", "explanation": "the run timed out"},
            # The fix→verify loop retries once; the retry fails too, so the
            # final attempt's TestsFailed is what fails the step.
            {"argv": None, "verdict": "fail", "explanation": "still timing out after the retry"},
        ]
        await self.executor.run_planning(self.goal.id)
        step = self.goals.steps(self.goal.id)[0]
        self.goals.update_status(self.goal.id, self.goal.version + 1, "RUNNING")
        await self.executor.run_step(self.goal.id, step.id)

        # A hang is reported like a failing run, not swallowed: the verifier saw
        # the timeout and returned the verdict it owed, so the goal reached a
        # terminal state instead of wedging on a blocked subprocess (which used
        # to leave the step IN_PROGRESS and unretryable).
        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "FAILED")
        self.assertEqual(self.goals.get(self.goal.id).status, "FAILED")
        error = next(e for e in self.goals.events_after(self.goal.id, 0) if e.type == "error")
        self.assertEqual(error.payload["code"], "tests_failed")

        result = next(
            e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == "test_result"
        )
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["argv"], ["pytest", "-q"])
        self.assertEqual(result["exit_code"], 124, "timeout(1)'s code for a killed run")
        self.assertTrue(result["ran"])
        self.assertEqual(self.executor.sandbox.last_timeout, 120, "the hang is bounded")  # type: ignore[attr-defined]

        # The verifier saw what happened, exactly like ordinary command output.
        self.assertIn("timed out after", self.provider.verifier_prompt_seq[1])
        self.assertIn("Now return the verdict", self.provider.verifier_prompt_seq[1])


class _FailingProvider(BaseProvider):
    """Breaks the fixer in a chosen way; every other role behaves normally."""

    def __init__(self, fixer_failure: str):
        self.fixer_failure = fixer_failure

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
        # Real providers report their token usage through the sink the
        # orchestrator attaches; the double does the same so latency tests can
        # read the usage events a goal would actually produce.
        self._report_usage("openai_compat", {"usage": {"prompt_tokens": 5, "completion_tokens": 7}})
        if "You are Codify Fixer" in system_prompt:
            if self.fixer_failure == "garbage":
                return "I am not JSON at all."
            raise ProviderError("provider_http", "openai_compat 500")
        if "You are Codify Planner" in system_prompt:
            return json.dumps(
                {"steps": [{"title": "S1", "description": "d", "suggested_paths": []}]}
            )
        return json.dumps({})


class TestFailureAttribution(unittest.IsolatedAsyncioTestCase):
    """Error events must name the role, or "why did this fail?" is guesswork."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def _run_a_step_that_fails_in_the_fixer(self, failure: str) -> dict[str, Any]:
        registry = AgentRegistryService(
            self.conn, _ScriptedFactory(_FailingProvider(failure), Keychain()), Keychain()
        )
        configure_every_role(registry)
        executor = ExecutorService(
            self.goals, self.workspaces, registry, SandboxService(), laya=_SkippedGate()
        )
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="T", description=""))
        await executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, goal.version + 1, "RUNNING")
        await executor.run_step(goal.id, step.id)
        return next(e.payload for e in self.goals.events_after(goal.id, 0) if e.type == "error")

    async def test_invalid_output_names_the_role_that_produced_it(self) -> None:
        error = await self._run_a_step_that_fails_in_the_fixer("garbage")
        self.assertEqual(error["code"], "agent_output_invalid")
        self.assertEqual(error["role"], "fixer", "a non-JSON fixer reply is the fixer's failure")

    async def test_a_provider_failure_keeps_its_code_and_still_names_the_role(self) -> None:
        """A 500 is not a configuration gap; saying so would send users to Settings."""
        error = await self._run_a_step_that_fails_in_the_fixer("http")
        self.assertEqual(error["code"], "provider_http")
        self.assertEqual(error["role"], "fixer")
        self.assertIn("openai_compat 500", error["message"])


class _QueuedProvider(BaseProvider):
    """Answers each role from its own queue, so a round-by-round exchange can be
    scripted — the librarian asks, the engine fetches, the librarian answers."""

    def __init__(self, by_role: dict[str, list[Any]] | None = None):
        self.by_role = {k: list(v) for k, v in (by_role or {}).items()}
        self.current_role: str | None = None
        self.calls: list[tuple[str, str, str]] = []

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
        role = self.current_role or "unknown"
        self.calls.append((role, system_prompt, user_prompt))
        queue = self.by_role.setdefault(role, [])
        if not queue:
            return json.dumps({"status": "ok"})
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)

    def role_calls(self, role: str) -> list[tuple[str, str, str]]:
        return [c for c in self.calls if c[0] == role]


class _QueuedFactory(ProviderFactory):
    def __init__(self, provider: _QueuedProvider, keychain: Keychain) -> None:
        super().__init__(keychain)
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        self.provider.current_role = config.role
        return self.provider


class TestLibrarianReconnaissance(unittest.IsolatedAsyncioTestCase):
    """The librarian is what stops the pipeline planning blind.

    Before it, the planner received a title and a description, and the fixer read
    only the paths that blind planner guessed — so a wrong guess meant no agent
    ever saw the right file.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text(
            "def greet(name):\n    return f'hello {name}'\n", encoding="utf-8"
        )
        (self.root / "README.md").write_text("# Project\n", encoding="utf-8")

        self.conn = connect(self.root / "lib.db")
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

    def _goal(self) -> Goal:
        return self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="Add greeting", description="make it polite")
        )

    def _events(self, goal_id: str, type_: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.goals.events_after(goal_id, 0) if e.type == type_]

    def _warnings(self, goal_id: str) -> str:
        return " | ".join(
            e.payload["message"]
            for e in self.goals.events_after(goal_id, 0)
            if e.type == "log" and e.payload.get("level") == "warn"
        )

    ONE_STEP = {"steps": [{"title": "S1", "description": "d", "suggested_paths": ["src/app.py"]}]}

    async def test_it_looks_around_then_hands_the_planner_what_it_found(self) -> None:
        self.provider.by_role = {
            "librarian": [
                {"reads": ["src/app.py"], "searches": ["greet"], "enough": False},
                {
                    "summary": "app.py defines greet",
                    "files": [{"path": "src/app.py", "why": "the function to change"}],
                    "conventions": ["four-space indent"],
                    "test_command": ["python3", "-m", "pytest", "-q"],
                    "enough": True,
                },
            ],
            "planner": [self.ONE_STEP],
        }
        goal = self._goal()
        await self.executor.run_planning(goal.id)

        self.assertEqual(
            len(self.provider.role_calls("librarian")), 2,
            "one round to ask for material, one to answer with the pack",
        )
        handed_back = self.provider.role_calls("librarian")[1][2]
        self.assertIn("def greet", handed_back, "the read it asked for must be handed back")
        self.assertIn("greet", handed_back, "search results must be handed back")

        packs = self._events(goal.id, "library_evidence")
        self.assertEqual(len(packs), 1)
        self.assertEqual([f["path"] for f in packs[0]["files"]], ["src/app.py"])
        self.assertEqual(packs[0]["files"][0]["evidence"], "opened")
        self.assertEqual(packs[0]["test_command"], ["python3", "-m", "pytest", "-q"])

        planner_prompt = self.provider.role_calls("planner")[0][2]
        self.assertIn("app.py defines greet", planner_prompt)
        self.assertIn("python3 -m pytest -q", planner_prompt, "the real test command travels with it")
        self.assertEqual(self.goals.get(goal.id).status, "PENDING")

    async def test_a_path_it_never_opened_is_dropped_not_passed_on(self) -> None:
        self.provider.by_role = {
            "librarian": [
                {
                    "summary": "s",
                    "files": [
                        {"path": "src/ghost.py", "why": "invented"},
                        {"path": "README.md", "why": "seen in the tree listing"},
                    ],
                    "enough": True,
                }
            ],
            "planner": [self.ONE_STEP],
        }
        goal = self._goal()
        await self.executor.run_planning(goal.id)

        pack = self._events(goal.id, "library_evidence")[0]
        self.assertEqual([f["path"] for f in pack["files"]], ["README.md"])
        self.assertEqual(pack["files"][0]["evidence"], "listed")
        self.assertEqual(pack["dropped_paths"], ["src/ghost.py"])
        self.assertIn("never saw", self._warnings(goal.id))
        planner_prompt = self.provider.role_calls("planner")[0][2]
        self.assertIn("do not rely on", planner_prompt, "the planner is told which claims are unverified")

    async def test_asking_forever_is_capped(self) -> None:
        self.provider.by_role = {
            "librarian": [{"reads": ["src/app.py"], "enough": False} for _ in range(MAX_LIBRARY_ROUNDS)],
            "planner": [self.ONE_STEP],
        }
        goal = self._goal()
        await self.executor.run_planning(goal.id)

        self.assertEqual(len(self.provider.role_calls("librarian")), MAX_LIBRARY_ROUNDS)
        self.assertIn("round cap", self._warnings(goal.id))
        self.assertEqual(self.goals.get(goal.id).status, "PENDING", "planning still happens")

    async def test_a_refused_request_is_fed_back_instead_of_failing_the_goal(self) -> None:
        self.provider.by_role = {
            "librarian": [
                {"run": [["rm", "-rf", "src"]], "enough": False},
                {"summary": "could not remove anything", "enough": True},
            ],
            "planner": [self.ONE_STEP],
        }
        goal = self._goal()
        await self.executor.run_planning(goal.id)

        second = self.provider.role_calls("librarian")[1][2]
        self.assertIn("refused", second, "the librarian is told why nothing happened")
        self.assertIn("may not have", self._warnings(goal.id))
        self.assertEqual(self.goals.get(goal.id).status, "PENDING")

    async def test_an_escape_attempt_is_refused(self) -> None:
        self.provider.by_role = {
            "librarian": [
                {"reads": ["../../etc/passwd"], "enough": False},
                {"summary": "nothing outside", "enough": True},
            ],
            "planner": [self.ONE_STEP],
        }
        goal = self._goal()
        await self.executor.run_planning(goal.id)

        second = self.provider.role_calls("librarian")[1][2]
        self.assertIn("refused", second)
        self.assertNotIn("root:", second, "nothing outside the workspace is ever read")

    async def test_a_librarian_that_cannot_run_leaves_planning_working(self) -> None:
        self.provider.by_role = {
            "librarian": [ProviderError("provider_http", "openai_compat 500")],
            "planner": [self.ONE_STEP],
        }
        goal = self._goal()
        await self.executor.run_planning(goal.id)

        self.assertEqual(self.goals.get(goal.id).status, "PENDING")
        self.assertIn("without evidence", self._warnings(goal.id))
        planner_prompt = self.provider.role_calls("planner")[0][2]
        self.assertIn("no reconnaissance", planner_prompt)

    async def test_the_fixer_is_given_the_same_evidence_the_planner_planned_from(self) -> None:
        self.provider.by_role = {
            "librarian": [{"summary": "app.py is the only module", "enough": True}],
            "planner": [self.ONE_STEP],
            "fixer": [{"files": [{"path": "src/app.py", "action": "update", "content": "x = 1\n"}]}],
            "verifier": [{"argv": None, "verdict": "pass", "explanation": "nothing to run"}],
            "critic": [{"decision": "approve", "reasons": []}],
            "scribe": [{"summary": "did it", "commit_message": "feat: greet"}],
        }
        goal = self._goal()
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, goal.version + 1, "RUNNING")
        await self.executor.run_step(goal.id, step.id)

        fixer_prompt = self.provider.role_calls("fixer")[0][2]
        self.assertIn("app.py is the only module", fixer_prompt)
        self.assertIn("def greet", fixer_prompt, "the step's own files are still read in full")
        self.assertEqual(self.goals.steps(goal.id)[0].status, "COMPLETED")


class TestWhatAStepChanges(unittest.IsolatedAsyncioTestCase):
    """A step may only claim — and commit — what it actually changed.

    Two failures this pins down. A proposal identical to the file's current
    contents was reported as a touched file and handed the scribe a commit message
    for a commit that could not happen. And the commit itself ran `git add -A`, so a
    developer with half-finished work in the tree found it swept into a commit whose
    message names our step.
    """

    ON_STEP = {
        "planner": {"steps": [{"title": "S1", "description": "d", "suggested_paths": ["a.py"]}]},
        "verifier": {"argv": None, "verdict": "pass", "explanation": "nothing to run"},
        "critic": {"decision": "approve", "reasons": []},
        "scribe": {"summary": "did it", "commit_message": "feat: our step"},
    }

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "a.py").write_text("value = 1\n", encoding="utf-8")
        self.git = GitService()
        self.git.init_repo(str(self.root))
        self.base = self.git.commit(str(self.root), "chore: base", ["a.py"])

        self.conn = connect(self.root / "step.db")
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

    async def _run(self, responses: dict[str, Any], library: bool = False) -> tuple[Goal, PlanStep]:
        # Each role answers from its own queue, and a repeated call gets its queue's
        # next entry, so `responses` may supply a list for a multi-round role.
        scripted = {**self.ON_STEP, **responses}
        self.provider.by_role = {
            role: (value if isinstance(value, list) else [value])
            for role, value in scripted.items()
        }
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="T", description=""))
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, goal.version + 1, "RUNNING")
        await self.executor.run_step(goal.id, step.id)
        return goal, step

    def _events(self, goal_id: str, type_: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.goals.events_after(goal_id, 0) if e.type == type_]

    def _committed(self) -> set[str]:
        out = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=self.root, capture_output=True, text=True, check=False,
        ).stdout
        return {line for line in out.splitlines() if line}

    def _porcelain(self) -> str:
        return subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.root, capture_output=True, text=True, check=False,
        ).stdout

    async def test_a_proposal_identical_to_the_file_is_not_a_change(self) -> None:
        goal, _ = await self._run({
            "fixer": {"files": [{"path": "a.py", "action": "update", "content": "value = 1\n"}]},
        })

        summary = self._events(goal.id, "file_change_summary")
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["paths"], [], "nothing changed, so nothing is claimed")
        self.assertEqual(summary[0]["unchanged"], ["a.py"])
        self.assertEqual(self._events(goal.id, "diff"), [], "an empty diff is not a diff")
        logs = " | ".join(
            e.payload["message"] for e in self.goals.events_after(goal.id, 0)
            if e.type == "log" and e.payload.get("level") == "info"
        )
        self.assertIn("no change: a.py", logs, "the no-op is stated, not hidden")
        # Nothing to commit, so no commit was invented — HEAD is still the base.
        self.assertEqual(
            subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                           capture_output=True, text=True, check=False).stdout.strip(),
            self.base,
        )
        self.assertEqual(self.goals.steps(goal.id)[0].status, "COMPLETED")

    async def test_a_real_change_is_reported_and_committed(self) -> None:
        goal, _ = await self._run({
            "fixer": {"files": [{"path": "a.py", "action": "update", "content": "value = 2\n"}]},
        })
        summary = self._events(goal.id, "file_change_summary")[0]
        self.assertEqual(summary["paths"], ["a.py"])
        self.assertEqual(summary["unchanged"], [])
        self.assertIn("+value = 2", self._events(goal.id, "diff")[0]["unified_diff"])
        self.assertIn("a.py", self._committed())

    async def test_the_commit_leaves_the_users_own_work_alone(self) -> None:
        (self.root / "user_wip.py").write_text("half-finished\n", encoding="utf-8")
        (self.root / "user_staged.py").write_text("staged by hand\n", encoding="utf-8")
        subprocess.run(["git", "add", "--", "user_staged.py"], cwd=self.root, check=False)

        goal, _ = await self._run({
            "fixer": {"files": [{"path": "a.py", "action": "update", "content": "value = 3\n"}]},
        })

        self.assertIn("a.py", self._committed())
        self.assertNotIn("user_wip.py", self._committed())
        self.assertNotIn("user_staged.py", self._committed())
        status = self._porcelain()
        self.assertIn("?? user_wip.py", status)
        self.assertIn("A  user_staged.py", status)

    async def test_a_path_the_planner_guessed_badly_does_not_kill_the_step(self) -> None:
        """`suggested_paths` is a guess: it must not fail a step before the fixer runs."""
        (self.root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        self.ON_STEP = {
            "planner": {
                "steps": [{
                    "title": "S1", "description": "d",
                    "suggested_paths": ["../../etc/passwd", "blob.bin", "missing.py"],
                }]
            },
            "fixer": {"files": [{"path": "a.py", "action": "update", "content": "value = 4\n"}]},
            "verifier": {"argv": None, "verdict": "pass", "explanation": "nothing to run"},
            "critic": {"decision": "approve", "reasons": []},
            "scribe": {"summary": "did it", "commit_message": "feat: our step"},
        }
        goal, _ = await self._run({})

        fixer_prompt = self.provider.role_calls("fixer")[0][2]
        self.assertIn("could not be read", fixer_prompt)
        self.assertIn("../../etc/passwd", fixer_prompt)
        self.assertIn("outside the workspace", fixer_prompt)
        self.assertIn("blob.bin (not readable as text)", fixer_prompt)
        self.assertIn("missing.py (not readable as text)", fixer_prompt)
        self.assertEqual(self.goals.steps(goal.id)[0].status, "COMPLETED")


class TestFixRetryLoop(unittest.IsolatedAsyncioTestCase):
    """A failing test run feeds back into one bounded fix attempt.

    The verifier's output is exactly the evidence the fixer was missing when it
    wrote the broken code; the old behavior threw the step away on the first
    failing verdict. The loop is bounded (MAX_FIX_ATTEMPTS), publishes a
    fix_retry event so the chat explains the extra work, and a cancel still
    wins over the retry.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.provider = _ScriptedProvider(
            {
                "planner": {
                    "steps": [{
                        "title": "Step 1", "description": "make greet.py",
                        "suggested_paths": ["greet.py"],
                    }]
                },
                "critic": {"decision": "approve", "reasons": []},
                "scribe": {"summary": "did it", "commit_message": "feat: greet"},
            }
        )
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _fixer_replies(self, *contents: str) -> None:
        """Script successive fixer attempts via a per-call queue on the provider."""
        replies = [
            {"files": [{"path": "greet.py", "action": "create", "content": c}]}
            for c in contents
        ]
        self.provider.others["fixer"] = replies

        async def completing(
            system_prompt: str, user_prompt: str, model: str,
            temperature: float, max_tokens: int,
        ) -> str:
            role = next(
                (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
            )
            self.provider.calls.append((role, user_prompt))
            if role == "fixer" and self.provider.others["fixer"]:
                return json.dumps(self.provider.others["fixer"].pop(0))
            if role == "verifier" and self.provider.verifier_replies:
                return json.dumps(self.provider.verifier_replies.pop(0))
            return json.dumps(self.provider.others.get(role, {}))

        self.provider.complete = completing  # type: ignore[method-assign]

    def _fixer_prompts(self) -> list[str]:
        return [p for r, p in self.provider.calls if r == "fixer"]

    def _events(self, type_: str) -> list[Any]:
        return [e for e in self.goals.events_after(self.goal.id, 0) if e.type == type_]

    async def _plan(self) -> None:
        await self.executor.run_planning(self.goal.id)
        self.goals.update_status(self.goal.id, self.goals.get(self.goal.id).version, "RUNNING")

    async def test_first_failure_is_retried_and_the_retry_passes(self) -> None:
        # Attempt 1 writes broken code; attempt 2 (after the failure feedback) writes good code.
        self._fixer_replies("def broken():\n  assert False\n", "print('hello')\n")
        self.provider.verifier_replies = [
            {"argv": None, "verdict": "fail", "explanation": "assertion failed in broken()"},
            {"argv": None, "verdict": "pass", "explanation": "all good now"},
        ]
        await self._plan()
        step = self.goals.steps(self.goal.id)[0]

        await self.executor.run_step(self.goal.id, step.id)

        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "COMPLETED")
        self.assertEqual(self.goals.get(self.goal.id).status, "RUNNING")
        self.assertEqual((self.root / "greet.py").read_text(), "print('hello')\n",
                         "the retry's fixed content is what ends up on disk")

        retries = self._events("fix_retry")
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0].payload["attempt"], 1)
        self.assertEqual(retries[0].payload["max_attempts"], 1)
        self.assertIn("assertion failed", retries[0].payload["reason"])

        # The retry prompt carried the failure back to the fixer.
        prompts = self._fixer_prompts()
        self.assertEqual(len(prompts), 2, "one attempt, one retry")
        self.assertIn("FAILED verification", prompts[1])
        self.assertIn("assertion failed in broken()", prompts[1])
        self.assertIn("Do not start over from scratch", prompts[1])

        # The second verifier call knows it is verifying a retry.
        verdict_events = self._events("test_result")
        self.assertEqual(len(verdict_events), 2, "both verdicts are published")

    async def test_retry_failure_fails_the_step_with_tests_failed(self) -> None:
        self._fixer_replies("bad one\n", "still bad\n")
        self.provider.verifier_replies = [
            {"argv": None, "verdict": "fail", "explanation": "first failure"},
            {"argv": None, "verdict": "fail", "explanation": "second failure"},
        ]
        await self._plan()
        step = self.goals.steps(self.goal.id)[0]

        await self.executor.run_step(self.goal.id, step.id)

        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "FAILED")
        self.assertEqual(self.goals.get(self.goal.id).status, "FAILED")
        error = next(e for e in self._events("error") if e.payload.get("code") == "tests_failed")
        self.assertIn("second failure", error.payload["message"],
                      "the FINAL failure is what the error reports")
        self.assertEqual(len(self._fixer_prompts()), 2, "bounded: one retry, no more")
        self.assertEqual(len(self._events("fix_retry")), 1)

    async def test_cancel_wins_over_the_retry(self) -> None:
        """A cancel landing during the failed verification stops the retry."""
        self._fixer_replies("broken\n", "should never be written\n")
        self.provider.verifier_replies = [
            {"argv": None, "verdict": "fail", "explanation": "doomed"},
        ]
        await self._plan()
        step = self.goals.steps(self.goal.id)[0]
        # Simulate the user cancelling between the failure and the retry: the
        # hook runs mid-run_step via the fix_retry publication below.
        original_publish = self.goals.publish

        def publish_and_cancel(event: Event) -> Event:
            result = original_publish(event)
            if event.type == "fix_retry":
                g = self.goals.get(self.goal.id)
                self.goals.update_status(g.id, g.version, "CANCELLED")
            return result

        self.goals.publish = publish_and_cancel  # type: ignore[method-assign]
        await self.executor.run_step(self.goal.id, step.id)

        self.assertEqual(self.goals.get(self.goal.id).status, "CANCELLED")
        self.assertEqual((self.root / "greet.py").read_text(), "broken\n",
                         "the retry's fixer call never ran")
        self.assertEqual(len(self._fixer_prompts()), 1, "no second fixer call after cancel")

    async def test_replay_path_never_loops(self) -> None:
        """apply_goal replays reviewed files — a failing verdict there is final."""
        self._fixer_replies("content\n")
        self.provider.verifier_replies = [
            {"argv": None, "verdict": "fail", "explanation": "nope"},
        ]
        await self._plan()
        step = self.goals.steps(self.goal.id)[0]
        # A replay is driven by stored_files; no fixer call happens at all.
        stored = [{"path": "greet.py", "action": "create", "content": "stored\n"}]

        await self.executor.run_step(self.goal.id, step.id, stored_files=stored)

        self.assertEqual(len(self._fixer_prompts()), 0, "a replay never calls the fixer")
        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "FAILED")
        self.assertEqual(len(self._events("fix_retry")), 0, "no retry on a reviewed replay")


class TestCancelStopsBeforeCommit(unittest.IsolatedAsyncioTestCase):
    """A cancel that lands mid-step must prevent the commit.

    `_run_steps` checks goal status only *between* steps, so a cancel landing
    mid-step used to change nothing: the step ran to the end — including the
    scribe's `git commit` of changes the user had just asked to stop. The fix
    re-checks status after the verifier and again immediately before the
    commit; this test holds the verifier mid-step so the cancel provably lands
    inside the step, then asserts the commit never happens.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.hold = threading.Event()
        self.hold_released = threading.Event()
        self.provider = _HoldingProvider(
            {
                "planner": {
                    "steps": [{
                        "title": "Step 1", "description": "create x.txt",
                        "suggested_paths": ["x.txt"],
                    }]
                },
                "fixer": {"files": [{"path": "x.txt", "action": "create", "content": "hi\n"}]},
                "verifier": {"argv": None, "verdict": "pass", "explanation": "held then released"},
                "critic": {"decision": "approve", "reasons": []},
                "scribe": {"summary": "did it", "commit_message": "feat: x"},
            },
            hold_role="verifier",
            hold_event=self.hold,
            released_event=self.hold_released,
        )
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(),
            git=_RecordingGit(), laya=_SkippedGate(),
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def asyncTearDown(self) -> None:
        self.hold.set()  # never leave a parked call parked
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_cancel_during_verifier_stops_review_and_commit(self) -> None:
        await self.executor.run_planning(self.goal.id)
        step = self.goals.steps(self.goal.id)[0]
        self.goals.update_status(self.goal.id, self.goal.version + 1, "RUNNING")

        run_task = asyncio.create_task(self.executor.run_step(self.goal.id, step.id))

        # The fixer finishes (x.txt exists); the verifier call is now parked.
        deadline = time.time() + 5
        while not (self.root / "x.txt").exists() and time.time() < deadline:
            await asyncio.sleep(0.01)
        self.assertTrue(self.hold_released.wait(5), "verifier call never parked")

        # Cancel while the verifier is genuinely mid-flight.
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "CANCELLED")
        self.hold.set()
        await asyncio.wait_for(run_task, timeout=5)

        self.assertEqual(self.goals.get(self.goal.id).status, "CANCELLED",
                         "a cancel mid-step must not be overwritten")
        self.assertEqual([], self.provider.critic_calls,
                         "the critic must not judge a cancelled step")
        self.assertEqual([], self.provider.scribe_calls,
                         "the scribe must not run for a cancelled step")
        self.assertEqual([], self.executor.git.commits,  # type: ignore[attr-defined]
                         "nothing may be committed after a cancel")
        # The fixer's work itself stays (documented mid-run-cancel semantics):
        self.assertTrue((self.root / "x.txt").exists())

    async def test_cancel_landing_during_critic_still_blocks_the_commit(self) -> None:
        """The belt-and-braces guard: a cancel that slips in *after* the critic
        approved must still be caught by the check right before the commit."""
        self.provider.hold_role = "critic"
        await self.executor.run_planning(self.goal.id)
        step = self.goals.steps(self.goal.id)[0]
        self.goals.update_status(self.goal.id, self.goal.version + 1, "RUNNING")

        run_task = asyncio.create_task(self.executor.run_step(self.goal.id, step.id))
        deadline = time.time() + 5
        while not (self.root / "x.txt").exists() and time.time() < deadline:
            await asyncio.sleep(0.01)
        self.assertTrue(self.hold_released.wait(5), "critic call never parked")

        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "CANCELLED")
        self.hold.set()
        await asyncio.wait_for(run_task, timeout=5)

        self.assertEqual(self.goals.get(self.goal.id).status, "CANCELLED")
        self.assertEqual([], self.provider.scribe_calls,
                         "the scribe must not run for a cancelled step")
        self.assertEqual([], self.executor.git.commits,  # type: ignore[attr-defined]
                         "nothing may be committed after a cancel, even from the late guard")


class _HoldingProvider(BaseProvider):
    """Scripted replies, with one role's call parked on an Event mid-flight.

    Parking (rather than delaying) is what makes the cancel provably land
    *inside* the step: the test cancels only after the held call has started.
    """

    def __init__(self, others: dict[str, Any], hold_role: str, hold_event: threading.Event,
                 released_event: threading.Event):
        self.others = others
        self.hold_role = hold_role
        self.hold_event = hold_event
        self.released_event = released_event
        self.critic_calls: list[str] = []
        self.scribe_calls: list[str] = []
        self.calls: list[tuple[str, str]] = []

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
        role = next(
            (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
        )
        self.calls.append((role, user_prompt))
        if role == "critic":
            self.critic_calls.append(user_prompt)
        if role == "scribe":
            self.scribe_calls.append(user_prompt)
        if role == self.hold_role:
            self.released_event.set()
            await asyncio.get_event_loop().run_in_executor(None, self.hold_event.wait)
        return json.dumps(self.others.get(role, {}))


class _RecordingGit(GitService):
    """Stands in for git so the test can assert the commit never happens."""

    def __init__(self) -> None:
        self.commits: list[tuple[str, list[str]]] = []

    def is_git_repo(self, root_path: str) -> bool:
        return True

    def commit(
        self, root_path: str, message: str, paths: list[str], *args: Any, **kwargs: Any
    ) -> str | None:
        self.commits.append((message, paths))
        return "abcd1234"


class TestFixerSelfContinuation(unittest.IsolatedAsyncioTestCase):
    """The fixer may declare a change multi-stage (needs_another_pass).

    One reply cannot always finish a step: a config file in this pass, the code
    that reads it in the next. The fixer asks by returning needs_another_pass
    alongside its files; the engine grants the pass BEFORE verifying — verifying
    an admittedly half-written change only burns a test run the fixer already
    said it cannot pass — bounded by MAX_FIXER_PASSES, visible as fixer_pass
    events, and cancel-aware like every other grant of extra work.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.provider = _ScriptedProvider(
            {
                "planner": {
                    "steps": [{
                        "title": "Step 1", "description": "config then code",
                        "suggested_paths": ["settings.conf"],
                    }]
                },
                "critic": {"decision": "approve", "reasons": []},
                "scribe": {"summary": "did it", "commit_message": "feat: staged"},
            }
        )
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _script_fixer(self, replies: list[dict[str, Any]]) -> None:
        self.provider.others["fixer"] = replies

        async def completing(
            system_prompt: str, user_prompt: str, model: str,
            temperature: float, max_tokens: int,
        ) -> str:
            role = next(
                (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
            )
            self.provider.calls.append((role, user_prompt))
            if role == "fixer" and self.provider.others["fixer"]:
                return json.dumps(self.provider.others["fixer"].pop(0))
            if role == "verifier" and self.provider.verifier_replies:
                return json.dumps(self.provider.verifier_replies.pop(0))
            return json.dumps(self.provider.others.get(role, {}))

        self.provider.complete = completing  # type: ignore[method-assign]

    def _events(self, type_: str) -> list[Any]:
        return [e for e in self.goals.events_after(self.goal.id, 0) if e.type == type_]

    async def _plan_and_run(self) -> None:
        await self.executor.run_planning(self.goal.id)
        self.goals.update_status(self.goal.id, self.goals.get(self.goal.id).version, "RUNNING")
        await self.executor.run_step(self.goal.id, self.goals.steps(self.goal.id)[0].id)

    async def test_two_staged_passes_finish_the_step(self) -> None:
        self._script_fixer([
            {"files": [{"path": "settings.conf", "action": "create", "content": "mode=fast\n"}],
             "needs_another_pass": True},
            {"files": [{"path": "reader.py", "action": "create", "content": "print(open('settings.conf').read())\n"}]},
        ])
        self.provider.verifier_replies = [
            {"argv": None, "verdict": "pass", "explanation": "nothing to run"},
        ]

        await self._plan_and_run()

        passes = self._events("fixer_pass")
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0].payload["attempt"], 1)
        self.assertEqual(passes[0].payload["passes_left"], 1)
        # Both files exist: the pass actually ran, not just announced.
        self.assertTrue((self.root / "settings.conf").exists())
        self.assertTrue((self.root / "reader.py").exists())
        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "COMPLETED")
        fixer_prompts = [p for r, p in self.provider.calls if r == "fixer"]
        self.assertEqual(len(fixer_prompts), 2, "pass 2 must actually call the fixer")

    async def test_the_pass_bound_holds(self) -> None:
        self._script_fixer([
            {"files": [{"path": f"f{i}.txt", "action": "create", "content": "x\n"}],
             "needs_another_pass": True}
            for i in range(5)
        ])
        self.provider.verifier_replies = [
            {"argv": None, "verdict": "pass", "explanation": "nothing to run"},
        ]

        await self._plan_and_run()

        passes = self._events("fixer_pass")
        self.assertEqual(len(passes), 2, "MAX_FIXER_PASSES grants two, no more")
        self.assertEqual(passes[-1].payload["passes_left"], 0)
        fixer_calls = [p for r, p in self.provider.calls if r == "fixer"]
        self.assertEqual(len(fixer_calls), 3, "1 initial + 2 granted passes")
        # The verifier ran once, after the LAST pass — no verify of half work.
        self.assertEqual(len(self.provider.verifier_calls()), 1)
        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "COMPLETED")

    async def test_needs_another_pass_without_files_is_a_contract_error(self) -> None:
        self._script_fixer([{"files": [], "needs_another_pass": True}])

        await self._plan_and_run()

        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "FAILED")
        errors = [e.payload for e in self._events("error")]
        self.assertTrue(any("needs_another_pass requires files" in str(e.get("message")) for e in errors))

class TestPlannerConsult(unittest.IsolatedAsyncioTestCase):
    """The planner may reopen the librarian once while planning.

    The evidence pack is frozen when the librarian says `enough` — but a pack
    built before the goal's real question was asked can miss exactly what a
    step needs. The planner used to plan a guess; now it may make ONE bounded
    follow-up through the same read-only machinery, merge the answer, and plan
    next round. A second consult is a contract error, and a planner that plans
    immediately never enters the loop at all.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.conn = connect(self.root / "t.db")
        self.provider = _ScriptedProvider({})
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _planner_prompts(self) -> list[str]:
        return [p for r, p in self.provider.calls if r == "planner"]

    def _script_planner(self, replies: list[dict[str, Any]]) -> None:
        # _ScriptedProvider serves each role one static reply from `others`, so
        # successive planner rounds need a queue wrapped over it. The librarian
        # answers {"enough": true} immediately: empty evidence is exactly the
        # blind-spot situation a consult exists for.
        queue = list(replies)
        original = self.provider.complete

        async def completing(
            system_prompt: str, user_prompt: str, model: str,
            temperature: float, max_tokens: int,
        ) -> str:
            role = next(
                (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
            )
            self.provider.calls.append((role, user_prompt))
            if role == "planner" and queue:
                return json.dumps(queue.pop(0))
            return await original(system_prompt, user_prompt, model, temperature, max_tokens)

        self.provider.complete = completing  # type: ignore[method-assign]
        self.provider.others["librarian"] = {"enough": True}

    def _events(self, type_: str) -> list[Any]:
        return [e for e in self.goals.events_after(self.goal.id, 0) if e.type == type_]

    async def test_a_consult_reaches_round_two_with_the_material(self) -> None:
        self._script_planner([
            {"consult": {"reads": ["src/core.py"]}},
            {"steps": [{"title": "S1", "description": "uses VALUE", "suggested_paths": ["src/core.py"]}]},
        ])

        await self.executor.run_planning(self.goal.id)

        planner_calls = self._planner_prompts()
        self.assertEqual(len(planner_calls), 2, "consult round + planning round")
        self.assertIn("VALUE = 1", planner_calls[1], "the consulted material must reach round 2")
        self.assertIn("The librarian answered your follow-up", planner_calls[1])
        consults = self._events("plan_consult")
        self.assertEqual(len(consults), 1)
        self.assertEqual(consults[0].payload["refused"], 0)
        # The plan landed: the goal is PENDING with its step inserted.
        self.assertEqual(self.goals.get(self.goal.id).status, "PENDING")
        self.assertEqual(len(self.goals.steps(self.goal.id)), 1)

    async def test_a_refused_request_is_information_not_a_failure(self) -> None:
        self._script_planner([
            {"consult": {"reads": ["../../etc/passwd"]}},
            {"steps": [{"title": "S1", "description": "d", "suggested_paths": []}]},
        ])

        await self.executor.run_planning(self.goal.id)

        planner_calls = self._planner_prompts()
        self.assertEqual(len(planner_calls), 2)
        self.assertIn("refused", planner_calls[1])
        consults = self._events("plan_consult")
        self.assertEqual(consults[0].payload["refused"], 1)
        self.assertEqual(self.goals.get(self.goal.id).status, "PENDING")

    async def test_a_second_consult_is_refused(self) -> None:
        self._script_planner([
            {"consult": {"reads": ["src/core.py"]}},
            {"consult": {"reads": ["src/core.py"]}},
        ])

        await self.executor.run_planning(self.goal.id)

        planner_calls = self._planner_prompts()
        self.assertEqual(len(planner_calls), 2, "initial + one granted consult")
        self.assertEqual(self.goals.get(self.goal.id).status, "FAILED")
        errors = [e.payload for e in self._events("error")]
        self.assertTrue(any("last allowed follow-up" in str(e.get("message")) for e in errors))

    async def test_a_direct_plan_never_consults(self) -> None:
        self._script_planner([
            {"steps": [{"title": "S1", "description": "d", "suggested_paths": []}]},
        ])

        await self.executor.run_planning(self.goal.id)

        self.assertEqual(len(self._planner_prompts()), 1)
        self.assertEqual(self._events("plan_consult"), [])
        self.assertEqual(self.goals.get(self.goal.id).status, "PENDING")

    async def test_a_consult_with_steps_is_a_contract_error(self) -> None:
        """steps and consult are mutually exclusive — a reply claiming both is
        not a plan and not a question; it is a malformed reply."""
        self._script_planner([
            {"steps": [{"title": "S1", "description": "d", "suggested_paths": []}],
             "consult": {"reads": ["src/core.py"]}},
        ])

        await self.executor.run_planning(self.goal.id)

        # steps present => parsed as the plan; the stray consult key is ignored.
        self.assertEqual(self.goals.get(self.goal.id).status, "PENDING")
        self.assertEqual(len(self.goals.steps(self.goal.id)), 1)
        self.assertEqual(self._events("plan_consult"), [])

class TestUsageAccounting(unittest.IsolatedAsyncioTestCase):
    """Token usage from provider responses becomes attributable events.

    Every provider names usage differently and the raw counts used to be
    dropped on the floor. Providers now report through an optional sink; the
    orchestrator attaches one per call, labeled with the role and the target
    that actually served it (so a fallback's usage is billed to the fallback).
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.provider = _ScriptedProvider({
            "planner": {"steps": [{"title": "S1", "description": "d", "suggested_paths": []}]},
            "critic": {"decision": "approve", "reasons": []},
            "scribe": {"summary": "s", "commit_message": "feat: x"},
        })
        # Report ollama-style usage after each scripted completion.
        scripted = self.provider
        real_complete = scripted.complete

        async def completing(
            system_prompt: str, user_prompt: str, model: str,
            temperature: float, max_tokens: int,
        ) -> str:
            text = await real_complete(system_prompt, user_prompt, model, temperature, max_tokens)
            scripted._report_usage("ollama", {"response": text, "prompt_eval_count": 10, "eval_count": 5})
            return text

        scripted.complete = completing  # type: ignore[method-assign]
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _usage_events(self) -> list[Event]:
        return [e for e in self.goals.events_after(self.goal.id, 0) if e.type == "usage"]

    async def test_every_role_call_reports_attributed_usage(self) -> None:
        await self.executor.run_planning(self.goal.id)

        events = self._usage_events()
        # Librarian rounds + planner (critic/scribe run per step, not in planning).
        roles = {e.payload["role"] for e in events}
        self.assertIn("librarian", roles)
        self.assertIn("planner", roles)
        for e in events:
            self.assertEqual(e.payload["provider"], "ollama")
            self.assertEqual(e.payload["model"], "test-model")
            self.assertEqual(e.payload["input_tokens"], 10)
            self.assertEqual(e.payload["output_tokens"], 5)

    async def test_a_provider_without_usage_reports_nothing(self) -> None:
        # Strip the reporting wrapper: a silent server must not produce events.
        complete = self.provider.complete
        unwrapped = getattr(complete, "__wrapped__", complete)
        self.provider.complete = unwrapped  # type: ignore[method-assign]
        self.provider._report_usage = lambda *a, **k: None  # type: ignore[method-assign]


        await self.executor.run_planning(self.goal.id)
        self.assertEqual(self._usage_events(), [])

    async def test_normalize_usage_handles_every_provider_dialect(self) -> None:
        from engine.providers import normalize_usage
        self.assertEqual(
            normalize_usage("anthropic", {"usage": {"input_tokens": 3, "output_tokens": 4}}),
            {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        )
        self.assertEqual(
            normalize_usage("openai_compat", {"usage": {"prompt_tokens": 3, "completion_tokens": 4}}),
            {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        )
        self.assertEqual(
            normalize_usage("google", {"usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4}}),
            {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        )
        self.assertEqual(
            normalize_usage("ollama", {"prompt_eval_count": 3, "eval_count": 4}),
            {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        )
        # No usage at all is normal for some local servers.
        self.assertIsNone(normalize_usage("ollama", {"response": "hi"}))
        self.assertIsNone(normalize_usage("openai_compat", {"usage": {"prompt_tokens": "x"}}))

class TestParallelStepExecution(unittest.IsolatedAsyncioTestCase):
    """Opt-in per-goal parallelism for independent steps.

    A batch is the longest prefix of steps whose suggested_paths are pairwise
    disjoint; the batch runs concurrently while the sandbox and git are
    serialized. Every scenario drives the production loop (app._run_steps),
    not a hand-rolled re-implementation, so the tests exercise the same code
    path a real goal takes.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.provider = _ScriptedProvider({})
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def _drive(self) -> float:
        """Run the goal exactly like the API layer: start (RUNNING) then _run_steps.
        Returns elapsed."""
        from engine.app import _run_steps

        class _App:
            state: Any

        app = _App()
        app.state = _App()
        app.state.goals = self.goals
        app.state.executor = self.executor
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        started = time.monotonic()
        await _run_steps(app, self.goal.id)  # type: ignore[arg-type]
        return time.monotonic() - started

    def _script_parallel(self, delays: dict[str, float] | None = None,
                         fail_on: str | None = None, n_steps: int = 2) -> None:
        """Planner produces n_steps path-disjoint steps (a..z); the fixer
        identifies its step from the prompt's `Step: {title}` line (never from
        suggested-path contents, which don't exist yet for create steps) and
        sleeps before writing, so overlap is measurable by wall clock.
        fail_on: make that step's fixer raise. Concurrent fixers are also
        tracked live: self.concurrent_peak records the max number in flight."""
        delays = delays or {}
        names = [chr(ord("a") + i) for i in range(n_steps)]
        self.provider.others["planner"] = {"steps": [
            {"title": n, "description": f"write {n}", "suggested_paths": [f"{n}.txt"]}
            for n in names
        ]}
        self._fixers_in_flight = 0
        self._concurrent_peak = 0
        original = self.provider.complete

        async def completing(
            system_prompt: str, user_prompt: str, model: str,
            temperature: float, max_tokens: int,
        ) -> str:
            role = next(
                (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
            )
            self.provider.calls.append((role, user_prompt))
            if role == "fixer":
                m = re.search(r"^Step: (\S+)$", user_prompt, re.MULTILINE)
                target = m.group(1) if m else "x.txt"
                if fail_on and target == fail_on:
                    from engine.providers import ProviderError as PE
                    raise PE("provider_unreachable", f"boom on {target}")
                self._fixers_in_flight += 1
                self._concurrent_peak = max(self._concurrent_peak, self._fixers_in_flight)
                try:
                    await asyncio.sleep(delays.get(target, 0.0))
                finally:
                    self._fixers_in_flight -= 1
                return json.dumps({"files": [{
                    "path": f"{target}.txt", "action": "create", "content": f"{target} body\n",
                }]})
            if role == "verifier":
                return json.dumps({"argv": None, "verdict": "pass", "explanation": "trivial"})
            if role == "critic":
                return json.dumps({"decision": "approve", "reasons": []})
            if role == "scribe":
                return json.dumps({"summary": "ok", "commit_message": "feat: ok"})
            # Planner (and any role this wrapper does not script) answers from
            # the underlying scripted map — dropping a role on the floor here
            # makes planning fail silently and the goal "complete" with no steps.
            return await original(system_prompt, user_prompt, model, temperature, max_tokens)

        self.provider.complete = completing  # type: ignore[method-assign]

    async def test_independent_batch_rules(self) -> None:
        from engine.models import PlanStep

        def mk(i: int, title: str, paths: list[str]) -> PlanStep:
            return PlanStep(
                id=str(i), goal_id="g", ordinal=i, title=title, description="d",
                status="PENDING", suggested_paths=paths,
            )
        batch = self.executor._independent_batch([
            mk(0, "a", ["a.txt"]),
            mk(1, "b", ["b.txt"]),
            mk(2, "c", ["a.txt"]),   # collides with step 0
        ])
        self.assertEqual([x.title for x in batch], ["a", "b"])
        alone = self.executor._independent_batch([mk(0, "vague", [])])
        self.assertEqual(len(alone), 0, "a step with no paths never batches — it runs alone")
        solo = self.executor._independent_batch([mk(0, "solo", ["s.txt"])])
        self.assertEqual([x.title for x in solo], ["solo"])

    async def test_parallel_goal_really_overlaps_and_completes(self) -> None:
        self.goals.set_parallel(self.goal.id, True)
        self._script_parallel(delays={"a": 0.6, "b": 0.6})
        await self.executor.run_planning(self.goal.id)
        elapsed = await self._drive()
        g = self.goals.get(self.goal.id)
        self.assertEqual(g.status, "COMPLETED", f"goal status: {g.status}")
        # Sequential would be ~1.2s of fixer sleeps; overlapped, ~0.6s.
        self.assertLess(elapsed, 1.05, f"steps did not overlap (took {elapsed:.2f}s)")
        for name in ("a.txt", "b.txt"):
            self.assertTrue((self.root / name).exists(), f"{name} missing")

    async def test_sequential_goal_stays_sequential(self) -> None:
        # No parallel flag: the same driver must take at least the SUM of the sleeps.
        self._script_parallel(delays={"a": 0.4, "b": 0.4})
        await self.executor.run_planning(self.goal.id)
        elapsed = await self._drive()
        g = self.goals.get(self.goal.id)
        self.assertEqual(g.status, "COMPLETED", f"goal status: {g.status}")
        self.assertGreaterEqual(elapsed, 0.8, f"sequential steps unexpectedly overlapped ({elapsed:.2f}s)")

    async def test_parallel_failure_fails_the_goal_after_join(self) -> None:
        # run_step absorbs a ProviderError into _fail (step FAILED, goal FAILED)
        # rather than raising; _run_parallel must still join the healthy sibling
        # before the driver observes the failure.
        self.goals.set_parallel(self.goal.id, True)
        self._script_parallel(delays={"a": 0.4}, fail_on="b")
        await self.executor.run_planning(self.goal.id)
        await self._drive()
        g = self.goals.get(self.goal.id)
        self.assertEqual(g.status, "FAILED", f"expected FAILED, got {g.status}")
        steps = self.goals.steps(self.goal.id)
        statuses = {s.title: s.status for s in steps}
        self.assertEqual(statuses["b"], "FAILED", f"statuses: {statuses}")
        # The healthy sibling a.txt (0.4s of work) must have been allowed to
        # finish: its file exists, proving the batch joined rather than
        # abandoning it the instant b failed.
        self.assertTrue((self.root / "a.txt").exists(), "healthy sibling was abandoned")

    async def test_parallel_width_caps_concurrency_in_waves(self) -> None:
        """A batch wider than the configured width runs in waves, not all at once.

        5 disjoint steps with a width of 2: the peak number of fixers in
        flight must be 2 (never 5), yet the goal still completes everything —
        the cap throttles concurrency, it does not serialize the plan.
        """
        from engine.executor import _env_parallel_width
        os.environ["CODIFY_PARALLEL_WIDTH"] = "2"
        try:
            self.assertEqual(_env_parallel_width(), 2)
            self.goals.set_parallel(self.goal.id, True)
            self._script_parallel(delays={n: 0.3 for n in "abcde"}, n_steps=5)
            await self.executor.run_planning(self.goal.id)
            elapsed = await self._drive()
        finally:
            os.environ.pop("CODIFY_PARALLEL_WIDTH", None)

        g = self.goals.get(self.goal.id)
        self.assertEqual(g.status, "COMPLETED", f"goal status: {g.status}")
        self.assertEqual(
            self._concurrent_peak, 2,
            f"expected waves of 2, saw peak concurrency {self._concurrent_peak}",
        )
        # Waves of 2 over 5 steps with 0.3s sleeps: ~3 waves ~= 0.9s. If the
        # cap were ignored (all 5 at once) it would be ~0.3s.
        self.assertGreaterEqual(elapsed, 0.85, f"waves not enforced ({elapsed:.2f}s)")
        for name in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
            self.assertTrue((self.root / name).exists(), f"{name} missing")
        # Every step ran exactly once.
        fixer_calls = [c for c in self.provider.calls if c[0] == "fixer"]
        self.assertEqual(len(fixer_calls), 5)

    async def test_parallel_width_is_clamped(self) -> None:
        """A nonsense or extreme env value cannot disable the bound."""
        from engine.executor import _env_parallel_width, DEFAULT_PARALLEL_WIDTH
        self.assertEqual(_env_parallel_width() or DEFAULT_PARALLEL_WIDTH, DEFAULT_PARALLEL_WIDTH)
        for raw, expected in (("0", 1), ("-3", 1), ("99", 16), ("abc", None)):
            os.environ["CODIFY_PARALLEL_WIDTH"] = raw
            self.assertEqual(_env_parallel_width(), expected, f"raw={raw!r}")
        os.environ.pop("CODIFY_PARALLEL_WIDTH", None)

    async def test_parallel_width_reads_the_persisted_setting(self) -> None:
        """Without an env override, the width comes from the settings store.

        Priority: env (operator override) > persisted setting > default. Read
        per batch, so a settings change lands on the next wave with no restart.
        """
        from engine.services import SettingsService

        self.assertEqual(self.executor._parallel_width(), 4, "no setting → default")

        settings = SettingsService(self.conn)
        self.executor.settings = settings
        settings.set_int("parallel_width", 3)
        self.assertEqual(self.executor._parallel_width(), 3, "persisted setting wins over default")

        # The env var is the operator's explicit override of the field.
        os.environ["CODIFY_PARALLEL_WIDTH"] = "2"
        try:
            self.assertEqual(self.executor._parallel_width(), 2, "env overrides the setting")
        finally:
            os.environ.pop("CODIFY_PARALLEL_WIDTH", None)

        # Changing the setting again is picked up immediately (same instance).
        settings.set_int("parallel_width", 6)
        self.assertEqual(self.executor._parallel_width(), 6, "re-read per batch")

    async def test_batch_refused_when_paths_change_between_batching_and_dispatch(self) -> None:
        """The dispatch re-check catches a plan edited after batching.

        Batch two disjoint steps, then (between batching and gather) rewrite
        one step's stored paths to collide with its sibling. _run_parallel
        must refuse with batch_no_longer_disjoint — not race the two onto the
        same file — and the driver must recover by re-batching.
        """
        self.goals.set_parallel(self.goal.id, True)
        self._script_parallel(delays={"a": 0.15, "b": 0.15})
        await self.executor.run_planning(self.goal.id)
        steps = self.goals.steps(self.goal.id)
        self.assertEqual(len(steps), 2)

        # Simulate the edit landing after _independent_batch proved disjointness
        # but before the gather: write the collision straight into the store.
        plan_row = self.conn.execute(
            "SELECT id FROM plan_steps WHERE goal_id = ? ORDER BY ordinal LIMIT 1",
            (self.goal.id,),
        ).fetchone()
        self.conn.execute(
            "UPDATE plan_steps SET suggested_paths = ? WHERE id = ?",
            (json.dumps(["b.txt"]), plan_row["id"]),
        )
        self.conn.commit()

        # Direct dispatch (what the driver does) must refuse.
        from engine.services import ApiError
        with self.assertRaises(ApiError) as ctx:
            await self.executor._run_parallel(self.goal.id, steps, None)
        self.assertEqual(ctx.exception.code, "batch_no_longer_disjoint")

        # And the files must NOT have been written by the refused batch.
        self.assertFalse((self.root / "a.txt").exists())
        self.assertFalse((self.root / "b.txt").exists())

    async def test_driver_recovers_by_rebatching_after_a_refused_batch(self) -> None:
        """A refused batch is not a failure: the driver re-batches from the store.

        Same collision as above, but driven through the production loop. The
        refused pass logs a warning and re-batches; the colliding steps then
        run one at a time and the goal still COMPLETED.
        """
        self.goals.set_parallel(self.goal.id, True)
        self._script_parallel(delays={"a": 0.1, "b": 0.1})
        await self.executor.run_planning(self.goal.id)
        plan_row = self.conn.execute(
            "SELECT id FROM plan_steps WHERE goal_id = ? ORDER BY ordinal LIMIT 1",
            (self.goal.id,),
        ).fetchone()
        self.conn.execute(
            "UPDATE plan_steps SET suggested_paths = ? WHERE id = ?",
            (json.dumps(["b.txt"]), plan_row["id"]),
        )
        self.conn.commit()

        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        await self._drive()
        g = self.goals.get(self.goal.id)
        self.assertEqual(g.status, "COMPLETED", f"status: {g.status}")
        # Both files written (sequentially after the refusal), exactly once each.
        self.assertTrue((self.root / "a.txt").exists())
        self.assertTrue((self.root / "b.txt").exists())
        fixer_calls = [c for c in self.provider.calls if c[0] == "fixer"]
        self.assertEqual(len(fixer_calls), 2)

    async def test_retry_refused_when_edited_paths_collide_with_unfinished_step(self) -> None:
        """retry_step re-proves disjointness against unfinished siblings.

        Two parallel steps, one failed. Edit the failed step's paths to collide
        with the sibling (allowed while paused/failed), then retry: refused
        with retry_collides_with_running rather than racing the sibling.
        """
        from engine.services import ApiError
        self.goals.set_parallel(self.goal.id, True)
        self._script_parallel(delays={"a": 0.1}, fail_on="a")
        await self.executor.run_planning(self.goal.id)
        steps = {s.title: s for s in self.goals.steps(self.goal.id)}
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        await self._drive()
        g = self.goals.get(self.goal.id)
        self.assertEqual(g.status, "FAILED")

        # The failure left the goal FAILED; edit a's paths to collide with b.
        # update_step refuses edits unless every step is PENDING, so mirror the
        # store the way a real edit-after-crash could: straight SQL, which the
        # retry check must not trust around.
        self.conn.execute(
            "UPDATE plan_steps SET suggested_paths = ? WHERE id = ?",
            (json.dumps(["b.txt"]), steps["a"].id),
        )
        self.conn.commit()

        with self.assertRaises(ApiError) as ctx:
            await self.executor.retry_step(self.goal.id, steps["a"].id, g.version)
        self.assertEqual(ctx.exception.code, "retry_collides_with_running")

    async def test_apply_batches_on_proposed_paths_not_plan_guesses(self) -> None:
        """apply_goal proves disjointness from the files that will be written.

        A dry run stores proposals for paths the plan never named (or stopped
        naming after an edit); batching on suggested_paths there would prove
        the wrong thing. Two steps whose plans collide on paper but whose
        proposals are disjoint must still batch; and vice versa.
        """
        self.goals.set_parallel(self.goal.id, True)
        self._script_parallel(n_steps=2)
        await self.executor.run_planning(self.goal.id)
        steps = self.goals.steps(self.goal.id)

        # _independent_batch with a paths_for override reads from the override.
        proposals = {
            steps[0].id: [{"path": "shared.txt", "action": "create", "content": "x"}],
            steps[1].id: [{"path": "other.txt", "action": "create", "content": "y"}],
        }
        batch = self.executor._independent_batch(
            steps, paths_for=lambda sid: {f["path"] for f in proposals[sid]}
        )
        self.assertEqual(len(batch), 2, "disjoint proposals must batch despite plan paths")

        # And colliding proposals must not, even with disjoint plan paths.
        proposals[steps[1].id] = [{"path": "shared.txt", "action": "create", "content": "z"}]
        batch = self.executor._independent_batch(
            steps, paths_for=lambda sid: {f["path"] for f in proposals[sid]}
        )
        self.assertEqual(len(batch), 1, "colliding proposals must never batch")

    async def test_unprovable_head_step_runs_alone(self) -> None:
        """A head step with no provable paths runs alone, not crashes.

        _independent_batch returns [] for a step with no paths (its contract:
        "it runs alone"). Both drivers used to do batch[0] on that empty list —
        IndexError, swallowed by _spawn into `internal_error: list index out
        of range`, and a goal that died with nothing on screen explaining why.
        """
        from engine.models import PlanStep

        def mk(i: int, title: str, paths: list[str]) -> PlanStep:
            return PlanStep(
                id=str(i), goal_id="g", ordinal=i, title=title, description="d",
                status="PENDING", suggested_paths=paths,
            )
        self.assertEqual(self.executor._independent_batch([mk(0, "vague", [])]), [])

        self.goals.set_parallel(self.goal.id, True)
        self._script_parallel(n_steps=2)
        # Make the head step provably unbatchable: no suggested paths at all.
        await self.executor.run_planning(self.goal.id)
        self.conn.execute(
            "UPDATE plan_steps SET suggested_paths='[]' WHERE ordinal = 0 AND goal_id = ?",
            (self.goal.id,),
        )
        self.conn.commit()

        elapsed = await self._drive()
        g = self.goals.get(self.goal.id)
        self.assertEqual(g.status, "COMPLETED", f"goal status: {g.status}")
        statuses = {s.title: s.status for s in self.goals.steps(self.goal.id)}
        self.assertEqual(statuses, {"a": "COMPLETED", "b": "COMPLETED"}, f"statuses: {statuses}")
        del elapsed  # only completion matters here

    async def test_second_driver_claim_is_refused(self) -> None:
        """A retry landing mid-run must not spawn a second driver loop.

        start and retry each spawn _run_steps with no mutual exclusion: the
        second driver re-read the same unfinished steps and ran fixers on the
        same files concurrently — the torn write the batching gate exists to
        prevent. The executor's claim/release guard makes the second claim a
        no-op that just joins nothing.
        """
        self.goals.set_parallel(self.goal.id, True)
        self._script_parallel(delays={"a": 0.35, "b": 0.35})
        await self.executor.run_planning(self.goal.id)

        from engine.app import _run_steps

        class _App:
            state: Any

        app = _App()
        app.state = _App()
        app.state.goals = self.goals
        app.state.executor = self.executor
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")

        first = asyncio.create_task(_run_steps(app, self.goal.id))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)  # let the first driver claim and enter its wave
        started = time.monotonic()
        # second claim: must return promptly, run nothing
        await _run_steps(app, self.goal.id)  # type: ignore[arg-type]  # noqa: E501
        second_elapsed = time.monotonic() - started
        await first

        self.assertLess(
            second_elapsed, 0.2,
            f"second driver ran steps instead of yielding ({second_elapsed:.2f}s)",
        )
        self.assertEqual(self.goals.get(self.goal.id).status, "COMPLETED")
        self.assertFalse(self.executor._drivers, "driver claim leaked after completion")

    async def test_apply_recheck_uses_the_batchers_proof(self) -> None:
        """apply's dispatch re-check must prove the same footprint it batched on.

        apply_goal batches on stored-proposal paths but _run_parallel used to
        re-check suggested_paths. Two steps whose paper paths collide but whose
        proposals are disjoint were refused at dispatch, re-batched (still
        'disjoint' per the batcher), refused again — an infinite loop: apply
        never completed and never ran a step. Both halves now share one proof.
        """
        self.goals.set_parallel(self.goal.id, True)
        self.goals.set_dry_run(self.goal.id, True)
        self._script_parallel(n_steps=2)
        await self.executor.run_planning(self.goal.id)

        # Both steps' PLANS collide on shared.txt (as if the plan was edited
        # that way after planning), so the dry run itself cannot batch — fine,
        # it runs sequentially and stores disjoint proposals (a.txt, b.txt).
        self.conn.execute(
            "UPDATE plan_steps SET suggested_paths=? WHERE goal_id = ?",
            (json.dumps(["shared.txt"]), self.goal.id),
        )
        self.conn.commit()

        # Complete the dry run so apply's terminal-status guard passes.
        elapsed = await self._drive()
        del elapsed
        self.assertEqual(self.goals.get(self.goal.id).status, "COMPLETED")
        stored = self.conn.execute(
            "SELECT DISTINCT step_id FROM proposed_files WHERE goal_id = ?", (self.goal.id,)
        ).fetchall()
        self.assertEqual(len(stored), 2, f"both steps must hold proposals: {stored}")

        # Reaching dispatch proves the batcher accepted the plan-collision
        # (batching on proposals); completing proves the re-check did too.
        result = await self.executor.apply_goal(self.goal.id)
        self.assertEqual(result.status, "COMPLETED", f"apply status: {result.status}")
        for name in ("a.txt", "b.txt"):
            self.assertTrue((self.root / name).exists(), f"{name} missing after apply")
        self.assertFalse((self.root / "shared.txt").exists())
        refused = [e for e in self.goals.events_after(self.goal.id, 0)
                   if e.type == "log" and "batch refused" in e.payload.get("message", "")]
        self.assertEqual(refused, [], "apply hit a refuse/re-batch loop")

    async def test_cancel_fails_the_batch_promptly(self) -> None:
        self.goals.set_parallel(self.goal.id, True)
        self._script_parallel(delays={"a": 0.35, "b": 0.35})
        await self.executor.run_planning(self.goal.id)
        remaining = [s for s in self.goals.steps(self.goal.id) if s.status != "COMPLETED"]
        self.assertEqual(len(remaining), 2)

        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            g = self.goals.get(self.goal.id)
            self.goals.update_status(self.goal.id, g.version, "CANCELLED")

        started = time.monotonic()
        await asyncio.gather(cancel_soon(), self._drive())
        elapsed = time.monotonic() - started
        g = self.goals.get(self.goal.id)
        self.assertEqual(g.status, "CANCELLED", f"expected CANCELLED, got {g.status}")
        # A cancel must stop the batch promptly, not wait out the sleeps.
        self.assertLess(elapsed, 1.0, f"cancel did not stop the batch promptly ({elapsed:.2f}s)")


class TestModelDeltaStreaming(unittest.IsolatedAsyncioTestCase):
    """Streamed replies reach the chat as coalesced snapshot events.

    The WebSocket layer polls the event store, so tokens must flow as events;
    coalescing to *snapshots* (full text so far, ~400ms) keeps the store light
    and makes any reconnect self-healing — the newest snapshot is the truth,
    no fragment replay needed.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "a.py").write_text("value = 1\n", encoding="utf-8")
        self.conn = connect(self.root / "t.db")
        self.provider = _ScriptedProvider(
            {
                "planner": {
                    "steps": [{
                        "title": "Step 1", "description": "bump value",
                        "suggested_paths": ["a.py"],
                    }]
                },
                "fixer": {
                    "files": [{"path": "a.py", "action": "create", "content": "value = 2\n"}]
                },
                "scribe": {"summary": "did it", "commit_message": "feat: bump"},
            }
        )
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))
        self.provider.verifier_replies = [
            {"argv": None, "verdict": "pass", "explanation": "nothing to run"},
        ]

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _deltas(self, goal_id: str) -> list[Any]:
        return [
            e for e in self.goals.events_after(goal_id, 0) if e.type == "model_delta"
        ]

    async def test_streaming_role_emits_snapshots_and_a_final_event(self) -> None:
        # Script the fixer to stream: on_delta fires as the "model" produces.
        original = self.provider.complete

        async def streaming(
            system_prompt: str, user_prompt: str, model: str,
            temperature: float, max_tokens: int,
        ) -> str:
            role = next(
                (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
            )
            self.provider.calls.append((role, user_prompt))
            if role == "fixer" and self.provider.on_delta:
                pieces = ['{"files"', ': [{"path"', ': "a.py", "action"']
                full = '{"files": [{"path": "a.py", "action": "create", "content": "value = 2\n"}]}'
                acc = ""
                for piece in pieces:
                    acc += piece
                    self.provider.on_delta(acc)
                self.provider.on_delta(full)
                return full
            return await original(system_prompt, user_prompt, model, temperature, max_tokens)

        self.provider.complete = streaming  # type: ignore[method-assign]
        await self.executor.run_planning(self.goal.id)
        step = self.goals.steps(self.goal.id)[0]
        await self.executor.run_step(self.goal.id, step.id)
        deltas = self._deltas(self.goal.id)
        # Every role call flushes exactly one final; scope to the fixer's stream.
        fixer = [d for d in deltas if d.payload["role"] == "fixer"]
        self.assertTrue(fixer, "streamed reply must produce model_delta events")
        self.assertFalse(fixer[0].payload["final"], "intermediate snapshots stream first")
        finals = [d for d in fixer if d.payload["final"]]
        self.assertEqual(len(finals), 1, "exactly one final snapshot for the role")
        self.assertTrue(finals[0].payload["text"].startswith('{"files"'))
        self.assertTrue(
            all(a.sequence < b.sequence for a, b in zip(fixer, fixer[1:], strict=False)),
            "snapshots must be ordered",
        )

    async def test_non_streaming_provider_still_gets_one_final_card(self) -> None:
        # _ScriptedProvider never calls on_delta: the flush must still emit a
        # single final snapshot carrying the complete reply.
        await self.executor.run_planning(self.goal.id)
        step = self.goals.steps(self.goal.id)[0]
        await self.executor.run_step(self.goal.id, step.id)
        deltas = [d for d in self._deltas(self.goal.id) if d.payload["role"] == "fixer"]
        self.assertEqual(len(deltas), 1)
        self.assertTrue(deltas[0].payload["final"])
        self.assertIn("files", deltas[0].payload["text"])

    async def test_failed_call_still_closes_the_stream(self) -> None:
        # The flush must run on the error path too, or the live card would
        # spin forever on a role whose provider just blew up.
        from engine.providers import ProviderError as PE

        original = self.provider.complete

        async def failing(
            system_prompt: str, user_prompt: str, model: str,
            temperature: float, max_tokens: int,
        ) -> str:
            role = next(
                (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
            )
            self.provider.calls.append((role, user_prompt))
            if role == "fixer":
                raise PE("provider_unreachable", "ollama unreachable: X")
            return await original(system_prompt, user_prompt, model, temperature, max_tokens)

        self.provider.complete = failing  # type: ignore[method-assign]
        await self.executor.run_planning(self.goal.id)
        from engine.executor import FALLBACK_TRIGGER_CODES

        if "provider_unreachable" not in FALLBACK_TRIGGER_CODES:
            with self.assertRaises(PE):
                await self.executor.run_step(self.goal.id, "1")
            deltas = [d for d in self._deltas(self.goal.id) if d.payload["role"] == "fixer"]
            self.assertTrue(deltas, "the failed call must still have closed its stream")
            self.assertTrue(
                all(d.payload["final"] for d in deltas),
                "every snapshot after a failure must be final (stream closed)",
            )
            self.assertEqual(deltas[-1].payload["text"], "")


class TestCriticInspectionCommand(unittest.IsolatedAsyncioTestCase):
    """The critic may ask for ONE read-only command per round, max 2.

    "Is this symbol actually used?" is not answerable from a diff. The critic
    used to guess; now it can request evidence the way the librarian does —
    through the same read-only allowlist, sandbox-refusals reported back as
    information rather than failing the step, and the whole thing bounded.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "a.py").write_text("value = 1\n", encoding="utf-8")
        self.conn = connect(self.root / "t.db")
        self.provider = _ScriptedProvider(
            {
                "planner": {
                    "steps": [{
                        "title": "Step 1", "description": "bump value",
                        "suggested_paths": ["a.py"],
                    }]
                },
                "fixer": {
                    "files": [{"path": "a.py", "action": "create", "content": "value = 2\n"}]
                },
                "scribe": {"summary": "did it", "commit_message": "feat: bump"},
            }
        )
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))
        self.provider.verifier_replies = [
            {"argv": None, "verdict": "pass", "explanation": "nothing to run"},
        ]

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _script_critic(self, replies: list[dict[str, Any]]) -> None:
        self.provider.others["critic"] = replies

        async def completing(
            system_prompt: str, user_prompt: str, model: str,
            temperature: float, max_tokens: int,
        ) -> str:
            role = next(
                (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
            )
            self.provider.calls.append((role, user_prompt))
            if role == "critic" and self.provider.others["critic"]:
                return json.dumps(self.provider.others["critic"].pop(0))
            if role == "verifier" and self.provider.verifier_replies:
                return json.dumps(self.provider.verifier_replies.pop(0))
            return json.dumps(self.provider.others.get(role, {}))

        self.provider.complete = completing  # type: ignore[method-assign]

    def _events(self, type_: str) -> list[Any]:
        return [e for e in self.goals.events_after(self.goal.id, 0) if e.type == type_]

    async def _plan_and_run(self) -> None:
        await self.executor.run_planning(self.goal.id)
        self.goals.update_status(self.goal.id, self.goals.get(self.goal.id).version, "RUNNING")
        await self.executor.run_step(self.goal.id, self.goals.steps(self.goal.id)[0].id)

    async def test_a_requested_command_runs_and_the_output_reaches_the_next_round(self) -> None:
        (self.root / "docs").mkdir()
        (self.root / "docs" / "note.txt").write_text("hello docs\n", encoding="utf-8")
        self._script_critic([
            {"decision": None, "reasons": [], "run_command": ["ls", "-la", "docs"]},
            {"decision": "approve", "reasons": []},
        ])

        await self._plan_and_run()

        critic_prompts = [p for r, p in self.provider.calls if r == "critic"]
        self.assertEqual(len(critic_prompts), 2, "one round to ask, one to decide")
        self.assertIn("note.txt", critic_prompts[1], "the command output must reach round 2")
        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "COMPLETED")
        logs = " | ".join(e.payload["message"] for e in self._events("log"))
        self.assertIn("critic inspection: ls -la docs", logs)

    async def test_a_refused_command_is_information_not_a_failure(self) -> None:
        self._script_critic([
            {"decision": None, "reasons": [], "run_command": ["rm", "-rf", "/"]},
            {"decision": "approve", "reasons": []},
        ])

        await self._plan_and_run()

        critic_prompts = [p for r, p in self.provider.calls if r == "critic"]
        self.assertEqual(len(critic_prompts), 2)
        self.assertIn("NOT run", critic_prompts[1])
        self.assertIn("Decide from what you have", critic_prompts[1])
        # The step still completes: a refusal is evidence about the command,
        # not a defect in the critic.
        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "COMPLETED")

    async def test_the_command_bound_holds(self) -> None:
        self._script_critic([
            {"decision": None, "reasons": [], "run_command": ["ls", "-l"]},
            {"decision": None, "reasons": [], "run_command": ["ls", "-a"]},
            {"decision": None, "reasons": [], "run_command": ["ls", "-la"]},
            {"decision": "approve", "reasons": []},
        ])

        await self._plan_and_run()

        critic_prompts = [p for r, p in self.provider.calls if r == "critic"]
        # Round 1 asks, rounds 2-3 run commands, round 3's third ask hits the
        # bound and the step fails as invalid output.
        self.assertEqual(len(critic_prompts), 3, "1 ask + 2 granted commands")
        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "FAILED")
        errors = [e.payload for e in self._events("error")]
        self.assertTrue(any("kept requesting commands" in str(e.get("message")) for e in errors))

    async def test_a_direct_decision_never_runs_a_command(self) -> None:
        self._script_critic([{"decision": "approve", "reasons": []}])

        await self._plan_and_run()

        critic_prompts = [p for r, p in self.provider.calls if r == "critic"]
        self.assertEqual(len(critic_prompts), 1, "no extra round when the critic decides immediately")
        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "COMPLETED")

class TestCriticAndScribeEvidence(unittest.IsolatedAsyncioTestCase):
    """The critic judges with the verdict; the scribe describes with the diff.

    Both roles were asked for a judgment the executor never gave them evidence
    for: the critic could approve changes whose tests had just failed (the
    verdict was published as an event but never shown to it), and the scribe
    was told to describe "what changed from the diff you are given" while being
    given nothing but file names — so commit subjects were invented.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.provider = _ScriptedProvider(
            {
                "planner": {
                    "steps": [{
                        "title": "Step 1", "description": "add a.py",
                        "suggested_paths": ["a.py"],
                    }]
                },
                "fixer": {
                    "files": [{"path": "a.py", "action": "create", "content": "value = 4\n"}]
                },
                "critic": {"decision": "approve", "reasons": []},
                "scribe": {"summary": "did it", "commit_message": "feat: a"},
            }
        )
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def _plan_and_run(self, verifier_reply: dict[str, Any] | None = None) -> None:
        await self.executor.run_planning(self.goal.id)
        step = self.goals.steps(self.goal.id)[0]
        self.goals.update_status(self.goal.id, self.goal.version + 1, "RUNNING")
        if verifier_reply is not None:
            self.provider.verifier_replies.append(verifier_reply)
        await self.executor.run_step(self.goal.id, step.id)

    def _prompts_for(self, role: str) -> list[str]:
        return [p for r, p in self.provider.calls if r == role]

    async def test_critic_prompt_carries_the_test_verdict(self) -> None:
        await self._plan_and_run(
            {"argv": None, "verdict": "pass", "explanation": "2 passed"}
        )

        critic_prompt = self._prompts_for("critic")[0]
        self.assertIn("Test verdict: pass", critic_prompt)
        self.assertIn("2 passed", critic_prompt)

    async def test_scribe_prompt_carries_the_actual_diff(self) -> None:
        await self._plan_and_run(
            {"argv": None, "verdict": "pass", "explanation": "2 passed"}
        )

        scribe_prompt = self._prompts_for("scribe")[0]
        self.assertIn("Test verdict: pass", scribe_prompt)
        self.assertIn("2 passed", scribe_prompt)
        self.assertIn("Diffs:", scribe_prompt)
        self.assertIn("File: a.py (create)", scribe_prompt)
        self.assertIn("+value = 4", scribe_prompt, "the unified diff body must reach the scribe")

    async def test_critic_prompt_says_when_no_verdict_exists(self) -> None:
        """A missing verdict must not read as "no tests ran", which a critic
        would treat as harmless — it has to be named as a wiring break."""
        from engine.fs import FileSystemService

        await self.executor.run_planning(self.goal.id)
        step = self.goals.steps(self.goal.id)[0]
        await self.executor._critic(
            self.goal.id, step, FileSystemService(str(self.root)),
            diffs=[], evidence={}, test_outcome=None,
        )

        critic_prompt = self._prompts_for("critic")[0]
        self.assertIn("NONE REPORTED", critic_prompt)


class TestEditActionEndToEnd(unittest.IsolatedAsyncioTestCase):
    """The fixer's search/replace op, through the real step pipeline.

    `edit` is a description ("replace this exact text") until the engine resolves
    it against the file as it exists. Only the resolved full content is a proposal
    Apply can replay deterministically — so a dry-run must store bytes, not the
    description, and an edit that cannot match must fail the step as the fixer's
    contract error, not an internal error.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "a.py").write_text("value = 1\n", encoding="utf-8")
        self.conn = connect(self.root / "t.db")
        self.provider = _ScriptedProvider(
            {
                "planner": {
                    "steps": [{
                        "title": "Step 1", "description": "bump value",
                        "suggested_paths": ["a.py"],
                    }]
                },
                "fixer": {
                    "files": [{"path": "a.py", "action": "edit",
                               "edits": [{"old_text": "value = 1", "new_text": "value = 2"}]}]
                },
                "critic": {"decision": "approve", "reasons": []},
                "scribe": {"summary": "did it", "commit_message": "feat: bump"},
            }
        )
        self.registry = AgentRegistryService(
            self.conn, _ScriptedFactory(self.provider, Keychain()), Keychain()
        )
        configure_every_role(self.registry)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _plan(self) -> PlanStep:
        g = self.goals.get(self.goal.id)  # fresh version — planning never ran here
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        self.provider.verifier_replies.append(
            {"argv": None, "verdict": "pass", "explanation": "nothing to run"}
        )
        self.executor._insert_steps(
            self.goal.id, [{"title": "S1", "description": "d", "suggested_paths": ["a.py"]}]
        )
        return self.goals.steps(self.goal.id)[0]

    async def test_a_dry_run_edit_stores_the_resolved_content_not_the_description(self) -> None:
        step = self._plan()
        self.executor.goals.set_dry_run(self.goal.id, True)

        await self.executor.run_step(self.goal.id, step.id)

        rows = self.conn.execute(
            "SELECT path, action, content FROM proposed_files WHERE goal_id = ? AND step_id = ?",
            (self.goal.id, step.id),
        ).fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [("a.py", "update")])
        # The stored content is the resolved file, byte-identical to what Apply
        # would write — not the {old_text, new_text} description.
        self.assertEqual(rows[0][2], "value = 2\n")
        # And the step's diff shows the edit as a normal change.
        diffs = [e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == "diff"]
        self.assertIn("+value = 2", diffs[0]["unified_diff"])

    async def test_an_edit_that_cannot_match_fails_the_step_as_invalid_output(self) -> None:
        self.provider.others["fixer"] = {
            "files": [{"path": "a.py", "action": "edit",
                       "edits": [{"old_text": "NO SUCH LINE", "new_text": "x"}]}]
        }
        step = self._plan()

        await self.executor.run_step(self.goal.id, step.id)

        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "FAILED")
        errors = [e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == "error"]
        self.assertTrue(any("old_text appears 0 time(s)" in str(e.get("message")) for e in errors))
        # The user's file is untouched — a refused edit writes nothing.
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), "value = 1\n")

    async def test_an_edit_with_no_resolved_change_stores_nothing_applyable(self) -> None:
        self.provider.others["fixer"] = {
            "files": [{"path": "a.py", "action": "edit",
                       "edits": [{"old_text": "value = 1", "new_text": "value = 1"}]}]
        }
        step = self._plan()
        self.executor.goals.set_dry_run(self.goal.id, True)

        await self.executor.run_step(self.goal.id, step.id)

        rows = self.conn.execute(
            "SELECT path FROM proposed_files WHERE goal_id = ? AND step_id = ?",
            (self.goal.id, step.id),
        ).fetchall()
        self.assertEqual(rows, [], "a no-op edit must not become an Apply proposal")


class TestLibrarianLineRangeReads(unittest.IsolatedAsyncioTestCase):
    """The librarian may ask for a line range: {path, offset, limit}.

    Before this, a read past MAX_READ_CHARS was head-truncated and everything
    after the cap was unreachable — the model had to pretend the bottom half of
    the file did not exist.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
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

    def _requests(self, out: dict[str, Any]) -> list[tuple[str, Any]]:
        return self.executor._library_requests(out)

    def test_a_range_request_is_parsed_with_normalized_bounds(self) -> None:
        reqs = self._requests({"reads": [
            {"path": "big.py", "offset": "200", "limit": "50"},
            {"path": "big.py", "offset": 0, "limit": 0},
            {"path": "big.py", "offset": "two", "limit": None},
        ]})
        self.assertEqual(reqs[0][1], {"path": "big.py", "offset": 200, "limit": 50})
        # Junk/zero bounds fall back to sane defaults, not an exception.
        self.assertEqual(reqs[1][1]["offset"], 1)
        self.assertEqual(reqs[2][1]["offset"], 1)

    def test_a_line_range_read_serves_that_slice_of_the_file(self) -> None:
        (self.root / "big.py").write_text(
            "\n".join(f"line {i}" for i in range(1, 51)) + "\n", encoding="utf-8"
        )
        lib = LibraryService(self.ws.root_path)
        text, opened, matched, refused = self.executor._serve_library_requests(
            "goal-x",
            lib,
            self._requests({"reads": [{"path": "big.py", "offset": 45, "limit": 5}]}),
        )
        self.assertIn("lines 45-49", text)
        self.assertIn("line 45", text)
        self.assertIn("line 49", text)
        self.assertNotIn("line 1\n", text, "the head of the file is not in a tail-window read")
        self.assertEqual(opened, {"big.py"})
        self.assertEqual(refused, 0)

    def test_a_beyond_eof_window_reports_an_empty_range_not_a_failure(self) -> None:
        (self.root / "big.py").write_text("only\n", encoding="utf-8")
        lib = LibraryService(self.ws.root_path)
        text, opened, matched, refused = self.executor._serve_library_requests(
            "goal-x", lib,
            self._requests({"reads": [{"path": "big.py", "offset": 999, "limit": 5}]}),
        )
        self.assertIn("empty range", text)
        self.assertEqual(refused, 0)


if __name__ == "__main__":
    unittest.main()
