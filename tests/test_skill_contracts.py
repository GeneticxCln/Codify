"""Every built-in skill meets the routing contract, and every built-in security profile says where it comes from (docs/21 P5).

The conductor is shown only a skill's name and description, on every call, and chooses from them. So the description is
the interface, and these are the properties of it a menu depends on: it fits where it is shown without being cut, it is
a sentence, and no two say the same thing. A skill that can write or run a command must say what ends it, because a loop
nobody bounded is the failure these recipes exist to prevent. A built-in is held to all of this; a workspace skill is
only loaded, not judged, so a person's own recipe is never refused for style.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import datetime
import json
import re
import unittest
from pathlib import Path

from engine import scan
from engine.conductor_tools import ConductorTools
from engine.skills import MAX_DESCRIPTION_CHARS, Skill, builtin_skills, parse_skill

ROOT = Path(__file__).resolve().parent.parent

# A skill that declares either of these changes the workspace or runs something, so it owes a `## Stop when`.
ACTING_MOVES = frozenset({"write", "run_command"})

# A path a skill body cites in backticks and that this repository must hold. Paths into a user's own workspace
# (`src/app.py` in an example) are not these.
OWN_PATH = re.compile(r"^(docs|engine|tests|scripts)/[\w./-]+$")


def contract_problems(skill: Skill) -> list[str]:
    """What is wrong with one skill, against the contract above. Empty when it meets it."""
    problems: list[str] = []
    text = skill.description
    if not text.strip():
        problems.append("has no description")
    if len(text) > MAX_DESCRIPTION_CHARS or text.endswith("…"):
        problems.append(f"its description is cut or over {MAX_DESCRIPTION_CHARS} characters, so the menu shows part of it")
    if text and not (text[0].isupper() and text.endswith(".")):
        problems.append("its description is not a sentence (capital first, full stop last)")
    if ACTING_MOVES & set(skill.moves) and "## Stop when" not in skill.body:
        problems.append(f"declares {sorted(ACTING_MOVES & set(skill.moves))} but has no `## Stop when`")
    if not skill.body.lstrip().startswith("# "):
        problems.append("its body does not open with a heading")
    for cited in re.findall(r"`([^`\n]+)`", skill.body):
        if OWN_PATH.match(cited) and not (ROOT / cited).exists():
            problems.append(f"cites {cited}, which this repository does not have")
    return problems


class TestTheContract(unittest.TestCase):
    def setUp(self) -> None:
        self.skills, self.loader_problems = builtin_skills(ConductorTools.NAMES)

    def test_the_loader_found_nothing_wrong_with_any_built_in(self) -> None:
        self.assertEqual([], self.loader_problems)
        self.assertGreaterEqual(len(self.skills), 34)

    def test_every_built_in_meets_the_contract(self) -> None:
        for skill in self.skills:
            with self.subTest(skill=skill.name):
                self.assertEqual([], contract_problems(skill))

    def test_no_two_descriptions_are_the_same_or_one_inside_the_other(self) -> None:
        seen: list[tuple[str, str]] = []
        for skill in self.skills:
            mine = skill.description.lower()
            for other_name, other in seen:
                with self.subTest(skill=skill.name, other=other_name):
                    self.assertNotEqual(mine, other)
                    self.assertFalse(mine.startswith(other) or other.startswith(mine))
            seen.append((skill.name, mine))

    def test_every_declared_move_exists(self) -> None:
        for skill in self.skills:
            with self.subTest(skill=skill.name):
                self.assertEqual([], [m for m in skill.moves if m not in ConductorTools.NAMES])
                self.assertEqual((), skill.notes)


class TestTheContractCanFail(unittest.TestCase):
    """A check that cannot fail is a comment. Each rule is shown to refuse the thing it names."""

    def skill(self, description: str, moves: str = "read_file", body: str = "# T\n\nbody\n") -> Skill:
        text = f"---\nname: probe\ndescription: {description}\nmoves: {moves}\n---\n\n{body}"
        skill, problem = parse_skill(text, "probe", "built-in", ConductorTools.NAMES)
        self.assertIsNone(problem)
        assert skill is not None
        return skill

    def test_a_three_hundred_character_description_is_caught_even_though_the_loader_clips_it(self) -> None:
        long = self.skill("A" + "b" * 298 + ".")
        self.assertLessEqual(len(long.description), MAX_DESCRIPTION_CHARS, "the loader clips it, which is why the clip is the tell")
        self.assertTrue(contract_problems(long))

    def test_a_description_that_is_not_a_sentence_is_caught(self) -> None:
        self.assertTrue(contract_problems(self.skill("does a thing")))
        self.assertTrue(contract_problems(self.skill("Does a thing")))
        self.assertEqual([], contract_problems(self.skill("Does a thing.")))

    def test_a_writer_with_no_stop_condition_is_caught_and_a_reader_is_not(self) -> None:
        self.assertTrue(contract_problems(self.skill("Edits files.", moves="write")))
        self.assertTrue(contract_problems(self.skill("Runs checks.", moves="run_command")))
        self.assertEqual([], contract_problems(self.skill("Reads files.", moves="read_file")))
        self.assertEqual(
            [], contract_problems(self.skill("Edits files.", moves="write", body="# T\n\n## Stop when\n\n- done\n")),
        )

    def test_a_citation_of_a_file_this_repository_lacks_is_caught(self) -> None:
        self.assertTrue(contract_problems(self.skill("Cites.", body="# T\n\nsee `docs/99-nothing.md`\n")))
        self.assertEqual([], contract_problems(self.skill("Cites.", body="# T\n\nsee `docs/00-codify-architecture-overview.md`\n")))
        self.assertEqual([], contract_problems(self.skill("Cites.", body="# T\n\nopen `src/app.py` in the workspace\n")))


class TestTheProfilesSayWhereTheyComeFrom(unittest.TestCase):
    PROFILES = sorted((ROOT / "engine" / "builtin_profiles").glob("*.json"))

    def test_there_are_profiles_to_check(self) -> None:
        self.assertGreaterEqual(len(self.PROFILES), 6)

    def test_each_has_a_reviewed_date_and_sources_that_match_its_rules(self) -> None:
        for path in self.PROFILES:
            with self.subTest(profile=path.name):
                doc = json.loads(path.read_text(encoding="utf-8"))
                reviewed = datetime.date.fromisoformat(doc["reviewed"])
                self.assertLessEqual(reviewed, datetime.date.today(), "a review dated in the future is not a review")
                self.assertTrue(doc["sources"])
                self.assertTrue(all(re.fullmatch(r"CWE-\d+", s) for s in doc["sources"]))
                self.assertEqual(sorted({r["cwe"] for r in doc["rules"]}), sorted(doc["sources"]),
                                 "the sources are the CWEs the rules cite, no more and no fewer")

    def test_the_extra_fields_do_not_stop_the_profiles_loading(self) -> None:
        loaded = scan.load_profiles(None)
        self.assertEqual((), tuple(loaded.problems))
        self.assertGreaterEqual(len(loaded.profiles), 6)


if __name__ == "__main__":
    unittest.main()
