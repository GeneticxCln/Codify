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

import asyncio
import subprocess
import tempfile
import time
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from httpx import ASGITransport

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from tests.test_design_role import (  # noqa: F401 — the rig is shared, not duplicated
    MockFactory,
    MockProvider,
    SkippedGate,
    _Harness,
    _responses,
)
from engine.app import BOOT_TOKEN, app
from engine.default_prompts import KNOWLEDGE_BRIEF_PROMPT
from engine.git import GitService
from engine.library import (
    MAX_KNOWLEDGE_CHARS,
    ROOT_KNOWLEDGE_MD,
    format_knowledge,
    read_knowledge,
)
from engine.executor import (
    ARTIFACT_PROPOSED,
    ARTIFACT_WRITTEN,
    DELIVERABLE_FILES,
    DELIVERABLE_ROLE,
    DELIVERABLE_SUBJECT,
    ExecutorService,
)
from engine.models import GoalCreate, PlanStep, WorkspaceCreate
from engine.providers import Keychain

KNOWLEDGE = """# What this repository is

A KPI dashboard. The entry point is `ui/src/board.html` and the token source
is `ui/src/tokens.css`. Tests run with `python3 -m pytest`.
"""

# The body a knowledge goal authors and a step writes. It is deliberately the
# same document as `KNOWLEDGE`: a goal's output is, by the time the next run
# starts, the workspace's `CODIFY.md`. It names real paths on purpose — the
# next run's staleness check compares every backticked token in it against the
# tree listing, and a body naming nothing would leave that check with nothing
# to do.
KNOWLEDGE_DRAFT = KNOWLEDGE

# A knowledge file a workspace already has. One claim is still true and one is
# about a file that has since gone, so the staleness check has something to say
# — which is what makes the two renderings of this file distinguishable in a
# single run's prompts.
PRIOR = (
    "# What this repository is\n\n"
    "A KPI dashboard. Tests run with `python3 -m pytest`.\n\n"
    "The old renderer lives in `ui/src/legacy/render.js`, and the entry point is\n"
    "`ui/src/board.html`.\n"
)

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


def _prior(text: str, stale: list[str]) -> dict[str, Any]:
    """A `read_knowledge` result, in the shape the callers index into."""
    return {
        "path": ROOT_KNOWLEDGE_MD,
        "text": text,
        "chars": len(text),
        "truncated": False,
        "paths": [],
        "stale_paths": list(stale),
    }


class KnowledgeRevisionCase(_Harness):
    """A knowledge-mode goal in a workspace that already has a `CODIFY.md`.

    The mirror of `DesignDeliverableRevisionCase`, and the sharper half of the
    mode. A design goal revises a contract it is *bound* by; a knowledge goal
    rewrites a prior it is *superseding*, so a draft that merely restates the
    existing file is worth less than no draft at all — it looks current, and the
    next run will aim its reads at whatever it says.

    So the same file reaches this run's two roles under two different labels,
    and the file that leaves is the drafter's own rather than the prior's. The
    rig's `BRAND`/`BRAND_PATH` hook is how a subclass says which file the
    workspace already has; here that file is `CODIFY.md`, written before the
    engine starts.
    """

    RESPONSES: dict[str, Any] = {
        **_responses(),
        "design": {
            "applies": True,
            "artifact": "document",
            "direction": "Record what this repository is, for the next run.",
            "design_system": {"name": ROOT_KNOWLEDGE_MD, "source": None},
            "conventions": ["name a file you were shown, not a description of it"],
            "design_md": KNOWLEDGE_DRAFT,
        },
        "planner": {
            "steps": [{
                "title": f"Write {ROOT_KNOWLEDGE_MD}",
                "description": "record the workspace knowledge",
                "suggested_paths": [ROOT_KNOWLEDGE_MD],
            }]
        },
    }
    BRAND = PRIOR
    BRAND_PATH = ROOT_KNOWLEDGE_MD
    GOAL_KWARGS: dict[str, Any] = {"mode": "knowledge"}

    def _revision_block(self, prompt: str) -> str:
        """The fresh copy of the existing file, out of the drafter's whole prompt.

        The drafter is handed that file twice — once inside the librarian's
        evidence summary, once read fresh — and the two copies are not the same.
        Only the block is worth asserting on: the pack's copy is a claim about
        the file, this one is the file.
        """
        begin = f"--- current {ROOT_KNOWLEDGE_MD} — revision material, NOT a prior you may trust ---"
        end_marker = f"--- end {ROOT_KNOWLEDGE_MD} ---"
        start = prompt.find(begin)
        self.assertNotEqual(
            start, -1,
            f"the drafter was not shown the existing {ROOT_KNOWLEDGE_MD}:\n{prompt}",
        )
        end = prompt.find(end_marker, start)
        self.assertNotEqual(end, -1, f"the revision block never closed:\n{prompt}")
        return prompt[start:end]

    async def test_the_existing_file_is_shown_as_revision_material(self) -> None:
        """Read fresh, labelled, and told to be checked rather than polished."""
        await self.executor.run_planning(self.goal.id)
        block = self._revision_block(self.provider.prompt_for("design"))
        self.assertIn(
            PRIOR, block,
            "the drafter must be handed what the workspace already believes, not a "
            "summary of it — it is rewriting that text",
        )
        design_prompt = self.provider.prompt_for("design")
        self.assertIn("Check it against the evidence pack above", design_prompt)
        self.assertIn(f"{ROOT_KNOWLEDGE_MD}'s whole failure mode", design_prompt)

    async def test_the_same_file_reaches_the_librarian_as_a_prior_and_the_drafter_as_material(
        self,
    ) -> None:
        """One run, one file, two labels — and they are not interchangeable.

        The librarian is told this is a prior it may aim with and must not cite.
        The drafter is told it is material it must check. Neither role is told
        the other's framing, because each is being asked a different question
        about the same bytes, and a file that arrived unlabelled would be read
        as a fact by one of them.
        """
        await self.executor.run_planning(self.goal.id)
        librarian_prompt = self.provider.prompt_for("librarian")
        self.assertIn(PRIOR, librarian_prompt, "the prior is what the librarian starts from")
        self.assertIn("a prior, NOT evidence", librarian_prompt)
        self.assertIn("Cite nothing from it as evidence", librarian_prompt)
        self.assertNotIn("revision material", librarian_prompt)

        self.assertNotIn("Cite nothing from it as evidence", self.provider.prompt_for("design"))

    async def test_only_the_copy_inside_the_pack_is_annotated(self) -> None:
        """The one asymmetry, and the reason the pack's copy has to exist.

        `_knowledge_prompt` reads the file fresh rather than from the pack, so
        that the drafter is not quoting the pack's own guarantee about a file
        the pack never opened. The cost is that the fresh copy arrives with no
        staleness check on it — so the annotation rides the *other* copy, in
        the evidence summary, and both tests are needed to keep that true: drop
        the summary's knowledge block and the drafter is holding an unannotated
        file, believing claims the workspace has already lost.
        """
        await self.executor.run_planning(self.goal.id)
        design_prompt = self.provider.prompt_for("design")

        block = self._revision_block(design_prompt)
        self.assertNotIn(
            "STALE", block,
            "the fresh copy is deliberately unannotated — the pack's copy carries "
            "the staleness check, and two annotated copies would be one too many",
        )
        self.assertNotIn("Treat every claim about them as wrong", block)

        # …and the pack's copy is annotated, which is the other half.
        self.assertIn(
            "PRIOR — not evidence: this workspace's CODIFY.md was read this run",
            design_prompt,
        )
        self.assertIn("It names path(s) absent from this workspace", design_prompt)
        self.assertIn("ui/src/legacy/render.js", design_prompt)
        self.assertIn("Do not plan a step against those", design_prompt)
        self.assertTrue(
            any("does not have" in w for w in self._warnings()),
            f"the librarian is told out loud which claims to ignore: {self._warnings()}",
        )

    async def test_the_drafter_s_own_copy_is_published_for_the_transcript(self) -> None:
        """The bytes the prompt quotes are the bytes the user is shown.

        Without this, a knowledge card can only render what the run will say.
        The half that makes it a *revision* — what it is replacing — lives
        inside a prompt, so a user saw the new document and had to reconstruct
        the old one from a diff. And the two copies must be the same bytes: a
        second read for the transcript would be a second file, and a reviewer
        comparing them would be comparing a document against a slightly older
        copy of itself.
        """
        await self.executor.run_planning(self.goal.id)
        contracts = self._events("design_contract")
        self.assertEqual(len(contracts), 1)
        revises = contracts[0]["revises"]
        self.assertEqual(revises["path"], ROOT_KNOWLEDGE_MD)
        self.assertEqual(
            revises["text"], PRIOR,
            "the published copy is the one the drafter was handed, byte for byte",
        )
        self.assertEqual(revises["chars"], len(PRIOR))
        self.assertFalse(revises["truncated"])
        self.assertIn(
            PRIOR, self.provider.prompt_for("design"),
            "and that is the same text the prompt quoted — one read, two uses",
        )

    async def test_the_published_copy_carries_the_staleness_the_drafter_was_given(self) -> None:
        """The pack's verdict on the old file, not the fresh read's empty one.

        The drafter's copy is read with no tree listing, so its own
        `stale_paths` is `[]` by construction — "no opinion", as docs/04 §4.9.1
        puts it. Publishing *that* would read as "nothing in this file is stale",
        which is the opposite of what the run was told. The paths the engine had
        already distrusted ride the pack's copy, and the user reviewing the
        revision is entitled to see which of the old claims were dead on arrival.
        """
        await self.executor.run_planning(self.goal.id)
        revises = self._events("design_contract")[0]["revises"]
        self.assertEqual(
            sorted(revises["stale_paths"]),
            ["ui/src/board.html", "ui/src/legacy/render.js"],
        )
        self.assertNotIn(
            "STALE", revises["text"],
            "the text is the file as it is, not the file as annotated",
        )

    async def test_a_workspace_with_no_file_publishes_nothing_to_revise(self) -> None:
        """No prior, no `revises`.

        An empty block would be a claim about a file that does not exist, and
        the card would offer a comparison with nothing on the other side of it.
        A second workspace is needed because this class's whole fixture is "the
        workspace already has a file".
        """
        fresh = self.root / "elsewhere"
        fresh.mkdir()
        workspace = self.workspaces.create(
            WorkspaceCreate(name="EMPTY", root_path=str(fresh))
        )
        goal = self.goals.create(
            GoalCreate(workspace_id=workspace.id, title="First knowledge", mode="knowledge")
        )
        await self.executor.run_planning(goal.id)
        contracts = self._events_for(goal.id, "design_contract")
        self.assertEqual(len(contracts), 1, "the knowledge stage still ran")
        self.assertNotIn(
            "revises", contracts[0],
            "there is no file here, so there is nothing this run revises",
        )

    def _events_for(self, goal_id: str, type_: str) -> list[dict[str, Any]]:
        return [
            e.payload for e in self.goals.events_after(goal_id, 0) if e.type == type_
        ]

    async def test_the_published_copy_is_the_one_the_prompt_quoted(self) -> None:
        """One read, two uses — checked by making a second read disagree.

        Nothing writes to `CODIFY.md` between the drafter's prompt and the
        publish today, so the two copies are identical and comparing them proves
        nothing. The claim worth holding is structural: a *second* read would be
        a second file, and the day something does write in between the
        transcript would show the user a document the drafter never read. So the
        two reads here are made to return different files, and the published
        copy has to be the one that reached the prompt.
        """
        other = PRIOR.replace("A KPI dashboard.", "A different file entirely.")
        self.assertNotEqual(PRIOR, other)
        # Two legitimate reads of the same path, in the shapes their callers
        # need: the librarian's carries the staleness verdict, the drafter's is
        # taken with no listing and so has none. A third would be the copy the
        # transcript is built from, and it would be a different file again.
        pack_read = _prior(PRIOR, ["ui/src/board.html", "ui/src/legacy/render.js"])
        drafter_read = _prior(other, [])
        with patch(
            "engine.executor.read_knowledge",
            side_effect=[pack_read, drafter_read],
        ) as reader:
            await self.executor.run_planning(self.goal.id)
        self.assertEqual(
            reader.call_count, 2, "one read for the pack, one for the drafter — and no third"
        )
        self.assertIn(other, self.provider.prompt_for("design"))
        published = self._events("design_contract")[0]["revises"]
        self.assertEqual(
            published["text"], other,
            "the transcript shows the document the drafter was handed, not an "
            "earlier read of the same path",
        )
        self.assertEqual(
            published["stale_paths"], pack_read["stale_paths"],
            "and the staleness it publishes is the pack's verdict, not the "
            "drafter's own empty list",
        )

    async def test_a_revision_still_delivers_its_own_body(self) -> None:
        """The draft is the deliverable even when the workspace has one already.

        A knowledge goal whose body restates last month's file has produced a
        file that looks current and says nothing — and a prior that lies by
        being plausible is worse than none, because the next run aims at it.
        """
        await self.executor.run_planning(self.goal.id)
        contracts = self._events("design_contract")
        self.assertEqual(len(contracts), 1)
        self.assertEqual(
            contracts[0]["design_md"], KNOWLEDGE_DRAFT,
            "the deliverable is what this run concluded, not what the last one did",
        )
        self.assertNotEqual(contracts[0]["design_md"], PRIOR)


class KnowledgeDeliverableEndToEndCase(_Harness):
    """The whole chain over HTTP: the file the mode exists to produce.

    `KnowledgeGoalCase` drives the planning half directly, which is the right
    unit of proof for one handoff and not for the promise. The promise is a
    path: `mode: "knowledge"` entering through `POST /goals`, a drafted body
    becoming `CODIFY.md` on disk, the two roles that judge it reading *that
    file*, and the next run's librarian handed the result.

    It is worth doing end to end for this mode and not only for `design`,
    because of what the two modes end with. DESIGN.md needs a pin to bind;
    CODIFY.md needs nothing but the file. The workspace picks it up on the
    next run by existing, so a chain that published a perfect draft, wrote
    nothing, and approved a proposal would satisfy every test above this one
    and leave the feature inert.

    The provider is scripted; nothing else is. Real routes, real database,
    real filesystem, real git, and the background planning and step tasks the
    routes spawn — so the waits poll the API the way the UI does rather than
    reaching into the executor for the answer.
    """

    RESPONSES = _responses()
    # The stub scribe's message, asserted against what git actually records.
    COMMIT_MESSAGE = "docs: record what this repository is"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # The harness's engine objects, wired into the app the routes read from:
        # the API is what is under test here, not a second engine built next to it.
        app.state.conn = self.conn
        app.state.registry = self.registry
        app.state.goals = self.goals
        app.state.workspaces = self.workspaces
        app.state.executor = self.executor
        app.state.token = BOOT_TOKEN
        self.client = httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}
        self._author_the_body()
        self._plan_the_write_step()
        self._scribe_commit()

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    # ── the scripted half ──────────────────────────────────────────────────

    def _author_the_body(self) -> None:
        """The design role's reply, in the shape a knowledge draft takes.

        The cast's default `design` reply is a brand contract, whose body would
        be written to DESIGN.md by a step naming it — so a knowledge goal run
        against the default cast would fail on the wrong file for the right
        reason. This is the mode's own reply.
        """
        self.provider.responses["design"] = {
            "applies": True,
            "artifact": "document",
            "direction": "Record what this repository is, for the next run.",
            "design_system": {"name": ROOT_KNOWLEDGE_MD, "source": None},
            "conventions": ["name a file you were shown, not a description of it"],
            "design_md": KNOWLEDGE_DRAFT,
        }

    def _plan_the_write_step(self) -> None:
        """A plan with the one shape a deliverable needs: a step naming its file.

        `suggested_paths` is the only honest signal a plan gives about intent,
        and it is what both `_deliverable_write_path` and the fixer's plan note
        read to recognize the write step.
        """
        self.provider.responses["planner"] = {
            "steps": [{
                "title": f"Write {ROOT_KNOWLEDGE_MD}",
                "description": "record the workspace knowledge",
                "suggested_paths": [ROOT_KNOWLEDGE_MD],
            }]
        }
        self.provider.responses["fixer"] = {
            "files": [{
                "path": ROOT_KNOWLEDGE_MD,
                "action": "create",
                "content": KNOWLEDGE_DRAFT,
            }]
        }

    def _reproduce_the_published_body(self, body: str) -> None:
        """Make the fixer write whatever the engine actually published.

        A fixer handed the draft is told to reproduce it verbatim, so the honest
        thing for a mock to do is write what the run published — and it is what
        makes "the file on disk is the deliverable" a claim about the chain
        rather than about this file. Scripting the content instead pins the
        mock's own constant: the engine could publish one body and write
        another, publish the prior it was told to replace, or stop publishing a
        body at all, and a test whose fixer ignores the prompt would stay green
        through all three.
        """
        self.provider.responses["fixer"] = {
            "files": [{
                "path": ROOT_KNOWLEDGE_MD,
                "action": "create",
                "content": body,
            }]
        }

    def _scribe_commit(self) -> None:
        """A commit message distinctive enough to assert against git's own log."""
        self.provider.responses["scribe"] = {
            "summary": "What this repository is, is written down and reviewed.",
            "commit_message": self.COMMIT_MESSAGE,
        }

    # ── the API, as the UI calls it ───────────────────────────────────────

    async def _wait_for(self, check: Callable[[], Awaitable[bool]], what: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if await check():
                return
            await asyncio.sleep(0.01)
        self.fail(f"timed out waiting for {what}")

    async def _make_workspace(self, name: str, *, git: bool = False) -> tuple[str, Path]:
        """Register a workspace over HTTP, optionally as a real git checkout.

        The repository matters: the scribe only commits against one, and a
        knowledge file that never reaches git is a file the next clone does not
        have — which for this mode is the whole deliverable.
        """
        ws_dir = self.root / name
        ws_dir.mkdir()
        if git:
            self.assertTrue(
                GitService().init_repo(str(ws_dir)),
                "the fixture repository must initialize",
            )
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": name.upper(), "root_path": str(ws_dir)},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"], ws_dir

    async def _create_knowledge_goal(self, ws_id: str, **extra: Any) -> str:
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={
                "workspace_id": ws_id,
                "title": "Record what this repository is",
                "description": "The workspace has no CODIFY.md today.",
                "mode": "knowledge",
                **extra,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        created: dict[str, Any] = r.json()
        self.assertEqual(created["mode"], "knowledge", "the mode must survive the API")
        goal_id: str = created["id"]
        return goal_id

    async def _goal_json(self, goal_id: str) -> dict[str, Any]:
        res = await self.client.get(f"/goals/{goal_id}", headers=self.headers)
        goal: dict[str, Any] = res.json()
        return goal

    async def _http_events(self, goal_id: str) -> list[dict[str, Any]]:
        """The event log as a client sees it — what the transcript renders from."""
        res = await self.client.get(f"/goals/{goal_id}/events", headers=self.headers)
        events: list[dict[str, Any]] = res.json()
        return events

    async def _payloads(self, goal_id: str, type_: str) -> list[dict[str, Any]]:
        return [e["payload"] for e in await self._http_events(goal_id) if e["type"] == type_]

    def _artifact_block(self, prompt: str, role: str, where: str = ARTIFACT_WRITTEN) -> str:
        """The document a judge was actually handed, out of its whole prompt.

        The critic is given the draft too, and must be: it is told what the
        deliverable was supposed to be before being shown what was written.
        Asserting over the whole prompt would call that a bug. The block
        between the markers is the thing being approved, and the label in its
        opener is the engine saying where those bytes came from — off the disk
        (`as written`) or out of the stored proposal (`as proposed`).
        """
        begin = f"--- {ROOT_KNOWLEDGE_MD} {where} ---"
        end_marker = f"--- end {ROOT_KNOWLEDGE_MD} ---"
        start = prompt.find(begin)
        self.assertNotEqual(
            start, -1, f"the {role} was shown no `{where}` artifact:\n{prompt}"
        )
        end = prompt.find(end_marker, start)
        self.assertNotEqual(end, -1, f"the {role}'s artifact block never closed:\n{prompt}")
        return prompt[start:end]

    async def _settle(self, goal_id: str, status: str, what: str) -> dict[str, Any]:
        """Wait for the goal to reach `status`, then hand back the goal body."""
        async def reached() -> bool:
            return bool((await self._goal_json(goal_id))["status"] == status)

        await self._wait_for(reached, what)
        return await self._goal_json(goal_id)

    async def _start(self, goal_id: str) -> None:
        goal = await self._goal_json(goal_id)
        r = await self.client.post(
            f"/goals/{goal_id}/start", headers=self.headers,
            json={"expected_version": goal["version"]},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def _git(self, ws_dir: Path, *args: str) -> str:
        """Read the fixture repository directly.

        The engine's own report of a commit is a claim about the workspace;
        this is the check, run the way tests/test_git.py runs git.
        """
        res = subprocess.run(
            ["git", *args], cwd=ws_dir, capture_output=True, text=True, check=False,
        )
        self.assertEqual(res.returncode, 0, f"git {' '.join(args)}: {res.stderr}")
        return res.stdout.strip()

    # ── the chain ─────────────────────────────────────────────────────────

    async def test_the_goal_writes_codify_md_and_the_critic_reads_that_file(self) -> None:
        """Mode in, file out, both judges on the file — and the next run on it too.

        Each arrow is a handoff that can silently fail, so each is asserted
        where it happens rather than inferred from the one after it: a chain
        that only checked the last step would pass with a draft that was never
        written and a critic that never saw a document.
        """
        ws_id, ws_dir = await self._make_workspace("knowledge-e2e")

        # ── the mode enters through the API and the agent authors a body ────
        goal_id = await self._create_knowledge_goal(ws_id)
        await self._settle(goal_id, "PENDING", "the plan to be ready")
        self.assertIn(
            KNOWLEDGE_BRIEF_PROMPT,
            self.provider.prompt_for("design"),
            "the mode must reach the agent as its brief, not merely a column",
        )

        steps = (await self._goal_json(goal_id))["steps"]
        self.assertTrue(steps, "the body must be planned into at least one step")
        self.assertIn(ROOT_KNOWLEDGE_MD, steps[0]["suggested_paths"])
        self.assertEqual(
            DELIVERABLE_FILES["knowledge"], ROOT_KNOWLEDGE_MD,
            "the file comes from one table, so the plan and the engine cannot disagree",
        )

        contracts = await self._payloads(goal_id, "design_contract")
        self.assertTrue(contracts, "a knowledge goal must publish the body it authored")
        self.assertEqual(contracts[-1]["mode"], "knowledge")
        body = contracts[-1]["design_md"]
        self.assertTrue(body.strip(), "the published body must not be empty")
        self._reproduce_the_published_body(body)

        # ── nothing is on disk yet: a published body is not a file ─────────
        self.assertFalse(
            (ws_dir / ROOT_KNOWLEDGE_MD).exists(),
            "the design role authors a body, not a file — the fixer is still the only writer",
        )

        # ── the step runs: fixer writes, verifier reviews, critic rules ─────
        await self._start(goal_id)
        completed = await self._settle(goal_id, "COMPLETED", "the step to run and be reviewed")

        self.assertTrue(
            (ws_dir / ROOT_KNOWLEDGE_MD).exists(),
            f"a knowledge goal that writes no {ROOT_KNOWLEDGE_MD} delivers nothing: the "
            "file is the entire mechanism, and unlike DESIGN.md there is no pin to make it",
        )
        self.assertEqual(
            (ws_dir / ROOT_KNOWLEDGE_MD).read_text(encoding="utf-8"), body,
            "written verbatim — the fixer was handed the draft, not the direction to "
            "interpret it, and the file says so",
        )
        self.assertIn(
            DELIVERABLE_ROLE["knowledge"],
            self.provider.prompt_for("fixer"),
            "the writer must be told what this file is for, not just where to put it",
        )

        # The verifier reviews prose rather than guessing a command, and what it
        # reviews is the file: `as written` is only reachable when the engine
        # read bytes off disk, because a proposal would say `as proposed`.
        verifier_prompt = self.provider.prompt_for("verifier")
        self.assertIn("do not run a command", verifier_prompt)
        self.assertIn(body, self._artifact_block(verifier_prompt, "verifier"))

        # And the critic — the role that decides whether the step stands — is
        # told what approving this file means, and is given the same bytes to
        # read: the approver judges the document, not only the diff it also gets.
        critic_prompt = self.provider.prompt_for("critic")
        self.assertIn(DELIVERABLE_SUBJECT["knowledge"], critic_prompt)
        self.assertIn(body, self._artifact_block(critic_prompt, "critic"))
        self.assertEqual(completed["steps"][0]["status"], "COMPLETED")
        verdicts = [
            p for p in await self._payloads(goal_id, "test_result") if p.get("ran") is False
        ]
        self.assertTrue(verdicts, "the review must publish a verdict of its own")
        self.assertEqual(verdicts[0]["verdict"], "pass")
        self.assertFalse(verdicts[0]["ran"], "reviewing a document runs nothing")

        # ── and the next run is handed the file, as a prior and not a fact ──
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Use what is known"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        second_id: str = r.json()["id"]
        self.assertEqual(
            r.json()["mode"], "normal",
            "the mode is per goal; the next goal is an ordinary one",
        )
        await self._settle(second_id, "PENDING", "the second goal to plan")

        librarian_prompt = self.provider.prompt_for("librarian")
        self.assertIn(
            body, librarian_prompt,
            "what the previous run concluded is what the next run starts from — this "
            f"is the whole point of writing {ROOT_KNOWLEDGE_MD}",
        )
        self.assertIn("a prior, NOT evidence", librarian_prompt)

        pack = (await self._payloads(second_id, "library_evidence"))[-1]
        self.assertEqual(pack["knowledge"]["path"], ROOT_KNOWLEDGE_MD)
        self.assertNotIn(
            ROOT_KNOWLEDGE_MD, {f["path"] for f in pack["files"]},
            "the file is reported, never cited: a prior cannot borrow the pack's "
            "'this path was actually opened' guarantee",
        )
        # Every backticked path in the body this run just wrote is checked
        # against the next run's tree listing. The engine does not trust a
        # knowledge file for being its own author.
        self.assertTrue(
            pack["knowledge"]["stale_paths"],
            f"the body names paths this workspace does not have, and they must be "
            f"reported rather than followed: {pack['knowledge']}",
        )
        stale_warnings = [
            p["message"] for p in await self._payloads(second_id, "log")
            if p.get("level") == "warn" and "does not have" in p.get("message", "")
        ]
        self.assertTrue(
            stale_warnings,
            f"silent distrust is not distrust — the user is told what was ignored: "
            f"{await self._payloads(second_id, 'log')}",
        )

    async def test_both_judges_read_the_file_rather_than_the_drafted_body(self) -> None:
        """Where the bytes under review come from, proven by making them differ.

        Everywhere else the fixer writes the draft verbatim, so "the judges
        read the file" and "the judges read the draft the engine published" are
        indistinguishable. Making the fixer's file differ by one character
        separates them: an engine handing the judges the `design_contract`
        payload would review the draft, and this fails.
        """
        ws_id, ws_dir = await self._make_workspace("knowledge-bytes")
        # Rewritten in the middle, so neither text contains the other: an
        # `assertNotIn` against a file that merely extends the draft would
        # always fire, and this test would prove nothing.
        on_disk = KNOWLEDGE_DRAFT.replace(
            "A KPI dashboard.", "A KPI dashboard, rewritten by the fixer."
        )
        self.provider.responses["fixer"] = {
            "files": [{
                "path": ROOT_KNOWLEDGE_MD,
                "action": "create",
                "content": on_disk,
            }]
        }

        goal_id = await self._create_knowledge_goal(ws_id)
        await self._settle(goal_id, "PENDING", "the plan to be ready")
        body = (await self._payloads(goal_id, "design_contract"))[-1]["design_md"]
        await self._start(goal_id)
        await self._settle(goal_id, "COMPLETED", "the step to run and be reviewed")

        self.assertNotEqual(on_disk, body, "the fixture must actually differ")
        self.assertNotIn(body, on_disk, "neither text may contain the other")
        for role in ("verifier", "critic"):
            block = self._artifact_block(self.provider.prompt_for(role), role)
            self.assertIn(on_disk, block, f"the {role} must be given what is on disk")
            self.assertNotIn(
                body, block,
                f"the {role} was handed the draft instead of the file — both reviewers "
                "would then approve something the user never receives",
            )

    async def test_the_reviewed_file_is_what_the_scribe_commits(self) -> None:
        """The chain in a real repository, where the commit is the last stage.

        For this mode the commit is not bookkeeping. `CODIFY.md` lives in the
        repository precisely so it belongs in a diff and dies with the clone; a
        reviewed knowledge file that never reaches git is a file the next clone
        does not have. So the strongest form of the promise is not "a file
        exists" but "the reviewed file is committed, and the user's own
        uncommitted work was not swept into it" — the scribe is handed only the
        paths the step wrote.
        """
        ws_id, ws_dir = await self._make_workspace("knowledge-git", git=True)
        scratch = "half-finished, not mine to commit\n"
        (ws_dir / "scratch.txt").write_text(scratch, encoding="utf-8")

        goal_id = await self._create_knowledge_goal(ws_id)
        await self._settle(goal_id, "PENDING", "the plan to be ready")
        await self._start(goal_id)
        await self._settle(goal_id, "COMPLETED", "the step to run, be reviewed and committed")

        self.assertEqual(
            (ws_dir / ROOT_KNOWLEDGE_MD).read_text(encoding="utf-8"), KNOWLEDGE_DRAFT
        )

        # The engine's own report, in the transcript the user reads.
        committed = [
            p["message"] for p in await self._payloads(goal_id, "log")
            if "git committed" in str(p.get("message", ""))
        ]
        self.assertTrue(committed, f"the scribe must report its commit: {committed}")
        self.assertIn(self.COMMIT_MESSAGE, committed[-1])

        # And git agrees — one commit, exactly the file the step wrote.
        self.assertEqual(self._git(ws_dir, "log", "--format=%s", "-1"), self.COMMIT_MESSAGE)
        self.assertEqual(
            self._git(ws_dir, "show", "--name-only", "--format=", "HEAD").split(),
            [ROOT_KNOWLEDGE_MD],
            "the user's own work must not ride along in the step's commit",
        )
        self.assertEqual((ws_dir / "scratch.txt").read_text(encoding="utf-8"), scratch)

    async def test_a_revision_replaces_the_prior_the_next_run_was_holding(self) -> None:
        """Over HTTP, the whole reason a revision goal is worth a goal.

        `KnowledgeRevisionCase` above proves the drafter is handed the old file
        and told to check it. What only the full chain can show is the part that
        makes the check matter: the next run must be holding the *new* file, and
        nothing of the old one. A mode that re-published a prior it had already
        read would satisfy every planning-stage test in this file while leaving
        the workspace exactly where it was — which, for a file whose entire job
        is to be current, is the one outcome that is worse than having none.
        """
        ws_id, ws_dir = await self._make_workspace("knowledge-revision")
        (ws_dir / ROOT_KNOWLEDGE_MD).write_text(PRIOR, encoding="utf-8")

        goal_id = await self._create_knowledge_goal(ws_id)
        await self._settle(goal_id, "PENDING", "the plan to be ready")
        published = (await self._payloads(goal_id, "design_contract"))[-1]["design_md"]
        self.assertNotEqual(
            published, PRIOR,
            "this run's conclusion must be its own — a revision that republishes "
            "the file it was asked to replace has revised nothing",
        )
        self._reproduce_the_published_body(published)
        await self._start(goal_id)
        await self._settle(goal_id, "COMPLETED", "the revision to run and be reviewed")

        self.assertEqual(
            (ws_dir / ROOT_KNOWLEDGE_MD).read_text(encoding="utf-8"), published,
            "the step writes this run's body over the file the workspace had",
        )

        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Use what is known"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        second_id: str = r.json()["id"]
        await self._settle(second_id, "PENDING", "the second goal to plan")

        librarian_prompt = self.provider.prompt_for("librarian")
        self.assertIn(published, librarian_prompt)
        self.assertNotIn(
            PRIOR, librarian_prompt,
            "the next run must be holding this run's conclusion; the old one is "
            "what the step just replaced",
        )

    async def test_a_dry_run_is_reviewed_but_leaves_the_workspace_untouched(self) -> None:
        """The half of the chain where the reviewed draft is not yet the file.

        A dry run reaches the disk not at all, so there is no `CODIFY.md` for
        the verifier to read, nothing for the scribe to commit, and — the part
        that is specific to this mode — nothing for the next run's librarian to
        be handed. Apply writes exactly what was reviewed, and only then does
        the next run have a prior.

        The pin is what the design dry-run test reaches for here, and knowledge
        has no pin: the file binds by existing. So the engine saying nothing at
        all until Apply is not good enough, because "no file" and "a file the
        next run cannot see" are indistinguishable to a test that only looks at
        one goal. Hence the second goal in the middle — a workspace that must
        come out of a reviewed knowledge draft still knowing nothing — and the
        third, which is the same workspace after Apply and is no longer empty.
        """
        ws_id, ws_dir = await self._make_workspace("knowledge-dry-run", git=True)
        # The fixer proposes something the drafter did not write, rewritten in
        # the middle so neither text contains the other. A fixer told to
        # reproduce a draft would agree with it, and then "Apply replayed the
        # proposal", "the judges reviewed the proposal" and "Apply regenerated
        # the draft" are the same string in the test — three claims, one
        # assertion, and the one that matters (the user gets the bytes they
        # approved) would be unfalsifiable. Same device as
        # `test_both_judges_read_the_file_rather_than_the_drafted_body`.
        proposed = KNOWLEDGE_DRAFT.replace(
            "A KPI dashboard.", "A KPI dashboard, as the fixer proposed it."
        )
        self.provider.responses["fixer"] = {
            "files": [{
                "path": ROOT_KNOWLEDGE_MD,
                "action": "create",
                "content": proposed,
            }]
        }
        goal_id = await self._create_knowledge_goal(ws_id, dry_run=True)
        await self._settle(goal_id, "PENDING", "the plan to be ready")

        draft = (await self._payloads(goal_id, "design_contract"))[-1]["design_md"]
        self.assertTrue(draft.strip(), "a dry run still authors the deliverable")
        self.assertNotIn(draft, proposed, "the fixture must actually differ")

        await self._start(goal_id)
        await self._settle(goal_id, "COMPLETED", "the dry-run step to be reviewed")

        # ── nothing was written, and the review still had the artifact ───────
        self.assertFalse(
            (ws_dir / ROOT_KNOWLEDGE_MD).exists(),
            f"a dry run must not reach the disk: {ROOT_KNOWLEDGE_MD} binds by "
            "existing, so a dry-run file would be knowledge no one asked for",
        )
        verifier_prompt = self.provider.prompt_for("verifier")
        self.assertIn("nothing was written to disk", verifier_prompt)
        self.assertIn("do not answer 'skip' because it is not on disk", verifier_prompt)
        self.assertIn(
            proposed, self._artifact_block(verifier_prompt, "verifier", ARTIFACT_PROPOSED),
            "the proposal is the artifact under review — there is no file to read",
        )
        critic_block = self._artifact_block(
            self.provider.prompt_for("critic"), "critic", ARTIFACT_PROPOSED
        )
        self.assertIn(
            proposed, critic_block,
            "the role that decides the step reads the proposal too, not just its diff",
        )
        self.assertNotIn(
            draft, critic_block,
            "the critic was handed the drafter's body rather than what the step "
            "proposed, so it would approve something Apply will not write",
        )

        proposals = await self._payloads(goal_id, "file_change_summary")
        self.assertTrue(proposals, "a dry run must publish a proposal")
        self.assertTrue(proposals[-1]["dry_run"], "the change is a proposal, not a write")
        self.assertEqual(proposals[-1]["paths"], [ROOT_KNOWLEDGE_MD])

        # And the bytes the verifier was handed are the stored proposal itself —
        # the same record Apply replays, not a re-render of the diff.
        step_id = (await self._goal_json(goal_id))["steps"][0]["id"]
        self.assertEqual(
            self.goals.proposed_content(goal_id, step_id, ROOT_KNOWLEDGE_MD), proposed
        )
        self.assertEqual(
            self.goals.proposed_content(goal_id, step_id, ROOT_KNOWLEDGE_MD.lower()), proposed,
            "a plan that capitalized the name still finds its own proposal",
        )
        self.assertIsNone(
            self.goals.proposed_content(goal_id, step_id, "src/nothing.md"),
            "a path this step never proposed has no content to review",
        )
        self.assertIsNone(
            self.goals.proposed_content(goal_id, "no-such-step", ROOT_KNOWLEDGE_MD),
            "another step's proposal is not this step's to review",
        )
        self.assertNotEqual(
            subprocess.run(
                ["git", "rev-list", "--count", "HEAD"], cwd=ws_dir,
                capture_output=True, text=True, check=False,
            ).returncode,
            0,
            "a dry run must commit nothing — there is no revision to point at",
        )

        # ── a workspace that reviewed a draft and still knows nothing ────────
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Ask before Apply"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        during_id: str = r.json()["id"]
        await self._settle(during_id, "PENDING", "the second goal to plan")

        self.assertIn(
            format_knowledge(None), self.provider.prompt_for("librarian"),
            "a reviewed draft that is not on disk is not a prior, and the next run "
            "must be told the workspace has written nothing rather than left to guess",
        )
        self.assertIsNone(
            (await self._payloads(during_id, "library_evidence"))[-1]["knowledge"],
            "there is no file, so the pack has no knowledge block to report",
        )

        # ── Apply writes exactly what was reviewed, and commits it ───────────
        goal_body = await self._goal_json(goal_id)
        r = await self.client.post(
            f"/goals/{goal_id}/apply", headers=self.headers,
            json={"expected_version": goal_body["version"]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # Apply is a background run too; its terminal signal is the commit.
        async def applied() -> bool:
            return any(
                "git committed" in str(p.get("message", ""))
                for p in await self._payloads(goal_id, "log")
            )

        await self._wait_for(applied, "the applied knowledge file to be committed")
        self.assertEqual(
            (ws_dir / ROOT_KNOWLEDGE_MD).read_text(encoding="utf-8"), proposed,
            "Apply must write the bytes that were reviewed, not re-derive them",
        )
        self.assertEqual(
            self._git(ws_dir, "show", "--name-only", "--format=", "HEAD").split(),
            [ROOT_KNOWLEDGE_MD],
        )
        self.assertIn(
            f"{ROOT_KNOWLEDGE_MD} {ARTIFACT_WRITTEN}",
            self.provider.prompt_for("critic"),
            "after Apply the reviewer reads the file itself, not a proposal",
        )

        # ── and only now does the next run have something to be handed ───────
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Use what is known"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        after_id: str = r.json()["id"]
        await self._settle(after_id, "PENDING", "the third goal to plan")

        self.assertIn(proposed, self.provider.prompt_for("librarian"))
        self.assertEqual(
            (await self._payloads(after_id, "library_evidence"))[-1]["knowledge"]["path"],
            ROOT_KNOWLEDGE_MD,
            "the reviewed bytes became the workspace's knowledge the moment Apply "
            "wrote them — no pin, no setting, nothing to remember",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
