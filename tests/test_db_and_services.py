from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import tempfile
import unittest
from pathlib import Path

from engine.db import connect
from typing import Any

from engine.models import (
    AgentConfigUpdate,
    BUILTIN_PROVIDERS,
    Event,
    Goal,
    GoalCreate,
    ROLES,
    Workspace,
    WorkspaceCreate,
)
from engine.providers import Keychain, ProviderError, ProviderFactory
from engine.services import (
    AgentRegistryService,
    ApiError,
    GoalService,
    SettingsService,
    WorkspaceService,
)


class TestDbAndServices(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.conn = connect(self.db_path)
        self.keychain = Keychain()
        self.factory = ProviderFactory(self.keychain)
        self.registry = AgentRegistryService(self.conn, self.factory, self.keychain)
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.settings = SettingsService(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _ws_with_goal(self, name: str = "WS", status: str = "COMPLETED") -> tuple[Workspace, Goal]:
        root = Path(self.temp_dir.name) / f"root-{name}"
        root.mkdir(exist_ok=True)
        ws = self.workspaces.create(WorkspaceCreate(name=name, root_path=str(root)))
        goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="g"))
        self.goals.update_status(goal.id, 0, "PENDING")
        # Re-read: update_status returns a new object, so the local handle would
        # still be the PLANNING snapshot it was created as.
        goal = self.goals.update_status(goal.id, 1, status)
        return ws, goal

    def test_delete_goal_refuses_in_progress_states_directly(self) -> None:
        """The service guards itself, not just the HTTP route.

        The route re-checks the same rule (and the live driver set) because it
        owns the race window, so the HTTP tests pass either way. This one pins
        the service contract on its own — a caller reaching for `goals.delete`
        directly must not be able to delete a goal a coroutine is still driving.
        """
        for status in ("PLANNING", "RUNNING"):
            with self.subTest(status=status):
                _, goal = self._ws_with_goal(f"WS-{status}", status=status)
                with self.assertRaises(ApiError) as ctx:
                    self.goals.delete(goal.id)
                self.assertEqual(ctx.exception.code, "goal_in_progress")
                self.assertEqual(
                    self.conn.execute(
                        "SELECT count(*) FROM goals WHERE id = ?", (goal.id,)
                    ).fetchone()[0],
                    1, "a refused delete must leave the row alone",
                )

    def test_delete_goal_allows_terminal_states(self) -> None:
        """Every stopped state is deletable, including a cancelled run."""
        for status in ("COMPLETED", "FAILED", "CANCELLED"):
            with self.subTest(status=status):
                _, goal = self._ws_with_goal(f"WS-{status}", status=status)
                result = self.goals.delete(goal.id)
                self.assertTrue(result["deleted"])
                self.assertFalse(result["files_touched"])
                self.assertEqual(goal.status, status, "the report names the run it removed")

    def test_workspace_delete_requires_explicit_cascade(self) -> None:
        ws, goal = self._ws_with_goal()
        self.assertEqual(self.workspaces.goal_count(ws.id), 1)

        with self.assertRaises(ApiError) as ctx:
            self.workspaces.delete(ws.id)
        self.assertEqual(ctx.exception.code, "workspace_not_empty")
        self.assertEqual(ctx.exception.extra["goals"], 1)
        self.assertEqual(self.workspaces.goal_count(ws.id), 1, "refused means untouched")

        result = self.workspaces.delete(ws.id, delete_goals=True)
        self.assertEqual(result["goals"], 1)
        self.assertEqual(self.workspaces.goal_count(ws.id), 0)
        with self.assertRaises(ApiError):
            self.workspaces.get(ws.id)
        with self.assertRaises(ApiError):
            self.goals.get(goal.id)

    def test_workspace_delete_reports_the_underlying_infk_is_never_raised(self) -> None:
        """A bare IntegrityError here was a 500 with no actionable message."""
        ws, _ = self._ws_with_goal()
        try:
            self.workspaces.delete(ws.id)
            self.fail("expected a refusal, not a cascade")
        except ApiError as exc:
            self.assertIn("goal", exc.message.lower())
        except Exception as exc:  # pragma: no cover - the regression itself
            self.fail(f"raw DB error leaked: {type(exc).__name__}: {exc}")

    def test_seeded_agent_configs(self) -> None:
        configs = self.registry.list_configs()
        roles = [c.role for c in configs]
        # One config per role, in the canonical ROLES order — including Laya,
        # the System-1 gate that runs before the planner.
        self.assertEqual(roles, list(ROLES))
        self.assertEqual(len(configs), len(ROLES))

    def test_saving_a_key_never_wipes_a_corrupt_store(self) -> None:
        """The file backend's read-before-write must fail loudly on a corrupt
        store: silently treating it as empty makes the atomic replace discard
        every OTHER key the file held."""
        store = Path(self.temp_dir.name) / "secrets.json"
        kc = Keychain(secrets_path=store)
        kc.set_provider_key("openai", "sk-first")
        # Corrupt the store AFTER the first key is in.
        store.write_text("{not json at all", encoding="utf-8")
        with self.assertRaises(ProviderError) as ctx:
            kc.set_provider_key("anthropic", "sk-second")
        self.assertEqual(ctx.exception.code, "secrets_unreadable")
        # The corrupt bytes were NOT overwritten by an empty-dict save.
        self.assertEqual(store.read_text(encoding="utf-8"), "{not json at all")

    def test_secrets_temp_file_is_created_0600(self) -> None:
        """No world-readable window: the temp file is created O_CREAT 0600, not
        written with the umask and chmodded afterwards."""
        import stat

        store = Path(self.temp_dir.name) / "secrets.json"
        kc = Keychain(secrets_path=store)
        kc.set_provider_key("openai", "sk-perms")
        # The final file must be 0600 (the tmp path is gone after replace).
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o600)

    def test_workspace_service(self) -> None:
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
        all_ws = self.workspaces.list_workspaces()
        self.assertEqual(len(all_ws), 1)

    def test_goal_service_and_concurrency(self) -> None:
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

    def test_boot_rescue_fails_orphaned_planning_and_running_goals(self) -> None:
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

    def test_seeded_roles_name_no_model(self) -> None:
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

    def test_engine_settings_roundtrip_clamp_and_corrupt_value(self) -> None:
        """The persisted settings store: defaults, clamps, and recovery.

        The value is read on every parallel batch, so a corrupt or missing row
        must degrade to the default, never crash a running goal.
        """
        self.assertEqual(self.settings.get_int("parallel_width"), 4, "default")
        self.assertEqual(self.settings.set_int("parallel_width", 2), 2)
        self.assertEqual(self.settings.get_int("parallel_width"), 2, "persists")
        self.assertEqual(self.settings.set_int("parallel_width", 500), 16, "clamped high")
        self.assertEqual(self.settings.set_int("parallel_width", 0), 1, "clamped low")
        with self.assertRaises(ApiError):
            self.settings.set_int("no_such_key", 1)

        # A row a hand-edit (or an old build) mangled reads as unset.
        self.conn.execute(
            "UPDATE engine_settings SET value = 'not-a-number' WHERE key = 'parallel_width'"
        )
        self.assertEqual(self.settings.get_int("parallel_width"), 4, "corrupt → default")

    def test_provider_switch_inherits_new_protocol(self) -> None:
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

    def test_long_prompt_becomes_title_plus_description(self) -> None:
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

    def test_event_sequencing(self) -> None:
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

    def test_recent_run_models_reads_what_answered_not_what_was_asked_for(self) -> None:
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

    def test_agent_registry_service(self) -> None:
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

    def test_provider_switch_does_not_keep_the_old_endpoint(self) -> None:
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

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "old.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_legacy_role(self, role: str, **fields: Any) -> None:
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

    def test_a_configured_legacy_role_moves_onto_its_new_slot(self) -> None:
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

    def test_a_role_the_user_has_since_configured_wins(self) -> None:
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

    def test_running_twice_changes_nothing_the_second_time(self) -> None:
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
