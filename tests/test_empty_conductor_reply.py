"""A conductor whose model says nothing has not answered (audit of 2026-09-29, second pass).

`_Conducted.finished` counted any non-`None` answer as a finished turn, so a model that returned empty
content — a small model that spent its budget on `<think>`, an Ollama context overflow, a server that
answered `{}` — completed the turn with the literal text `(no answer)` and never reached the floor the
docs describe ("a conductor that ... produced neither an answer worth having nor a plan has not decided
anything, and falling back to the engine's own sequence is strictly better than failing the turn"). That floor has since
been removed for a request the gate read as a change (docs/09 §10.14): the silent conductor now ends such a turn with
a plain failure, and a question still falls back to the plain reply.
Found by driving a real engine against a server whose `/api/chat` answered in the wrong shape: the turn
"completed" without a single model call having said anything.
"""

from __future__ import annotations

from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from tests.test_conductor import ConductorTestCase, _call, _ToolProvider
from tests.test_one_writer_per_goal import _ChangeGate

from engine.chat_prompts import CHAT_SYSTEM_PROMPT
from engine.executor import _Conducted
from engine.toolcall import ToolReply


class _SaysNothingThenAnswers(_ToolProvider):
    """The loop's model returns empty content; the single-shot call (what a plain turn uses) answers."""

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        if system_prompt == CHAT_SYSTEM_PROMPT:
            return "The repository has one module, app.py."
        return await super().complete(
            system_prompt, user_prompt, model, temperature, max_tokens,
            num_ctx=num_ctx, keep_alive=keep_alive,
        )


class TestWhatFinishedMeans(ConductorTestCase):
    async def test_an_empty_answer_with_no_plan_is_not_a_finished_turn(self) -> None:
        for empty in ("", "   ", "\n"):
            self.assertFalse(
                _Conducted(answer=empty, exhausted=False, planned=False).finished, repr(empty)
            )

    async def test_an_empty_answer_after_a_plan_is_finished(self) -> None:
        # The plan is the turn's result; the conductor had nothing more to say about it.
        self.assertTrue(_Conducted(answer="", exhausted=False, planned=True).finished)

    async def test_a_real_answer_is_finished(self) -> None:
        self.assertTrue(_Conducted(answer="It parses.", exhausted=False, planned=False).finished)

    async def test_the_explanation_names_the_silence(self) -> None:
        text = _Conducted(answer="", exhausted=False, planned=False).explanation()
        self.assertIn("nothing", text)


class TestASilentModelFallsBack(ConductorTestCase):
    def _events(self) -> list[Any]:
        return list(self.goals.events_after(self.goal.id, 0))

    def _replies(self) -> list[str]:
        return [e.payload["message"] for e in self._events()
                if e.type == "log" and e.payload.get("turn")]

    async def test_a_question_the_loop_could_not_answer_is_answered_by_the_plain_call(self) -> None:
        provider = _SaysNothingThenAnswers(replies=[ToolReply(text="")])
        executor = self._executor(provider)

        await executor.run_chat(self.goal.id)

        self.assertEqual(["The repository has one module, app.py."], self._replies())
        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)

    async def test_the_person_is_told_the_conductor_said_nothing(self) -> None:
        executor = self._executor(_SaysNothingThenAnswers(replies=[ToolReply(text="")]))

        await executor.run_chat(self.goal.id)

        warnings = [e.payload["message"] for e in self._events()
                    if e.type == "log" and e.payload.get("level") == "warn"]
        self.assertTrue(any("did not finish" in w for w in warnings), warnings)

    async def test_a_silent_conductor_on_a_change_fails_the_turn_rather_than_running_a_second_pipeline(self) -> None:
        # Two empty replies: the first is met with the loop's one-time nudge (a change with nothing
        # planned), and the second is the silence that ends the loop. The recipe used to run here; on an
        # install with a conductor it no longer does (`tests/test_conductor_turn_end.py`).
        replies = [ToolReply(text=""), ToolReply(text="")]
        executor = self._executor(_ToolProvider(replies=replies), laya=_ChangeGate())

        await executor.run_chat(self.goal.id)

        self.assertEqual(0, len(self.goals.steps(self.goal.id)), "a plan appeared that the conductor did not make")
        self.assertEqual("FAILED", self.goals.get(self.goal.id).status)

    async def test_a_conductor_that_answers_is_not_second_guessed(self) -> None:
        provider = _SaysNothingThenAnswers(replies=[ToolReply(text="It parses text.")])
        executor = self._executor(provider)

        await executor.run_chat(self.goal.id)

        self.assertEqual(["It parses text."], self._replies())

    async def test_a_plan_made_before_the_silence_stands(self) -> None:
        provider = _SaysNothingThenAnswers(replies=[
            ToolReply(text="", tool_calls=[_call("recon", task="find the parser")]),
            ToolReply(text="", tool_calls=[_call("plan", task="add a test")]),
            ToolReply(text=""),
        ])
        executor = self._executor(provider, laya=_ChangeGate())

        await executor.run_chat(self.goal.id)

        self.assertEqual(1, len(self.goals.steps(self.goal.id)), "the plan was replaced or repeated")
        self.assertEqual("PENDING", self.goals.get(self.goal.id).status)
