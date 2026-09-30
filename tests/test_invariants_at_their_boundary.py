"""The invariants of docs/00 section 6, each held at the boundary it names: a route, not a constructor.

Most of the nine already had a test that named them. What a test that names an invariant does not show is
that it fails when the invariant is broken: a role list that grows a ninth name consistently (the roles, the
seeds, the prompts) passes every test that only checks the tables agree with each other, and a route that
echoes a stored key passes every test that only checks the key was stored. So each class here states one
invariant the way the doc does and asserts it about the running app, and each was checked by breaking the
enforcement and watching the named test fail (`docs/audit-2026-09-29.md`, section 0).

What is not covered, stated so it is not assumed: WebSocket frames (events, which are not built from
configuration) in the key sweep, and the two routes that act on the outside world in the config sweep.
"""

from __future__ import annotations

import re
import tempfile
import typing
import unittest
import uuid
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport
from starlette.routing import Route

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.executor import ExecutorService
from engine.model_catalog import ModelCatalogService
from engine.models import ROLES, AgentRole
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import (
    AgentRegistryService,
    ConversationService,
    GoalService,
    SettingsService,
    WorkspaceService,
)
from engine.stats_history import StatsSnapshotService
from engine.stats_import import StatsImportService

EIGHT = ["laya", "librarian", "design", "planner", "fixer", "verifier", "critic", "scribe"]


class BoundaryCase(unittest.IsolatedAsyncioTestCase):
    """The engine's app over a throwaway store, with nothing that could reach a model or the network."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "test.db")
        keychain = Keychain(secrets_path=self.root / "secrets.json")
        self.keychain = keychain
        app.state.conn = self.conn
        app.state.keychain = keychain
        app.state.registry = AgentRegistryService(self.conn, ProviderFactory(keychain), keychain)
        app.state.workspaces = WorkspaceService(self.conn)
        app.state.goals = GoalService(self.conn)
        app.state.conversations = ConversationService(self.conn)
        app.state.sandbox = SandboxService()
        app.state.settings = SettingsService(self.conn)
        app.state.stats_snapshots = StatsSnapshotService(self.conn)
        app.state.stats_imports = StatsImportService(self.conn)
        app.state.executor = ExecutorService(
            app.state.goals, app.state.workspaces, app.state.registry, app.state.sandbox
        )
        app.state.executor.settings = app.state.settings

        async def nothing(goal_id: str) -> None:
            return None

        # A goal or a turn that is created starts a background run; these tests are about what a *request*
        # may do, so the run is replaced by one that does nothing rather than one that calls a model.
        app.state.executor.run_planning = nothing
        app.state.executor.run_chat = nothing
        app.state.token = BOOT_TOKEN
        app.state.models = ModelCatalogService(
            app.state.registry,
            keychain,
            transport=httpx.MockTransport(lambda request: httpx.Response(404, json={"error": "no providers"})),
        )
        self.client = httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        )
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.conn.close()
        self.temp_dir.cleanup()

    def rows(self, table: str) -> list[tuple[Any, ...]]:
        return [tuple(r) for r in self.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]

    async def workspace_goal_and_thread(self) -> dict[str, str]:
        """A real workspace, a real goal in it and a real thread, so a sweep addresses things that exist."""
        folder = self.root / "work"
        folder.mkdir(exist_ok=True)
        ws = (await self.client.post(
            "/workspaces", headers=self.headers, json={"name": "W", "root_path": str(folder)},
        )).json()
        goal = (await self.client.post(
            "/goals", headers=self.headers, json={"workspace_id": ws["id"], "title": "a goal"},
        )).json()
        thread = (await self.client.post(
            "/conversations", headers=self.headers, json={"workspace_id": ws["id"]},
        )).json()
        return {"workspace_id": ws["id"], "goal_id": goal["id"], "conversation_id": thread["id"]}


def routes() -> list[tuple[str, str]]:
    """Every (method, path template) the engine registers over HTTP."""
    found: list[tuple[str, str]] = []
    for route in app.routes:
        if isinstance(route, Route):
            found += [(m, route.path) for m in sorted(route.methods or ()) if m not in ("HEAD", "OPTIONS")]
    return found


# --- 1. exactly eight roles ---------------------------------------------------------------------------------


class TestEightRolesAndNoOthers(BoundaryCase):
    """docs/00 section 6.1: exactly eight `AgentRole` values, and no create or delete of slots."""

    async def test_the_settings_screen_lists_these_eight_and_no_other(self) -> None:
        listed = (await self.client.get("/settings/agents", headers=self.headers)).json()
        described = (await self.client.get("/settings/roles", headers=self.headers)).json()

        self.assertEqual(EIGHT, [c["role"] for c in listed])
        self.assertEqual(EIGHT, [r["role"] for r in described])
        self.assertEqual(EIGHT, list(ROLES))
        self.assertEqual(sorted(EIGHT), sorted(typing.get_args(AgentRole)))

    def test_no_route_can_create_or_delete_a_slot(self) -> None:
        surface = {(m, p) for m, p in routes() if p.startswith("/settings/agents")}

        self.assertEqual(
            {
                ("GET", "/settings/agents"),
                ("GET", "/settings/agents/stats"),
                ("POST", "/settings/agents/repair"),
                ("GET", "/settings/agents/{role}"),
                ("PUT", "/settings/agents/{role}"),
                ("POST", "/settings/agents/{role}/test-connection"),
            },
            surface,
            "a route under /settings/agents was added or removed; if it creates or deletes a role it "
            "breaks docs/00 section 6.1, and if it does not, add it here on purpose",
        )

    async def test_an_unknown_role_is_a_404_and_adds_nothing(self) -> None:
        before = self.rows("agent_configs")

        for method, path, body in (
            ("GET", "/settings/agents/ninth", None),
            ("PUT", "/settings/agents/ninth", {"model_name": "x"}),
            ("POST", "/settings/agents/ninth/test-connection", None),
        ):
            with self.subTest(method=method, path=path):
                r = await self.client.request(method, path, headers=self.headers, json=body)
                self.assertEqual(404, r.status_code, r.text)
                self.assertEqual("unknown_role", r.json()["code"])

        self.assertEqual(before, self.rows("agent_configs"))
        self.assertEqual(len(EIGHT), len(before))


# --- 2. only the settings routes write agent configuration ---------------------------------------------------

# The three routes docs/00 section 6.2 names as the only writers.
SETTINGS_WRITERS = {
    ("PUT", "/settings/agents/{role}"),
    ("POST", "/settings/agents/repair"),
    ("PUT", "/settings/engine"),
}
# Routes the sweep cannot drive from a test, each with the reason; neither takes or stores configuration.
ACTS_OUTSIDE = {
    ("POST", "/workspaces/browse"): "opens the desktop folder dialog",
    ("POST", "/settings/agents/{role}/test-connection"): "calls the role's provider over the network; it reads "
    "the configuration and has no write to make",
}
# The bodies that could carry a config write in through a field the route really has: a goal and a turn both
# take the command bar's `provider` and `model`, and once were described as seeding them onto roles.
# A value written `{name}` is replaced by the id of the thing the sweep created; a route must be sent exactly the
# fields it has, because an unknown one is a 422 and the handler, where a writer would be, is never reached.
LEGITIMATE_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/goals"): {"workspace_id": "{workspace_id}", "title": "t", "provider": "evil", "model": "evil"},
    ("POST", "/conversations/{conversation_id}/turns"): {"prompt": "hello", "provider": "evil", "model": "evil"},
}


class TestAgentConfigHasOnlyTheSettingsWriters(BoundaryCase):
    """docs/00 section 6.2: configuration is written by the settings routes, never by a goal or a turn."""

    def config_state(self) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        return self.rows("agent_configs"), self.rows("engine_settings")

    async def test_no_other_route_changes_it_however_it_is_called(self) -> None:
        ids = await self.workspace_goal_and_thread()
        ids.update(step_id="x", key="x", role="fixer")
        hostile = {
            "agent_config": {"fixer": {"model_name": "evil"}},
            "config": {"model_name": "evil"},
            "provider": "evil",
            "model": "evil",
            "model_name": "evil",
            "conductor_model": "evil",
            "expected_version": 0,
            "prompt": "hello",
            "title": "t",
            "workspace_id": ids["workspace_id"],
            "enabled": True,
        }
        swept: list[tuple[str, str]] = []
        accepted = 0
        # Deletes last, so the things the other routes address still exist when they are called.
        mutating = sorted(
            ((m, p) for m, p in routes() if m in ("POST", "PUT", "PATCH", "DELETE")),
            key=lambda mp: mp[0] == "DELETE",
        )
        for method, template in mutating:
            if (method, template) in SETTINGS_WRITERS:
                continue
            if (method, template) in ACTS_OUTSIDE:
                continue
            path = re.sub(r"\{([^}]+)\}", lambda m: ids.get(m.group(1), "x"), template)
            bodies = [hostile]
            if (method, template) in LEGITIMATE_BODIES:
                bodies.append({
                    field: ids[value[1:-1]] if isinstance(value, str) and value.startswith("{") else value
                    for field, value in LEGITIMATE_BODIES[(method, template)].items()
                })
            for body in bodies:
                with self.subTest(method=method, path=template, body=sorted(body)):
                    before = self.config_state()
                    r = await self.client.request(method, path, headers=self.headers, json=body)
                    accepted += r.status_code < 300
                    self.assertEqual(before, self.config_state(), f"{method} {template} changed agent configuration")
            swept.append((method, template))

        self.assertGreater(len(swept), 20, "the sweep found almost no writing routes; is it reading app.routes?")
        self.assertGreater(accepted, 3, "no swept request was ever accepted, so the sweep proves little")
        # The names above are real routes: a rename that left a stale exemption would quietly sweep one more
        # route and stop exempting the one that was meant.
        self.assertLessEqual(set(ACTS_OUTSIDE) | SETTINGS_WRITERS | set(LEGITIMATE_BODIES), set(routes()))


# --- 4. no response carries a raw key -----------------------------------------------------------------------


class TestNoResponseCarriesAKey(BoundaryCase):
    """docs/00 section 6.4: responses never include raw API keys."""

    async def test_a_stored_key_appears_in_nothing_the_engine_sends(self) -> None:
        provider_key = f"sk-provider-{uuid.uuid4().hex}"
        role_key = f"sk-role-{uuid.uuid4().hex}"
        ids = await self.workspace_goal_and_thread()
        ids.update(step_id="x", key="x")

        seen: list[tuple[str, httpx.Response]] = []

        async def call(method: str, path: str, body: dict[str, Any] | None = None) -> None:
            seen.append((f"{method} {path}", await self.client.request(method, path, headers=self.headers, json=body)))

        # Both kinds of credential go in through the real routes, and what those routes answer is swept too.
        await call("POST", "/settings/keys", {"provider": "openai", "api_key": provider_key})
        await call("PUT", "/settings/agents/fixer", {"provider": "openai", "model_name": "m", "api_key": role_key})
        await call("POST", "/settings/agents/repair", {})
        await call("PUT", "/settings/engine", {})
        for role in EIGHT:
            await call("GET", f"/settings/agents/{role}")
        for method, template in routes():
            if method == "GET" and template not in ("/docs/oauth2-redirect", "/bridge/next"):
                await call("GET", re.sub(r"\{([^}]+)\}", lambda m: ids.get(m.group(1), "x"), template))

        stored = self.keychain.get_provider_key("openai")
        self.assertEqual(provider_key, stored, "the key was never stored, so the sweep below proves nothing")
        self.assertGreater(len(seen), 30)
        leaks = [
            label
            for label, r in seen
            for text in (r.text, *r.headers.values())
            if provider_key in text or role_key in text
        ]
        self.assertEqual([], leaks, "a stored key came back in a response")


if __name__ == "__main__":
    unittest.main()
