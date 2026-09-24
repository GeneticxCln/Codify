from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import asyncio
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import httpx
from httpx import ASGITransport

from unittest.mock import patch

from engine.app import app, BOOT_TOKEN
from engine.db import connect
from engine.executor import ExecutorService
from engine.model_catalog import ModelCatalogService
from engine.models import Event, PlanStep, ROLES, GoalCreate, WorkspaceCreate
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.stats_history import StatsSnapshotService
from engine.stats_import import StatsImportService
from engine.services import AgentRegistryService, GoalService, SettingsService, WorkspaceService


class _StubCatalog:
    """A model catalog with a fixed answer, so repair tests never touch a provider."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.invalidated = 0

    async def get(self, refresh: bool = False) -> dict:
        return self.payload

    def invalidate(self) -> None:
        self.invalidated += 1


class TestApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.db_path = self.root / "test.db"

        # Override app state with isolated test db
        conn = connect(self.db_path)
        # Hermetic secrets: never write to the developer's real ~/.codify store,
        # and make the file backend's behaviour assertable.
        keychain = Keychain(secrets_path=self.root / "secrets.json")
        factory = ProviderFactory(keychain)
        app.state.conn = conn
        app.state.keychain = keychain
        app.state.registry = AgentRegistryService(conn, factory, keychain)
        app.state.workspaces = WorkspaceService(conn)
        app.state.goals = GoalService(conn)
        app.state.sandbox = SandboxService()
        app.state.settings = SettingsService(conn)
        app.state.stats_snapshots = StatsSnapshotService(conn)
        app.state.stats_imports = StatsImportService(conn)
        app.state.executor = ExecutorService(app.state.goals, app.state.workspaces, app.state.registry, app.state.sandbox)
        app.state.executor.settings = app.state.settings
        # Mock run_planning so background planning does not call live LLM providers in API tests
        async def mock_run_planning(goal_id: str) -> None:
            pass
        app.state.executor.run_planning = mock_run_planning
        app.state.token = BOOT_TOKEN
        # Hermetic model discovery: the API tests must never reach a real
        # provider (or a developer's local Ollama) just because /models is hit.
        app.state.models = ModelCatalogService(
            app.state.registry,
            keychain,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(404, json={"error": "no providers in tests"})
            ),
        )

        self.transport = ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}

    async def asyncTearDown(self):
        await self.client.aclose()
        app.state.conn.close()
        self.temp_dir.cleanup()

    async def test_repair_points_only_the_broken_roles_at_a_discovered_model(self):
        """One action fixes what cannot run and leaves everything else untouched.

        The working role in this test is the point: an install that is partly
        configured must not have its good choices overwritten.
        """
        await self.client.put(
            "/settings/agents/fixer",
            headers=self.headers,
            json={"provider": "ollama", "model_name": "local-1"},
        )
        await self.client.put(
            "/settings/agents/planner",
            headers=self.headers,
            json={"provider": "ollama", "model_name": ""},
        )
        await self.client.put(
            "/settings/agents/scribe",
            headers=self.headers,
            json={"provider": "openai", "model_name": "gpt-4.1"},
        )

        app.state.models = _StubCatalog(
            {
                "models": [
                    {"id": "local-1", "name": "local-1", "provider": "ollama",
                     "protocol": "ollama", "description": "local", "supports_chat": None},
                    {"id": "local-2", "name": "local-2", "provider": "ollama",
                     "protocol": "ollama", "description": "local", "supports_chat": None},
                ],
                "providers": [{"provider": "ollama", "protocol": "ollama", "ok": True,
                               "count": 2, "error": None}],
                "fetched_at": 0,
                "cached": False,
            }
        )

        r = await self.client.post("/settings/agents/repair", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        report = r.json()
        self.assertTrue(report["changed"])
        self.assertEqual(report["target"], {"provider": "ollama", "model": "local-1"})
        repaired_roles = {row["role"] for row in report["repaired"]}
        self.assertIn("planner", repaired_roles, "no model chosen is repairable")
        self.assertIn("scribe", repaired_roles, "a provider with no key is repairable")
        self.assertNotIn("fixer", repaired_roles, "a working role must not be touched")
        self.assertIn("fixer", {row["role"] for row in report["left_alone"]})
        reasons = {row["role"]: row["reason"] for row in report["repaired"]}
        self.assertEqual(reasons["planner"], "no model is chosen")
        self.assertIn("credential", reasons["scribe"])

        configs = {c["role"]: c for c in (await self.client.get("/settings/agents", headers=self.headers)).json()}
        self.assertEqual(configs["planner"]["model_name"], "local-1")
        self.assertEqual(configs["fixer"]["model_name"], "local-1")
        self.assertEqual(configs["scribe"]["provider"], "ollama")
        self.assertEqual(configs["scribe"]["model_name"], "local-1")

    async def test_repair_is_a_no_op_when_the_catalog_is_empty(self):
        await self.client.put(
            "/settings/agents/planner",
            headers=self.headers,
            json={"provider": "ollama", "model_name": ""},
        )
        app.state.models = _StubCatalog({"models": [], "providers": [], "fetched_at": 0, "cached": False})

        r = await self.client.post("/settings/agents/repair", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        report = r.json()
        self.assertFalse(report["changed"])
        self.assertEqual(report["repaired"], [])
        self.assertIsNone(report["target"])
        self.assertTrue(any("nothing to point these roles at" in n or "discovered" in n for n in report["notes"] + [report["target_reason"]]))
        # A role that needs a model and one that is fine are not the same outcome:
        # the report has to say which roles were left broken.
        self.assertEqual(
            [row["role"] for row in report["unfixable"]],
            list(ROLES),
            "a fresh install has no model on any role, and the report must name them",
        )
        self.assertTrue(all(row["reason"] == "no model is chosen" for row in report["unfixable"]))
        configs = {c["role"]: c for c in (await self.client.get("/settings/agents", headers=self.headers)).json()}
        self.assertEqual(configs["planner"]["model_name"], "", "nothing invented")

    async def test_repair_says_nothing_needed_fixing_when_nothing_was_proven_broken(self):
        """An unreachable provider is an unknown, not a fault.

        Every role here already has a model, so no target is needed and none is
        explained away — only the caveat that discovery never confirmed them.
        """
        for cfg in (await self.client.get("/settings/agents", headers=self.headers)).json():
            await self.client.put(
                f"/settings/agents/{cfg['role']}",
                headers=self.headers,
                json={"provider": cfg["provider"], "model_name": cfg["model_name"] or "local-1"},
            )
        app.state.models = _StubCatalog(
            {
                "models": [],
                "providers": [{"provider": "ollama", "ok": False, "count": 0, "error": "ConnectError"}],
                "fetched_at": 0,
                "cached": False,
            }
        )

        r = await self.client.post("/settings/agents/repair", headers=self.headers)
        report = r.json()
        self.assertFalse(report["changed"])
        self.assertEqual(report["unfixable"], [])
        self.assertEqual(report["target_reason"], "", "no target is needed, so none is explained")
        self.assertEqual(len(report["notes"]), 1, "one caveat per provider, not per role")
        self.assertEqual(len(report["left_alone"]), len(ROLES))
        self.assertTrue(all("not verified" in row["reason"] for row in report["left_alone"]))

    async def test_roles_endpoint_describes_every_slot_and_its_abilities(self):
        """The settings screen reads the ability list from here, so it cannot
        describe a grant the engine does not make."""
        r = await self.client.get("/settings/roles", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        roles = r.json()
        self.assertEqual([entry["role"] for entry in roles], list(ROLES))
        self.assertEqual([entry["order"] for entry in roles], list(range(len(ROLES))))
        for entry in roles:
            self.assertTrue(entry["job"], f"{entry['role']} must say what it does")
            self.assertIn(entry["timing"], ("once per goal, before any model call", "once per goal, before planning", "once per goal", "once per step"))
        by_role = {entry["role"]: entry for entry in roles}
        self.assertIn("Never writes", by_role["librarian"]["job"])
        self.assertEqual(by_role["fixer"]["timing"], "once per step")
        self.assertEqual(by_role["librarian"]["timing"], "once per goal, before planning")

    async def test_a_renamed_role_is_reported_by_its_new_name(self):
        await self.client.put(
            "/settings/agents/fixer",
            headers=self.headers,
            json={"display_name": "My Fixer"},
        )
        r = await self.client.get("/settings/roles", headers=self.headers)
        by_role = {entry["role"]: entry for entry in r.json()}
        self.assertEqual(by_role["fixer"]["display_name"], "My Fixer")

    async def test_auth_middleware(self):
        # No token -> 401
        r = await self.client.get("/health")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "unauthorized")

        # Wrong token -> 401
        r = await self.client.get("/health", headers={"Authorization": "Bearer bad-token"})
        self.assertEqual(r.status_code, 401)

        # Correct token -> 200, with auth liveness info
        r = await self.client.get("/health", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "authenticated": True})
        # Without token: still 200 (engine is up), but unauthenticated.
        r = await self.client.get("/health")
        self.assertEqual(r.status_code, 401)

    async def test_models_route_is_discovered_not_hardcoded(self):
        """No provider reachable → an empty catalog plus reasons. A list of
        plausible model names would be the hardcoded-catalog bug returning."""
        r = await self.client.get("/models", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["models"], [])
        self.assertIn("providers", body)
        self.assertIn("fetched_at", body)
        self.assertTrue(all(not p["ok"] for p in body["providers"]))
        self.assertTrue(all(p["error"] for p in body["providers"]), "every failure names a reason")

    async def test_models_route_serves_cache_and_honours_refresh(self):
        calls: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"models": [{"name": "local-a"}]})

        app.state.models = ModelCatalogService(
            app.state.registry, app.state.keychain, transport=httpx.MockTransport(handle)
        )

        first = (await self.client.get("/models", headers=self.headers)).json()
        self.assertEqual([m["id"] for m in first["models"]], ["local-a"])
        self.assertFalse(first["cached"])

        second = (await self.client.get("/models", headers=self.headers)).json()
        self.assertTrue(second["cached"], "a second open should not re-query every provider")
        self.assertEqual(len(calls), 1)

        refreshed = (await self.client.get("/models?refresh=true", headers=self.headers)).json()
        self.assertFalse(refreshed["cached"])
        self.assertGreater(len(calls), 1, "refresh must re-query")

    async def test_saving_a_key_invalidates_the_catalog_cache(self):
        """Adding a provider key must surface its models immediately."""
        app.state.models._cache = {"models": [], "providers": [], "fetched_at": 0, "cached": False}
        app.state.models._cached_at = 10**9
        with patch.object(Keychain, "set_provider_key", return_value=None):
            r = await self.client.post(
                "/settings/keys", json={"provider": "openai", "api_key": "sk-new"}, headers=self.headers
            )
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(app.state.models._cache, "stale catalog must be dropped")

    async def test_saving_a_key_actually_stores_it(self):
        """The whole point of the key endpoint: the provider can use it afterwards.

        Backend-agnostic on purpose — OS keychain where one exists, the local
        file otherwise. Either way the key must resolve for that provider and
        the response must say where it went.
        """
        r = await self.client.post(
            "/settings/keys", json={"provider": "openai", "api_key": "sk-stored"}, headers=self.headers
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(app.state.keychain.get_provider_key("openai"), "sk-stored")

        keys = (await self.client.get("/settings/keys", headers=self.headers)).json()
        openai = next(k for k in keys if k["provider"] == "openai")
        self.assertTrue(openai["has_key"])
        self.assertIn(openai["storage"], ("keyring", "file"))
        # The reason travels with the destination: a `file` store is either a missing
        # keyring or a deliberately isolated run, and the UI must not guess which.
        self.assertIn(openai["storage_reason"], ("keyring", "no_keyring", "isolated_run"))
        self.assertEqual(
            openai["storage"] == "keyring",
            openai["storage_reason"] == "keyring",
            "the destination and its reason must agree",
        )
        self.assertTrue(openai["storage_detail"])

    async def test_saving_a_key_without_a_keyring_is_a_clear_error(self):
        """A machine with no OS keyring must get an explained 503, not a 500."""
        from engine.providers import ProviderError

        with patch.object(
            Keychain, "set_provider_key", side_effect=ProviderError("keyring_unavailable", "no Secret Service")
        ):
            r = await self.client.post(
                "/settings/keys", json={"provider": "openai", "api_key": "sk"}, headers=self.headers
            )
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["code"], "keyring_unavailable")
        self.assertIn("Secret Service", r.json()["message"])

    async def test_updating_an_agent_config_invalidates_the_catalog_cache(self):
        app.state.models._cache = {"models": [], "providers": [], "fetched_at": 0, "cached": False}
        app.state.models._cached_at = 10**9
        r = await self.client.put(
            "/settings/agents/fixer",
            json={"provider": "ollama", "protocol": "ollama", "model_name": "m"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(app.state.models._cache)

    async def test_settings_endpoints(self):
        # GET /settings/providers
        r = await self.client.get("/settings/providers", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("builtins", data)

        # GET /settings/agents
        r = await self.client.get("/settings/agents", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        agents = r.json()
        self.assertEqual(len(agents), len(ROLES))

        # GET /settings/laya — capability report, no weights loaded
        r = await self.client.get("/settings/laya", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        laya = r.json()
        self.assertIn("sdk_installed", laya)
        self.assertIn("prompt_injection", laya["questions"])
        self.assertEqual(laya["policy"]["injection_block_threshold"], 0.85)

        # PUT /settings/agents/planner
        r = await self.client.put(
            "/settings/agents/planner",
            headers=self.headers,
            json={"display_name": "API Planner", "temperature": 0.4},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["display_name"], "API Planner")

    async def test_workspace_refuses_the_filesystem_root(self):
        """A workspace at `/` makes every containment check vacuous — refused."""
        r = await self.client.post(
            "/workspaces",
            headers=self.headers,
            json={"name": "ROOT", "root_path": "/"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "invalid_root")

    async def _seed_workspace_with_goal(self, name: str, *, terminal: str = "COMPLETED") -> tuple[str, str]:
        """A workspace with one finished goal carrying a full record."""
        ws_dir = self.root / name
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": name, "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": f"{name} goal"},
        )
        goal_id = r.json()["id"]
        app.state.goals.update_status(goal_id, 0, "PENDING")
        app.state.goals.update_status(goal_id, 1, terminal)
        app.state.goals.publish(Event(
            id=str(uuid.uuid4()), goal_id=goal_id, step_id=None, type="usage",
            payload={"role": "fixer", "provider": "p", "model": "m",
                     "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                     "duration_ms": 20},
            timestamp=time.time(), sequence=app.state.goals.next_sequence(goal_id),
        ))
        return ws_id, goal_id

    async def test_delete_goal_cascades_and_reports_what_it_removed(self):
        """Deleting a goal takes its record with it, and says how much."""
        _, goal_id = await self._seed_workspace_with_goal("ws-del-goal")
        events_before = app.state.conn.execute(
            "SELECT count(*) FROM events WHERE goal_id = ?", (goal_id,)
        ).fetchone()[0]
        self.assertGreater(events_before, 0, "the seed must leave events behind to cascade")

        r = await self.client.delete(f"/goals/{goal_id}", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["deleted"])
        self.assertEqual(body["goal_id"], goal_id)
        self.assertEqual(body["events"], events_before, "the count is what actually went")
        # The one thing a user must never have to guess about a delete button.
        self.assertFalse(body["files_touched"], "deleting a record never deletes files")

        for table in ("goals", "plan_steps", "events", "proposed_files"):
            rows = app.state.conn.execute(
                f"SELECT count(*) FROM {table} WHERE " + ("id = ?" if table == "goals" else "goal_id = ?"),
                (goal_id,),
            ).fetchone()[0]
            self.assertEqual(rows, 0, f"{table} rows must cascade with the goal")

        self.assertEqual((await self.client.get(f"/goals/{goal_id}", headers=self.headers)).status_code, 404)

    async def test_delete_goal_refuses_while_it_is_in_progress(self):
        """A live coroutine must not outlive its row."""
        _, goal_id = await self._seed_workspace_with_goal("ws-del-running", terminal="RUNNING")
        r = await self.client.delete(f"/goals/{goal_id}", headers=self.headers)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "goal_in_progress")
        # Still there: a refusal must not half-delete.
        self.assertEqual((await self.client.get(f"/goals/{goal_id}", headers=self.headers)).status_code, 200)

    async def test_delete_goal_unknown_is_404(self):
        r = await self.client.delete("/goals/does-not-exist", headers=self.headers)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "unknown_goal")

    async def test_delete_workspace_refuses_to_cascade_implicitly(self):
        """The FK used to surface as a bare 500; it is now a 409 that counts."""
        ws_id, goal_id = await self._seed_workspace_with_goal("ws-del-ws")

        r = await self.client.delete(f"/workspaces/{ws_id}", headers=self.headers)
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertEqual(body["code"], "workspace_not_empty")
        # The count is what the UI needs to write an honest confirmation.
        self.assertEqual(body["goals"], 1)

        # Refused means untouched: the goal is still readable.
        self.assertEqual((await self.client.get(f"/goals/{goal_id}", headers=self.headers)).status_code, 200)
        self.assertEqual((await self.client.get(f"/workspaces/{ws_id}", headers=self.headers)).status_code, 200)

    async def test_delete_workspace_cascade_removes_goals_and_their_events(self):
        ws_id, goal_id = await self._seed_workspace_with_goal("ws-del-cascade")

        r = await self.client.delete(
            f"/workspaces/{ws_id}?delete_goals=true", headers=self.headers
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["deleted"])
        self.assertEqual(body["goals"], 1)
        self.assertFalse(body["files_touched"], "the user's directory is never removed")

        self.assertEqual((await self.client.get(f"/goals/{goal_id}", headers=self.headers)).status_code, 404)
        self.assertEqual((await self.client.get(f"/workspaces/{ws_id}", headers=self.headers)).status_code, 404)
        rows = app.state.conn.execute(
            "SELECT count(*) FROM events WHERE goal_id = ?", (goal_id,)
        ).fetchone()[0]
        self.assertEqual(rows, 0, "the cascaded goals' events go with them")

    async def test_delete_workspace_refuses_while_a_goal_is_running(self):
        """Cascading over live work is the same hazard as deleting a live goal."""
        ws_id, goal_id = await self._seed_workspace_with_goal("ws-del-active", terminal="RUNNING")
        r = await self.client.delete(
            f"/workspaces/{ws_id}?delete_goals=true", headers=self.headers
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "workspace_has_active_goals")
        self.assertEqual(r.json()["goal_id"], goal_id, "the refusal names what to cancel first")

    async def test_delete_empty_workspace_needs_no_cascade_flag(self):
        ws_dir = self.root / "ws-del-empty"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-EMPTY", "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]

        r = await self.client.delete(f"/workspaces/{ws_id}", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["goals"], 0)
        self.assertEqual((await self.client.get(f"/workspaces/{ws_id}", headers=self.headers)).status_code, 404)
        # The directory itself survives: this endpoint forgets a path.
        self.assertTrue(ws_dir.is_dir(), "deleting a workspace must never remove the user's folder")

    async def test_delete_requires_auth(self):
        ws_id, goal_id = await self._seed_workspace_with_goal("ws-del-auth")
        self.assertEqual((await self.client.delete(f"/goals/{goal_id}")).status_code, 401)
        self.assertEqual((await self.client.delete(f"/workspaces/{ws_id}")).status_code, 401)

    async def test_workspaces_and_goals_lifecycle(self):
        ws_dir = self.root / "ws1"
        ws_dir.mkdir()

        # Create workspace
        r = await self.client.post(
            "/workspaces",
            headers=self.headers,
            json={"name": "WS1", "root_path": str(ws_dir)},
        )
        self.assertEqual(r.status_code, 200)
        ws_id = r.json()["id"]

        # Create goal
        r = await self.client.post(
            "/goals",
            headers=self.headers,
            json={"workspace_id": ws_id, "title": "My Goal", "description": "Goal desc"},
        )
        self.assertEqual(r.status_code, 200)
        goal = r.json()
        self.assertEqual(goal["status"], "PLANNING")
        goal_id = goal["id"]

        # Reject starting while PLANNING
        r = await self.client.post(
            f"/goals/{goal_id}/start",
            headers=self.headers,
            json={"expected_version": 0},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "illegal_status")

        # Manually transition to PENDING (simulating planner success) and insert a step
        app.state.goals.update_status(goal_id, 0, "PENDING")
        app.state.executor._insert_steps(goal_id, [{"title": "Step 1", "description": "desc", "suggested_paths": []}])

        async def mock_run_step(g_id: str, s_id: str) -> None:
            await asyncio.sleep(1.0)
        app.state.executor.run_step = mock_run_step

        # Start with invalid expected_version -> 409 version_conflict
        r = await self.client.post(
            f"/goals/{goal_id}/start",
            headers=self.headers,
            json={"expected_version": 999},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "version_conflict")

        # Start with correct version -> RUNNING
        r = await self.client.post(
            f"/goals/{goal_id}/start",
            headers=self.headers,
            json={"expected_version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "RUNNING")
        current_version = r.json()["version"]

        # Pause goal
        r = await self.client.post(
            f"/goals/{goal_id}/pause",
            headers=self.headers,
            json={"expected_version": current_version},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "PAUSED")
        current_version = r.json()["version"]

        # Cancel goal
        r = await self.client.post(
            f"/goals/{goal_id}/cancel",
            headers=self.headers,
            json={"expected_version": current_version},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "CANCELLED")

    def _set_goal_created(self, goal_id: str, created_at: float) -> None:
        """Pin a goal's created_at so ordering assertions don't depend on clock
        granularity: two goals created in the same second order by id, which is
        a random UUID and would make the test flaky."""
        app.state.conn.execute("UPDATE goals SET created_at = ? WHERE id = ?", (created_at, goal_id))
        app.state.conn.commit()

    async def test_goal_history_lists_goals_newest_first_with_active_goals_leading(self):
        """The restore path's index: every persisted goal, readable after the fact.

        Active goals lead (the one just dispatched must not sink under finished
        runs), then terminal goals newest first.
        """
        ws_dir = self.root / "ws_hist"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers, json={"name": "H", "root_path": str(ws_dir)}
        )
        ws_id = r.json()["id"]

        ids = {}
        for name in ("g1", "g2", "g3"):
            r = await self.client.post(
                "/goals", headers=self.headers,
                json={"workspace_id": ws_id, "title": name, "description": ""},
            )
            self.assertEqual(r.status_code, 200)
            ids[name] = r.json()["id"]

        # Deterministic history: g1 finished oldest, g3 failed newest, g2 still active.
        app.state.goals.update_status(ids["g1"], 0, "PENDING")
        app.state.goals.update_status(ids["g1"], 1, "COMPLETED")
        app.state.goals.update_status(ids["g3"], 0, "FAILED")
        self._set_goal_created(ids["g1"], 1000.0)
        self._set_goal_created(ids["g2"], 2000.0)
        self._set_goal_created(ids["g3"], 3000.0)

        r = await self.client.get("/goals", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        goals = r.json()
        self.assertEqual([g["id"] for g in goals], [ids["g2"], ids["g3"], ids["g1"]])
        self.assertEqual(goals[0]["status"], "PLANNING")  # active, leads despite age
        self.assertEqual(goals[1]["status"], "FAILED")
        self.assertEqual(goals[2]["status"], "COMPLETED")

    async def test_goal_history_filters_by_workspace_and_status_and_pages(self):
        ws_dir = self.root / "ws_hist2"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers, json={"name": "H2", "root_path": str(ws_dir)}
        )
        ws_id = r.json()["id"]
        other_dir = self.root / "ws_hist3"
        other_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers, json={"name": "H3", "root_path": str(other_dir)}
        )
        other_id = r.json()["id"]

        r = await self.client.post(
            "/goals", headers=self.headers, json={"workspace_id": ws_id, "title": "a", "description": ""}
        )
        done_id = r.json()["id"]
        app.state.goals.update_status(done_id, 0, "PENDING")
        app.state.goals.update_status(done_id, 1, "COMPLETED")
        r = await self.client.post(
            "/goals", headers=self.headers, json={"workspace_id": other_id, "title": "b", "description": ""}
        )
        self.assertEqual(r.status_code, 200)

        # Another workspace's goals do not leak into this one's history.
        r = await self.client.get(f"/goals?workspace_id={ws_id}", headers=self.headers)
        self.assertEqual([g["id"] for g in r.json()], [done_id])

        r = await self.client.get("/goals?status=COMPLETED", headers=self.headers)
        self.assertEqual([g["id"] for g in r.json()], [done_id])

        r = await self.client.get("/goals?status=NOT_A_STATUS", headers=self.headers)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "invalid_status")

        r = await self.client.get("/goals?limit=1", headers=self.headers)
        self.assertEqual(len(r.json()), 1)
        r = await self.client.get("/goals?limit=1&offset=1", headers=self.headers)
        self.assertEqual(len(r.json()), 1)

    async def test_cancel_from_planning_is_accepted(self):
        """A PLANNING goal can be cancelled.

        Planning runs in the background, and a goal stuck there (slow planner,
        or one orphaned by an older engine build) could be neither started nor
        stopped — the one state with no exit at all.
        """
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "cxl", "root_path": str(self.root)},
        )
        self.assertEqual(r.status_code, 200)
        ws_id = r.json()["id"]
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "stuck in planning", "description": ""},
        )
        self.assertEqual(r.status_code, 200)
        goal = r.json()
        self.assertEqual(goal["status"], "PLANNING")

        r = await self.client.post(
            f"/goals/{goal['id']}/cancel",
            headers=self.headers,
            json={"expected_version": goal["version"]},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "CANCELLED")
        # (That a late planner cannot resurrect it is covered at the executor
        # level, where the planner can be parked mid-flight.)


    async def test_plan_only_goal_lifecycle(self):
        """plan_only goals must plan, then refuse /start until execution is
        explicitly enabled."""
        ws_dir = self.root / "ws_planonly"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-PO", "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]

        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Plan only", "plan_only": True},
        )
        self.assertEqual(r.status_code, 200)
        goal = r.json()
        self.assertTrue(goal["plan_only"])
        goal_id = goal["id"]

        # Simulate planning success (same as the regular lifecycle test).
        app.state.goals.update_status(goal_id, 0, "PENDING")
        app.state.executor._insert_steps(
            goal_id, [{"title": "Step 1", "description": "d", "suggested_paths": []}]
        )
        g = app.state.goals.get(goal_id)

        # /start must refuse while plan_only is set.
        r = await self.client.post(
            f"/goals/{goal_id}/start", headers=self.headers,
            json={"expected_version": g.version},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "plan_only")

        # Enable execution, then start must succeed.
        r = await self.client.post(
            f"/goals/{goal_id}/enable-execution", headers=self.headers,
            json={"expected_version": g.version},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["plan_only"])
        version = r.json()["version"]

        async def mock_run_step(gid, sid):
            await asyncio.sleep(0)
        app.state.executor.run_step = mock_run_step

        r = await self.client.post(
            f"/goals/{goal_id}/start", headers=self.headers,
            json={"expected_version": version},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "RUNNING")

    async def test_goal_usage_totals_from_usage_events(self):
        """GET /goals/{id}/usage sums the usage events providers report."""
        ws_dir = self.root / "ws-usage"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-USAGE", "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Usage"},
        )
        goal_id = r.json()["id"]
        # Seed two usage events through the real publisher.
        app.state.goals.publish(Event(
            id=str(uuid.uuid4()), goal_id=goal_id, step_id=None, type="usage",
            payload={"role": "librarian", "provider": "ollama", "model": "m",
                     "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            timestamp=time.time(), sequence=app.state.goals.next_sequence(goal_id),
        ))
        app.state.goals.publish(Event(
            id=str(uuid.uuid4()), goal_id=goal_id, step_id=None, type="usage",
            payload={"role": "planner", "provider": "openai_compat", "model": "gpt",
                     "input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            timestamp=time.time(), sequence=app.state.goals.next_sequence(goal_id),
        ))

        r = await self.client.get(f"/goals/{goal_id}/usage", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["calls"], 2)
        self.assertEqual(body["totals"]["total_tokens"], 135)
        self.assertEqual(body["by_role"]["librarian"]["total_tokens"], 15)
        self.assertEqual(body["by_model"]["openai_compat/gpt"]["total_tokens"], 120)

        # Unknown goal is a 404, not an empty report.
        r = await self.client.get("/goals/nope/usage", headers=self.headers)
        self.assertEqual(r.status_code, 404)

    async def test_agent_stats_report_last_call_and_last_error_per_role(self):
        """The Settings cards' "last call / last error": the newest outcome for
        each role, from the event log. A role that only ever fails shows its
        failure, never a comforting blank."""
        ws_dir = self.root / "ws-stats"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-STATS", "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Stats"},
        )
        goal_id = r.json()["id"]

        def ev(eid, type_, payload, ts):
            return Event(
                id=eid, goal_id=goal_id, step_id=None, type=type_, payload=payload,
                timestamp=ts, sequence=app.state.goals.next_sequence(goal_id),
            )

        now = time.time()
        # Fixer: a failure, then a successful call after it — last_call wins,
        # and the earlier failure is still counted and kept as last_error? No:
        # "last" is the newest outcome, so the error block must stay. The card
        # reports both lanes independently.
        app.state.goals.publish(ev("st1", "agent_call_failed", {
            "role": "fixer", "provider": "anthropic", "model": "claude",
            "target": "primary", "code": "provider_http", "message": "429",
            "duration_ms": 120,
        }, now - 30))
        app.state.goals.publish(ev("st2", "usage", {
            "role": "fixer", "provider": "anthropic", "model": "claude",
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
            "duration_ms": 1400,
        }, now - 10))
        # Scribe: only ever failed — no usage event exists for it at all.
        app.state.goals.publish(ev("st3", "agent_call_failed", {
            "role": "scribe", "provider": "ollama", "model": "qwen",
            "target": "primary", "code": "provider_unreachable", "message": "refused",
            "duration_ms": 5,
        }, now - 5))
        # A usage event without duration_ms (written by an older engine): the
        # call counts, its duration reads as unknown rather than instant.
        app.state.goals.publish(ev("st4", "usage", {
            "role": "planner", "provider": "openai", "model": "gpt",
            "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
        }, now - 3))

        r = await self.client.get("/settings/agents/stats", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        by_role = {s["role"]: s for s in body["stats"]}
        self.assertEqual(len(body["stats"]), len(ROLES), "one entry per role, even the silent ones")

        fixer = by_role["fixer"]
        self.assertEqual(fixer["calls_seen"], 1)
        self.assertEqual(fixer["failures_seen"], 1)
        self.assertEqual(fixer["last_call"]["duration_ms"], 1400)
        self.assertEqual(fixer["last_error"]["code"], "provider_http")

        scribe = by_role["scribe"]
        self.assertIsNone(scribe["last_call"])
        self.assertEqual(scribe["failures_seen"], 1)
        self.assertEqual(scribe["last_error"]["code"], "provider_unreachable")

        planner = by_role["planner"]
        self.assertEqual(planner["last_call"]["duration_ms"], None, "a pre-duration event reads as unknown")
        self.assertEqual(planner["last_error"], None)

        critic = by_role["critic"]
        self.assertIsNone(critic["last_call"])
        self.assertIsNone(critic["last_error"])
        self.assertEqual(critic["calls_seen"], 0)

    @staticmethod
    def _export_doc(*days: str, tokens: int = 10) -> dict:
        """A minimal but fully valid stats-history export, oldest first."""
        out = []
        for day in days:
            out.append({
                "day": day,
                "day_stats": {
                    "date": day, "created": 1, "succeeded": 1, "failed": 0,
                    "cancelled": 0, "total_tokens": tokens, "calls": 1,
                },
                "goals": {
                    "goals": 1, "active": 0, "succeeded": 1, "failed": 0,
                    "cancelled": 0, "success_rate": 100,
                },
                "usage": {
                    "input_tokens": tokens - 1, "output_tokens": 1,
                    "total_tokens": tokens, "calls": 1, "avg_duration_ms": 12,
                },
            })
        return {"exported_at": "2026-01-01T00:00:00.000Z", "days": out}

    async def test_stats_import_persists_and_survives_a_service_rebuild(self):
        """The whole point: the import is in the engine, not the React state.

        Re-reading the store through a *new* service on the same connection is
        the closest a test gets to an app restart, and it is what a client-state
        import could never satisfy.
        """
        doc = self._export_doc("2026-01-01", "2026-01-02")
        r = await self.client.post(
            "/stats/import", headers=self.headers, json={**doc, "source": "history.json"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["days"], 2)
        self.assertEqual(r.json()["source"], "history.json")

        # A fresh service object = a fresh process's view of the same store.
        reborn = StatsImportService(app.state.conn)
        stored = reborn.get()
        assert stored is not None, "an import must outlive the request that created it"
        self.assertEqual([d["day"] for d in stored["days"]], ["2026-01-01", "2026-01-02"])
        self.assertEqual(stored["days"][0]["day_stats"]["total_tokens"], 10)
        self.assertEqual(stored["source"], "history.json")

        # And it is what a client reads back on open.
        r = await self.client.get("/stats/import", headers=self.headers)
        self.assertTrue(r.json()["imported"])
        self.assertEqual(len(r.json()["days"]), 2)

    async def test_stats_import_absent_is_a_normal_state_not_a_404(self):
        r = await self.client.get("/stats/import", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["imported"])
        self.assertEqual(body["days"], [])
        self.assertIsNone(body["source"])

    async def test_stats_import_replaces_rather_than_accumulates(self):
        """One imported file at a time, so the store cannot grow unbounded."""
        await self.client.post(
            "/stats/import", headers=self.headers, json=self._export_doc("2026-01-01", "2026-01-02")
        )
        r = await self.client.post(
            "/stats/import", headers=self.headers, json=self._export_doc("2026-02-01")
        )
        self.assertEqual(r.json()["days"], 1)

        body = (await self.client.get("/stats/import", headers=self.headers)).json()
        self.assertEqual([d["day"] for d in body["days"]], ["2026-02-01"],
                         "the previous import is replaced, not merged")
        self.assertEqual(
            app.state.conn.execute("SELECT count(*) FROM stats_imports").fetchone()[0], 1
        )

    async def test_stats_import_clear_is_idempotent(self):
        await self.client.post(
            "/stats/import", headers=self.headers, json=self._export_doc("2026-01-01")
        )
        r = await self.client.delete("/stats/import", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cleared"], 1)
        self.assertFalse((await self.client.get("/stats/import", headers=self.headers)).json()["imported"])

        # Clearing again is fine: the panel may race its own request.
        r = await self.client.delete("/stats/import", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cleared"], 0)

    async def test_stats_import_rejects_invalid_documents_with_a_reason(self):
        """The engine re-validates; a client cannot skip the check."""
        cases = [
            ({"exported_at": "x", "days": []}, "empty"),
            ({"days": [self._export_doc("2026-01-01")["days"][0]]}, "missing_exported_at"),
            (self._export_doc("2026-01-01", "2026-01-01"), "duplicate_day"),
            (self._export_doc("2026-01-02", "2026-01-01"), "out_of_order"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                r = await self.client.post("/stats/import", headers=self.headers, json=payload)
                self.assertEqual(r.status_code, 422)
                self.assertEqual(r.json()["code"], expected)
                # A rejected document must not become the stored one.
                self.assertFalse(
                    (await self.client.get("/stats/import", headers=self.headers)).json()["imported"]
                )

    async def test_stats_import_strips_unknown_keys_from_what_it_stores(self):
        """Only the known shape is persisted, so a re-export stays clean."""
        doc = self._export_doc("2026-01-01")
        doc["days"][0]["injected"] = "should not be stored"
        await self.client.post("/stats/import", headers=self.headers, json=doc)
        stored = StatsImportService(app.state.conn).get()
        assert stored is not None, "the import is readable back"
        self.assertNotIn("injected", stored["days"][0])

    async def test_stats_import_does_not_disturb_engine_snapshots(self):
        """An imported day is another machine's claim, not a local snapshot.

        Retention prunes snapshots by count; if imports shared that table the
        policy would silently delete data Codify never measured.
        """
        doc = self._export_doc("2026-01-01")
        await self.client.post("/stats/import", headers=self.headers, json=doc)
        app.state.conn.execute(
            "INSERT INTO stats_snapshots (day, document, created_at) VALUES (?, ?, ?)",
            ("2026-01-01", json.dumps({"goals": {"succeeded": 7}}), time.time()),
        )
        app.state.conn.commit()

        # Pruning everything but the newest N must not reach the import.
        app.state.stats_snapshots.prune(keep=0)
        self.assertEqual(
            app.state.conn.execute("SELECT count(*) FROM stats_imports").fetchone()[0],
            1, "pruning snapshots must never delete an imported day",
        )
        # And the two stores stay independently readable.
        self.assertIsNotNone(StatsImportService(app.state.conn).get())
        self.assertIsNotNone(app.state.stats_snapshots.get_day("2026-01-01"))

    async def test_stats_import_sanitizes_the_source_label(self):
        """The file name is echoed into the UI, so it is not stored raw."""
        r = await self.client.post(
            "/stats/import", headers=self.headers,
            json={**self._export_doc("2026-01-01"), "source": "../../etc/passwd\n\u0000evil"},
        )
        self.assertEqual(r.status_code, 200)
        stored = StatsImportService(app.state.conn).get()
        assert stored is not None, "the import is readable back"
        self.assertNotIn("/", stored["source"])
        self.assertNotIn("\n", stored["source"])
        self.assertNotIn("\x00", stored["source"])

    async def test_stats_import_requires_auth(self):
        doc = self._export_doc("2026-01-01")
        self.assertEqual((await self.client.get("/stats/import")).status_code, 401)
        self.assertEqual((await self.client.post("/stats/import", json=doc)).status_code, 401)
        self.assertEqual((await self.client.delete("/stats/import")).status_code, 401)

    async def test_stats_overview_window_anchors_to_the_wall_clock(self):
        """The endpoint must inject `now` into the aggregation.

        `engine/stats.py` has a data-anchored fallback that a stale store makes
        absurd — it slides the window forward until an ancient goal lands inside
        "last 24 hours". The arithmetic tests cover that helper; this pins the
        wiring, because a dropped `now=` at the one call site is invisible to
        every other test in the suite.
        """
        ws_dir = self.root / "ws-stale"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-STALE", "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "long ago"},
        )
        goal_id = r.json()["id"]
        app.state.goals.update_status(goal_id, 0, "PENDING")
        app.state.goals.update_status(goal_id, 1, "COMPLETED")
        app.state.goals.publish(Event(
            id=str(uuid.uuid4()), goal_id=goal_id, step_id=None, type="usage",
            payload={"role": "fixer", "provider": "p", "model": "m",
                     "input_tokens": 40, "output_tokens": 20, "total_tokens": 60,
                     "duration_ms": 90},
            timestamp=time.time(), sequence=app.state.goals.next_sequence(goal_id),
        ))

        # Backdate 90 days: the store is now idle, the goal is ancient.
        stale = time.time() - 90 * 86400
        app.state.conn.execute(
            "UPDATE goals SET created_at = ?, updated_at = ? WHERE id = ?",
            (stale, stale, goal_id),
        )
        app.state.conn.execute(
            "UPDATE events SET timestamp = ? WHERE goal_id = ? AND type = 'usage'",
            (stale, goal_id),
        )
        app.state.conn.commit()

        # A bounded window over an idle store is empty, and says so honestly.
        r = await self.client.get("/stats/overview?window=1", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(
            body["goals"]["goals"], 0,
            "a 90-day-old goal is not in the last 24 hours",
        )
        self.assertIsNone(
            body["goals"]["success_rate"],
            "reporting 100% here would be the false claim this wiring prevents",
        )
        self.assertEqual(body["usage"]["calls"], 0)
        self.assertEqual(body["daily"], [])

        # "All time" still reports it — the window is bounded, not the data.
        r = await self.client.get("/stats/overview?window=0", headers=self.headers)
        self.assertEqual(r.json()["goals"]["goals"], 1)

    async def test_stats_overview_aggregates_across_goals(self):
        """The cross-goal view: rates and spend that no single goal can answer.

        Seeded through the real publisher and two real goals, one of them
        cancelled — the rate must count COMPLETED only, never "success-ish"."""
        ws_dir = self.root / "ws-overview"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-OV", "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]

        async def mk(title: str) -> str:
            r = await self.client.post(
                "/goals", headers=self.headers,
                json={"workspace_id": ws_id, "title": title},
            )
            return r.json()["id"]

        done_id = await mk("done")
        cancel_id = await mk("cancelled")
        app.state.goals.update_status(done_id, 0, "PENDING")
        app.state.goals.update_status(done_id, 1, "COMPLETED")
        app.state.goals.update_status(cancel_id, 0, "CANCELLED")

        def ev(eid, type_, payload):
            app.state.goals.publish(Event(
                id=eid, goal_id=done_id, step_id=None, type=type_, payload=payload,
                timestamp=time.time(), sequence=app.state.goals.next_sequence(done_id),
            ))

        ev("ov1", "usage", {"role": "fixer", "provider": "ollama", "model": "q",
                            "input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
                            "duration_ms": 250})
        ev("ov2", "usage", {"role": "fixer", "provider": "ollama", "model": "q",
                            "input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
                            "duration_ms": 750})
        ev("ov3", "agent_call_failed", {"role": "critic", "provider": "p", "model": "m",
                                        "target": "primary", "code": "provider_http",
                                        "message": "500", "duration_ms": 40})

        r = await self.client.get("/stats/overview?window=0", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("generated_at", body)
        # Two terminal goals, one a cancellation: rate is 1/2, not 2/2.
        self.assertEqual(body["goals"]["goals"], 2)
        self.assertEqual(body["goals"]["succeeded"], 1)
        self.assertEqual(body["goals"]["cancelled"], 1)
        self.assertEqual(body["goals"]["success_rate"], 50)
        self.assertEqual(body["usage"]["calls"], 2)
        self.assertEqual(body["usage"]["failures"], 1)
        self.assertEqual(body["usage"]["total_tokens"], 60)
        self.assertEqual(body["usage"]["avg_duration_ms"], 500)
        self.assertEqual(body["usage"]["by_role"]["fixer"]["avg_duration_ms"], 500)
        self.assertTrue(body["daily"], "a day with activity produces a row")

        # A nonsense window clamps rather than erroring — a stats view has no
        # broken state to refuse.
        r = await self.client.get("/stats/overview?window=999", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["window_days"], 7)

        # The default window is all time.
        r = await self.client.get("/stats/overview", headers=self.headers)
        self.assertEqual(r.json()["window_days"], 0)

    async def test_stats_retention_setting_round_trips_and_prunes(self):
        """The retention policy is a real setting: clamped at the engine, echoed
        honestly, and actually enforced — a lowered policy takes effect on the
        next read, not on some future freeze."""
        # The setting appears with its bounds, and saves echo the clamped truth.
        r = await self.client.get("/settings/engine", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stats_retention_days"], {"value": 90, "min": 0, "max": 730})

        r = await self.client.put(
            "/settings/engine", headers=self.headers, json={"stats_retention_days": 2}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["saved"]["stats_retention_days"], 2)
        r = await self.client.put(
            "/settings/engine", headers=self.headers, json={"stats_retention_days": 999999}
        )
        self.assertEqual(r.json()["saved"]["stats_retention_days"], 730, "a fat-fingered forever clamps to the band")
        r = await self.client.put(
            "/settings/engine", headers=self.headers, json={"stats_retention_days": 0}
        )
        self.assertEqual(r.json()["saved"]["stats_retention_days"], 0, "0 = keep everything is storable")

        # Now the enforcement: three frozen days, retention 2, one read.
        app.state.settings.set_int("stats_retention_days", 2)
        (self.root / "ws-ret").mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-RET", "root_path": str(self.root / "ws-ret")},
        )
        ws_id = r.json()["id"]
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "retention source"},
        )
        goal_id = r.json()["id"]
        app.state.goals.update_status(goal_id, 0, "PENDING")
        app.state.goals.update_status(goal_id, 1, "COMPLETED")

        now = time.time()
        for offset in (6, 4, 2):  # three distinct past days, oldest first
            app.state.conn.execute(
                "UPDATE goals SET created_at = ?, updated_at = ? WHERE id = ?",
                (now - offset * 86400, now - offset * 86400 + 60, goal_id),
            )
            app.state.conn.execute(
                "UPDATE events SET timestamp = ? WHERE goal_id = ? AND type = 'usage'",
                (now - offset * 86400 + 120, goal_id),
            )
            app.state.conn.commit()
            r = await self.client.get("/stats/overview", headers=self.headers)
            self.assertEqual(r.status_code, 200)

        # Only the two most recent frozen days survive the policy.
        r = await self.client.get("/stats/history", headers=self.headers)
        days = r.json()["days"]
        self.assertEqual(len(days), 2, "retention kept the newest two")
        expected_oldest = time.strftime("%Y-%m-%d", time.gmtime(now - 4 * 86400))
        self.assertEqual(days[0]["day"], expected_oldest, "and they are the newest two, not any two")

    async def test_stats_history_serves_frozen_days_and_freezes_on_read(self):
        """The trend that outlives the log: a day's final document freezes on
        the first read after it ends, and /stats/history serves the frozen
        days oldest first while leaving the still-moving today to the live
        overview."""
        ws_dir = self.root / "ws-stats-history"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-SH", "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "history source"},
        )
        goal_id = r.json()["id"]
        app.state.goals.update_status(goal_id, 0, "PENDING")
        app.state.goals.update_status(goal_id, 1, "COMPLETED")
        app.state.goals.publish(Event(
            id=str(uuid.uuid4()), goal_id=goal_id, step_id=None, type="usage",
            payload={"role": "fixer", "provider": "p", "model": "m",
                     "input_tokens": 40, "output_tokens": 20, "total_tokens": 60,
                     "duration_ms": 90},
            timestamp=time.time(), sequence=app.state.goals.next_sequence(goal_id),
        ))

        # First read: everything happened today, so nothing freezes yet — but
        # the read itself must not fail on the snapshot path.
        r = await self.client.get("/stats/overview", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        r = await self.client.get("/stats/history", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["days"], [], "today is still moving and must not appear")

        # The day ends. Force the boundary by rewriting the activity's stamps
        # into a previous UTC day, then read again — the freeze must happen
        # without any special endpoint.
        app.state.conn.execute(
            "UPDATE goals SET created_at = ?, updated_at = ? WHERE id = ?",
            (time.time() - 86400 * 2, time.time() - 86400 * 2, goal_id),
        )
        app.state.conn.execute(
            "UPDATE events SET timestamp = ? WHERE goal_id = ? AND type = 'usage'",
            (time.time() - 86400 * 2, goal_id),
        )
        app.state.conn.commit()

        r = await self.client.get("/stats/overview", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        r = await self.client.get("/stats/history", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        days = r.json()["days"]
        self.assertEqual(len(days), 1, "yesterday froze on the read that noticed it ended")
        self.assertEqual(days[0]["goals"]["succeeded"], 1)
        self.assertEqual(days[0]["usage"]["total_tokens"], 60)
        # day_stats is the frozen day's OWN row, stated explicitly — the
        # document's top-level blocks are cumulative, and a chart that read
        # them per-day would redraw history as a running total.
        self.assertEqual(days[0]["day_stats"]["date"], days[0]["day"])
        self.assertEqual(days[0]["day_stats"]["succeeded"], 1)
        self.assertEqual(days[0]["day_stats"]["created"], 1)
        self.assertEqual(days[0]["day_stats"]["total_tokens"], 60)

        # The freeze is idempotent: another read neither duplicates nor rewrites.
        await self.client.get("/stats/overview", headers=self.headers)
        r = await self.client.get("/stats/history", headers=self.headers)
        self.assertEqual(len(r.json()["days"]), 1)

        # The export asks for every stored day explicitly; zero is the sentinel
        # for that unbounded request, not the same as a one-row limit.
        r = await self.client.get("/stats/history?limit=0", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["days"]), 1)

    async def test_enable_execution_rejects_a_stale_version(self):
        """The route documented a version check it never performed (B5).

        A client enabling execution from a stale view must get a 409, not
        silently lift the guard on a plan the user has since edited.
        """
        ws_dir = self.root / "ws-stale-enable"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-STALE-EN", "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Stale enable", "plan_only": True},
        )
        goal_id = r.json()["id"]
        app.state.goals.update_status(goal_id, 0, "PENDING")
        app.state.executor._insert_steps(
            goal_id, [{"title": "S1", "description": "d", "suggested_paths": []}]
        )

        # Bump the version behind the client's back (a status change landing
        # elsewhere — the only mutation that moves the version).
        g_now = app.state.goals.get(goal_id)
        app.state.goals.update_status(goal_id, g_now.version, "PAUSED")
        app.state.goals.update_status(goal_id, g_now.version + 1, "PENDING")

        r = await self.client.post(
            f"/goals/{goal_id}/enable-execution", headers=self.headers,
            json={"expected_version": g_now.version},  # the stale view
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "version_conflict")
        # The guard is still up — nothing was enabled.
        self.assertTrue(app.state.goals.get(goal_id).plan_only)

        # The fresh version is accepted and the guard lifts.
        g_fresh = app.state.goals.get(goal_id)
        r = await self.client.post(
            f"/goals/{goal_id}/enable-execution", headers=self.headers,
            json={"expected_version": g_fresh.version},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["plan_only"])

    async def test_edit_plan_steps(self):
        """Plan-only goals: PATCH step before execution, guards after."""
        ws_dir = self.root / "ws-edit"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-EDIT", "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Editable plan", "plan_only": True},
        )
        goal_id = r.json()["id"]
        app.state.goals.update_status(goal_id, 0, "PENDING")
        app.state.executor._insert_steps(
            goal_id,
            [
                {"title": "Step 1", "description": "original", "suggested_paths": ["a.txt"]},
                {"title": "Step 2", "description": "other", "suggested_paths": []},
            ],
        )
        g = app.state.goals.get(goal_id)
        steps = app.state.goals.steps(goal_id)
        step_id = steps[0].id

        # Happy path: edit title, description, and paths in one patch.
        r = await self.client.patch(
            f"/goals/{goal_id}/steps/{step_id}", headers=self.headers,
            json={
                "expected_version": g.version,
                "title": "Renamed step",
                "description": "edited description",
                "suggested_paths": [" b.txt ", "", "c.txt"],
            },
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["title"], "Renamed step")
        self.assertEqual(body["description"], "edited description")
        self.assertEqual(body["suggested_paths"], ["b.txt", "c.txt"])  # trimmed + dropped empties

        # Version bumped by the edit; plan_updated event emitted.
        g2 = app.state.goals.get(goal_id)
        self.assertEqual(g2.version, g.version + 1)
        events = app.state.goals.events_after(goal_id, 0)
        plan_events = [e for e in events if e.type == "plan_updated"]
        self.assertEqual(len(plan_events), 1)
        self.assertEqual(plan_events[0].payload["step_id"], step_id)
        # The event carries before/after per field so the transcript can show
        # the drift itself — paths gate parallel batching, so their edit is
        # execution-relevant history, not just a UI refresh signal.
        changes = plan_events[0].payload["changes"]
        self.assertEqual(changes["suggested_paths"]["before"], ["a.txt"])
        self.assertEqual(changes["suggested_paths"]["after"], ["b.txt", "c.txt"])
        self.assertEqual(changes["title"]["before"], "Step 1")
        self.assertEqual(changes["title"]["after"], "Renamed step")

        # A title-only edit announces only the title — the event names what the
        # patch edited, not the merged record (which always carries all fields).
        r = await self.client.patch(
            f"/goals/{goal_id}/steps/{step_id}", headers=self.headers,
            json={"expected_version": g2.version, "title": "Renamed again"},
        )
        self.assertEqual(r.status_code, 200)
        g3 = app.state.goals.get(goal_id)
        plan_events = [e for e in app.state.goals.events_after(goal_id, 0) if e.type == "plan_updated"]
        self.assertEqual(len(plan_events), 2)
        self.assertEqual(plan_events[-1].payload["fields"], ["title"])

        # Stale expected_version -> 409 version_conflict.
        r = await self.client.patch(
            f"/goals/{goal_id}/steps/{step_id}", headers=self.headers,
            json={"expected_version": g.version, "title": "Stale edit"},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "version_conflict")

        # Unknown step -> 404.
        r = await self.client.patch(
            f"/goals/{goal_id}/steps/nope", headers=self.headers,
            json={"expected_version": g2.version, "title": "X"},
        )
        self.assertEqual(r.status_code, 404)

        # Empty patch -> 422.
        r = await self.client.patch(
            f"/goals/{goal_id}/steps/{step_id}", headers=self.headers,
            json={"expected_version": g3.version},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "empty_patch")

        # Once execution starts (status RUNNING), edits are refused.
        async def mock_run_step(gid, sid):
            await asyncio.sleep(0)
        app.state.executor.run_step = mock_run_step
        r = await self.client.post(
            f"/goals/{goal_id}/enable-execution", headers=self.headers,
            json={"expected_version": g3.version},
        )
        version = r.json()["version"]
        r = await self.client.post(
            f"/goals/{goal_id}/start", headers=self.headers,
            json={"expected_version": version},
        )
        self.assertEqual(r.status_code, 200)
        r = await self.client.patch(
            f"/goals/{goal_id}/steps/{step_id}", headers=self.headers,
            json={"expected_version": version + 1, "title": "Too late"},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "illegal_status")

    async def test_retry_of_an_unknown_step_is_a_404(self):
        """A bad step id is a client mistake: 404, not a model-defect retry.

        The lookup used to raise AgentOutputInvalid — the class the fallback
        machinery retries on — so a typo'd id earned a 500 and a pointless
        second run that failed identically.
        """
        ws_dir = self.root / "ws-retry-404"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-RETRY-404", "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Retry target"},
        )
        goal_id = r.json()["id"]
        g = app.state.goals.get(goal_id)
        app.state.goals.update_status(goal_id, g.version, "FAILED")

        r = await self.client.post(
            f"/goals/{goal_id}/steps/nope/retry", headers=self.headers,
            json={"expected_version": app.state.goals.get(goal_id).version},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "unknown_step")

    async def test_recent_models_endpoint_feeds_the_pickers(self):
        """`/models/recent` is what orders and badges the two model menus.

        It must answer with something usable on a fresh install (an empty list, not
        an error) and with the models that actually answered once a goal has run.
        """
        r = await self.client.get("/models/recent", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [], "a fresh install ran nothing yet")

        ws_dir = self.root / "ws_recent"
        ws_dir.mkdir()
        ws = (
            await self.client.post(
                "/workspaces",
                headers=self.headers,
                json={"name": "WS", "root_path": str(ws_dir)},
            )
        ).json()
        goal = (
            await self.client.post(
                "/goals",
                headers=self.headers,
                json={
                    "workspace_id": ws["id"],
                    "title": "Goal",
                    "description": "",
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                },
            )
        ).json()
        app.state.goals.publish(
            Event(
                id="a1",
                goal_id=goal["id"],
                step_id=None,
                type="agent_assigned",
                payload={"role": "fixer", "provider": "ollama", "model": "qwen3:8b"},
                timestamp=100.0,
                sequence=1,
            )
        )

        r = await self.client.get("/models/recent?limit=3", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["provider"], "ollama")
        self.assertEqual(body[0]["model"], "qwen3:8b")
        self.assertEqual(body[0]["role"], "fixer")
        # The model the goal *asked* for is deliberately absent: it never ran.
        self.assertNotIn("claude-sonnet-4-6", [m["model"] for m in body])

        # A limit outside the allowed range is refused, not silently clamped.
        r = await self.client.get("/models/recent?limit=0", headers=self.headers)
        self.assertEqual(r.status_code, 422)

    def _mkstep(self, goal_id: str, sid: str) -> PlanStep:
        return PlanStep(
            id=sid, goal_id=goal_id, ordinal=0, title=sid, description="",
            status="PENDING", suggested_paths=[],
        )

    async def test_usage_reports_parallel_peak_and_waves(self):
        """The usage endpoint reconstructs how wide a run was from the log.

        A two-step parallel goal with staggered overlap: peak 2, one wave. A
        sequential goal of the same shape: peak 1, two waves. This is the
        after-the-fact evidence the usage card shows, so both halves matter.
        """
        ws = app.state.workspaces.create(
            WorkspaceCreate(name="peakws", root_path=str(self.root))
        )
        goal = app.state.goals.create(
            GoalCreate(workspace_id=ws.id, title="peak", description="", parallel=True)
        )
        # Two steps with genuinely overlapping lifetimes.
        g = app.state.goals.get(goal.id)
        app.state.goals.update_status(goal.id, g.version, "RUNNING")
        for sid in ("s1", "s2"):
            app.state.executor._set_step(goal.id, PlanStep(id=sid, goal_id=goal.id, ordinal=0, title=sid, description="", status="PENDING", suggested_paths=[]), "IN_PROGRESS")
        app.state.executor._set_step(goal.id, self._mkstep(goal.id, "s1"), "COMPLETED")
        app.state.executor._set_step(goal.id, self._mkstep(goal.id, "s2"), "COMPLETED")

        r = await self.client.get(f"/goals/{goal.id}/usage", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["parallel_peak"], 2, body)
        self.assertEqual(body["parallel_waves"], 1, body)

        # Sequential goal of the same shape: every step starts on an empty set.
        goal2 = app.state.goals.create(
            GoalCreate(workspace_id=ws.id, title="seq", description="")
        )
        g2 = app.state.goals.get(goal2.id)
        app.state.goals.update_status(goal2.id, g2.version, "RUNNING")
        for sid in ("t1", "t2"):
            app.state.executor._set_step(goal2.id, PlanStep(id=sid, goal_id=goal2.id, ordinal=0, title=sid, description="", status="PENDING", suggested_paths=[]), "IN_PROGRESS")
            app.state.executor._set_step(goal2.id, PlanStep(id=sid, goal_id=goal2.id, ordinal=0, title=sid, description="", status="PENDING", suggested_paths=[]), "COMPLETED")
        r = await self.client.get(f"/goals/{goal2.id}/usage", headers=self.headers)
        body = r.json()
        self.assertEqual(body["parallel_peak"], 1, body)
        self.assertEqual(body["parallel_waves"], 2, body)

    async def test_audit_trail_collects_edits_fallbacks_and_failures(self):
        """The audit endpoint reconstructs the run's full structured trail.

        One goal exercises every auditable lane: a plan edit with real
        before/after values, a provider fallback naming who was skipped and
        who answered instead, a fix retry, an error with its responsible
        role, and a step that never got a terminal status (it must report
        IN_PROGRESS, not pretend to have finished).
        """
        ws = app.state.workspaces.create(
            WorkspaceCreate(name="auditws", root_path=str(self.root))
        )
        goal = app.state.goals.create(
            GoalCreate(workspace_id=ws.id, title="audit-me", description="", parallel=True)
        )
        g = app.state.goals.get(goal.id)
        app.state.goals.update_status(goal.id, g.version, "RUNNING")
        app.state.executor._set_step(goal.id, self._mkstep(goal.id, "s1"), "IN_PROGRESS")

        # A plan edit that actually changed something, plus a no-op field the
        # document must not report as drift. Sequences come from the store so
        # they interleave with the goal_status/step_status events already
        # published above.
        def ev(eid, type_, payload, ts):
            return Event(
                id=eid, goal_id=goal.id, step_id="s1", type=type_, payload=payload,
                timestamp=ts, sequence=app.state.goals.next_sequence(goal.id),
            )

        app.state.goals.publish(ev("e1", "plan_updated", {
            "step_id": "s1", "step_title": "s1", "fields": ["suggested_paths"],
            "changes": {
                "suggested_paths": {"before": ["alpha.txt"], "after": ["gamma.txt"]},
                "description": {"before": "same", "after": "same"},
            },
        }, 100.0))
        app.state.goals.publish(ev("e2", "provider_fallback", {
            "role": "fixer",
            "from": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "to": {"provider": "ollama", "model": "qwen3:8b"},
            "code": "connection_refused", "detail": "primary down",
        }, 101.0))
        app.state.goals.publish(ev("e3", "fix_retry", {
            "attempt": 1, "max_attempts": 2, "reason": "tests failed",
        }, 102.0))
        app.state.goals.publish(ev("e4", "error", {
            "code": "model_output_invalid", "message": "bad json", "role": "fixer",
        }, 103.0))
        # Role transition inside the step: the engine re-publishes IN_PROGRESS
        # at fixer → verifier → critic → scribe. This is NOT a new attempt —
        # the count must stay 1 (the regression that once read attempts: 5).
        app.state.goals.publish(ev("e5", "step_status", {"status": "IN_PROGRESS"}, 104.0))
        # Token usage: two fixer calls (one via the fallback target) and one
        # planner call — the by-role split must reflect who actually spent.
        app.state.goals.publish(ev("u1", "usage", {
            "role": "fixer", "provider": "anthropic", "model": "claude-sonnet-4-6",
            "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
        }, 105.0))
        app.state.goals.publish(ev("u2", "usage", {
            "role": "fixer", "provider": "ollama", "model": "qwen3:8b",
            "input_tokens": 50, "output_tokens": 30, "total_tokens": 80,
        }, 106.0))
        app.state.goals.publish(ev("u3", "usage", {
            "role": "planner", "provider": "ollama", "model": "qwen3:8b",
            "input_tokens": 200, "output_tokens": 50, "total_tokens": 250,
        }, 107.0))
        # s1 never gets a terminal status — the engine died here.

        # Assignment without completion: the critic was announced but never
        # produced a model call — it must be flagged as silent. The fixer and
        # planner DID complete calls (usage events above), so they must not
        # be flagged even though the goal died mid-run.
        app.state.goals.publish(ev("a1", "agent_assigned", {
            "role": "critic", "provider": "ollama", "model": "qwen3:8b",
        }, 108.0))
        app.state.goals.publish(ev("a2", "agent_assigned", {
            "role": "fixer", "provider": "ollama", "model": "qwen3:8b",
        }, 109.0))

        r = await self.client.get(f"/goals/{goal.id}/audit", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["final_status"], "RUNNING")
        self.assertEqual(body["mode"], {"plan_only": False, "dry_run": False, "parallel": True})

        # Plan edits: only genuinely-changed fields, with before/after.
        self.assertEqual(len(body["plan_edits"]), 1, body["plan_edits"])
        edit = body["plan_edits"][0]
        self.assertEqual(edit["fields"], ["suggested_paths"])
        self.assertEqual(edit["changes"]["suggested_paths"], {"from": ["alpha.txt"], "to": ["gamma.txt"]})

        self.assertEqual(len(body["fallbacks"]), 1)
        fb = body["fallbacks"][0]
        self.assertEqual(fb["role"], "fixer")
        self.assertEqual(fb["from"]["provider"], "anthropic")
        self.assertEqual(fb["to"]["model"], "qwen3:8b")
        self.assertEqual(fb["code"], "connection_refused")

        self.assertEqual(len(body["fix_retries"]), 1)
        self.assertEqual(body["fix_retries"][0]["attempt"], 1)

        self.assertEqual(len(body["errors"]), 1)
        err = body["errors"][0]
        self.assertEqual(err["role"], "fixer")
        self.assertEqual(err["code"], "model_output_invalid")

        # The step open at sweep end keeps its in-flight status...
        self.assertEqual(len(body["step_outcomes"]), 1)
        step = body["step_outcomes"][0]
        self.assertEqual(step["status"], "IN_PROGRESS")
        self.assertEqual(step["attempts"], 1)
        # ...and the parallel sweep still sees it as the peak.
        self.assertEqual(body["parallel_peak"], 1)
        self.assertEqual(body["parallel_waves"], 1)

        # Status timeline records every published transition in order (the
        # initial PENDING comes from the row, not an event).
        statuses = [t["status"] for t in body["status_timeline"]]
        self.assertEqual(statuses, ["RUNNING"])

        # Unknown goal → 404 like every other goal route.
        r = await self.client.get("/goals/does-not-exist/audit", headers=self.headers)
        self.assertEqual(r.status_code, 404)

        # Token usage rides in the same document, aggregated exactly as the
        # usage endpoint aggregates it: per role (fixer spent on two models),
        # per model, and totals that count each call once.
        usage = body["usage"]
        self.assertEqual(usage["calls"], 3)
        self.assertEqual(usage["totals"]["total_tokens"], 120 + 80 + 250)
        self.assertEqual(usage["by_role"]["fixer"]["total_tokens"], 200)
        self.assertEqual(usage["by_role"]["fixer"]["calls"], 2)
        self.assertEqual(usage["by_role"]["planner"]["total_tokens"], 250)
        self.assertEqual(usage["by_model"]["anthropic/claude-sonnet-4-6"]["total_tokens"], 120)
        self.assertEqual(usage["by_model"]["ollama/qwen3:8b"]["total_tokens"], 80 + 250)
        # And the usage endpoint must agree with the audit export to the token.
        r = await self.client.get(f"/goals/{goal.id}/usage", headers=self.headers)
        live = r.json()
        self.assertEqual(live["totals"], usage["totals"])
        self.assertEqual(live["by_role"], usage["by_role"])
        self.assertEqual(live["by_model"], usage["by_model"])

        # Silent roles: this goal is still RUNNING, so no verdict is issued —
        # its roles may simply not have taken their turn yet. In-flight runs
        # must never be called silent, or every healthy goal would be flagged.
        self.assertEqual(body["silent_roles"], [])

        # Drive the goal to a terminal status: NOW the missing critic is
        # judged. fixer and planner ran (usage above) and stay unflagged.
        g2 = app.state.goals.get(goal.id)
        app.state.goals.update_status(goal.id, g2.version, "FAILED")
        r = await self.client.get(f"/goals/{goal.id}/audit", headers=self.headers)
        body = r.json()
        silent = body["silent_roles"]
        self.assertEqual([s["role"] for s in silent], ["critic"])
        self.assertEqual(silent[0]["assigned_model"], "ollama/qwen3:8b")

    async def test_engine_settings_roundtrip_clamp_and_unknown_key(self):
        """GET/PUT /settings/engine: values persist, clamp, and reject typos."""
        r = await self.client.get("/settings/engine", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["parallel_width"]["value"], 4)
        self.assertEqual(body["parallel_width"]["min"], 1)
        self.assertEqual(body["parallel_width"]["max"], 16)

        # A save persists and echoes what was stored.
        r = await self.client.put(
            "/settings/engine", headers=self.headers, json={"parallel_width": 8}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["saved"]["parallel_width"], 8)
        r = await self.client.get("/settings/engine", headers=self.headers)
        self.assertEqual(r.json()["parallel_width"]["value"], 8)

        # Out-of-band values are clamped, and the clamp is what comes back.
        r = await self.client.put(
            "/settings/engine", headers=self.headers, json={"parallel_width": 999}
        )
        self.assertEqual(r.json()["saved"]["parallel_width"], 16)

        # A typo'd key is refused, not silently ignored.
        r = await self.client.put(
            "/settings/engine", headers=self.headers, json={"paralel_width": 4}
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "unknown_setting")

    async def test_provider_switch_does_not_carry_the_key_ref_over(self):
        """A credential stored for provider A must never be sent to provider B.

        `api_key_ref` is saved under the provider the role had at save time; a
        patch that switches provider must null it, or ProviderFactory resolves
        the old key and POSTs it to the new provider's endpoint.
        """
        r = await self.client.put(
            "/settings/agents/planner",
            headers=self.headers,
            json={"api_key": "sk-anthropic-secret"},
        )
        self.assertEqual(r.status_code, 200)
        planner = r.json()
        self.assertTrue(planner["api_key_ref"])

        # Switch provider with no new key: the ref belongs to the old provider.
        r = await self.client.put(
            "/settings/agents/planner",
            headers=self.headers,
            json={"provider": "openai"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["api_key_ref"])

        # An explicit key in the same patch as the switch wins — the ref points
        # at the key the user just supplied, not a stale one.
        r = await self.client.put(
            "/settings/agents/planner",
            headers=self.headers,
            json={"provider": "deepseek", "api_key": "sk-deepseek-new"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["api_key_ref"])

    async def test_apply_rejects_a_stale_version(self):
        """POST /apply is version-guarded like every other mutating goal route."""
        ws_dir = self.root / "ws-applyv"
        ws_dir.mkdir()
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": "WS-APPLYV", "root_path": str(ws_dir)},
        )
        ws_id = r.json()["id"]
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Dry run", "dry_run": True},
        )
        goal_id = r.json()["id"]
        g = app.state.goals.get(goal_id)
        app.state.goals.update_status(goal_id, 0, "COMPLETED")
        # Seed a proposal so the route reaches the version check, not 409
        # nothing_to_apply first.
        app.state.executor._store_proposed_files(
            goal_id, "step-1",
            [{"path": "x.txt", "action": "write", "content": "hi"}],
        )
        g = app.state.goals.get(goal_id)

        # A client answering from a stale view must 409, not double-apply.
        r = await self.client.post(
            f"/goals/{goal_id}/apply", headers=self.headers,
            json={"expected_version": g.version + 5},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "version_conflict")

        # The current version is accepted (the apply itself is dispatched to a
        # background task; the assertion is the gate, not the run).
        r = await self.client.post(
            f"/goals/{goal_id}/apply", headers=self.headers,
            json={"expected_version": g.version},
        )
        self.assertEqual(r.status_code, 200)

    def test_ws_refuses_an_unknown_goal_after_auth(self):
        """Authenticated socket to a goal that does not exist → close 4404.

        The old endpoint accepted every socket and only ever closed 4401, so a
        bad goal id surfaced as a mystery stream that sends nothing.
        """
        try:
            from starlette.testclient import TestClient
        except ImportError:
            self.skipTest("starlette testclient's websocket support unavailable")
        from starlette.websockets import WebSocketDisconnect
        # TestClient enters lifespan, which would rebuild app.state on the real
        # home store — so reuse the isolated state ASGI app is not possible; but
        # TestClient(app) raises on lifespan only if it errors. The engine's
        # lifespan honors CODIFY_HOME/CODIFY_SECRETS, which tests/hermetic.py
        # has already pointed at a throwaway dir for this process.
        with TestClient(app) as client:
            with self.assertRaises(WebSocketDisconnect) as ctx:
                with client.websocket_connect(
                    "/ws/goals/does-not-exist",
                    headers={"Authorization": f"Bearer {app.state.token}"},
                ) as ws:
                    ws.receive_text()
            self.assertEqual(ctx.exception.code, 4404)


if __name__ == "__main__":
    unittest.main()
