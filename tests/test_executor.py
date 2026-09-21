import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from engine.db import connect
from engine.executor import ExecutorService
from engine.models import GoalCreate, WorkspaceCreate
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService


class MockProvider(BaseProvider):
    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[dict] = []

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
        })
        # match by role or return configured response
        for role, resp in self.responses.items():
            if role in system_prompt.lower():
                if callable(resp):
                    return json.dumps(resp(user_prompt))
                return json.dumps(resp)
        return json.dumps({"status": "ok"})


class MockFactory(ProviderFactory):
    def __init__(self, mock_provider: MockProvider):
        self.mock_provider = mock_provider

    def build(self, config):
        return self.mock_provider


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
        self.mock_responses["coder"] = {
            "files": [
                {"path": "a.py", "action": "create", "content": "print('hello world')\n"}
            ]
        }
        self.mock_responses["tester"] = {
            "argv": None,
            "verdict": "pass",
            "explanation": "tests passed",
        }
        self.mock_responses["reviewer"] = {
            "decision": "approve",
            "reasons": [],
        }
        self.mock_responses["summarizer"] = {
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

    async def test_reviewer_rejection_and_retry(self):
        self.mock_responses["planner"] = {
            "steps": [{"title": "Step 1", "description": "Do something", "suggested_paths": []}]
        }
        self.mock_responses["coder"] = {
            "files": [{"path": "b.py", "action": "create", "content": "x = 1\n"}]
        }
        self.mock_responses["tester"] = {"argv": None, "verdict": "pass", "explanation": "ok"}
        # Reviewer rejects
        self.mock_responses["reviewer"] = {
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
        self.mock_responses["reviewer"] = {"decision": "approve", "reasons": []}
        self.mock_responses["summarizer"] = {"summary": "Done", "commit_message": "feat: b"}

        await self.executor.retry_step(goal.id, step.id, goal_after.version)

        step_final = self.goals.steps(goal.id)[0]
        self.assertEqual(step_final.status, "COMPLETED")

        # Retrying a COMPLETED step should raise ApiError
        from engine.services import ApiError
        with self.assertRaises(ApiError) as ctx:
            await self.executor.retry_step(goal.id, step.id, self.goals.get(goal.id).version)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "step_not_retryable")

    async def test_tester_fail_marks_step_failed(self):
        self.mock_responses["planner"] = {
            "steps": [{"title": "Step 1", "description": "Test failure", "suggested_paths": []}]
        }
        self.mock_responses["coder"] = {
            "files": [{"path": "fail.py", "action": "create", "content": "assert False\n"}]
        }
        self.mock_responses["tester"] = {
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


if __name__ == "__main__":
    unittest.main()
