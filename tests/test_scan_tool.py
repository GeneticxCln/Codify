"""The conductor's `scan_code`: what it is offered as, what it answers, and what it cannot be made to do.

`tests/test_scan.py` holds the scanner. This file holds what stands between a model and it: the menu, the two shapes
of call (list the profiles; scan with one), the sentences a refusal comes back as, the skill that tells the model
to read a hit before it believes it, and the one thing that must stay true however the call is phrased: it reads.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import unittest
from pathlib import Path
from typing import Any

from engine.chat_prompts import CONDUCTOR_SYSTEM_PROMPT
from engine.conductor import BASE_TOOLS, SCAN_CODE, STEP_TOOLS, TOOLS, Conductor
from engine.conductor_tools import ConductorTools
from engine.skills import builtin_skills, load_skills
from engine.toolcall import ToolReply
from tests.test_conductor import ConductorTestCase, _call, _ToolProvider


class ScanToolCase(ConductorTestCase):
    def table(self) -> dict[str, Any]:
        return self._dispatch(self.goal.id)


class TestItIsOnTheMenuLikeTheOtherReadTools(ScanToolCase):
    async def test_it_is_a_base_tool_the_table_answers_to_and_no_setting_gates_it(self) -> None:
        self.assertIn("scan_code", [t.name for t in BASE_TOOLS])
        self.assertNotIn("scan_code", [t.name for t in STEP_TOOLS])
        self.assertIn("scan_code", ConductorTools.NAMES)
        self.assertIn("scan_code", self.table())
        executor = self._executor(_ToolProvider())
        for planned in (False, True):
            if planned:
                executor._insert_steps(self.goal.id, [{"title": "S", "description": "d", "suggested_paths": ["a.py"]}])
            self.assertIn("scan_code", [t.name for t in executor.conductor_menu(self.goal.id)()])

    async def test_it_takes_nothing_it_needs_and_nothing_a_model_could_use_to_reach_further(self) -> None:
        props = SCAN_CODE.parameters["properties"]
        self.assertEqual(set(props), {"profile", "glob", "include_tests", "include_comments"})
        self.assertNotIn("required", SCAN_CODE.parameters)
        self.assertEqual(props["include_tests"]["type"], "boolean")

    async def test_the_description_and_the_prompt_say_a_hit_is_a_candidate(self) -> None:
        description = " ".join(SCAN_CODE.description.split())
        self.assertIn("CANDIDATES, not vulnerabilities", description)
        self.assertIn("read each hit with `read_file`", description)
        prompt = " ".join(CONDUCTOR_SYSTEM_PROMPT.split())
        self.assertIn("`scan_code` runs curated security rules; its hits are candidates, so read one before calling it real", prompt)


class TestWhatACallAnswers(ScanToolCase):
    def write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    async def test_no_profile_lists_them_and_says_how_to_choose(self) -> None:
        answer = await self.table()["scan_code"]({})
        for name in ("secrets", "injection", "crypto", "deserialization", "unsafe-c", "web"):
            self.assertIn(f"- {name} (built-in,", answer)
        self.assertIn("Call `scan_code` with `profile` set to one of these names, or `all`.", answer)
        self.assertEqual(answer, await self.table()["scan_code"]({"profile": "   "}))

    async def test_a_profile_returns_candidates_at_their_lines(self) -> None:
        self.write("app/views.py", "import os\n\ndef run(name):\n    os.system('ls ' + name)\n")
        answer = await self.table()["scan_code"]({"profile": "injection"})
        self.assertIn("CANDIDATES for a code review", answer)
        self.assertIn("[HIGH] py-os-system · CWE-78", answer)
        self.assertIn("app/views.py:4  os.system('ls ' + name)", answer)

    async def test_glob_and_the_two_switches_reach_the_scan(self) -> None:
        self.write("src/a.py", "eval(x)\n")
        self.write("tests/test_a.py", "eval(x)\n")
        self.write("src/b.py", "# eval(x) in a comment\n")
        scan = self.table()["scan_code"]
        plain = await scan({"profile": "injection"})
        self.assertIn("src/a.py:1", plain)
        self.assertNotIn("tests/test_a.py", plain.split("Coverage:")[0])
        self.assertNotIn("src/b.py", plain)
        self.assertIn("tests/test_a.py:1", await scan({"profile": "injection", "include_tests": True}))
        self.assertIn("src/b.py:1", await scan({"profile": "injection", "include_comments": True}))
        narrowed = await scan({"profile": "injection", "glob": "src/b.py"})
        self.assertIn("No rule matched.", narrowed)

    async def test_an_unknown_profile_is_a_sentence_that_names_the_ones_there_are(self) -> None:
        answer = await self.table()["scan_code"]({"profile": "everything-please"})
        self.assertTrue(answer.startswith("That scan did not run: there is no profile called 'everything-please'"), answer)
        self.assertIn("injection", answer)

    async def test_a_model_that_calls_it_through_the_loop_gets_the_result_back(self) -> None:
        self.write("a.py", "eval(x)\n")
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("scan_code", profile="injection")]),
            ToolReply(text="The scanner flagged one line; I have not read it yet."),
        ])
        conductor = Conductor(provider, "m", str(self.repo), list(TOOLS), self.table(), system_prompt="s")
        await conductor.run("review this for security problems")
        result = [m for m in provider.seen_messages[1] if m.get("role") == "tool"][0]["content"]
        self.assertIn("py-eval-exec", result)
        self.assertIn("a.py:1", result)


class TestItCannotBeMadeToWrite(ScanToolCase):
    def snapshot(self) -> list[tuple[str, int, int]]:
        return sorted(
            (str(p.relative_to(self.repo)), p.stat().st_size, p.stat().st_mtime_ns)
            for p in self.repo.rglob("*") if p.is_file()
        )

    async def test_no_call_changes_a_file_or_makes_one(self) -> None:
        (self.repo / "bad.py").write_text("eval(x)\npassword = 'swordfish1'\n", encoding="utf-8")
        before = self.snapshot()
        names_before = sorted(p.name for p in self.repo.iterdir())
        scan = self.table()["scan_code"]
        for args in (
            {}, {"profile": "all"}, {"profile": "secrets", "include_comments": True, "include_tests": True},
            {"profile": "injection", "glob": "../*"}, {"profile": "../../etc/passwd"}, {"profile": "all", "glob": "/etc/*"},
        ):
            await scan(args)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(names_before, sorted(p.name for p in self.repo.iterdir()))

    async def test_a_workspace_profile_that_tries_to_name_a_path_or_a_command_changes_nothing_about_what_is_read(self) -> None:
        directory = self.repo / ".codify" / "profiles"
        directory.mkdir(parents=True)
        (directory / "sneaky.json").write_text(json.dumps({
            "name": "sneaky", "root": "/", "paths": ["/etc/passwd"], "run": ["touch", "/tmp/pwned"],
            "rules": [{
                "id": "everything", "severity": "high", "pattern": "root", "where": "raw", "languages": [],
                "path": "/etc/passwd", "command": ["touch", "/tmp/pwned"], "write": "/tmp/pwned",
            }],
        }), encoding="utf-8")
        answer = await self.table()["scan_code"]({"profile": "sneaky"})
        # The profile file is itself in the workspace, so it is scanned and its own text is quoted back: that is the
        # scanner reading the workspace, which is its job. What must not happen is anything outside it being read.
        self.assertNotIn("root:x", answer, "the contents of /etc/passwd reached the model")
        for line in answer.splitlines():
            if line.startswith("  ") and ":" in line:
                self.assertTrue(line.strip().startswith(".codify/profiles/sneaky.json:"), line)
        self.assertIn("Coverage: 3 files scanned", answer, "a file outside the workspace was walked")
        self.assertFalse(Path("/tmp/pwned").exists())  # noqa: S108 — the path a hostile profile asked for


class TestTheSecurityReviewSkill(ScanToolCase):
    def skill(self) -> Any:
        skills, problems = builtin_skills(ConductorTools.NAMES)
        self.assertEqual(problems, [])
        return {s.name: s for s in skills}["security-review"]

    async def test_it_ships_and_every_move_it_names_is_a_tool_that_exists(self) -> None:
        skill = self.skill()
        self.assertEqual(skill.source, "built-in")
        self.assertEqual(skill.moves, ("scan_code", "read_file", "search_code", "recall"))
        self.assertEqual(skill.notes, ())
        self.assertIn("security-review", load_skills(str(self.repo)).names())

    async def test_it_tells_the_model_to_read_before_believing_and_to_say_what_was_not_scanned(self) -> None:
        body = " ".join(self.skill().body.split())
        self.assertIn("finds *candidates*", body)
        self.assertIn("Read before you decide", body)
        self.assertIn("Do not call something exploitable because a rule matched", body)
        self.assertIn("an unscanned file is not a clean file", body)
        self.assertIn("Do not claim a clean bill of health", body)

    async def test_it_sends_fixes_through_the_plan_and_never_around_it(self) -> None:
        body = " ".join(self.skill().body.split())
        self.assertIn("Fixes go through `plan`, which the person approves", body)
        self.assertNotIn("`write`", body, "a review skill that names the writing move is one step from using it unapproved")

    async def test_use_skill_returns_it_with_no_note_because_every_move_it_names_is_on_the_menu(self) -> None:
        # The first version of this skill named `todo`, which is a step tool and so is not offered to a plain turn.
        # The engine's note said so, and this is the test that would have caught the skill promising what the menu lacks.
        answer = await self.table()["use_skill"]({"name": "security-review"})
        self.assertIn("# Security review", answer)
        self.assertNotIn("Note from the engine", answer)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
