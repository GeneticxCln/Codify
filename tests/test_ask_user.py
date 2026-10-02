"""`ask_user`: the conductor puts one question to the person, and the turn ends there.

A conductor that cannot go on without an answer used to have one way to say so: prose that happened to end in a
question mark. Nothing knew it was a question, so a model could ask and then, on the same reply, carry on and
make the move it had just asked about; the window could not offer the choices as choices; and the nudge told a
stuck model to "ask in one sentence and stop" with nothing that made it stop.

`ask_user` is a tool whose whole effect is to end the run. It is not a capability: it cannot approve anything,
write, or run a command. The answer is the person's next message, an ordinary turn through the one door that
creates turns (docs/00 §6.8), so there is no new route and no state to hold between the question and the answer.

It is offered on a turn and never while an approved plan is running. A run that is carrying out a step the person
approved has nobody sitting at it to answer, and a step must end finished or paused with a reason, not parked on
a question.
"""

from __future__ import annotations

import unittest
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.ask import (
    MAX_OPTION_CHARS,
    MAX_OPTIONS,
    MAX_QUESTION_CHARS,
    AskRefused,
    parse_question,
)
from engine.conductor import ASK_USER, TOOLS, Conductor, EndTurn
from engine.conductor_tools import ConductorTools
from engine.skills import load_skills
from engine.toolcall import DIALECTS, ToolReply, schema_problems
from tests.test_conductor import ConductorTestCase, _call, _ToolProvider
from tests.test_moves import _ChangeGate


class TestTheQuestion(unittest.TestCase):
    def test_a_question_alone_is_a_question_with_no_options(self) -> None:
        asked = parse_question({"question": "Which database should this use?"})
        self.assertEqual("Which database should this use?", asked.text)
        self.assertEqual([], asked.options)
        self.assertEqual("Which database should this use?", asked.prose())

    def test_the_options_are_numbered_in_the_prose_so_history_and_speech_carry_them(self) -> None:
        asked = parse_question({"question": "Which one?", "options": ["SQLite", "Postgres"]})
        self.assertEqual("Which one?\n\n1. SQLite\n2. Postgres", asked.prose())
        self.assertEqual({"text": "Which one?", "options": ["SQLite", "Postgres"]}, asked.to_payload())

    def test_an_option_is_one_line_and_a_repeat_is_said_once(self) -> None:
        asked = parse_question({
            "question": "Which?",
            "options": ["  SQLite \n (local) ", "sqlite (local)", "Postgres", "", "   "],
        })
        self.assertEqual(["SQLite (local)", "Postgres"], asked.options)

    def test_the_question_keeps_its_paragraphs_but_not_control_characters(self) -> None:
        asked = parse_question({"question": "First line.\n\nSecond\x00 line.\x1b[0m"})
        self.assertEqual("First line.\n\nSecond line.[0m", asked.text)

    def test_no_question_is_refused(self) -> None:
        for args in ({}, {"question": ""}, {"question": "   \n"}, {"question": 7}, {"question": None}):
            with self.assertRaises(AskRefused, msg=repr(args)):
                parse_question(args)

    def test_a_question_over_the_limit_is_refused_and_names_it(self) -> None:
        with self.assertRaises(AskRefused) as raised:
            parse_question({"question": "x" * (MAX_QUESTION_CHARS + 1)})
        self.assertIn(str(MAX_QUESTION_CHARS), str(raised.exception))
        parse_question({"question": "x" * MAX_QUESTION_CHARS})  # exactly the limit is fine

    def test_more_than_four_options_is_refused_and_names_the_limit(self) -> None:
        self.assertEqual(4, MAX_OPTIONS)
        with self.assertRaises(AskRefused) as raised:
            parse_question({"question": "Which?", "options": [f"option {n}" for n in range(MAX_OPTIONS + 1)]})
        self.assertIn(str(MAX_OPTIONS), str(raised.exception))
        parse_question({"question": "Which?", "options": [f"option {n}" for n in range(MAX_OPTIONS)]})

    def test_an_option_over_the_limit_is_refused_and_names_which(self) -> None:
        self.assertEqual(80, MAX_OPTION_CHARS)
        with self.assertRaises(AskRefused) as raised:
            parse_question({"question": "Which?", "options": ["fine", "y" * (MAX_OPTION_CHARS + 1)]})
        self.assertIn("2", str(raised.exception))
        self.assertIn(str(MAX_OPTION_CHARS), str(raised.exception))
        parse_question({"question": "Which?", "options": ["fine", "y" * MAX_OPTION_CHARS]})

    def test_one_option_is_not_a_choice(self) -> None:
        with self.assertRaises(AskRefused) as raised:
            parse_question({"question": "Which?", "options": ["only this"]})
        self.assertIn("two", str(raised.exception))
        # ...and a list that *becomes* one option once repeats and blanks go is the same mistake.
        with self.assertRaises(AskRefused):
            parse_question({"question": "Which?", "options": ["same", "SAME", ""]})

    def test_options_that_are_not_a_list_of_text_are_refused(self) -> None:
        for bad in ("SQLite, Postgres", {"a": 1}, 3, [1, 2], ["ok", None], [["nested"], "x"]):
            with self.assertRaises(AskRefused, msg=repr(bad)):
                parse_question({"question": "Which?", "options": bad})

    def test_an_empty_options_list_is_no_options(self) -> None:
        self.assertEqual([], parse_question({"question": "Which?", "options": []}).options)
        self.assertEqual([], parse_question({"question": "Which?", "options": None}).options)


class TestTheSpec(unittest.TestCase):
    def test_it_is_one_of_the_tools_and_well_formed_in_every_dialect(self) -> None:
        self.assertIn(ASK_USER, TOOLS)
        self.assertIn("ask_user", ConductorTools.NAMES)
        for dialect in DIALECTS:
            self.assertEqual([], schema_problems(ASK_USER, dialect), dialect)

    def test_options_is_an_array_of_strings_and_only_the_question_is_required(self) -> None:
        props = ASK_USER.parameters["properties"]
        self.assertEqual("array", props["options"]["type"])
        self.assertEqual({"type": "string"}, props["options"]["items"])
        self.assertEqual(["question"], ASK_USER.parameters["required"])

    def test_the_description_says_when_not_to_ask(self) -> None:
        text = ASK_USER.description.lower()
        self.assertIn("ends", text, "the model has to be told the run stops")
        self.assertIn("not", text)


class TestTheLoopStopsAtTheQuestion(ConductorTestCase):
    def _loop(self, provider: _ToolProvider, dispatch: dict[str, Any]) -> Conductor:
        return Conductor(
            provider, "m", str(self.repo), list(TOOLS), dispatch, system_prompt="s", max_turns=8,
        )

    async def test_the_run_ends_with_the_question_as_its_answer(self) -> None:
        table = self._dispatch(self.goal.id)
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("ask_user", question="Which one?", options=["A", "B"])]),
            ToolReply(text="this must never be asked for"),
        ])
        loop = self._loop(provider, table)

        answer = await loop.run("do the thing")

        self.assertEqual("Which one?\n\n1. A\n2. B", answer)
        self.assertEqual(1, len(provider.seen_messages), "the loop made another model call after the question")
        self.assertIsInstance(loop.ended, EndTurn)
        assert loop.ended is not None
        self.assertEqual({"text": "Which one?", "options": ["A", "B"]}, loop.ended.question)

    async def test_nothing_else_in_the_same_reply_runs_after_the_question(self) -> None:
        ran: list[str] = []

        async def read_file(args: dict[str, Any]) -> str:
            ran.append("read_file")
            return "contents"

        table = self._dispatch(self.goal.id, read_file=read_file)
        provider = _ToolProvider([
            ToolReply(tool_calls=[
                _call("ask_user", question="Which one?"),
                _call("read_file", path="app.py"),
            ]),
        ])

        await self._loop(provider, table).run("go")

        self.assertEqual([], ran, "a tool ran after the person had been asked something")

    async def test_what_ran_before_the_question_stays_run(self) -> None:
        ran: list[str] = []

        async def read_file(args: dict[str, Any]) -> str:
            ran.append("read_file")
            return "contents"

        table = self._dispatch(self.goal.id, read_file=read_file)
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(tool_calls=[_call("ask_user", question="Which one?")]),
        ])

        answer = await self._loop(provider, table).run("go")

        self.assertEqual(["read_file"], ran)
        self.assertEqual("Which one?", answer)

    async def test_a_question_is_not_a_stage_move_and_costs_none_of_the_move_budget(self) -> None:
        table = self._dispatch(self.goal.id)
        provider = _ToolProvider([ToolReply(tool_calls=[_call("ask_user", question="Which one?")])])
        loop = self._loop(provider, table)

        await loop.run("go")

        self.assertEqual(0, loop.moves_made)

    async def test_a_refused_question_is_answered_and_the_run_goes_on(self) -> None:
        table = self._dispatch(self.goal.id)
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("ask_user", question="Which?", options=["only one"])]),
            ToolReply(tool_calls=[_call("ask_user", question="Which?", options=["A", "B"])]),
        ])
        loop = self._loop(provider, table)

        answer = await loop.run("go")

        self.assertEqual("Which?\n\n1. A\n2. B", answer)
        told = [m["content"] for m in provider.seen_messages[1] if m.get("role") == "tool"]
        self.assertTrue(any("two" in t for t in told), told)
        # The refusal is a sentence about the question, not "That call failed: AskRefused…", which reads
        # as a fault in the engine and not as something the model can fix.
        self.assertFalse(any(t.startswith("That call failed") for t in told), told)

    async def test_the_tool_answers_a_refusal_with_the_sentence_and_does_not_raise(self) -> None:
        table = self._dispatch(self.goal.id)

        out = await table["ask_user"]({"question": "Which?", "options": ["one", "two", "three", "four", "five"]})

        self.assertIn(str(MAX_OPTIONS), out)


class TestWhereItIsOffered(ConductorTestCase):
    def _offered(self, goal_id: str) -> list[str]:
        return [t.name for t in self._executor(_ToolProvider()).conductor_menu(goal_id)()]

    async def test_it_is_offered_on_a_turn(self) -> None:
        self.assertNotEqual("RUNNING", self.goals.get(self.goal.id).status)
        self.assertIn("ask_user", self._offered(self.goal.id))

    async def test_it_is_not_offered_once_there_is_a_plan_because_the_plan_is_the_question(self) -> None:
        # A turn that planned is drawn as its plan, so a question asked after the plan would not be seen.
        # The person's answer to a plan is to approve it, edit it, or say what to change.
        executor = self._executor(_ToolProvider())
        executor._insert_steps(self.goal.id, [{"title": "S", "description": "d", "suggested_paths": []}])
        self.assertNotIn("ask_user", [t.name for t in executor.conductor_menu(self.goal.id)()])

    async def test_the_tool_itself_refuses_once_there_is_a_plan(self) -> None:
        executor = self._executor(_ToolProvider())
        executor._insert_steps(self.goal.id, [{"title": "S", "description": "d", "suggested_paths": []}])
        table = executor._conductor_dispatch(
            self.goal.id, self.goals.get(self.goal.id), str(self.repo), load_skills(str(self.repo)),
        )

        out = await table["ask_user"]({"question": "Which name?"})

        self.assertIn("plan", out)

    async def test_it_is_not_offered_while_an_approved_plan_is_running(self) -> None:
        current = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, current.version, "RUNNING")
        self.assertNotIn("ask_user", self._offered(self.goal.id))

    async def test_a_model_that_names_it_anyway_while_running_is_refused_and_not_ended(self) -> None:
        current = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, current.version, "RUNNING")
        executor = self._executor(_ToolProvider())
        loop = Conductor(
            _ToolProvider([
                ToolReply(tool_calls=[_call("ask_user", question="May I?")]),
                ToolReply(text="carrying on"),
            ]),
            "m", str(self.repo),
            dispatch=executor._conductor_dispatch(
                self.goal.id, self.goals.get(self.goal.id), str(self.repo), load_skills(str(self.repo)),
            ),
            system_prompt="s", menu=executor.conductor_menu(self.goal.id),
        )

        answer = await loop.run("go")

        self.assertEqual("carrying on", answer)
        self.assertIsNone(loop.ended)

    async def test_the_tool_itself_refuses_while_running_even_if_called_directly(self) -> None:
        # Defence in depth: the menu is the first gate and the tool is the second, so a caller that builds
        # its own dispatch with no menu (a test, a future caller) still cannot park an approved run.
        current = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, current.version, "RUNNING")
        table = self._dispatch(self.goal.id)

        out = await table["ask_user"]({"question": "May I?"})

        self.assertIn("approved", out)

    async def test_a_step_run_is_not_offered_it(self) -> None:
        current = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, current.version, "RUNNING")
        seen = _ToolProvider([ToolReply(text="done")])
        executor = self._executor(seen)
        executor._insert_steps(self.goal.id, [{"title": "S", "description": "d", "suggested_paths": []}])

        await executor._conduct(
            self.goal.id, self.goals.get(self.goal.id), str(self.repo),
            prompt_override="Take step one.", intent="execute",
        )

        self.assertTrue(seen.seen_tools)
        self.assertNotIn("ask_user", seen.seen_tools[0])


class TestTheTurnThatAsks(ConductorTestCase):
    def _turn_logs(self) -> list[dict[str, Any]]:
        return [
            e.payload for e in self.goals.events_after(self.goal.id, 0)
            if e.type == "log" and e.payload.get("turn")
        ]

    async def test_the_turn_ends_with_the_question_published_as_its_answer(self) -> None:
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("ask_user", question="Which database?", options=["SQLite", "Postgres"])]),
        ])
        executor = self._executor(provider)

        await executor.run_chat(self.goal.id)

        logs = self._turn_logs()
        self.assertEqual(1, len(logs))
        self.assertEqual("Which database?\n\n1. SQLite\n2. Postgres", logs[0]["message"])
        self.assertEqual({"text": "Which database?", "options": ["SQLite", "Postgres"]}, logs[0]["question"])
        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)
        self.assertEqual([], self.goals.steps(self.goal.id))

    async def test_a_turn_that_did_not_ask_carries_no_question(self) -> None:
        provider = _ToolProvider([ToolReply(text="Just an answer.")])
        executor = self._executor(provider)

        await executor.run_chat(self.goal.id)

        logs = self._turn_logs()
        self.assertEqual(1, len(logs))
        self.assertNotIn("question", logs[0])

    async def test_it_is_a_finished_turn_not_a_failure_even_for_a_change_request(self) -> None:
        # A change the conductor cannot start without an answer is not a conductor that failed: it asked,
        # which is a decision, and the turn must not end in "nothing was changed" as if it had given up.
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("ask_user", question="Which module?", options=["a.py", "b.py"])]),
        ])
        executor = self._executor(provider, laya=_ChangeGate())

        await executor.run_chat(self.goal.id)

        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)
        self.assertEqual([], [e for e in self.goals.events_after(self.goal.id, 0) if e.type == "error"])
        warnings = [
            e.payload["message"] for e in self.goals.events_after(self.goal.id, 0)
            if e.type == "log" and e.payload.get("level") == "warn"
        ]
        self.assertFalse(any("no file was changed" in w for w in warnings), warnings)

    async def test_the_question_is_in_the_next_turns_history_so_the_answer_means_something(self) -> None:
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("ask_user", question="Which database?", options=["SQLite", "Postgres"])]),
        ])
        await self._executor(provider).run_chat(self.goal.id)

        history = self.goals.turn_history(self.thread.id, 12)

        self.assertEqual(1, len(history))
        self.assertIn("Which database?", history[0]["reply"])
        self.assertIn("2. Postgres", history[0]["reply"])

    async def test_a_question_asked_before_the_plan_is_asked_and_one_after_it_is_refused(self) -> None:
        # The menu offers `ask_user` until `plan` has made something, and not after: the model that names it
        # anyway is told so and the plan it made stands, waiting for approval.
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("recon", task="look")]),
            ToolReply(tool_calls=[_call("plan", task="change it")]),
            ToolReply(tool_calls=[_call("ask_user", question="Shall I use the new name?")]),
            ToolReply(text="The plan is above; approve it or tell me what to change."),
        ])
        executor = self._executor(provider, laya=_ChangeGate())

        await executor.run_chat(self.goal.id)

        self.assertEqual("PENDING", self.goals.get(self.goal.id).status, "the plan waits for approval")
        self.assertEqual(["S1"], [s.title for s in self.goals.steps(self.goal.id)])
        self.assertNotIn("question", self._turn_logs()[-1])
        self.assertIn("ask_user", provider.seen_tools[0], "before a plan it is offered")
        self.assertNotIn("ask_user", provider.seen_tools[-1], "after a plan it is not")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
