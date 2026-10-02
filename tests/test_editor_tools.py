"""The conductor's eyes and hands on the editor: `read_editor`, `open_in_editor`, `edit_editor`.

Three tools, each a question put to the person's editor through the surface bridge and nothing else. What matters, and
what is asserted:

  * **They touch no file.** Not a read of the disk, not a write: `edit_editor` changes the text in an open buffer, the
    person's Save is the only way that text reaches the disk (docs/00 §6.9), and the proof is the workspace tree
    unchanged with every filesystem writer patched to raise.
  * **They are scoped to the run's workspace**, because the editor may have files from several open.
  * **A missing editor is a sentence, never an exception**, the way a missing browser is: most callers (a benchmark, a
    command-line turn) have no window, and a tool that raised there would fail every turn on all of them.
  * **A refusal from the editor reaches the model in words it can act on.**
  * They are always offered (like the page verbs), cost model calls only (they are not stage moves), and are named in the
    prompt (`test_conductor_prompt`).
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import hashlib
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from engine.conductor import BASE_TOOLS, STAGE_MOVES, TOOLS
from engine.conductor_tools import ConductorTools
from engine.fs import FileSystemService
from engine.surfaces import SurfaceBridge

TOOLS_UNDER_TEST = ("read_editor", "open_in_editor", "edit_editor")

OPEN_RESULT = {"path": "src/a.py", "opened": True, "shown": "beside", "from_line": 4, "to_line": 6}
EDIT_RESULT = {"path": "src/a.py", "replaced": 1, "from_line": 3, "to_line": 3, "opened": False, "dirty": True}
READ_RESULT = {
    "open": [],
    "file": {
        "path": "src/a.py", "dirty": True, "ai_changed": False, "from_line": 1, "total_lines": 2,
        "lines": ["def f():", "    return 1"], "truncated": False,
    },
}


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class ToolCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        self.bridge = SurfaceBridge()
        self.logged: list[str] = []
        self.asked: list[dict[str, Any]] = []
        self.edits: dict[str, int] = {}
        self.tools = self.make(self.bridge)

    def make(self, bridge: SurfaceBridge | None) -> ConductorTools:
        service = SimpleNamespace(
            git=None,
            workspaces=SimpleNamespace(get=lambda _id: SimpleNamespace(root_path=str(self.root))),
            surfaces=bridge,
            _editor_edits=self.edits,
            _log=lambda goal_id, step_id, level, message: self.logged.append(message),
        )
        goal = SimpleNamespace(workspace_id="ws-1")
        return ConductorTools(cast(Any, service), "goal-1", cast(Any, goal), str(self.root), cast(Any, None))

    async def with_window(self, call: Any, *, answer: Any = None, ok: bool = True, error: str | None = None) -> str:
        """Run a tool while the app window polls and answers once."""
        self.bridge.note_ui()
        running = asyncio.create_task(call)
        request = await self.bridge.next_request(wait_s=2.0)
        assert request is not None, "the tool never asked the window anything"
        self.asked.append(request)
        self.bridge.answer(request["id"], ok, answer if ok else None, error)
        return cast(str, await running)


class TestTheyAreOfferedLikeThePageVerbs(unittest.TestCase):
    def test_each_is_a_tool_with_a_handler_that_is_always_on_the_menu_and_costs_no_move(self) -> None:
        for name in TOOLS_UNDER_TEST:
            with self.subTest(name=name):
                self.assertIn(name, ConductorTools.NAMES)
                self.assertTrue(inspect.iscoroutinefunction(getattr(ConductorTools, name, None)), f"{name} has no handler")
                self.assertIn(name, [t.name for t in BASE_TOOLS], "only offered once a plan exists")
                self.assertIn(name, [t.name for t in TOOLS])
                self.assertNotIn(name, STAGE_MOVES, "a view of an editor should not spend one of the run's twelve moves")

    def test_the_parameters_say_what_is_required(self) -> None:
        spec = {t.name: t.parameters for t in TOOLS}
        self.assertEqual([], spec["read_editor"].get("required", []))
        self.assertEqual(["path"], spec["open_in_editor"]["required"])
        self.assertEqual(["path", "old_text", "new_text"], spec["edit_editor"]["required"])

    def test_the_descriptions_say_what_the_model_must_know_before_calling(self) -> None:
        by_name = {t.name: t.description.lower() for t in TOOLS}
        self.assertIn("unsaved", by_name["read_editor"])
        self.assertIn("unsaved", by_name["edit_editor"])
        self.assertIn("never saves", by_name["edit_editor"])
        self.assertIn("say what you", by_name["edit_editor"])


class TestAMissingEditorIsASentence(ToolCase):
    async def test_an_engine_with_no_window_says_so_for_every_tool(self) -> None:
        bare = self.make(None)
        calls = {
            "read_editor": {},
            "open_in_editor": {"path": "src/a.py"},
            "edit_editor": {"path": "src/a.py", "old_text": "1", "new_text": "2"},
        }

        for name, args in calls.items():
            with self.subTest(name=name):
                text = await getattr(bare, name)(args)
                self.assertIn("no editor", text.lower())

    async def test_a_window_that_is_not_polling_says_so_and_nothing_is_queued(self) -> None:
        text = await self.tools.read_editor({})

        self.assertIn("window", text.lower())
        self.assertEqual(0, self.bridge.state()["inflight"])


class TestReading(ToolCase):
    async def test_it_asks_about_this_workspace_and_returns_what_the_editor_holds(self) -> None:
        text = await self.with_window(self.tools.read_editor({"path": "src/a.py", "from_line": 1}), answer=READ_RESULT)

        request = self.asked[0]
        self.assertEqual(("editor", "read", "ws-1"), (request["surface"], request["op"], request["workspace_id"]))
        self.assertEqual({"path": "src/a.py", "from_line": 1, "to_line": None}, request["args"])
        self.assertIn("    return 1", text)
        self.assertIn("unsaved", text)

    async def test_a_path_outside_the_workspace_is_refused_here_and_the_window_is_not_asked(self) -> None:
        # The same confinement every other file door uses, checked before a round trip: the model is told at once, and
        # the window is never asked to open somebody else's file.
        self.bridge.note_ui()
        calls = {
            "read_editor": {"path": "../../etc/passwd"},
            "open_in_editor": {"path": "/etc/passwd"},
            "edit_editor": {"path": ".git/config", "old_text": "a", "new_text": "b"},
        }

        for name, args in calls.items():
            with self.subTest(name=name):
                text = await getattr(self.tools, name)(args)
                self.assertIn("outside this workspace", text)

        self.assertEqual(0, self.bridge.state()["inflight"])

    async def test_a_bad_argument_is_a_sentence_and_the_window_is_not_asked(self) -> None:
        self.bridge.note_ui()

        text = await self.tools.read_editor({"from_line": 0})

        self.assertIn("read", text)
        self.assertEqual(0, self.bridge.state()["inflight"])


class TestDoing(ToolCase):
    async def test_open_asks_for_the_file_and_the_lines_and_says_where_it_went(self) -> None:
        text = await self.with_window(self.tools.open_in_editor({"path": "src/a.py", "line": 4, "end_line": 6}), answer=OPEN_RESULT)

        self.assertEqual(("open", {"path": "src/a.py", "line": 4, "end_line": 6}), (self.asked[0]["op"], self.asked[0]["args"]))
        self.assertEqual("ws-1", self.asked[0]["workspace_id"], "the editor was not told which workspace this is about")
        self.assertIn("beside", text)
        self.assertIn("lines 4-6", text)

    async def test_edit_sends_exactly_what_to_find_and_what_to_put_and_says_it_is_unsaved(self) -> None:
        text = await self.with_window(
            self.tools.edit_editor({"path": "src/a.py", "old_text": "return 1", "new_text": "return 2", "count": 1}),
            answer=EDIT_RESULT,
        )

        self.assertEqual(
            ("edit", {"path": "src/a.py", "old_text": "return 1", "new_text": "return 2", "count": 1}),
            (self.asked[0]["op"], self.asked[0]["args"]),
        )
        self.assertEqual("ws-1", self.asked[0]["workspace_id"], "the editor was not told which workspace this is about")
        self.assertIn("unsaved", text)
        self.assertIn("until the person saves", text)

    async def test_an_edit_that_does_not_say_how_many_means_exactly_one(self) -> None:
        # The default that makes an ambiguous edit fail instead of changing the wrong place. Zero would mean "every one".
        await self.with_window(
            self.tools.edit_editor({"path": "src/a.py", "old_text": "1", "new_text": "2"}), answer=EDIT_RESULT
        )

        self.assertEqual(1, self.asked[0]["args"]["count"])

    async def test_an_edit_that_landed_is_counted_for_the_goal_and_one_that_did_not_is_not(self) -> None:
        await self.with_window(
            self.tools.edit_editor({"path": "src/a.py", "old_text": "1", "new_text": "2"}), answer=EDIT_RESULT
        )
        self.assertEqual({"goal-1": 1}, self.edits, "an edit that changed the text was not counted")

        await self.with_window(
            self.tools.edit_editor({"path": "src/a.py", "old_text": "1", "new_text": "2"}), answer={**EDIT_RESULT, "replaced": 0}
        )
        self.assertEqual({"goal-1": 1}, self.edits, "an edit that replaced nothing was counted as a change")

        await self.with_window(
            self.tools.edit_editor({"path": "src/a.py", "old_text": "1", "new_text": "2"}), ok=False, error="That text is not in src/a.py."
        )
        self.assertEqual({"goal-1": 1}, self.edits, "a refused edit was counted as a change")

    async def test_a_refusal_from_the_editor_reaches_the_model_and_says_nothing_changed(self) -> None:
        text = await self.with_window(
            self.tools.edit_editor({"path": "src/a.py", "old_text": "nope", "new_text": "x"}),
            ok=False, error="that text is not in src/a.py",
        )

        self.assertIn("not in src/a.py", text)
        self.assertIn("not made", text)

    async def test_a_refusal_to_open_says_so_in_its_own_words(self) -> None:
        text = await self.with_window(self.tools.open_in_editor({"path": "missing.py"}), ok=False, error="there is no file at missing.py")

        self.assertIn("no file at missing.py", text)
        self.assertIn("not opened", text)

    async def test_a_window_that_never_answers_is_a_sentence_not_a_hang(self) -> None:
        from engine import surfaces

        self.bridge.note_ui()
        with mock.patch.object(surfaces, "ASK_TIMEOUT_S", 0.05):
            text = await self.tools.open_in_editor({"path": "src/a.py"})

        self.assertIn("did not answer", text)

    async def test_what_the_tool_did_is_in_the_runs_log(self) -> None:
        await self.with_window(
            self.tools.edit_editor({"path": "src/a.py", "old_text": "1", "new_text": "2"}), answer=EDIT_RESULT
        )

        self.assertTrue(any("src/a.py" in line and "editor" in line for line in self.logged), self.logged)


class TestNoneOfThemTouchesTheDisk(ToolCase):
    async def test_with_every_writer_patched_to_raise_the_tree_is_byte_identical(self) -> None:
        before = tree_hash(self.root)
        boom = AssertionError("an editor tool reached the filesystem")
        calls = [
            (self.tools.read_editor({"path": "src/a.py"}), READ_RESULT),
            (self.tools.open_in_editor({"path": "src/a.py", "line": 1}), OPEN_RESULT),
            (self.tools.edit_editor({"path": "src/a.py", "old_text": "1", "new_text": "2"}), EDIT_RESULT),
        ]

        with mock.patch.object(FileSystemService, "apply", side_effect=boom), \
                mock.patch.object(FileSystemService, "save_text", side_effect=boom), \
                mock.patch.object(FileSystemService, "_write_atomic", side_effect=boom), \
                mock.patch.object(Path, "write_text", side_effect=boom), \
                mock.patch.object(Path, "write_bytes", side_effect=boom):
            for call, answer in calls:
                await self.with_window(call, answer=answer)

        self.assertEqual(before, tree_hash(self.root))
        self.assertEqual(3, len(self.asked))


if __name__ == "__main__":
    unittest.main()
