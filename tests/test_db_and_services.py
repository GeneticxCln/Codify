import tempfile
import unittest
from pathlib import Path

from engine.db import connect
from engine.models import (
    AgentConfigUpdate,
    Event,
    GoalCreate,
    ROLES,
    WorkspaceCreate,
)
from engine.providers import Keychain, ProviderFactory
from engine.services import (
    AgentRegistryService,
    ApiError,
    GoalService,
    WorkspaceService,
)


class TestDbAndServices(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.conn = connect(self.db_path)
        self.keychain = Keychain()
        self.factory = ProviderFactory(self.keychain)
        self.registry = AgentRegistryService(self.conn, self.factory, self.keychain)
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_seeded_agent_configs(self):
        configs = self.registry.list_configs()
        self.assertEqual(len(configs), 5)
        roles = [c.role for c in configs]
        self.assertEqual(roles, list(ROLES))

    def test_workspace_service(self):
        ws_path = Path(self.temp_dir.name) / "workspace1"
        ws_path.mkdir()

        # Create workspace
        ws = self.workspaces.create(WorkspaceCreate(name="WS1", root_path=str(ws_path)))
        self.assertEqual(ws.name, "WS1")
        self.assertEqual(ws.root_path, str(ws_path.resolve()))

        # Non-existent path fails
        with self.assertRaises(ApiError) as ctx:
            self.workspaces.create(WorkspaceCreate(name="Bad", root_path=str(ws_path / "missing")))
        self.assertEqual(ctx.exception.code, "invalid_root")

        # Duplicate root_path fails
        with self.assertRaises(ApiError) as ctx:
            self.workspaces.create(WorkspaceCreate(name="Duplicate", root_path=str(ws_path)))
        self.assertEqual(ctx.exception.code, "duplicate_workspace")

        # Get and list
        fetched = self.workspaces.get(ws.id)
        self.assertEqual(fetched.id, ws.id)
        all_ws = self.workspaces.list()
        self.assertEqual(len(all_ws), 1)

    def test_goal_service_and_concurrency(self):
        ws_path = Path(self.temp_dir.name) / "workspace_goals"
        ws_path.mkdir()
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(ws_path)))

        # Create goal
        goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="Test Goal", description="desc"))
        self.assertEqual(goal.status, "PLANNING")
        self.assertEqual(goal.version, 0)

        # Update status with correct version -> version increments
        updated = self.goals.update_status(goal.id, 0, "PENDING")
        self.assertEqual(updated.status, "PENDING")
        self.assertEqual(updated.version, 1)

        # Update with stale version -> raises 409 version_conflict
        with self.assertRaises(ApiError) as ctx:
            self.goals.update_status(goal.id, 0, "RUNNING")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "version_conflict")

    def test_event_sequencing(self):
        ws_path = Path(self.temp_dir.name) / "workspace_events"
        ws_path.mkdir()
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(ws_path)))
        goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="Goal", description=""))

        # Next sequence increments monotonically
        s1 = self.goals.next_sequence(goal.id)
        s2 = self.goals.next_sequence(goal.id)
        self.assertEqual(s1, 1)
        self.assertEqual(s2, 2)

        # Publish event
        ev = Event(
            id="e1",
            goal_id=goal.id,
            step_id=None,
            type="log",
            payload={"level": "info", "message": "hello"},
            timestamp=100.0,
            sequence=s1,
        )
        self.goals.publish(ev)

        events = self.goals.events_after(goal.id, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, "e1")

    def test_agent_registry_service(self):
        # Update planner
        patch = AgentConfigUpdate(
            display_name="Updated Planner",
            temperature=0.5,
            system_prompt_override="Custom prompt override",
        )
        updated = self.registry.set_config("planner", patch)
        self.assertEqual(updated.display_name, "Updated Planner")
        self.assertEqual(updated.temperature, 0.5)
        self.assertEqual(updated.system_prompt_override, "Custom prompt override")

        # Exceeding system_prompt_override length fails at Pydantic model validation
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            AgentConfigUpdate(system_prompt_override="a" * 32769)

        # set_config also validates prompt_too_long
        with self.assertRaises(ApiError) as ctx:
            self.registry.set_config(
                "planner",
                AgentConfigUpdate.model_construct(system_prompt_override="a" * 32769),
            )
        self.assertEqual(ctx.exception.code, "prompt_too_long")

        # Ollama requires local base_url
        with self.assertRaises(ApiError) as ctx:
            self.registry.set_config(
                "coder",
                AgentConfigUpdate(provider="ollama", protocol="ollama", base_url="https://remote.server.com"),
            )
        self.assertEqual(ctx.exception.code, "invalid_base_url")

        # Provider catalog
        catalog = self.registry.provider_catalog()
        self.assertIn("builtins", catalog)
        self.assertTrue(any(b["slug"] == "anthropic" for b in catalog["builtins"]))


if __name__ == "__main__":
    unittest.main()
