from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import tempfile
import unittest
from pathlib import Path

from engine.db import connect
from engine.models import (
    AgentConfigUpdate,
    BUILTIN_PROVIDERS,
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
        roles = [c.role for c in configs]
        # One config per role, in the canonical ROLES order — including Laya,
        # the System-1 gate that runs before the planner.
        self.assertEqual(roles, list(ROLES))
        self.assertEqual(len(configs), len(ROLES))

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

    def test_boot_rescue_fails_orphaned_planning_and_running_goals(self):
        """Goals left PLANNING/RUNNING by a dead process are failed at boot.

        Before this existed, a PLANNING goal orphaned by a crash was a dead
        end: start refused it ("planning is still in progress" forever) and so
        did cancel — it could be neither run nor stopped.
        """
        ws_path = self.temp_dir.name
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(ws_path)))
        planning = self.goals.create(GoalCreate(workspace_id=ws.id, title="P", description=""))
        running = self.goals.create(GoalCreate(workspace_id=ws.id, title="R", description=""))
        paused = self.goals.create(GoalCreate(workspace_id=ws.id, title="S", description=""))
        done = self.goals.create(GoalCreate(workspace_id=ws.id, title="D", description=""))
        self.goals.update_status(running.id, running.version, "RUNNING")
        self.goals.update_status(paused.id, paused.version, "PAUSED")
        self.goals.update_status(done.id, done.version, "COMPLETED")

        rescued = self.goals.fail_orphaned_active_goals()

        by_id = {goal_id: previous for goal_id, previous, _ in rescued}
        self.assertEqual(by_id, {planning.id: "PLANNING", running.id: "RUNNING"},
                         "exactly the coroutine-less states are rescued")
        self.assertEqual(self.goals.get(planning.id).status, "FAILED")
        self.assertEqual(self.goals.get(running.id).status, "FAILED")
        # PAUSED is a state the user chose; COMPLETED is already terminal.
        self.assertEqual(self.goals.get(paused.id).status, "PAUSED")
        self.assertEqual(self.goals.get(done.id).status, "COMPLETED")

        # A rescued goal is startable again: the failed status is a real exit,
        # not another wedge.
        g = self.goals.get(planning.id)
        updated = self.goals.update_status(g.id, g.version, "PENDING")
        self.assertEqual(updated.status, "PENDING")

    def test_seeded_roles_name_no_model(self):
        """A fresh install must not ship hardcoded model ids.

        A compiled default is wrong within weeks (retired, renamed) and cannot
        know what this machine can reach; seeding one per role also meant every
        role pointed at a provider whose key the user had not added, so the first
        prompt failed with nothing to explain it. Roles are seeded onto the local
        keyless provider with an empty model, and the engine says which role
        needs configuring when one is missing.
        """
        for role in ROLES:
            cfg = self.registry.get_config(role)
            self.assertEqual(cfg.model_name, "", f"{role} must not ship a model id")
            self.assertEqual(cfg.provider, "ollama")
            self.assertEqual(cfg.protocol, "ollama")

    def test_provider_switch_inherits_new_protocol(self):
        """Regression: switching provider without an explicit protocol used to
        keep the OLD provider's protocol, sending e.g. Anthropic wire format to
        an Ollama endpoint."""
        # Seed is ollama/ollama; switch to anthropic naming no protocol.
        cfg = self.registry.get_config("planner")
        self.assertEqual(cfg.provider, "ollama")
        self.assertEqual(cfg.protocol, "ollama")

        updated = self.registry.set_config("planner", AgentConfigUpdate(provider="anthropic"))
        self.assertEqual(updated.provider, "anthropic")
        self.assertEqual(updated.protocol, "anthropic")

        # Explicit protocol in the patch still wins over the builtin default.
        explicit = self.registry.set_config(
            "planner",
            AgentConfigUpdate(provider="openai", protocol="ollama"),
        )
        self.assertEqual(explicit.provider, "openai")
        self.assertEqual(explicit.protocol, "ollama")

    def test_long_prompt_becomes_title_plus_description(self):
        """Chat UIs send the whole prompt as `title`; engine must accept >200 chars.

        Regression test: GoalCreate used to cap title at 200, so any real
        chat prompt failed validation with HTTP 422 before normalization.
        """
        ws_path = Path(self.temp_dir.name) / "workspace_long_prompt"
        ws_path.mkdir()
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(ws_path)))

        long_prompt = (
            "Add JWT authentication with refresh token rotation, role-based access "
            "control for the admin panel, rate limiting on the login endpoint, and "
            "comprehensive pytest coverage for all new middleware including edge "
            "cases around expired tokens and malformed authorization headers. "
            "Also update the existing user service and add database migrations."
        )
        self.assertGreater(len(long_prompt), 200)

        goal = self.goals.create(
            GoalCreate(workspace_id=ws.id, title=long_prompt, description=long_prompt)
        )
        # Title normalized to <=200 chars, full prompt preserved for the planner.
        self.assertLessEqual(len(goal.title), 200)
        self.assertGreater(len(goal.title), 0)
        self.assertIn(long_prompt, goal.description)

        # Short prompts pass through untouched.
        short = self.goals.create(
            GoalCreate(workspace_id=ws.id, title="Fix flaky test", description="details")
        )
        self.assertEqual(short.title, "Fix flaky test")
        self.assertEqual(short.description, "details")

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

    def test_recent_run_models_reads_what_answered_not_what_was_asked_for(self):
        """The model menu's recency signal.

        A goal can be created asking for one model and executed by four others, so
        this must come from `agent_assigned` events. Dedup keeps the newest run of
        each model, a role with nothing chosen contributes nothing, and the whole
        thing is ordered by when it ran — not by which goal is newer.
        """
        ws_path = Path(self.temp_dir.name) / "workspace_runs"
        ws_path.mkdir()
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(ws_path)))
        goal = self.goals.create(
            GoalCreate(
                workspace_id=ws.id,
                title="Goal",
                description="",
                provider="anthropic",
                model="claude-sonnet-4-6",
            )
        )

        def assigned(seq: int, when: float, role: str, provider: str, model: str) -> None:
            self.goals.publish(
                Event(
                    id=f"a{seq}",
                    goal_id=goal.id,
                    step_id=None,
                    type="agent_assigned",
                    payload={"role": role, "provider": provider, "model": model},
                    timestamp=when,
                    sequence=seq,
                )
            )

        assigned(1, 100.0, "planner", "ollama", "qwen3:8b")
        assigned(2, 200.0, "fixer", "openai", "gpt-4.1-mini")
        # A role with no model chosen is announced with an empty id — not a run.
        assigned(3, 300.0, "critic", "anthropic", "")
        assigned(4, 400.0, "scribe", "ollama", "qwen3:8b")
        # A non-run event must not enter the list, however recent.
        self.goals.publish(
            Event(
                id="log1",
                goal_id=goal.id,
                step_id=None,
                type="log",
                payload={"provider": "nobody", "model": "nowhere"},
                timestamp=500.0,
                sequence=5,
            )
        )

        runs = self.goals.recent_run_models(limit=5)
        self.assertEqual(
            [(r["provider"], r["model"]) for r in runs],
            [("ollama", "qwen3:8b"), ("openai", "gpt-4.1-mini")],
        )
        # The newest run of a model wins, with the role that ran it.
        self.assertEqual(runs[0]["role"], "scribe")
        self.assertEqual(runs[0]["ran_at"], 400.0)
        # The goal asked for claude-sonnet-4-6 and nothing ever ran on it.
        self.assertNotIn("claude-sonnet-4-6", [r["model"] for r in runs])
        # Limit is respected.
        self.assertEqual(len(self.goals.recent_run_models(limit=1)), 1)

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
                "fixer",
                AgentConfigUpdate(provider="ollama", protocol="ollama", base_url="https://remote.server.com"),
            )
        self.assertEqual(ctx.exception.code, "invalid_base_url")

        # Provider catalog
        catalog = self.registry.provider_catalog()
        self.assertIn("builtins", catalog)
        self.assertTrue(any(b["slug"] == "anthropic" for b in catalog["builtins"]))

    def test_provider_switch_does_not_keep_the_old_endpoint(self):
        """Switching provider must move the endpoint with it.

        Keeping the previous provider's base_url sent OpenAI wire format to the
        local Ollama socket (and Ollama format to a cloud endpoint), which just
        looks like a mysterious connection failure in the UI.
        """
        ollama = BUILTIN_PROVIDERS["ollama"]["base_url"]
        openai = BUILTIN_PROVIDERS["openai"]["base_url"]

        self.registry.set_config(
            "critic",
            AgentConfigUpdate(
                provider="ollama", protocol="ollama", model_name="local-model", base_url=ollama,
            ),
        )
        self.assertEqual(self.registry.get_config("critic").base_url, ollama)

        # Switch provider, no endpoint given: the old one must not survive.
        switched = self.registry.set_config(
            "critic", AgentConfigUpdate(provider="openai", model_name="gpt-4o")
        )
        self.assertEqual(switched.provider, "openai")
        self.assertEqual(switched.protocol, "openai_compat")
        self.assertEqual(switched.base_url, openai)

        # An explicit endpoint still wins (e.g. an OpenAI-compatible proxy).
        proxied = self.registry.set_config(
            "critic",
            AgentConfigUpdate(provider="deepseek", base_url="https://proxy.internal/v1"),
        )
        self.assertEqual(proxied.base_url, "https://proxy.internal/v1")

        # An explicit null means "go back to the provider's own endpoint".
        cleared = self.registry.set_config("critic", AgentConfigUpdate(base_url=None))
        self.assertEqual(cleared.base_url, BUILTIN_PROVIDERS["deepseek"]["base_url"])


class TestRoleMigration(unittest.TestCase):
    """An existing install keeps the provider and model it had configured.

    Role ids changed once (coder→fixer, tester→verifier, reviewer→critic,
    summarizer→scribe). Without a migration, an upgrade would bring back every new
    role unconfigured — reported as "no model is configured" with no mention of the
    model the user had actually been running.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "old.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_legacy_role(self, role: str, **fields) -> None:
        conn = connect(self.db_path)
        values = {
            "provider": "acme",
            "protocol": "openai_compat",
            "model_name": "acme-1",
            "base_url": "https://acme.test/v1",
            "temperature": 0.7,
            "max_tokens": 1234,
            "updated_at": 123.0,
        }
        values.update(fields)
        conn.execute(
            """INSERT OR REPLACE INTO agent_configs (
                 role, display_name, provider, protocol, model_name, api_key_ref,
                 base_url, system_prompt_override, temperature, max_tokens, updated_at
               ) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)""",
            (role, role.title(), values["provider"], values["protocol"], values["model_name"],
             values["base_url"], values["temperature"], values["max_tokens"], values["updated_at"]),
        )
        conn.commit()
        conn.close()

    def test_a_configured_legacy_role_moves_onto_its_new_slot(self):
        self._seed_legacy_role("coder")
        moved: list[tuple[str, str]] = []
        conn = connect(self.db_path, on_role_migrated=lambda old, new: moved.append((old, new)))
        try:
            roles = {r["role"] for r in conn.execute("SELECT role FROM agent_configs")}
            self.assertNotIn("coder", roles, "the dead role id must not linger")
            self.assertIn("fixer", roles)
            fixer = conn.execute(
                "SELECT * FROM agent_configs WHERE role = 'fixer'"
            ).fetchone()
            self.assertEqual(fixer["provider"], "acme")
            self.assertEqual(fixer["model_name"], "acme-1")
            self.assertEqual(fixer["base_url"], "https://acme.test/v1")
            self.assertEqual(fixer["temperature"], 0.7)
            self.assertEqual(fixer["max_tokens"], 1234)
            self.assertEqual(fixer["updated_at"], 123.0)
            self.assertEqual(fixer["display_name"], "Fixer Agent")
            self.assertEqual(moved, [("coder", "fixer")], "the caller needs the pairs to move keys")
        finally:
            conn.close()

    def test_a_role_the_user_has_since_configured_wins(self):
        self._seed_legacy_role("tester")
        conn = connect(self.db_path)
        conn.execute(
            "UPDATE agent_configs SET provider = 'ollama', model_name = 'mine', updated_at = 999 "
            "WHERE role = 'verifier'"
        )
        conn.commit()
        conn.close()

        conn = connect(self.db_path)
        try:
            verifier = conn.execute(
                "SELECT * FROM agent_configs WHERE role = 'verifier'"
            ).fetchone()
            self.assertEqual(verifier["model_name"], "mine", "an explicit choice is not overwritten")
            roles = {r["role"] for r in conn.execute("SELECT role FROM agent_configs")}
            self.assertNotIn("tester", roles)
        finally:
            conn.close()

    def test_running_twice_changes_nothing_the_second_time(self):
        self._seed_legacy_role("summarizer")
        for _ in range(2):
            conn = connect(self.db_path)
            conn.close()
        conn = connect(self.db_path)
        try:
            scribe = conn.execute("SELECT * FROM agent_configs WHERE role = 'scribe'").fetchone()
            self.assertEqual(scribe["model_name"], "acme-1")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_configs").fetchone()[0], len(ROLES))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
