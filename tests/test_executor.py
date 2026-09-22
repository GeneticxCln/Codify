from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from engine.db import connect
from engine.git import GitService
from engine.executor import MAX_LIBRARY_ROUNDS, MAX_REFUSED_TEST_COMMANDS, ExecutorService
from engine.laya import LayaDecision, LayaService
from engine.models import ROLES, AgentConfigUpdate, GoalCreate, WorkspaceCreate
from engine.providers import BaseProvider, Keychain, ProviderError, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService


class MockProvider(BaseProvider):
    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[dict] = []
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

    def build(self, config):
        self.mock_provider.current_role = config.role
        return self.mock_provider


def configure_every_role(registry, model: str = "test-model") -> None:
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

    def setUp(self):
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

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_each_role_uses_its_own_config(self):
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
        await self.executor.run_step(goal.id, step.id)

        fixer_calls = [c for c in self.mock_provider.calls if "You are Codify Fixer" in c["system_prompt"]]
        self.assertTrue(fixer_calls, "fixer call must happen")
        self.assertEqual(fixer_calls[0]["model"], "fixer-model")

        # And the goal must have completed the step.
        self.assertEqual(self.goals.steps(goal.id)[0].status, "COMPLETED")


class TestExecutorService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.db_path = self.root / "test.db"
        self.conn = connect(self.db_path)
        self.keychain = Keychain()

        self.mock_responses = {}
        self.mock_provider = MockProvider(self.mock_responses)
        self.mock_factory = MockFactory(self.mock_provider)

        self.registry = AgentRegistryService(self.conn, self.mock_factory, self.keychain)
        configure_every_role(self.registry)
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.sandbox = SandboxService()
        self.executor = ExecutorService(self.goals, self.workspaces, self.registry, self.sandbox)

        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))

    async def asyncTearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_planning_flow(self):
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

    async def test_step_execution_success(self):
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

    async def test_role_without_a_model_says_so_instead_of_calling_nothing(self):
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

    async def test_missing_credential_is_reported_as_a_setup_problem(self):
        """A role whose provider has no key is a configuration gap, not bad output."""

        class _NoCredentialFactory(ProviderFactory):
            def build(self, config):
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

    async def test_critic_rejection_and_retry(self):
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

    async def test_verifier_fail_marks_step_failed(self):
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

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
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
    def __init__(self, provider, keychain):
        super().__init__(keychain)
        self.provider = provider

    def build(self, config):
        return self.provider


class _SkippedGate(LayaService):
    """The gate is a separate concern; these tests are about the verifier."""

    async def decide(self, state):
        return LayaDecision(engine="skipped", skipped_reason="test double")


class TestSandboxRefusalRecovery(unittest.IsolatedAsyncioTestCase):
    """A refused test command must not end the goal.

    Real case: the verifier proposed `touch …` to create a file, the sandbox
    refused the binary, and the step — along with a change the fixer had already
    written to disk — died with `command_not_allowed`. The refusal is something
    the verifier can act on, so it is fed back like command output.
    """

    REFUSED = {"argv": ["touch", "x"], "verdict": None, "explanation": "create a file"}

    async def asyncSetUp(self):
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

    async def asyncTearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    async def _plan_and_run(self) -> None:
        await self.executor.run_planning(self.goal.id)
        step = self.goals.steps(self.goal.id)[0]
        self.goals.update_status(self.goal.id, self.goal.version + 1, "RUNNING")
        await self.executor.run_step(self.goal.id, step.id)

    def _test_results(self) -> list[dict]:
        return [
            e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == "test_result"
        ]

    async def test_refused_command_is_replaced_and_the_step_completes(self):
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

    async def test_nothing_runnable_ends_as_a_skip_not_a_dead_goal(self):
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

    async def test_endless_refusals_are_capped(self):
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

    async def test_proposing_another_command_after_a_run_is_still_rejected(self):
        """The single-execution rule survives the retry path."""
        self.provider.verifier_replies = [
            {"argv": ["git", "status"], "verdict": None, "explanation": "run it"},
            {"argv": ["git", "diff"], "verdict": None, "explanation": "a second command"},
        ]
        await self._plan_and_run()

        self.assertEqual(self.goals.get(self.goal.id).status, "FAILED")
        error = next(e for e in self.goals.events_after(self.goal.id, 0) if e.type == "error")
        self.assertIn("argv null", error.payload["message"])


class _HangingSandbox(SandboxService):
    """Every command hangs until the timeout — no real process, no real wait."""

    def run_command(self, root_path, argv, timeout_s=None):
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

    async def asyncSetUp(self):
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

    async def asyncTearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_a_hang_reports_exit_124_and_the_goal_reaches_a_verdict(self):
        self.provider.verifier_replies = [
            {"argv": ["pytest", "-q"], "verdict": None, "explanation": "run the suite"},
            {"argv": None, "verdict": "fail", "explanation": "the run timed out"},
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
        self.assertEqual(self.executor.sandbox.last_timeout, 120, "the hang is bounded")

        # The verifier saw what happened, exactly like ordinary command output.
        self.assertIn("timed out after", self.provider.verifier_prompt_seq[1])
        self.assertIn("Now return the verdict", self.provider.verifier_prompt_seq[1])


class _FailingProvider(BaseProvider):
    """Breaks the fixer in a chosen way; every other role behaves normally."""

    def __init__(self, fixer_failure: str):
        self.fixer_failure = fixer_failure

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
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

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))

    async def asyncTearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    async def _run_a_step_that_fails_in_the_fixer(self, failure: str) -> dict:
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

    async def test_invalid_output_names_the_role_that_produced_it(self):
        error = await self._run_a_step_that_fails_in_the_fixer("garbage")
        self.assertEqual(error["code"], "agent_output_invalid")
        self.assertEqual(error["role"], "fixer", "a non-JSON fixer reply is the fixer's failure")

    async def test_a_provider_failure_keeps_its_code_and_still_names_the_role(self):
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

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
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
    def __init__(self, provider, keychain):
        super().__init__(keychain)
        self.provider = provider

    def build(self, config):
        self.provider.current_role = config.role
        return self.provider


class TestLibrarianReconnaissance(unittest.IsolatedAsyncioTestCase):
    """The librarian is what stops the pipeline planning blind.

    Before it, the planner received a title and a description, and the fixer read
    only the paths that blind planner guessed — so a wrong guess meant no agent
    ever saw the right file.
    """

    async def asyncSetUp(self):
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

    async def asyncTearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def _goal(self):
        return self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="Add greeting", description="make it polite")
        )

    def _events(self, goal_id: str, type_: str) -> list[dict]:
        return [e.payload for e in self.goals.events_after(goal_id, 0) if e.type == type_]

    def _warnings(self, goal_id: str) -> str:
        return " | ".join(
            e.payload["message"]
            for e in self.goals.events_after(goal_id, 0)
            if e.type == "log" and e.payload.get("level") == "warn"
        )

    ONE_STEP = {"steps": [{"title": "S1", "description": "d", "suggested_paths": ["src/app.py"]}]}

    async def test_it_looks_around_then_hands_the_planner_what_it_found(self):
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

    async def test_a_path_it_never_opened_is_dropped_not_passed_on(self):
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

    async def test_asking_forever_is_capped(self):
        self.provider.by_role = {
            "librarian": [{"reads": ["src/app.py"], "enough": False} for _ in range(MAX_LIBRARY_ROUNDS)],
            "planner": [self.ONE_STEP],
        }
        goal = self._goal()
        await self.executor.run_planning(goal.id)

        self.assertEqual(len(self.provider.role_calls("librarian")), MAX_LIBRARY_ROUNDS)
        self.assertIn("round cap", self._warnings(goal.id))
        self.assertEqual(self.goals.get(goal.id).status, "PENDING", "planning still happens")

    async def test_a_refused_request_is_fed_back_instead_of_failing_the_goal(self):
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

    async def test_an_escape_attempt_is_refused(self):
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

    async def test_a_librarian_that_cannot_run_leaves_planning_working(self):
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

    async def test_the_fixer_is_given_the_same_evidence_the_planner_planned_from(self):
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

    async def asyncSetUp(self):
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

    async def asyncTearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    async def _run(self, responses: dict, library: bool = False):
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

    def _events(self, goal_id: str, type_: str) -> list[dict]:
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

    async def test_a_proposal_identical_to_the_file_is_not_a_change(self):
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

    async def test_a_real_change_is_reported_and_committed(self):
        goal, _ = await self._run({
            "fixer": {"files": [{"path": "a.py", "action": "update", "content": "value = 2\n"}]},
        })
        summary = self._events(goal.id, "file_change_summary")[0]
        self.assertEqual(summary["paths"], ["a.py"])
        self.assertEqual(summary["unchanged"], [])
        self.assertIn("+value = 2", self._events(goal.id, "diff")[0]["unified_diff"])
        self.assertIn("a.py", self._committed())

    async def test_the_commit_leaves_the_users_own_work_alone(self):
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

    async def test_a_path_the_planner_guessed_badly_does_not_kill_the_step(self):
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


if __name__ == "__main__":
    unittest.main()
