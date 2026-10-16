"""A file whose name is not UTF-8 text must not be able to end a turn, a plan's evidence or the editor's file list.

`os.walk` hands such a name back as a `str` of surrogate escapes (`b"bad\\xffname.txt"` is `'bad\\udcffname.txt'`). Python
holds it happily and nothing that follows can encode it: a provider's request body (`httpx` raises `UnicodeEncodeError`
building it), a JSON response, an SQLite binding. Found in the audit of 2026-10 (PR 56's list of what was not fixed).

Left out and counted, and the model told so, rather than listed under a spelling: the model cannot open that file by any
name (its JSON cannot spell the byte, a query string's invalid UTF-8 is U+FFFD), and a display escape would be a name it
could write a *new* file under (`bad\\xffname.txt` with a literal backslash). `docs/04` section 4.0.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import BOOT_TOKEN, _spawn, app
from engine.db import connect
from engine.library import LibraryService, format_search
from engine.models import ROLES, AgentConfigUpdate, GoalCreate, TurnCreate, WorkspaceCreate
from engine.providers import BaseProvider, Keychain
from engine.sandbox import SandboxService
from engine.scan import list_profiles, run_scan
from engine.services import AgentRegistryService, GoalService, WorkspaceService
from engine.executor import ExecutorService
from engine.toolcall import ToolReply, to_ollama_messages
from tests.test_conductor import ConductorTestCase, _call, _ToolProvider
from tests.test_fixer_reask import Factory, Gate

ODD_FILE = b"bad\xffname.py"
ODD_DIR = b"sub\xfedir"


def make_odd_names(root: str | Path) -> None:
    """A file, and a directory with a file in it, whose names are bytes UTF-8 cannot hold. Fails loudly where the
    filesystem refuses such a name (macOS), as a test that cannot build its case should."""
    raw = os.fsencode(str(root))
    with open(raw + b"/" + ODD_FILE, "wb") as fh:
        fh.write(b"hello needle world\neval(input())\n")
    os.mkdir(raw + b"/" + ODD_DIR)
    with open(raw + b"/" + ODD_DIR + b"/inner.py", "wb") as fh:
        fh.write(b"needle inner\neval(input())\n")


def encodes(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def sent_as_a_request(text: str) -> None:
    """What every provider does with a prompt: put it in a JSON body. Raises `UnicodeEncodeError` for a bad one."""
    httpx.Request("POST", "http://127.0.0.1:11434/api/chat", json={"messages": [{"role": "user", "content": text}]})


class OddWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "good.txt").write_text("needle in good\n", encoding="utf-8")
        # An ordinary directory beside the odd one: a count of "every directory" would be 2 here, not 1.
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "ok.txt").write_text("plain\n", encoding="utf-8")
        make_odd_names(self.root)
        self.lib = LibraryService(str(self.root))


class TestTheListings(OddWorkspace):
    def test_tree_leaves_the_names_out_and_counts_them(self) -> None:
        tree = self.lib.tree()
        self.assertEqual(["good.txt", "pkg/ok.txt"], tree["files"])
        self.assertEqual(2, tree["non_utf8_names"], "the file, and the directory once; the ordinary directory is not one")

    def test_list_files_leaves_the_names_out_and_counts_them(self) -> None:
        listed = self.lib.list_files(100)
        self.assertEqual(["good.txt", "pkg/ok.txt"], listed["files"])
        self.assertEqual(2, listed["non_utf8_names"])

    def test_a_workspace_without_one_gets_the_result_it_always_got(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            (Path(plain) / "a.txt").write_text("needle\n", encoding="utf-8")
            (Path(plain) / "sub").mkdir()
            (Path(plain) / "sub" / "b.txt").write_text("needle\n", encoding="utf-8")
            lib = LibraryService(plain)
            self.assertEqual({"files", "truncated", "limit"}, set(lib.tree()))
            self.assertEqual({"files", "truncated", "limit"}, set(lib.list_files(10)))
            for kwargs in ({}, {"regex": True}, {"mode": "keyword"}):
                found = lib.search("needle", **kwargs)
                self.assertNotIn("non_utf8_names", found, kwargs)
                self.assertNotIn("UTF-8", format_search(found), kwargs)
            self.assertNotIn("UTF-8", run_scan(plain, profile="all"))

    def test_a_cut_short_walk_says_its_count_is_a_floor(self) -> None:
        # A listing stopped at its limit never reached the names after the stop.
        short = self.lib.search("needle", regex=True)
        short["truncated"] = True
        self.assertIn("at least 2 names", format_search(short))
        self.assertNotIn("at least", format_search(self.lib.search("needle")))


class TestTheSearches(OddWorkspace):
    def test_every_strategy_searches_the_rest_and_reports_what_it_left_out(self) -> None:
        for kwargs in ({}, {"regex": True}, {"mode": "keyword"}):
            with self.subTest(**kwargs):
                result = self.lib.search("needle", **kwargs)
                self.assertEqual(["good.txt"], [m["path"] for m in result["matches"]])
                self.assertEqual(2, result["non_utf8_names"])
                shown = format_search(result)
                self.assertTrue(encodes(shown), ascii(shown))
                self.assertIn("not valid UTF-8", shown)

    def test_a_scan_covers_the_rest_and_says_what_it_left_out(self) -> None:
        shown = run_scan(str(self.root), profile="all")
        self.assertTrue(encodes(shown), ascii(shown)[:300])
        self.assertIn("2 names that are not valid UTF-8", shown)
        # A folder counts once and takes its whole subtree with it: the count must not read as a count of files.
        self.assertIn("a folder is one name, and everything under it is unscanned", shown)
        # `inner.py` holds `eval(input())`, a finding wherever it is read: it is not read, so it is not reported, and
        # nothing in the report spells the directory (as a surrogate escape, or as a display escape a model could copy).
        self.assertNotIn("inner.py", shown)
        self.assertNotIn("udcfe", shown)
        self.assertNotIn("xfe", shown)

    def test_a_profile_file_with_such_a_name_is_reported_in_words(self) -> None:
        os.makedirs(os.fsencode(str(self.root)) + b"/.codify/profiles")
        with open(os.fsencode(str(self.root)) + b"/.codify/profiles/bad\xff.json", "wb") as fh:
            fh.write(b"{not json")
        shown = list_profiles(str(self.root))
        self.assertTrue(encodes(shown), ascii(shown)[:300])
        self.assertIn("bad\\udcff.json", shown)


class TestWhatReachesAModel(unittest.IsolatedAsyncioTestCase):
    """The librarian and the conductor, with a provider that builds its request body as a real one does."""

    async def librarian_prompt(self, good_files: int) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            for n in range(good_files):
                (root / f"good{n}.txt").write_text("x\n", encoding="utf-8")
            make_odd_names(root)
            conn = connect(Path(tmp) / "t.db")
            self.addCleanup(conn.close)
            goals, workspaces = GoalService(conn), WorkspaceService(conn)
            prompts: list[str] = []

            class Provider(BaseProvider):
                async def complete(self, system_prompt: str, user_prompt: str, model: str, temperature: float,
                                   max_tokens: int, *, num_ctx: int | None = None, keep_alive: str | None = None) -> str:
                    role = next((r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown")
                    sent_as_a_request(user_prompt)
                    if role == "librarian":
                        prompts.append(user_prompt)
                    return json.dumps({
                        "librarian": {"summary": "s", "files": [], "conventions": [], "enough": True},
                        "design": {"applies": False},
                        "planner": {"steps": [{"title": "T", "description": "d", "suggested_paths": ["good.txt"]}]},
                    }.get(role, {}))

            registry = AgentRegistryService(conn, Factory(Provider()), Keychain())
            for role in ROLES:
                registry.set_config(role, AgentConfigUpdate(model_name="m"))
            executor = ExecutorService(goals, workspaces, registry, SandboxService(), laya=Gate())
            ws = workspaces.create(WorkspaceCreate(name="WS", root_path=str(root)))
            goal = goals.create(GoalCreate(workspace_id=ws.id, title="T", description="do"))

            await executor.run_planning(goal.id)

            warnings = [e.payload["message"] for e in goals.events_after(goal.id, 0)
                        if e.type == "log" and e.payload.get("level") == "warn"]
            self.assertEqual([], [w for w in warnings if "librarian unavailable" in w], warnings)
            self.assertEqual(1, len(prompts), "the librarian should have been asked")
            self.assertIn("good0.txt", prompts[0])
            return prompts[0]

    async def test_the_librarian_is_not_lost_to_a_name_in_the_tree(self) -> None:
        prompt = await self.librarian_prompt(1)
        self.assertIn(", 2 names that are not valid UTF-8 left out", prompt)
        self.assertNotIn("at least", prompt)

    async def test_a_tree_cut_short_says_the_count_is_a_floor(self) -> None:
        with mock.patch("engine.library.MAX_TREE_ENTRIES", 2):
            prompt = await self.librarian_prompt(3)
        self.assertIn("TRUNCATED, at least", prompt)
        self.assertIn("not valid UTF-8 left out", prompt)


class TestTheConductor(ConductorTestCase):
    async def test_a_hit_in_such_a_file_does_not_end_the_turn(self) -> None:
        make_odd_names(self.repo)

        class Encoding(_ToolProvider):
            async def complete_with_tools(self, system_prompt: str, messages: list[dict[str, Any]], tools: Any,
                                          model: str, temperature: float, max_tokens: int, *,
                                          num_ctx: int | None = None, keep_alive: str | None = None) -> ToolReply:
                sent_as_a_request(json.dumps(to_ollama_messages(messages), ensure_ascii=False))
                return await super().complete_with_tools(
                    system_prompt, messages, tools, model, temperature, max_tokens,
                    num_ctx=num_ctx, keep_alive=keep_alive,
                )

        for call in (_call("search_code", query="needle"), _call("search_code", query="needle", regex=True),
                     _call("scan_code", profile="all")):
            with self.subTest(call=call.name, arguments=call.arguments):
                goal = self.goals.create_turn(self.thread.id, TurnCreate(prompt="find needle"))
                provider = Encoding(replies=[ToolReply(tool_calls=[call]), ToolReply(text="done")])
                executor = self._executor(provider)
                holder = types.SimpleNamespace(state=types.SimpleNamespace(executor=executor))
                _spawn(holder, executor.run_chat(goal.id), goal.id)  # type: ignore[arg-type]
                for _ in range(100):
                    if self.goals.get(goal.id).status in ("COMPLETED", "FAILED"):
                        break
                    await asyncio.sleep(0.1)
                errors = [e.payload for e in self.goals.events_after(goal.id, 0) if e.type == "error"]
                self.assertEqual("COMPLETED", self.goals.get(goal.id).status, errors)


class TestTheEditorsFileList(unittest.IsolatedAsyncioTestCase):
    async def get(self, root: str, tmp: str) -> httpx.Response:
        conn = connect(Path(tmp) / "t.db")
        self.addCleanup(conn.close)
        workspaces = WorkspaceService(conn)
        app.state.conn, app.state.workspaces, app.state.token = conn, workspaces, BOOT_TOKEN
        ws = workspaces.create(WorkspaceCreate(name="W", root_path=root))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver",
            headers={"Authorization": f"Bearer {BOOT_TOKEN}"},
        ) as client:
            return await client.get(f"/workspaces/{ws.id}/files")

    async def test_the_list_answers_with_the_rest_and_the_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            (root / "good.txt").write_text("g\n", encoding="utf-8")
            make_odd_names(root)
            res = await self.get(str(root), tmp)
            self.assertEqual(200, res.status_code, res.text[:200])
            self.assertEqual(
                {"files": ["good.txt"], "truncated": False, "limit": 5000, "non_utf8_names": 2}, res.json(),
            )

    async def test_an_ordinary_workspace_gets_the_body_it_always_got(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            (root / "good.txt").write_text("g\n", encoding="utf-8")
            res = await self.get(str(root), tmp)
            self.assertEqual({"files": ["good.txt"], "truncated": False, "limit": 5000}, res.json())


if __name__ == "__main__":
    unittest.main()
