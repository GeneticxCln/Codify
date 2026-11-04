"""`codify-cli`: a client of the running engine, and nothing else (engine/cli.py).

What is pinned is mostly what the client must not do: invent a credential, hand the token to whatever is listening in
the engine's port range, approve a plan, reach for the engine's internals, or put anything but the person's words in a
turn. The commands themselves are exercised against the real app over ASGI, so a route rename on either side fails here.
"""

from __future__ import annotations

import ast
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine import cli
from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.models import WorkspaceCreate
from engine.notes import NotesService
from engine.providers import Keychain, ProviderFactory
from engine.services import AgentRegistryService, GoalService, SettingsService, WorkspaceService
from engine.suggestions import Candidate, SuggestionWatch


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err


async def invoke(
    argv: list[str], *, transport: httpx.AsyncBaseTransport | None, base: str | None = "http://test",
    env: dict[str, str] | None = None, state_file: Path | None = None,
) -> Run:
    out, err = io.StringIO(), io.StringIO()
    code = await cli.run(argv, env=env or {}, out=out, err=err, transport=transport, base_url=base, state_file=state_file)
    return Run(code, out.getvalue(), err.getvalue())


class TestFindingTheEngine(unittest.IsolatedAsyncioTestCase):
    def test_the_token_comes_from_the_flag_then_the_environment_then_the_engines_own_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "boot_token"
            file.write_text("from-file\n")
            self.assertEqual("flag", cli.read_token({cli.ENV_TOKEN: "env"}, "flag", file))
            self.assertEqual("env", cli.read_token({cli.ENV_TOKEN: "env"}, None, file))
            self.assertEqual("from-file", cli.read_token({}, None, file))

    def test_a_missing_token_file_is_an_error_and_is_never_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "boot_token"
            with self.assertRaises(cli.CliError) as caught:
                cli.read_token({}, None, file)
            self.assertEqual(cli.EXIT_UNREACHABLE, caught.exception.code)
            self.assertFalse(file.exists(), "the client minted a credential for an engine that never ran")

    async def test_the_token_is_never_offered_to_something_that_is_not_the_engine(self) -> None:
        seen: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url.port), request.headers.get("authorization")))
            if request.url.port == 7432:
                return httpx.Response(401, json={"code": "unauthorized", "message": "missing or invalid token"})
            return httpx.Response(200, json={"hello": "some other local program"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            base = await cli.find_base(client, {}, None)
        self.assertEqual("http://127.0.0.1:7432", base)
        self.assertEqual([("7430", None), ("7431", None), ("7432", None)], seen, "stopped at the first engine")
        self.assertTrue(all(auth is None for _port, auth in seen), "a token went out during discovery")

    async def test_no_engine_in_the_range_is_a_clear_error_with_the_way_out(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nothing listening")

        run = await invoke(["status"], transport=httpx.MockTransport(refuse), base=None,
                           env={cli.ENV_TOKEN: "t"})
        self.assertEqual(cli.EXIT_UNREACHABLE, run.code)
        self.assertIn("no Codify engine answered", run.err)
        self.assertIn("--port", run.err)

    async def test_an_explicit_port_skips_discovery(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
            self.assertEqual("http://127.0.0.1:7500", await cli.find_base(client, {}, 7500))
            self.assertEqual("http://127.0.0.1:7501", await cli.find_base(client, {cli.ENV_PORT: "7501"}, None))


class TestCommandsAgainstTheRealApp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.conn = connect(self.root / "c.db")
        keychain = Keychain(secrets_path=self.root / "secrets.json")
        app.state.conn = self.conn
        app.state.keychain = keychain
        app.state.registry = AgentRegistryService(self.conn, ProviderFactory(keychain), keychain)
        app.state.workspaces = WorkspaceService(self.conn)
        app.state.goals = GoalService(self.conn)
        app.state.settings = SettingsService(self.conn)
        app.state.notes = NotesService(self.conn)
        app.state.suggestions = SuggestionWatch(self.conn, app.state.settings, app.state.workspaces)
        app.state.token = BOOT_TOKEN
        folder = self.root / "w"
        folder.mkdir()
        self.ws = app.state.workspaces.create(WorkspaceCreate(name="demo", root_path=str(folder)))
        self.transport = ASGITransport(app=app)
        self.env = {cli.ENV_TOKEN: BOOT_TOKEN}

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    async def go(self, *argv: str, env: dict[str, str] | None = None) -> Run:
        return await invoke(list(argv), transport=self.transport, env=env or self.env)

    async def test_status_and_workspaces(self) -> None:
        status = await self.go("status")
        self.assertEqual(0, status.code, status.err)
        self.assertIn("1 workspace(s)", status.out)
        listed = await self.go("workspaces")
        self.assertIn(self.ws.id, listed.out)
        self.assertIn("demo", listed.out)
        as_json = await self.go("--json", "workspaces")
        self.assertEqual(self.ws.id, json.loads(as_json.out)[0]["id"])

    async def test_goals_lists_and_filters_by_workspace(self) -> None:
        (self.root / "o").mkdir()
        other = app.state.workspaces.create(WorkspaceCreate(name="other", root_path=str(self.root / "o")))
        from engine.models import GoalCreate

        mine = app.state.goals.create(GoalCreate(workspace_id=self.ws.id, title="mine", description=""))
        app.state.goals.create(GoalCreate(workspace_id=other.id, title="theirs", description=""))
        everything = await self.go("goals")
        self.assertEqual(0, everything.code, everything.err)
        self.assertIn("mine", everything.out)
        self.assertIn("theirs", everything.out)
        scoped = await self.go("goals", "--workspace", self.ws.id)
        self.assertIn(mine.id, scoped.out)
        self.assertNotIn("theirs", scoped.out)

    async def test_a_wrong_token_is_refused_in_words_and_exits_nonzero(self) -> None:
        run = await self.go("workspaces", env={cli.ENV_TOKEN: "not-the-token"})
        self.assertEqual(cli.EXIT_REFUSED, run.code)
        self.assertIn("refused the token", run.err)

    async def test_notes_add_list_and_delete(self) -> None:
        added = await self.go("notes", "add", "Prefer small commits.")
        self.assertEqual(0, added.code, added.err)
        listing = await self.go("notes")
        self.assertIn("Prefer small commits.", listing.out)
        self.assertIn("notes are off", listing.out, "off by default, and the client says so")
        note_id = json.loads((await self.go("--json", "notes")).out)["notes"][0]["id"]
        self.assertEqual(0, (await self.go("notes", "delete", note_id)).code)
        self.assertNotIn("Prefer small", (await self.go("notes")).out)
        again = await self.go("notes", "delete", note_id)
        self.assertEqual(cli.EXIT_REFUSED, again.code)
        self.assertIn("404", again.err)

    async def test_suggestions_list_dismiss_and_the_engines_refusal_is_reported(self) -> None:
        app.state.suggestions.service.offer(
            self.ws.id, Candidate(kind="paused_goal", key="g", title="Paused", detail="d", prompt="p"), 1.0,
        )
        listed = await self.go("suggestions")
        self.assertIn("[paused_goal]  Paused", listed.out)
        sid = json.loads((await self.go("--json", "suggestions")).out)["suggestions"][0]["id"]
        self.assertEqual(0, (await self.go("suggestions", "dismiss", sid)).code)
        twice = await self.go("suggestions", "dismiss", sid)
        self.assertEqual(cli.EXIT_REFUSED, twice.code)
        self.assertIn("already dismissed", twice.err, "the engine's own words, not the client's")

    async def test_profile_export_writes_an_owner_only_file_and_import_reports_what_it_left_alone(self) -> None:
        app.state.settings.set_int("parallel_width", 11)
        target = self.root / "me.json"
        exported = await self.go("profile", "export", "--workspace", self.ws.id, "-o", str(target))
        self.assertEqual(0, exported.code, exported.err)
        self.assertEqual(0o600, stat.S_IMODE(os.stat(target).st_mode), "a profile is private by default")
        document = json.loads(target.read_text())
        document["settings"]["ints"]["sandbox_network"] = 1

        app.state.settings.set_int("parallel_width", 2)
        target.write_text(json.dumps(document))
        imported = await self.go("profile", "import", str(target))
        self.assertEqual(0, imported.code, imported.err)
        self.assertEqual(11, app.state.settings.get_int("parallel_width"))
        self.assertEqual(0, app.state.settings.get_int("sandbox_network"), "a file turned a door on through the client")
        self.assertIn("left alone: setting sandbox_network", imported.out)

    async def test_profile_import_applies_skills_only_when_asked(self) -> None:
        """A skill is instructions from a file that may have come from anyone: `--skills` is the person saying yes."""
        target = self.root / "with-skill.json"
        target.write_text(json.dumps({
            "format": "codify-profile", "version": 1,
            "skills": [{"name": "from-a-file", "description": "Use when testing.", "instructions": "Do the thing."}],
        }))
        landed = Path(self.ws.root_path) / ".codify" / "skills" / "from-a-file.md"

        plain = await self.go("profile", "import", str(target), "--workspace", self.ws.id)
        self.assertEqual(0, plain.code, plain.err)
        self.assertFalse(landed.exists(), "a skill was applied because a file was opened")
        self.assertIn("skills: none", plain.out)
        self.assertIn("left alone: skills", plain.out)

        asked = await self.go("profile", "import", str(target), "--workspace", self.ws.id, "--skills")
        self.assertEqual(0, asked.code, asked.err)
        self.assertTrue(landed.is_file())
        self.assertIn("skills: from-a-file", asked.out)

    async def test_a_profile_that_is_not_one_is_refused_before_anything_is_sent(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text("{ not json")
        run = await self.go("profile", "import", str(bad))
        self.assertEqual(cli.EXIT_USAGE, run.code)
        self.assertIn("could not read", run.err)
        wrong = self.root / "wrong.json"
        wrong.write_text(json.dumps({"format": "other", "version": 1}))
        refused = await self.go("profile", "import", str(wrong))
        self.assertEqual(cli.EXIT_REFUSED, refused.code)
        self.assertIn("422", refused.err)

    async def test_usage_errors_exit_two_and_help_exits_zero(self) -> None:
        self.assertEqual(cli.EXIT_USAGE, (await self.go("nonsense")).code)
        self.assertEqual(cli.EXIT_OK, (await self.go("--help")).code)


class TestAsk(unittest.IsolatedAsyncioTestCase):
    """`ask` against a stand-in engine, because the real turn route would start a real model."""

    def fake(self, final_status: str, reply: str | None) -> tuple[httpx.MockTransport, list[tuple[str, str, Any]]]:
        calls: list[tuple[str, str, Any]] = []
        polls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            calls.append((request.method, request.url.path, body))
            path = request.url.path
            if path == "/conversations":
                return httpx.Response(201, json={"id": "c1"})
            if path == "/conversations/c1/turns":
                return httpx.Response(200, json={"id": "g1", "status": "PLANNING"})
            if path == "/goals/g1":
                polls["n"] += 1
                return httpx.Response(200, json={"id": "g1", "status": "PLANNING" if polls["n"] < 2 else final_status})
            if path == "/goals/g1/events":
                events = [{"type": "log", "payload": {"message": "thinking"}}]
                if reply is not None:
                    events.append({"type": "log", "payload": {"message": reply, "turn": True}})
                return httpx.Response(200, json=events)
            return httpx.Response(404, json={"code": "nope", "message": "no such route"})

        return httpx.MockTransport(handler), calls

    async def test_a_turn_carries_the_words_and_nothing_else_and_the_reply_is_printed(self) -> None:
        cli.WAIT_POLL_S, saved = 0.0, cli.WAIT_POLL_S
        try:
            transport, calls = self.fake("COMPLETED", "The build fails on line 3.")
            run = await invoke(["ask", "ws-1", "why does it fail?", "--wait"], transport=transport, env={cli.ENV_TOKEN: "t"})
        finally:
            cli.WAIT_POLL_S = saved
        self.assertEqual(0, run.code, run.err)
        self.assertIn("[COMPLETED] The build fails on line 3.", run.out)
        turn = next(body for method, path, body in calls if path == "/conversations/c1/turns")
        self.assertEqual({"prompt": "why does it fail?"}, turn, "the client chose what the turn becomes")
        self.assertEqual({"workspace_id": "ws-1", "title": "why does it fail?"},
                         next(body for _m, path, body in calls if path == "/conversations"))

    async def test_a_plan_waiting_for_approval_is_reported_and_never_approved(self) -> None:
        cli.WAIT_POLL_S, saved = 0.0, cli.WAIT_POLL_S
        try:
            transport, calls = self.fake("PENDING", "Here is the plan.")
            run = await invoke(["ask", "ws-1", "add a banner", "--wait"], transport=transport, env={cli.ENV_TOKEN: "t"})
        finally:
            cli.WAIT_POLL_S = saved
        self.assertEqual(0, run.code, run.err)
        self.assertIn("waiting for your approval", run.out)
        touched = {path for _m, path, _b in calls}
        for door in ("/goals/g1/start", "/goals/g1/enable-execution", "/goals/g1/apply", "/goals/g1/retry"):
            self.assertNotIn(door, touched, f"the client pressed {door}")
        self.assertTrue(all(method in ("GET", "POST") for method, _p, _b in calls))
        self.assertEqual(2, sum(1 for m, p, _b in calls if m == "POST"), "a thread and a turn, no more")

    async def test_without_wait_it_returns_at_once_with_the_ids(self) -> None:
        transport, calls = self.fake("COMPLETED", None)
        run = await invoke(["ask", "ws-1", "hello"], transport=transport, env={cli.ENV_TOKEN: "t"})
        self.assertEqual(0, run.code, run.err)
        self.assertIn("turn g1 started in thread c1", run.out)
        self.assertNotIn("/goals/g1", {p for _m, p, _b in calls}, "it did not poll without --wait")


class TestItIsOnlyAClient(unittest.TestCase):
    def test_it_imports_none_of_the_engines_internals(self) -> None:
        """Importing `engine.app` creates the boot token file as a side effect, and any service import would be a way
        around the API. The module may use `engine.home` (where the token file is) and nothing else of the engine."""
        tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
        engine_imports = sorted(
            {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names if a.name.startswith("engine")}
            | {f"{n.module}.{a.name}" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "engine"
               for a in n.names}
            | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("engine.")}
        )
        self.assertEqual(["engine.home"], engine_imports)

    def test_it_starts_no_process(self) -> None:
        source = Path(cli.__file__).read_text(encoding="utf-8")
        for spawn in ("subprocess", "os.system", "os.exec", "Popen", "create_subprocess"):
            self.assertNotIn(spawn, source)

    def test_the_package_exposes_it_as_a_command_and_not_over_the_desktop_launcher(self) -> None:
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('codify-cli = "engine.cli:main"', text)
        self.assertNotIn('\ncodify = ', text)


if __name__ == "__main__":
    unittest.main()
