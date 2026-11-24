"""Regression tests for Item 9: Offline mode (`offline_mode` setting).

Proves all engine-side guarantees of the single switch:
1. Chat & agent roles may only call local loopback endpoints (remote providers raise `offline_mode`).
2. Web fetch (`_web_policy`) is forced to `MODE_OFF` even if `web_fetch` was set to 1 or 2.
3. Remote MCP tools are removed from the conductor menu, refused in `list_mcp_tools`/`call_mcp`,
   and blocked on `POST /settings/mcp` and `POST /settings/mcp/{id}/probe` (`409 offline_mode`).
4. Remote provider discovery/health pings are skipped in `ModelCatalogService` while local
   loopback endpoints are still discovered, and toggling `offline_mode` invalidates the cache.
5. Background GitHub CI suggestions (`suggest_ci`) are skipped while offline mode is active.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — isolated CODIFY_HOME

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from httpx import ASGITransport

from engine.app import BOOT_TOKEN, _is_offline_mode, app
from engine.conductor_tools import ConductorTools
from engine.db import connect
from engine.executor import ExecutorService
from engine.mcp import McpService
from engine.model_catalog import ModelCatalogService
from engine.models import AgentConfig, AgentConfigUpdate, GoalCreate, WorkspaceCreate
from engine.providers import Keychain, ProviderError, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, SettingsService, WorkspaceService
from engine.skills import SkillSet
from engine.suggestions import SuggestionWatch
from engine.web_fetch import MODE_ANY, MODE_OFF


class _FakeMcp:
    def has_admitted_tools(self, _goal_running: bool) -> bool:
        return True

    def list_for_model(self, _server: str, *, goal_running: bool) -> str:
        raise AssertionError("MCP must not be called while offline_mode is on")

    async def call_tool(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("MCP must not be called while offline_mode is on")


class TestOfflineMode(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.conn = connect(self.root / "offline.db")
        self.settings = SettingsService(self.conn)
        self.keychain = Keychain()
        self.factory = ProviderFactory(
            self.keychain, is_offline=lambda: _is_offline_mode(self.settings)
        )
        self.registry = AgentRegistryService(self.conn, self.factory, self.keychain)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.mcp = McpService(self.conn, self.keychain)
        self.ws_dir = self.root / "repo"
        self.ws_dir.mkdir()
        (self.ws_dir / "README.md").write_text("# offline fixture\n")
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.ws_dir)))

        self._prev_state = {
            k: getattr(app.state, k, None)
            for k in ("conn", "settings", "keychain", "factory", "registry", "goals", "workspaces", "mcp", "models", "token")
        }
        app.state.conn = self.conn
        app.state.settings = self.settings
        app.state.keychain = self.keychain
        app.state.factory = self.factory
        app.state.registry = self.registry
        app.state.goals = self.goals
        app.state.workspaces = self.workspaces
        app.state.mcp = self.mcp
        app.state.token = BOOT_TOKEN
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}
        self.client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        self.keychain.delete_provider_key("openai")
        for k, v in self._prev_state.items():
            if v is None:
                if hasattr(app.state, k):
                    delattr(app.state, k)
            else:
                setattr(app.state, k, v)
        await self.client.aclose()
        self.conn.close()
        self.tmp.cleanup()

    async def test_local_models_allowed_and_remote_models_refused_when_offline(self) -> None:
        self.settings.set_int("offline_mode", 1)

        remote_cfg = AgentConfig(
            role="planner",
            display_name="Planner",
            provider="openai",
            protocol="openai_compat",
            model_name="gpt-4o",
            base_url="https://api.openai.com/v1",
        )
        with self.assertRaises(ProviderError) as ctx:
            self.factory.build(remote_cfg)
        self.assertEqual("offline_mode", ctx.exception.code)

        local_cfg = AgentConfig(
            role="planner",
            display_name="Planner",
            provider="ollama",
            protocol="ollama",
            model_name="qwen2.5-coder:7b",
            base_url="http://127.0.0.1:11434",
        )
        provider = self.factory.build(local_cfg)
        self.assertIsNotNone(provider)

    async def test_web_fetch_and_mcp_are_disabled_in_conductor_when_offline(self) -> None:
        self.settings.set_int("web_fetch", MODE_ANY)
        self.settings.set_int("offline_mode", 1)

        executor = ExecutorService(
            self.goals,
            self.workspaces,
            self.registry,
            SandboxService(self.settings),
        )
        executor.settings = self.settings
        executor.mcp = _FakeMcp()  # type: ignore[assignment]
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="t", description=""))

        self.assertEqual(MODE_OFF, executor._web_policy().mode)
        offered_names = {t.name for t in executor.conductor_menu(goal.id)()}
        self.assertNotIn("fetch_page", offered_names)
        self.assertNotIn("list_mcp_tools", offered_names)
        self.assertNotIn("call_mcp", offered_names)

        tools = ConductorTools(executor, goal.id, goal, str(self.ws_dir), SkillSet(skills=(), problems=(), shadows=()))
        list_msg = await tools.list_mcp_tools({"server": "docs"})
        call_msg = await tools.call_mcp({"server": "docs", "tool": "search", "arguments": {}})
        self.assertIn("Offline mode is on", list_msg)
        self.assertIn("Offline mode is on", call_msg)

    async def test_mcp_http_create_and_probe_return_409_offline_mode(self) -> None:
        r_save = await self.client.put(
            "/settings/engine", headers=self.headers, json={"offline_mode": True}
        )
        self.assertEqual(200, r_save.status_code, r_save.text)
        self.assertEqual(1, r_save.json()["saved"]["offline_mode"])

        r_create = await self.client.post(
            "/settings/mcp",
            headers=self.headers,
            json={"name": "Docs", "url": "https://mcp.example.com/mcp", "auth_kind": "none"},
        )
        self.assertEqual(409, r_create.status_code, r_create.text)
        self.assertEqual("offline_mode", r_create.json()["code"])

        r_probe = await self.client.post(
            "/settings/mcp/any-id/probe", headers=self.headers
        )
        self.assertEqual(409, r_probe.status_code, r_probe.text)
        self.assertEqual("offline_mode", r_probe.json()["code"])

    async def test_model_catalog_skips_remote_pings_when_offline_and_still_checks_local(self) -> None:
        self.keychain.set_provider_key("openai", "sk-test-offline-key")
        self.registry.set_config(
            "planner",
            AgentConfigUpdate(
                provider="ollama",
                model_name="qwen2.5-coder:7b",
                base_url="http://127.0.0.1:11434",
            ),
        )
        seen_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_hosts.append(request.url.host or "")
            if request.url.host == "127.0.0.1":
                return httpx.Response(
                    200, json={"models": [{"name": "qwen2.5-coder:7b"}]}
                )
            return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

        catalog = ModelCatalogService(
            self.registry,
            self.keychain,
            transport=httpx.MockTransport(handler),
            is_offline=lambda: _is_offline_mode(self.settings),
        )
        app.state.models = catalog

        # Turn offline mode on via the API route (also tests cache invalidation).
        r = await self.client.put(
            "/settings/engine", headers=self.headers, json={"offline_mode": True}
        )
        self.assertEqual(200, r.status_code, r.text)

        payload = await catalog.get(refresh=True)
        self.assertNotIn("api.openai.com", seen_hosts, "remote provider must not be pinged offline")
        self.assertIn("127.0.0.1", seen_hosts, "local Ollama endpoint should still be discovered")
        model_ids = [m["id"] for m in payload["models"]]
        self.assertIn("qwen2.5-coder:7b", model_ids)
        self.assertNotIn("gpt-4o", model_ids)
        by_provider = {p["provider"]: p for p in payload["providers"]}
        self.assertIn("offline mode", str(by_provider["openai"]["error"]).lower())

    async def test_suggestions_worker_skips_github_ci_when_offline(self) -> None:
        self.settings.set_int("suggestions", 1)
        self.settings.set_int("suggest_ci", 1)
        self.settings.set_int("offline_mode", 1)

        ci_calls: list[str] = []

        async def fake_ci(root: str) -> dict[str, Any]:
            ci_calls.append(root)
            return {}

        worker = SuggestionWatch(
            self.conn,
            self.settings,
            self.workspaces,
            ci_checker=fake_ci,
        )
        await worker.sweep(now=1_700_000_000.0)
        self.assertEqual([], ci_calls, "CI suggestions must not contact GitHub in offline mode")

    async def test_codify_offline_env_enforces_all_engine_paths_and_settings_view(self) -> None:
        # Preserve the v1.9.0 bug: CODIFY_OFFLINE=1 was ignored by _web_policy, MCP conductor tools,
        # read_ci, SuggestionWatch, SandboxService, and GET /settings/engine.
        self.settings.set_int("web_fetch", MODE_ANY)
        self.settings.set_int("suggestions", 1)
        self.settings.set_int("suggest_ci", 1)
        self.settings.set_int("sandbox_network", 1)

        ci_calls: list[str] = []

        async def fake_ci(root: str) -> dict[str, Any]:
            ci_calls.append(root)
            return {}

        with patch.dict(os.environ, {"CODIFY_OFFLINE": "1"}):
            r_settings = await self.client.get("/settings/engine", headers=self.headers)
            self.assertEqual(200, r_settings.status_code, r_settings.text)
            self.assertEqual(1, r_settings.json()["offline_mode"]["value"])

            executor = ExecutorService(
                self.goals,
                self.workspaces,
                self.registry,
                SandboxService(self.settings),
            )
            executor.settings = self.settings
            executor.mcp = _FakeMcp()  # type: ignore[assignment]
            goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="env offline", description=""))

            self.assertEqual(MODE_OFF, executor._web_policy().mode)
            offered_names = {t.name for t in executor.conductor_menu(goal.id)()}
            self.assertNotIn("fetch_page", offered_names)
            self.assertNotIn("list_mcp_tools", offered_names)
            self.assertNotIn("call_mcp", offered_names)
            self.assertNotIn("read_ci", offered_names)

            tools = ConductorTools(executor, goal.id, goal, str(self.ws_dir), SkillSet(skills=(), problems=(), shadows=()))
            self.assertIn("Offline mode is on", await tools.list_mcp_tools({"server": "docs"}))
            self.assertIn("Offline mode is on", await tools.call_mcp({"server": "docs", "tool": "search", "arguments": {}}))
            self.assertIn("Offline mode is on", await tools.read_ci({}))

            sandbox = SandboxService(self.settings)
            self.assertFalse(sandbox._network_allowed(True))
            self.assertFalse(sandbox._network_allowed(None))

            worker = SuggestionWatch(self.conn, self.settings, self.workspaces, ci_checker=fake_ci)
            await worker.sweep(now=1_700_000_000.0)
            self.assertEqual([], ci_calls)

    async def test_offline_mode_blocks_read_ci_workspace_ci_sandbox_and_mcp_url_change(self) -> None:
        self.settings.set_int("sandbox_network", 1)
        self.settings.set_int("offline_mode", 1)

        # 1. Sandbox network is forced off even when goal allow_network=True or sandbox_network=1.
        sandbox = SandboxService(self.settings)
        self.assertFalse(sandbox._network_allowed(True))
        self.assertFalse(sandbox._network_allowed(None))

        # 2. Conductor read_ci tool is omitted from menu and refuses if called directly.
        executor = ExecutorService(self.goals, self.workspaces, self.registry, sandbox)
        executor.settings = self.settings
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="ci offline", description=""))
        offered_names = {t.name for t in executor.conductor_menu(goal.id)()}
        self.assertNotIn("read_ci", offered_names)
        tools = ConductorTools(executor, goal.id, goal, str(self.ws_dir), SkillSet(skills=(), problems=(), shadows=()))
        with patch("engine.github_remote.read_checks", new_callable=AsyncMock) as mock_checks:
            msg = await tools.read_ci({})
            self.assertIn("Offline mode is on", msg)
            mock_checks.assert_not_awaited()

            # 3. GET /workspaces/{id}/ci must not contact GitHub when offline.
            r_ci = await self.client.get(f"/workspaces/{self.ws.id}/ci", headers=self.headers)
            self.assertEqual(200, r_ci.status_code, r_ci.text)
            self.assertEqual("unknown", r_ci.json()["state"])
            self.assertIn("offline", str(r_ci.json().get("reason") or "").lower())
            mock_checks.assert_not_awaited()

        # 4. PUT /settings/mcp/{id} changing URL must be refused offline (no DNS lookup),
        #    while toggling enabled=False locally stays allowed.
        self.conn.execute(
            "INSERT INTO mcp_servers "
            "(id, name, url, auth_kind, auth_header, api_key_ref, protocol_version, server_name, "
            " enabled, last_probe_at, last_probe_error, created_at, updated_at) "
            "VALUES ('srv-1', 'Docs', 'https://mcp.example.com/mcp', 'none', '', '', '', '', 1, NULL, NULL, 1.0, 1.0)"
        )
        self.conn.commit()
        r_url = await self.client.put(
            "/settings/mcp/srv-1",
            headers=self.headers,
            json={"url": "https://other.example.com/mcp"},
        )
        self.assertEqual(409, r_url.status_code, r_url.text)
        self.assertEqual("offline_mode", r_url.json()["code"])

        r_disable = await self.client.put(
            "/settings/mcp/srv-1",
            headers=self.headers,
            json={"enabled": False},
        )
        self.assertEqual(200, r_disable.status_code, r_disable.text)
        self.assertFalse(r_disable.json()["enabled"])


if __name__ == "__main__":
    unittest.main()
