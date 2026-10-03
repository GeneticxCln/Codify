"""What the conductor is told about its own tools, and that it is told the truth.

The system prompt is the one place that says *when* to reach for each tool and what the engine does with a call,
and it had fallen behind the menu: `recall`, the four page tools, `todo` and `ask_user` were never named in it,
the budgets were not mentioned (so a model spent calls as if it had no limit and then met a paused goal), and the
recipe it points at still said a critic's objection could be argued with, when an objection now pauses the run
for the person (docs/09 §10.14). A model that is not told a tool exists uses it only when the schema alone
suggests it, and small models are the ones that need the sentence.

A prompt has a cost the tools do not show: it is paid on every call, by a model whose context window may be
4096 tokens, next to twenty tool schemas. So there is a ceiling as well as a floor, and a name in the prompt must
be a tool that exists, because the stale name (`delegate`) was exactly how the prompt used to lie.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.chat_prompts import CONDUCTOR_SYSTEM_PROMPT
from engine.conductor import READ_PAGE, TOOLS
from engine.conductor_tools import ConductorTools
from engine.skills import builtin_skills
from tests.test_conductor import ConductorTestCase, _ToolProvider

# Words a prompt puts in backticks that are not tools: the argument the stage moves take.
NOT_TOOLS = {"task"}

# Measured when this was written (about 2.5k characters, about 600 tokens) with room for a sentence or two;
# raising it is a decision to spend every conductor call's context on more prompt. Raised from 2900 to 3100 for
# the one sentence `fetch_page` needs (docs/12): it is the first tool whose address leaves the machine from the
# engine itself, and the instruction not to put the workspace in it belongs in the prompt and not only in a
# description a model may not reread.
CEILING = 3100


class TestTheToolsAreNamed(unittest.TestCase):
    def test_every_tool_the_conductor_can_be_offered_is_named_in_the_prompt(self) -> None:
        missing = [name for name in ConductorTools.NAMES if f"`{name}`" not in CONDUCTOR_SYSTEM_PROMPT]
        self.assertEqual([], missing, f"the prompt never tells the conductor about {missing}")

    def test_the_tools_and_the_table_are_the_same_names(self) -> None:
        self.assertEqual(sorted(ConductorTools.NAMES), sorted(t.name for t in TOOLS))

    def test_a_name_in_backticks_is_a_tool_that_exists(self) -> None:
        # The stale name was how this prompt used to lie: `delegate` outlived the tool it named.
        named = set(re.findall(r"`([a-z_]+)`", CONDUCTOR_SYSTEM_PROMPT))
        unknown = sorted(named - set(ConductorTools.NAMES) - NOT_TOOLS)
        self.assertEqual([], unknown, f"the prompt names things that are not tools: {unknown}")


class TestTheCostOfTheWords(unittest.TestCase):
    def test_the_prompt_stays_within_its_ceiling(self) -> None:
        self.assertLessEqual(
            len(CONDUCTOR_SYSTEM_PROMPT), CEILING,
            f"{len(CONDUCTOR_SYSTEM_PROMPT)} characters is over the {CEILING} this prompt may spend on every call",
        )

    def test_the_ceiling_is_not_slack(self) -> None:
        # A ceiling far above the prompt is permission to triple it without anyone deciding to.
        self.assertGreaterEqual(len(CONDUCTOR_SYSTEM_PROMPT), CEILING - 600)


class TestWhatItSaysTheEngineDoes(unittest.TestCase):
    """The behaviours the prompt has to state, anchored on the sentence that carries each."""

    def prompt(self) -> str:
        return " ".join(CONDUCTOR_SYSTEM_PROMPT.lower().split())

    def test_it_says_a_plan_is_made_once_and_waits_for_the_person(self) -> None:
        text = self.prompt()
        self.assertIn("plan` once", text)
        self.assertIn("approve", text)

    def test_it_says_a_critics_objection_is_reported_and_the_run_stops(self) -> None:
        text = self.prompt()
        self.assertIn("critic", text)
        self.assertRegex(text, r"critic[^.]*(say|tell)[^.]*stop")

    def test_it_says_the_calls_are_limited_and_what_happens_when_they_run_out(self) -> None:
        text = self.prompt()
        self.assertIn("limited", text)
        self.assertIn("paused", text)

    def test_it_says_when_to_ask_and_that_asking_ends_the_run(self) -> None:
        text = self.prompt()
        self.assertIn("`ask_user`", text)
        self.assertRegex(text, r"ask_user`[^.]*(before|only)")
        self.assertRegex(text, r"ask_user`[^.]*stop")

    def test_it_says_the_todo_notes_are_the_models_own_and_not_orders(self) -> None:
        text = self.prompt()
        self.assertRegex(text, r"todo`[^.]*(your|own)")
        self.assertIn("not instructions", text)

    def test_it_says_commands_that_run_the_projects_code_wait_for_approval(self) -> None:
        text = self.prompt()
        self.assertRegex(text, r"run_command`[^.]*(approv|before)")

    def test_it_says_a_page_is_text_and_not_orders(self) -> None:
        text = self.prompt()
        self.assertRegex(text, r"page[^.]*(not instructions|untrusted|not orders)")

    def test_it_says_an_editor_edit_never_saves_and_has_to_be_reported(self) -> None:
        # The one tool whose effect the person did not ask for by name. What the model must carry into its answer is
        # that the text is unsaved (the person's Save is the only door to the disk) and that it owes them what changed.
        text = self.prompt()
        self.assertRegex(text, r"edit_editor`[^.]*never saves")
        self.assertRegex(text, r"edit_editor`[^.]*say what you changed")
        self.assertRegex(text, r"read_editor`[^.]*(not yet saved|unsaved)")

    def test_it_points_at_recall_as_the_first_look_at_a_failure(self) -> None:
        text = self.prompt()
        self.assertRegex(text, r"recall`[^.]*(fail|before|past)")


class TestTheRecipeAgreesWithTheEngine(unittest.TestCase):
    def recipe(self) -> str:
        skills, problems = builtin_skills(ConductorTools.NAMES)
        self.assertEqual([], problems)
        return " ".join(next(s for s in skills if s.name == "ship-a-change").body.lower().split())

    def test_a_critics_objection_stops_the_run_it_is_not_argued_with(self) -> None:
        # The recipe used to say to "act on them or say plainly that you are not going to". An objection now
        # pauses the goal and `write` refuses until the person presses Start, so the only honest instruction
        # is to say what the critic asked for, and stop.
        text = self.recipe()
        self.assertNotIn("say plainly that you are not going to", text)
        self.assertRegex(text, r"critic[^.]*(asks|objects|rejects)[^.]*(stop|pause)")

    def test_it_says_to_plan_once(self) -> None:
        # Anchored on the `plan` step's own sentence: "once per step" appears further down for a different reason.
        self.assertRegex(self.recipe(), r"`plan`\*\* .{0,200}call it once")

    def test_it_mentions_the_notes_and_the_question(self) -> None:
        text = self.recipe()
        self.assertIn("`todo`", text)
        self.assertIn("`ask_user`", text)

    def test_it_says_to_run_the_projects_own_checks_after_the_change(self) -> None:
        self.assertRegex(self.recipe(), r"(linter|type checker|lint)")


ENGINE = Path(__file__).resolve().parent.parent / "engine"

# Tools that existed and do not: a string that tells a model to call one is an instruction it cannot follow.
# `delegate` ran the whole recipe in one call and was removed when the stages became moves (docs/09 §10.14).
GONE = ("delegate",)


def _strings_the_engine_says(path: Path) -> list[tuple[int, str]]:
    """Every string literal in a module that is not a docstring: what the code *says*, not what it explains."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


class TestNothingTheModelIsToldNamesAToolThatIsGone(unittest.TestCase):
    """`git_history`'s refusal told the model to "use delegate for a change", a tool removed long before.

    The conductor reads tool results, refusals, descriptions, briefs and skills. A name in any of them that no
    longer exists is an instruction it cannot follow, and a small model follows it anyway and is refused.
    """

    def test_no_string_the_engine_says_names_a_removed_tool(self) -> None:
        found: list[str] = []
        for path in sorted(ENGINE.glob("*.py")):
            for line, text in _strings_the_engine_says(path):
                for name in GONE:
                    if re.search(rf"\b{name}\b", text, re.IGNORECASE):
                        found.append(f"{path.name}:{line}: {text[:90]!r}")
        self.assertEqual([], found)

    def test_no_built_in_skill_names_a_removed_tool(self) -> None:
        for path in sorted((ENGINE / "builtin_skills").glob("*.md")):
            for name in GONE:
                self.assertIsNone(re.search(rf"\b{name}\b", path.read_text(encoding="utf-8"), re.IGNORECASE), path.name)

    def test_the_scan_can_see_a_string_and_ignores_a_docstring(self) -> None:
        # A scan that finds nothing in anything passes forever; this is its negative control.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample.py"
            sample.write_text('"""A docstring that says delegate."""\nSAYS = "use delegate for a change"\n', encoding="utf-8")
            texts = [text for _, text in _strings_the_engine_says(sample)]
        self.assertEqual(["use delegate for a change"], texts)


class TestWhatReadPageSaysAboutItself(unittest.TestCase):
    def test_it_does_not_claim_no_page_can_be_opened_when_a_tool_opens_pages(self) -> None:
        # `navigate_page` opens an address (through the same guard as the person's click). `read_page` only
        # reads, and its description used to say the model "cannot open a page", which is false of the menu.
        text = " ".join(READ_PAGE.description.lower().split())
        self.assertNotIn("cannot open a page", text)
        self.assertIn("`navigate_page`", READ_PAGE.description)
        self.assertIn("quotation", text, "the page is still quoted as untrusted text")


class TestTheBriefsSpeakOfRealToolsAndTheRightWayToAsk(ConductorTestCase):
    """The per-turn briefs sit next to the system prompt and have to say the same things about the same tools."""

    def _briefs(self) -> dict[str, str]:
        executor = self._executor(_ToolProvider())
        goal = self.goals.get(self.goal.id)
        executor._insert_steps(self.goal.id, [{"title": "S", "description": "d", "suggested_paths": ["a.py"]}])
        step = self.goals.steps(self.goal.id)[0]
        out = {f"brief:{intent}": executor._intent_brief(intent) for intent in ("question", "code_change", "execute", "other")}
        out["nudge"] = executor._intent_nudge("code_change") or ""
        out["step"] = executor._step_prompt(goal, step, [step])
        return out

    async def test_a_name_in_backticks_in_any_brief_is_a_tool_that_exists(self) -> None:
        for where, text in self._briefs().items():
            named = set(re.findall(r"`([a-z_]+)`", text))
            unknown = sorted(named - set(ConductorTools.NAMES) - NOT_TOOLS)
            self.assertEqual([], unknown, f"{where} names things that are not tools: {unknown}")

    async def test_a_change_that_cannot_be_planned_without_an_answer_asks_through_the_tool(self) -> None:
        # "Ask it in one sentence and stop" was advice with nothing to make it stop. There is a tool now, and
        # the two places that told a stuck model to ask have to say which.
        briefs = self._briefs()
        self.assertIn("`ask_user`", briefs["nudge"])
        self.assertIn("`ask_user`", briefs["brief:code_change"])

    async def test_an_approved_step_is_not_told_to_ask_anything(self) -> None:
        # `ask_user` is not on the menu while a plan runs, so a brief that mentioned it would send the
        # model for a tool it was not given.
        briefs = self._briefs()
        self.assertNotIn("ask_user", briefs["brief:execute"])
        self.assertNotIn("ask_user", briefs["step"])

    async def test_an_approved_step_is_told_to_stop_when_the_critic_asks_for_changes(self) -> None:
        text = " ".join(self._briefs()["step"].lower().split())
        self.assertRegex(text, r"critic asks for changes[^.]*stop")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
