"""The invariants of docs/00 section 6, each held at the boundary it names: a route, not a constructor.

Most of the nine already had a test that named them. What a test that names an invariant does not show is
that it fails when the invariant is broken: a role list that grows a ninth name consistently (the roles, the
seeds, the prompts) passes every test that only checks the tables agree with each other, and a route that
echoes a stored key passes every test that only checks the key was stored. So each class here states one
invariant the way the doc does and asserts it about the running app, and each was checked by breaking the
enforcement and watching the named test fail (`docs/00` section 6 says what that found).

What is not covered, stated so it is not assumed: WebSocket frames (events, which are not built from
configuration) in the key sweep, and the two routes that act on the outside world in the config sweep.
"""

from __future__ import annotations

import ast
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
from engine.sandbox import CommandNotAllowed, SandboxService
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


# --- 5. a local provider only ever talks to loopback ---------------------------------------------------------

REMOTE = "http://192.168.1.5:11434"


class TestALocalProviderOnlyEverTalksToLoopback(BoundaryCase):
    """docs/00 section 6.5: `LocalProvider.base_url` must pass `validate_local_base_url` before every request."""

    def store_a_remote_target_directly(self) -> None:
        # What a hand-edited database, an older build's row or a fault in the save path would leave behind. The
        # check at save time is not the only line of defence, which is why the provider asks again at each request.
        self.conn.execute(
            "UPDATE agent_configs SET provider = 'ollama', protocol = 'ollama', base_url = ? WHERE role = 'fixer'",
            (REMOTE,),
        )
        self.conn.commit()

    async def test_saving_refuses_a_remote_address_for_a_local_provider(self) -> None:
        before = self.rows("agent_configs")

        for body in (
            {"provider": "ollama", "base_url": REMOTE},
            {"fallback_provider": "ollama", "fallback_protocol": "ollama", "fallback_base_url": REMOTE,
             "fallback_model_name": "m"},
            {"provider": "mybox", "protocol": "ollama", "base_url": REMOTE},
        ):
            with self.subTest(body=sorted(body)):
                r = await self.client.put("/settings/agents/fixer", headers=self.headers, json=body)
                self.assertEqual(400, r.status_code, r.text)
                self.assertEqual("invalid_base_url", r.json()["code"])

        self.assertEqual(before, self.rows("agent_configs"))

    async def test_no_request_is_sent_to_a_remote_address_that_is_already_stored(self) -> None:
        self.store_a_remote_target_directly()
        sent: list[str] = []

        async def refuse(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
            sent.append(str(request.url))
            raise httpx.ConnectError("no network in tests")

        # Every real HTTP request from any client in the engine passes through this; the test client does not.
        with mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", refuse):
            r = await self.client.post("/settings/agents/fixer/test-connection", headers=self.headers)

        self.assertEqual([], sent, "a request left for a non-loopback address")
        self.assertEqual(200, r.status_code, r.text)
        self.assertFalse(r.json()["ok"])
        self.assertIn("localhost", r.json()["message"])

    async def test_discovery_does_not_ask_a_remote_address_for_its_models(self) -> None:
        self.store_a_remote_target_directly()
        asked: list[str] = []

        def record(request: httpx.Request) -> httpx.Response:
            asked.append(request.url.host)
            return httpx.Response(404, json={"error": "no providers in tests"})

        app.state.models = ModelCatalogService(
            app.state.registry, self.keychain, transport=httpx.MockTransport(record)
        )
        r = await self.client.get("/models?refresh=true", headers=self.headers)

        self.assertEqual(200, r.status_code, r.text)
        # Discovery did consider the remote target, and said why it would not ask it: without that the silence
        # below could just be a catalog that had nothing to do.
        (ollama,) = [p for p in r.json()["providers"] if p["provider"] == "ollama"]
        self.assertFalse(ollama["ok"])
        self.assertIn("localhost", ollama["error"])
        self.assertNotIn("192.168.1.5", asked)


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


# --- 6. every command is validated before it runs -----------------------------------------------------------


class TestEveryCommandIsValidatedBeforeItRuns(unittest.TestCase):
    """docs/00 section 6.6: every `SandboxService.run_command` call goes through `validate_argv` first."""

    REFUSED: tuple[tuple[str, list[str]], ...] = (
        ("test", ["rm", "-rf", "x"]),
        ("test", ["sh", "-c", "true"]),
        ("test", ["bash", "-c", "id"]),
        ("test", ["curl", "http://example.com"]),
        ("test", ["git", "push"]),
        ("test", ["git", "commit", "-m", "x"]),
        ("test", ["python3", "-c", "print(1)"]),
        ("test", ["python3", "../escape.py"]),
        ("test", ["npm", "install"]),
        ("test", ["cargo", "build"]),
        ("test", ["/bin/ls"]),
        ("test", []),
        ("read_only", ["python3", "x.py"]),
        ("read_only", ["pytest"]),
        ("read_only", ["git", "commit", "-m", "x"]),
        ("read_only", ["git", "diff", "/etc/passwd", "/dev/null"]),
        ("read_only", ["ls", "/etc"]),
        ("read_only", ["cat", "x"]),
        ("read_only", ["rm", "x"]),
    )

    def test_a_refused_argv_never_starts_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch(
            "engine.sandbox.subprocess.Popen", side_effect=AssertionError("a process was started")
        ) as popen:
            for mode, argv in self.REFUSED:
                with self.subTest(mode=mode, argv=argv):
                    with self.assertRaises(CommandNotAllowed):
                        SandboxService().run_command(root, argv, mode=mode)

        popen.assert_not_called()

    def test_the_same_door_still_runs_what_it_allows(self) -> None:
        # The control: the refusals above are not a sandbox that refuses everything.
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "ok.py").write_text("print('ran')\n", encoding="utf-8")
            listed = SandboxService().run_command(root, ["ls"], mode="read_only")
            ran = SandboxService().run_command(root, ["python3", "ok.py"], mode="test")

        self.assertEqual(0, listed["exit_code"])
        self.assertIn("ok.py", listed["stdout"])
        self.assertEqual(0, ran["exit_code"])
        self.assertIn("ran", ran["stdout"])


class TestTheLibrariansCommandsCannotRunTheProject(unittest.TestCase):
    """docs/00 section 6.6: the librarian's requests use `read_only` mode and cannot run the workspace's code.

    The validator's refusals are tested on the validator. What decides whether they apply to the librarian is
    the mode it asks for, and that was not tested: the `test` allowlist admits `python3 evil.py` and `pytest`
    (which imports every conftest.py it finds), so a librarian asking in `test` mode would run a hostile
    repository's code while believing it was only reading.
    """

    def test_a_librarian_request_refuses_project_code_and_still_reads(self) -> None:
        from engine.library import LibraryService

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            mark = Path(tmp) / "MARK-project-code-ran"
            line = f"open({str(mark)!r}, 'w').write('ran')\n"
            (workspace / "evil.py").write_text(line, encoding="utf-8")
            (workspace / "conftest.py").write_text(line, encoding="utf-8")
            (workspace / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
            library = LibraryService(str(workspace))

            for label, ask, argv in (
                ("run", library.run, ["python3", "evil.py"]),
                ("run", library.run, ["pytest", "-q"]),
                ("run", library.run, ["python3", "-m", "pytest"]),
                ("run", library.run, ["npm", "test"]),
                ("git", library.git, ["commit", "-m", "x"]),
            ):
                with self.subTest(label=label, argv=argv):
                    with self.assertRaises(CommandNotAllowed):
                        ask(argv)
            ran = mark.exists()
            listing = library.run(["ls"])

        self.assertFalse(ran, "repository code ran on a librarian's request")
        self.assertEqual(0, listing["exit_code"])
        self.assertIn("evil.py", listing["stdout"])


# --- 9. only the fixer writes ---------------------------------------------------------------------------------

ENGINE_DIR = Path(__file__).resolve().parent.parent / "engine"
# Calls that change the filesystem, by attribute name. `replace`, `rename`, `remove` and `unlink` are also
# `str` methods or service methods, so those are only counted on a receiver that is the os or shutil module.
_WRITING_ATTRS = {
    "apply", "write_text", "write_bytes", "unlink", "rmtree", "mkdir", "touch", "symlink_to", "copyfile",
    "copytree", "move", "truncate", "makedirs", "rmdir", "chmod", "copy", "copy2", "symlink", "link",
}
_OS_ONLY = {"replace", "rename", "remove", "unlink", "rmdir", "makedirs", "mkdir", "chmod", "symlink", "link"}
# The only modules of the engine that change the filesystem, each with what it writes. Anything else that
# starts to is either a new way for a model's words to reach the disk, which this invariant forbids, or a
# new entry here made on purpose.
MAY_WRITE = {
    "fs.py": "the workspace writer itself: atomic apply, the one implementation",
    "executor_steps.py": "the fixer, and the replay of a proposal the fixer already made",
    "home.py": "the state directory and its permissions",
    "db.py": "creating the state directory the database lives in",
    "providers.py": "the keychain's file backend",
    "app.py": "no file operation: `.rename()` and `.remove()` there are database services",
}


def filesystem_writes(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            receiver = fn.value.id if isinstance(fn.value, ast.Name) else ""
            if fn.attr in _WRITING_ATTRS or (receiver in ("os", "shutil") and fn.attr in _OS_ONLY):
                found.append(f"{path.name}:{node.lineno} {receiver or '?'}.{fn.attr}()")
        elif isinstance(fn, ast.Name) and fn.id == "open":
            mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and set(mode.value) & set("wax+"):
                found.append(f"{path.name}:{node.lineno} open({mode.value!r})")
    return found


class TestOnlyTheFixerWrites(unittest.TestCase):
    """docs/00 section 6.9: only the fixer writes; the `write` move is the single path from a conductor run."""

    def test_nothing_outside_the_writers_changes_the_filesystem(self) -> None:
        modules = sorted(ENGINE_DIR.glob("*.py"))
        offenders = [hit for p in modules if p.name not in MAY_WRITE for hit in filesystem_writes(p)]

        self.assertGreater(len(modules), 30, "the scan found almost no engine modules")
        self.assertEqual([], offenders, "a module that is not a writer changes the filesystem")
        # The scan sees writes where they are known to be, so a change to it that stopped seeing them fails here.
        self.assertTrue(filesystem_writes(ENGINE_DIR / "fs.py"))
        self.assertTrue(filesystem_writes(ENGINE_DIR / "executor_steps.py"))

    def test_the_workspace_is_written_from_one_module(self) -> None:
        appliers = sorted(
            p.name for p in ENGINE_DIR.glob("*.py") if any(".apply()" in hit for hit in filesystem_writes(p))
        )

        self.assertEqual(["executor_steps.py"], appliers)


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


# --- the ledger ---------------------------------------------------------------------------------------------

ARCHITECTURE = Path(__file__).resolve().parent.parent / "docs" / "00-codify-architecture-overview.md"
_HERE = "tests.test_invariants_at_their_boundary"

# For each invariant in docs/00 section 6, the tests that hold it at its boundary. Every name was put through a
# mutation survey (the result is summarised under `docs/00` section 6): the enforcement removed, and the named
# test seen to fail. A name here that stops resolving (a test renamed, moved or deleted) fails `TestTheLedger`,
# so the claim that an invariant is held cannot outlive the test that holds it.
LEDGER: dict[int, tuple[str, ...]] = {
    1: (f"{_HERE}.TestEightRolesAndNoOthers",),
    2: (
        f"{_HERE}.TestAgentConfigHasOnlyTheSettingsWriters",
        "tests.test_api.TestApi.test_creating_a_goal_refuses_agent_config",
        "tests.test_api.TestApi.test_starting_a_goal_refuses_agent_config",
        "tests.test_api.TestApi.test_no_goal_or_turn_route_writes_agent_config",
        "tests.test_turns.TestOneDoor.test_the_turn_route_refuses_agent_config",
    ),
    3: (
        "tests.test_every_route_is_authenticated.TestEveryHttpRoute",
        "tests.test_every_route_is_authenticated.TestEveryWebSocket",
        "tests.test_home.TestIsolatedRunsCannotTouchTheRealStore.test_the_engine_listens_on_loopback_and_nowhere_else",
    ),
    4: (f"{_HERE}.TestNoResponseCarriesAKey",),
    5: (f"{_HERE}.TestALocalProviderOnlyEverTalksToLoopback",),
    6: (
        f"{_HERE}.TestEveryCommandIsValidatedBeforeItRuns",
        f"{_HERE}.TestTheLibrariansCommandsCannotRunTheProject",
        "tests.test_conductor.TestTheToolsAreThePipelinesDoors.test_project_code_runs_only_once_the_plan_is_approved",
    ),
    7: (f"{_HERE}.TestOneDatabaseFile",),
    8: (f"{_HERE}.TestATurnHasOneDoor", "tests.test_turns.TestOneDoor"),
    9: (
        f"{_HERE}.TestOnlyTheFixerWrites",
        "tests.test_conductor.TestTheToolsAreThePipelinesDoors.test_write_refuses_and_writes_nothing_while_unapproved",
        "tests.test_write_gate_timing.TestTheConductorRoad",
    ),
}


def _tests_in(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    found: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            found += _tests_in(item)
        else:
            found.append(item)
    return found


class TestTheLedger(unittest.TestCase):
    def test_every_invariant_in_the_doc_has_an_entry_and_no_other_does(self) -> None:
        lines = ARCHITECTURE.read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("## 6."))
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        numbered = {int(m.group(1)) for line in lines[start + 1 : end] if (m := re.match(r"^(\d+)\.\s", line))}

        self.assertTrue(numbered, "no invariants were read from docs/00 section 6")
        self.assertEqual(numbered, set(LEDGER), "an invariant was added to or removed from docs/00 section 6")

    def test_every_named_test_exists(self) -> None:
        unresolved: list[str] = []
        for number, names in LEDGER.items():
            self.assertTrue(names, f"invariant {number} names no test")
            for name in names:
                found = _tests_in(unittest.defaultTestLoader.loadTestsFromName(name))
                if not found or any(type(t).__name__ == "_FailedTest" for t in found):
                    unresolved.append(f"{number}: {name}")

        self.assertEqual([], unresolved, "a test the ledger names no longer exists")


if __name__ == "__main__":
    unittest.main()
