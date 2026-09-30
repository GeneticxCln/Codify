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

import os
import re
import tempfile
import typing
import unittest
import uuid
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from httpx import ASGITransport
from starlette.routing import Route

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.executor import ExecutorService
from engine.model_catalog import ModelCatalogService
from engine.models import ROLES, AgentRole, TurnCreate
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

# The three routes docs/00 section 6.2 names as the only writers of agent configuration.
SETTINGS_WRITERS = {
    ("PUT", "/settings/agents/{role}"),
    ("POST", "/settings/agents/repair"),
    ("PUT", "/settings/engine"),
}
# The one route that may create a turn (docs/00 section 6.8).
TURN_ROUTE = ("POST", "/conversations/{conversation_id}/turns")
# Routes a sweep cannot drive from a test, each with the reason; neither takes or stores configuration, and
# neither creates a goal.
ACTS_OUTSIDE = {
    ("POST", "/workspaces/browse"): "opens the desktop folder dialog",
    ("POST", "/settings/agents/{role}/test-connection"): "calls the role's provider over the network; it reads "
    "the configuration and has no write to make",
}
# Bodies made of the fields a route really has, so that the handler, where a writer would be, is reached: an
# unknown field is a 422 before it. A goal and a turn both take the command bar's `provider` and `model`, which
# were once described as seeded onto roles; a goal may name the thread it joins. A value written `{name}` is
# replaced by the id of the thing the sweep created.
LEGITIMATE_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/goals"): {
        "workspace_id": "{workspace_id}",
        "conversation_id": "{conversation_id}",
        "title": "t",
        "provider": "evil",
        "model": "evil",
    },
    TURN_ROUTE: {"prompt": "hello", "provider": "evil", "model": "evil"},
}


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

    async def sweep(
        self, observe: Callable[[], Any], skip: Collection[tuple[str, str]] = ()
    ) -> tuple[list[tuple[tuple[str, str], Any, Any]], int]:
        """Call every route that writes, with hostile bodies and with bodies made of the route's own fields.

        Returns, for each call, the route and what `observe()` read just before and just after it, and how
        many calls were accepted. The hostile body carries every field a client might use to reach a write it
        should not have: a smuggled `agent_config`, a model to seed, the flags that shape a run, `mode: chat`.
        Routes are addressed with ids of things that exist, deletes come last, and what `skip` names is left out.
        """
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
            "conversation_id": ids["conversation_id"],
            "mode": "chat",
            "dry_run": True,
            "plan_only": True,
            "parallel": True,
            "enabled": True,
        }
        calls: list[tuple[tuple[str, str], Any, Any]] = []
        accepted = 0
        writing = sorted(
            ((m, p) for m, p in routes() if m in ("POST", "PUT", "PATCH", "DELETE") and (m, p) not in skip),
            key=lambda mp: mp[0] == "DELETE",
        )
        for key in writing:
            method, template = key
            path = re.sub(r"\{([^}]+)\}", lambda m: ids.get(m.group(1), "x"), template)
            bodies = [hostile]
            if key in LEGITIMATE_BODIES:
                bodies.append({
                    field: ids[value[1:-1]] if isinstance(value, str) and value.startswith("{") else value
                    for field, value in LEGITIMATE_BODIES[key].items()
                })
            for body in bodies:
                before = observe()
                r = await self.client.request(method, path, headers=self.headers, json=body)
                accepted += r.status_code < 300
                calls.append((key, before, observe()))
        return calls, accepted


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

class TestAgentConfigHasOnlyTheSettingsWriters(BoundaryCase):
    """docs/00 section 6.2: configuration is written by the settings routes, never by a goal or a turn."""

    def config_state(self) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        return self.rows("agent_configs"), self.rows("engine_settings")

    async def test_no_other_route_changes_it_however_it_is_called(self) -> None:
        calls, accepted = await self.sweep(self.config_state, skip=SETTINGS_WRITERS | set(ACTS_OUTSIDE))

        for key, before, after in calls:
            with self.subTest(method=key[0], path=key[1]):
                self.assertEqual(before, after, f"{key[0]} {key[1]} changed agent configuration")
        self.assertGreater(len({key for key, _, _ in calls}), 20, "the sweep found almost no writing routes")
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


# --- 8. a turn has one door ---------------------------------------------------------------------------------


class TestATurnHasOneDoor(BoundaryCase):
    """docs/00 section 6.8: a turn is created only by `POST /conversations/{id}/turns`."""

    def chat_goals(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM goals WHERE mode = 'chat'").fetchone()[0])

    async def test_post_goals_refuses_a_chat_goal_at_the_route_and_creates_nothing(self) -> None:
        ids = await self.workspace_goal_and_thread()
        before = self.chat_goals()

        r = await self.client.post(
            "/goals", headers=self.headers, json={"workspace_id": ids["workspace_id"], "title": "hi", "mode": "chat"},
        )

        self.assertEqual(422, r.status_code, r.text)
        self.assertIn("conversations", r.text, "the refusal says where a turn is made")
        self.assertEqual(before, self.chat_goals())

    async def test_a_turn_body_carries_no_pipeline_flags(self) -> None:
        ids = await self.workspace_goal_and_thread()
        before = self.chat_goals()

        for field, value in (
            ("mode", "design"),
            ("mode", "normal"),
            ("dry_run", True),
            ("plan_only", True),
            ("parallel", True),
            ("workspace_id", ids["workspace_id"]),
        ):
            with self.subTest(field=field, value=value):
                r = await self.client.post(
                    f"/conversations/{ids['conversation_id']}/turns",
                    headers=self.headers,
                    json={"prompt": "hi", field: value},
                )
                self.assertEqual(422, r.status_code, r.text)

        self.assertEqual(before, self.chat_goals())
        self.assertEqual({"prompt", "provider", "model", "trace"}, set(TurnCreate.model_fields))

    async def test_no_route_but_the_turn_route_creates_a_turn(self) -> None:
        calls, _ = await self.sweep(self.chat_goals, skip=set(ACTS_OUTSIDE))

        created = {key for key, before, after in calls if after > before}

        self.assertEqual({TURN_ROUTE}, created, "the turn route makes a turn, and nothing else does")


# --- 7. one database file ------------------------------------------------------------------------------------


class TestOneDatabaseFile(unittest.TestCase):
    """docs/00 section 6.7: a single SQLite file, `~/.codify/codify.db`; there is no `agents.db`."""

    def test_a_running_engine_keeps_everything_it_stores_in_one_database(self) -> None:
        from starlette.testclient import TestClient

        from engine import home

        with tempfile.TemporaryDirectory() as scratch, mock.patch.dict(
            os.environ, {home.ENV_HOME: scratch, home.ENV_SECRETS: str(Path(scratch) / "secrets.json")}
        ):
            os.environ.pop(home.ENV_DB, None)
            folder = Path(scratch) / "work"
            folder.mkdir()
            with TestClient(app) as client:
                headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}
                # What writes: a workspace, a thread, a tab, an engine setting, an agent's configuration.
                # What opens the store a second time: the statistics routes, which read on their own connection.
                calls: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
                    ("POST", "/workspaces", {"name": "W", "root_path": str(folder)}),
                    ("PUT", "/settings/agents/fixer", {"model_name": "m"}),
                    ("PUT", "/settings/engine", {}),
                    ("GET", "/stats/overview", None),
                    ("GET", "/stats/history", None),
                    ("GET", "/stats/failures", None),
                    ("GET", "/goals", None),
                )
                statuses = [client.request(m, p, headers=headers, json=b).status_code for m, p, b in calls]

            # Looked for by content, not by name: a second database called anything at all is a second database.
            databases = sorted(
                str(p.relative_to(scratch))
                for p in Path(scratch).rglob("*")
                if p.is_file() and p.read_bytes()[:16] == b"SQLite format 3\x00"
            )

        self.assertLess(max(statuses), 500, statuses)
        self.assertEqual(["codify.db"], databases)


if __name__ == "__main__":
    unittest.main()
