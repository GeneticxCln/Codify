"""Workspace knowledge: a prior the librarian may aim with, and never cite.

`CODIFY.md` is the only file this project reads as a statement about the
repository that it did not verify itself. Every test here is about the boundary
around that file, because the boundary is the feature:

* it is **not evidence** — it never reaches `evidence["files"]`, so it cannot
  borrow the pack's "this path was actually opened" guarantee;
* it is **bounded** — capped, with the truncation said out loud;
* it is **checked for staleness** — a path it names that the workspace does not
  have is reported, not quietly followed;
* in `knowledge` mode it becomes the goal's **deliverable**, written by a step
  like DESIGN.md, reviewed by the critic before anyone relies on it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from tests.test_design_role import (  # noqa: F401 — the rig is shared, not duplicated
    MockFactory,
    MockProvider,
    SkippedGate,
    _Harness,
    _responses,
)
from engine.library import (
    MAX_KNOWLEDGE_CHARS,
    ROOT_KNOWLEDGE_MD,
    format_knowledge,
    read_knowledge,
)
from engine.executor import ExecutorService
from engine.models import GoalCreate, PlanStep
from engine.providers import Keychain

KNOWLEDGE = """# What this repository is

A KPI dashboard. The entry point is `ui/src/board.html` and the token source
is `ui/src/tokens.css`. Tests run with `python3 -m pytest`.
"""

# Names a file that was deleted, and a directory entry the depth-limited
# listing would not reach. Both are reported; neither is followed.
PARTLY_STALE = KNOWLEDGE + "\nThe old renderer was `ui/src/legacy/render.js`.\n"


class ReadKnowledgeTests(unittest.TestCase):
    """`read_knowledge`, against real files in a real directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, body: str) -> None:
        (self.root / ROOT_KNOWLEDGE_MD).write_text(body, encoding="utf-8")

    def test_a_workspace_with_no_knowledge_file_has_none(self) -> None:
        self.assertIsNone(read_knowledge(str(self.root), set()))

    def test_an_empty_file_is_not_knowledge(self) -> None:
        """Whitespace is not a prior. Binding every goal to nothing is worse."""
        self._write("   \n\n\t\n")
        self.assertIsNone(read_knowledge(str(self.root), set()))

    def test_a_real_file_is_read_with_its_own_name(self) -> None:
        self._write(KNOWLEDGE)
        known = read_knowledge(str(self.root), set())
        assert known is not None
        self.assertEqual(known["path"], ROOT_KNOWLEDGE_MD)
        self.assertIn("KPI dashboard", known["text"])
        self.assertFalse(known["truncated"])

    def test_a_long_file_is_capped_and_says_so(self) -> None:
        self._write("x" * (MAX_KNOWLEDGE_CHARS + 5_000))
        known = read_knowledge(str(self.root), set())
        assert known is not None
        self.assertTrue(known["truncated"])
        self.assertEqual(len(known["text"]), MAX_KNOWLEDGE_CHARS)
        self.assertIn("truncated", format_knowledge(known))

    def test_a_path_the_workspace_lost_is_reported_stale(self) -> None:
        self._write(PARTLY_STALE)
        tree = {"ui/src/board.html", "ui/src/tokens.css"}
        known = read_knowledge(str(self.root), tree)
        assert known is not None
        self.assertEqual(known["stale_paths"], ["ui/src/legacy/render.js"])
        # The paths that do resolve are not dragged down with the one that does.
        self.assertIn("ui/src/board.html", known["paths"])

    def test_without_a_listing_nothing_is_called_stale(self) -> None:
        """No tree means no opinion — used by the knowledge drafter, which
        re-reads the file to revise it rather than to distrust it."""
        self._write(PARTLY_STALE)
        known = read_knowledge(str(self.root))
        assert known is not None
        self.assertEqual(known["stale_paths"], [])

    def test_a_url_and_a_bare_word_are_not_paths(self) -> None:
        self._write("See `https://example.com/a/b` and `somecommand` and `x`.\n")
        known = read_knowledge(str(self.root), set())
        assert known is not None
        self.assertEqual(known["paths"], [])

    def test_trailing_prose_punctuation_is_not_part_of_the_path(self) -> None:
        self._write("Read `src/app.py`, then `src/db.py`.\n")
        known = read_knowledge(str(self.root), {"src/app.py", "src/db.py"})
        assert known is not None
        self.assertEqual(known["stale_paths"], [])


class FormatKnowledgeTests(unittest.TestCase):
    """The words are the contract: a prior that reads as a fact is a bug."""

    def test_no_knowledge_says_so_rather_than_saying_nothing(self) -> None:
        self.assertIn("nothing yet", format_knowledge(None))

    def test_the_prior_is_labelled_as_unverified(self) -> None:
        text = format_knowledge({
            "path": ROOT_KNOWLEDGE_MD, "text": KNOWLEDGE, "chars": 100,
            "truncated": False, "stale_paths": [],
        })
        self.assertIn("prior, NOT evidence", text)
        self.assertIn("you have not opened it", text)
        self.assertIn("Cite nothing from it as evidence", text)

    def test_a_stale_prior_is_loud_about_which_paths(self) -> None:
        text = format_knowledge({
            "path": ROOT_KNOWLEDGE_MD, "text": KNOWLEDGE, "chars": 100,
            "truncated": False, "stale_paths": ["ui/src/legacy/render.js"],
        })
        self.assertIn("STALE", text)
        self.assertIn("ui/src/legacy/render.js", text)
        self.assertIn("Treat every claim about them as wrong", text)


class LibrarianPriorTests(unittest.TestCase):
    """The librarian, through a real goal."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def test_a_prior_never_becomes_evidence(self) -> None:
        """The one assertion that matters most, and the easiest to regress.

        The pack's strength ladder is what makes it trustworthy: a path is
        listed because the engine actually saw it. A prior that landed in
        `files` would arrive wearing that guarantee while being a note somebody
        wrote months ago about a module that no longer exists.
        """
        from engine.sandbox import SandboxService
        from engine.db import connect
        from engine.models import ROLES, AgentConfigUpdate, WorkspaceCreate
        from engine.services import AgentRegistryService, GoalService, WorkspaceService

        (self.root / ROOT_KNOWLEDGE_MD).write_text(PARTLY_STALE, encoding="utf-8")
        # Every path KNOWLEDGE names, except the one PARTLY_STALE adds: the
        # deleted renderer is the only thing that should come back as stale, and
        # a fixture that quietly omits the others would make the assertion
        # pass for the wrong reason.
        (self.root / "ui" / "src").mkdir(parents=True)
        (self.root / "ui" / "src" / "board.html").write_text("<main></main>\n", encoding="utf-8")
        (self.root / "ui" / "src" / "tokens.css").write_text(":root {}\n", encoding="utf-8")

        conn = connect(self.root / "t.db")
        self.addCleanup(conn.close)
        provider = MockProvider(_responses())
        registry = AgentRegistryService(conn, MockFactory(provider, Keychain()), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(provider="ollama", model_name="m"))
        goals = GoalService(conn)
        workspaces = WorkspaceService(conn)
        executor = ExecutorService(
            goals, workspaces, registry, SandboxService(), laya=SkippedGate()
        )
        ws = workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        goal = goals.create(
            GoalCreate(workspace_id=ws.id, title="Tune the board", description="")
        )

        import asyncio

        asyncio.run(executor.run_planning(goal.id))

        pack = [e.payload for e in goals.events_after(goal.id, 0)
                if e.type == "library_evidence"][-1]
        known = pack["knowledge"]
        self.assertEqual(known["path"], ROOT_KNOWLEDGE_MD)
        self.assertEqual(known["stale_paths"], ["ui/src/legacy/render.js"])
        # Not evidence, in the sense that matters: not a cited file.
        cited = {f["path"] for f in pack["files"]}
        self.assertNotIn(ROOT_KNOWLEDGE_MD, cited)
        self.assertNotIn("ui/src/legacy/render.js", cited)
        # And the prior's own text is not smuggled into the pack either.
        self.assertNotIn("KPI dashboard", str(pack["files"]))

    def test_a_stale_prior_is_warned_about_in_the_transcript(self) -> None:
        """Silent distrust is not distrust — the user is told what was ignored."""
        from engine.sandbox import SandboxService
        from engine.db import connect
        from engine.models import ROLES, AgentConfigUpdate, WorkspaceCreate
        from engine.services import AgentRegistryService, GoalService, WorkspaceService
        import asyncio

        (self.root / ROOT_KNOWLEDGE_MD).write_text(PARTLY_STALE, encoding="utf-8")
        conn = connect(self.root / "t.db")
        self.addCleanup(conn.close)
        registry = AgentRegistryService(
            conn, MockFactory(MockProvider(_responses()), Keychain()), Keychain()
        )
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(provider="ollama", model_name="m"))
        goals = GoalService(conn)
        workspaces = WorkspaceService(conn)
        executor = ExecutorService(
            goals, workspaces, registry, SandboxService(), laya=SkippedGate()
        )
        ws = workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        goal = goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

        asyncio.run(executor.run_planning(goal.id))

        warnings = [
            e.payload["message"] for e in goals.events_after(goal.id, 0)
            if e.type == "log" and e.payload.get("level") == "warn"
        ]
        self.assertTrue(
            any("ui/src/legacy/render.js" in w for w in warnings),
            f"no warning named the stale path; warnings were {warnings}",
        )


class DeliverablePathTests(unittest.TestCase):
    """Which file a step means to write, decided by the mode and not by the name."""

    def _step(self, *paths: str) -> PlanStep:
        return PlanStep(
            id="s", goal_id="g", ordinal=0, title="t", description="d",
            suggested_paths=list(paths),
        )

    def test_a_knowledge_step_naming_codify_md_is_the_write_step(self) -> None:
        path = ExecutorService._deliverable_write_path(
            self._step("CODIFY.md"), {"mode": "knowledge"}
        )
        self.assertEqual(path, "CODIFY.md")

    def test_a_nested_codify_md_still_counts(self) -> None:
        path = ExecutorService._deliverable_write_path(
            self._step("docs/CODIFY.md"), {"mode": "knowledge"}
        )
        self.assertEqual(path, "docs/CODIFY.md")

    def test_a_design_goal_does_not_get_a_codify_md_step(self) -> None:
        """A step naming CODIFY.md in a design goal is writing a document the
        pipeline did not ask for, and must not be handed a draft to reproduce."""
        self.assertEqual(
            ExecutorService._deliverable_write_path(
                self._step("CODIFY.md"), {"mode": "design"}
            ),
            "",
        )

    def test_a_knowledge_goal_does_not_get_a_design_md_step(self) -> None:
        self.assertEqual(
            ExecutorService._deliverable_write_path(
                self._step("DESIGN.md"), {"mode": "knowledge"}
            ),
            "",
        )

    def test_a_normal_goal_has_no_deliverable_at_all(self) -> None:
        for mode in ({}, {"mode": "normal"}, {"mode": None}, {"mode": ""}):
            with self.subTest(mode=mode):
                self.assertEqual(
                    ExecutorService._deliverable_write_path(
                        self._step("CODIFY.md", "DESIGN.md"), mode
                    ),
                    "",
                )


class KnowledgeGoalCase(_Harness):
    """A knowledge-mode goal, end to end through the planning half."""

    RESPONSES: dict[str, Any] = {
        **_responses(),
        "design": {
            "applies": True,
            "artifact": "document",
            "direction": "Record what this repository is.",
            "design_system": {"name": ROOT_KNOWLEDGE_MD, "source": None},
            "conventions": ["name the file, not the idea"],
            "design_md": "# What this repository is\n\nA KPI dashboard.\n",
        },
        "planner": {
            "steps": [{
                "title": "Write CODIFY.md",
                "description": "record the workspace knowledge",
                "suggested_paths": ["CODIFY.md"],
            }]
        },
    }
    GOAL_KWARGS: dict[str, Any] = {"mode": "knowledge"}

    async def test_the_goal_publishes_the_knowledge_body(self) -> None:
        await self.executor.run_planning(self.goal.id)
        contracts = self._events("design_contract")
        self.assertEqual(len(contracts), 1)
        self.assertEqual(contracts[0]["mode"], "knowledge")
        self.assertIn("A KPI dashboard", contracts[0]["design_md"])

    async def test_the_planner_is_told_codify_md_is_the_file(self) -> None:
        await self.executor.run_planning(self.goal.id)
        prompt = self.provider.prompt_for("planner")
        self.assertIn("CODIFY.md body (the file a step must produce)", prompt)
        self.assertNotIn("DESIGN.md body", prompt)

    async def test_the_drafter_is_told_it_is_authoring_knowledge(self) -> None:
        await self.executor.run_planning(self.goal.id)
        prompt = self.provider.prompt_for("design")
        self.assertIn("KNOWLEDGE goal", prompt)
        self.assertIn(ROOT_KNOWLEDGE_MD, prompt)

    async def test_the_write_step_is_handed_the_body_verbatim(self) -> None:
        await self._run_the_step()
        prompt = self.provider.prompt_for("fixer")
        self.assertIn("this step writes the knowledge deliverable itself", prompt)
        self.assertIn("A KPI dashboard", prompt)
        self.assertIn("(write exactly this)", prompt)


class KnowledgeGoalWithoutABodyCase(_Harness):
    """A knowledge goal whose drafter returns no body is a non-fatal warning."""

    RESPONSES: dict[str, Any] = {
        **_responses(),
        "design": {"applies": True, "direction": "Something", "design_system": {}},
    }
    GOAL_KWARGS: dict[str, Any] = {"mode": "knowledge"}

    async def test_a_missing_body_warns_and_planning_continues(self) -> None:
        await self.executor.run_planning(self.goal.id)
        self.assertTrue(
            any("knowledge-deliverable" in w for w in self._warnings()),
            f"warnings were {self._warnings()}",
        )
        self.assertEqual(len(self.goals.steps(self.goal.id)), 1)
        self.assertEqual(self._events("design_contract"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
