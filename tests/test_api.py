from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import asyncio
import os
import tempfile
import unittest
from pathlib import Path

import httpx
from httpx import ASGITransport

from unittest.mock import patch

from engine.app import app, BOOT_TOKEN
from engine.db import connect
from engine.executor import ExecutorService
from engine.model_catalog import ModelCatalogService
from engine.models import Event, ROLES
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService


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
        app.state.executor = ExecutorService(app.state.goals, app.state.workspaces, app.state.registry, app.state.sandbox)
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


if __name__ == "__main__":
    unittest.main()
