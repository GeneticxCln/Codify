"""The six recipes from the motion and film audit keep the limits that make them honest (docs/22).

They are prose, so what is pinned is what a rewrite must not lose: the thing each recipe says it cannot see, the
rule that stops it, the route to it from `choose-a-skill`, and that none of them is an installer. A skill is data
(docs/00 §6.9). The upstream projects these were audited against ship scripts that download browsers, models and
ffmpeg; the native recipes must stay free of every such instruction, because a recipe that told the conductor to
install something would be asking for a capability the engine does not give a skill.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import re
import unittest
from pathlib import Path

from engine.conductor_tools import ConductorTools
from engine.skills import MAX_DESCRIPTION_CHARS, Skill, builtin_skills

ROOT = Path(__file__).resolve().parent.parent

MOVES = {
    "match-a-reference": {
        "recon", "read_file", "search_code", "read_page", "plan", "write", "verify", "run_command", "todo", "ask_user",
    },
    "ui-motion": {"read_file", "search_code", "read_page", "plan", "write", "verify", "run_command"},
    "scoped-fix": {
        "read_file", "search_code", "git_history", "plan", "write", "verify", "run_command", "todo", "ask_user",
    },
    "claims-ledger": {"read_file", "search_code", "git_history", "recall", "todo", "plan", "write"},
    "tune-to-a-number": {"recon", "read_file", "search_code", "plan", "write", "verify", "run_command", "todo", "recall"},
    "film-as-code": {
        "ask_user", "recon", "read_file", "search_code", "fetch_page", "read_page", "plan", "design", "write", "verify",
        "run_command", "todo",
    },
}

# What each recipe must still say. Each phrase is the sentence a future edit is most tempted to trim.
KEPT = {
    "match-a-reference": (
        "Whose is it?",              # the ownership stop: observation is not permission to copy assets
        "not measured",              # the honest word for a row nobody could measure
        "noise floor",               # compare the reference with itself first
        "five rounds",               # the round cap
        "Never loosen a tolerance",  # no silent relaxing to finish
    ),
    "ui-motion": (
        "prefers-reduced-motion",
        "Transform and opacity only",
        "Interruptible",
        "under about 300 ms",
        "not watched",               # it cannot see motion and must say so
        "ui/budgets.json",
    ),
    "scoped-fix": (
        "Must not change",
        "Shortcuts that would fake it",
        "Compare sources, not products",
        "the reading taken",
        "outside the scope",
    ),
    "claims-ledger": (
        "Words no stronger than the source",
        "Timing is not cause",
        "An empty ledger is not a pass",
        "docs/08-benchmarks.md",
        "does not publish anything",
    ),
    "tune-to-a-number": (
        "selection rule",
        "independent check",
        "Three rounds in a row",
        "saturation",
        "held-out",
    ),
    "film-as-code": (
        "**Cannot:**",               # no render, screenshot, watch or listen
        "handoff to a person",
        "DECISION:",
        "forward, then backward",    # the determinism test that needs no renderer
        "Three review rounds",
        "retold",
        "does not post, upload or publish",
    ),
}

# Instructions that would turn a recipe into an installer or a downloader. None belongs in a native recipe.
INSTALLER = re.compile(
    r"(?i)\b(npm (i|install)|npx |pip3? install|brew install|apt(-get)? install|git clone|curl |wget |bun add|cargo install)"
)


def installer_phrases(skill: Skill) -> list[str]:
    return [m.group(0) for m in INSTALLER.finditer(skill.body)]


class TestTheSixRecipes(unittest.TestCase):
    def setUp(self) -> None:
        skills, problems = builtin_skills(ConductorTools.NAMES)
        self.assertEqual([], problems)
        self.by_name = {s.name: s for s in skills}

    def test_each_ships_and_declares_exactly_the_moves_it_is_written_around(self) -> None:
        for name, moves in MOVES.items():
            with self.subTest(skill=name):
                self.assertIn(name, self.by_name)
                self.assertEqual(moves, set(self.by_name[name].moves))
                self.assertEqual((), self.by_name[name].notes, "a declared move that does not exist is only a note")
                self.assertLessEqual(len(self.by_name[name].description), MAX_DESCRIPTION_CHARS)

    def test_none_reaches_beyond_the_workspace(self) -> None:
        """No MCP call, binary analysis or machine keystroke: these recipes sequence the moves a coding task needs."""
        for name in MOVES:
            with self.subTest(skill=name):
                for forbidden in ("call_mcp", "analyze_binary", "run_in_machine", "key_in_machine", "reset_machine"):
                    self.assertNotIn(forbidden, self.by_name[name].moves)

    def test_each_keeps_the_sentence_that_makes_it_honest(self) -> None:
        for name, phrases in KEPT.items():
            # Prose is re-wrapped when it is edited, so a phrase may span a line break.
            body = " ".join(self.by_name[name].body.split())
            for phrase in phrases:
                with self.subTest(skill=name, phrase=phrase):
                    self.assertIn(phrase, body, f"{name} dropped {phrase!r}")

    def test_every_recipe_that_writes_or_runs_says_when_it_stops(self) -> None:
        for name, moves in MOVES.items():
            if {"write", "run_command"} & moves:
                with self.subTest(skill=name):
                    self.assertIn("## Stop when", self.by_name[name].body)

    def test_none_tells_the_conductor_to_install_or_download_anything(self) -> None:
        for name in MOVES:
            with self.subTest(skill=name):
                self.assertEqual([], installer_phrases(self.by_name[name]))

    def test_the_router_reaches_each_of_them(self) -> None:
        router = self.by_name["choose-a-skill"].body
        for name in MOVES:
            with self.subTest(skill=name):
                self.assertIn(f"`{name}`", router)

    def test_the_installer_check_can_fail(self) -> None:
        """A check that cannot fail is a comment: show it refusing each thing it names."""
        for text in ("run npm i playwright", "npx skills add x/y", "pip install torch", "git clone https://x", "curl -sS u"):
            with self.subTest(text=text):
                self.assertTrue(INSTALLER.search(text))
        self.assertIsNone(INSTALLER.search("read the manifest and use the library the project already uses"))


class TestTheAuditIsHonest(unittest.TestCase):
    """docs/22 names what it audited and what it did not, and every recipe it says it built."""

    DOC = ROOT / "docs" / "22-motion-and-film-audit.md"
    REPOS = (
        "alchaincyf/huashu-motion", "rossark0/rossark-motion-skill", "talsiach/take-one", "NiazMorshed2007/reanimate",
        "shreyaskarnik/argo", "vincentsch/explainroo", "win4r/video-director", "Leonxlnx/earshot",
    )

    def test_it_names_all_eight_repositories_and_all_six_recipes(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")
        for repo in self.REPOS:
            with self.subTest(repo=repo):
                self.assertIn(repo, text)
        for name in MOVES:
            with self.subTest(recipe=name):
                self.assertIn(f"`{name}`", text)

    def test_it_says_what_it_did_not_read_and_what_it_does_not_claim(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")
        for heading in ("## 1. What was read, and what was not", "## 9. What this does not claim"):
            self.assertIn(heading, text)

    def test_it_pins_each_repository_to_a_commit(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")
        for repo in self.REPOS:
            with self.subTest(repo=repo):
                line = next((ln for ln in text.splitlines() if repo in ln and re.search(r"`[0-9a-f]{40}`", ln)), None)
                self.assertIsNotNone(line, f"{repo} has no 40-character commit pin")


if __name__ == "__main__":
    unittest.main()
