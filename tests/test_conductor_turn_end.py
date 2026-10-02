"""A turn the conductor could not finish ends honestly; the engine does not run a second pipeline over it.

When a conductor install's model errored, said nothing, or spent its calls without an answer or a plan, `run_chat`
ran `_plan_goal`: a second gate, a second librarian, a second planner, "Codify's own sequence", on a goal the
conductor had already been given. That was the floor under "the conductor decides", and it is gone, for the same
reason the sweep behind an approved plan is gone (docs/09 §10.14): a model that is bad at this should cost the person
a clear message and a retry, not a different driver quietly taking over, spending twice, and planning something
the conductor never chose.

What it ends in depends on what was asked. A question, a greeting or an unclassified request gets the cheap plain
reply, because answering is safe and a plain reply cannot change a file. A request the gate read as a change gets
no pipeline: its words if it had any, or a failed turn that says nothing was changed and why. An install with no
tool-capable conductor at all is unchanged: there, the recipe is the driver, as it always was.
"""

from __future__ import annotations

from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.chat_prompts import CHAT_SYSTEM_PROMPT
from engine.laya import LayaDecision, LayaService
from engine.toolcall import ToolReply
from tests.test_conductor import ConductorTestCase, _call, _ToolProvider
from tests.test_moves import _ChangeGate, _FailingConductor, _OtherGate

PLAIN_ANSWER = "The repository has one module, app.py."
RECIPE_ROLES = ("you are codify librarian", "you are codify planner", "you are codify design")


class _AnswersPlainly(_ToolProvider):
    """The loop's replies come from the script; the single-shot call (what a plain turn uses) answers."""

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        if system_prompt == CHAT_SYSTEM_PROMPT:
            return PLAIN_ANSWER
        return await super().complete(
            system_prompt, user_prompt, model, temperature, max_tokens,
            num_ctx=num_ctx, keep_alive=keep_alive,
        )


class _FailingConductorThatAnswersPlainly(_FailingConductor):
    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        if system_prompt == CHAT_SYSTEM_PROMPT:
            return PLAIN_ANSWER
        return await super().complete(
            system_prompt, user_prompt, model, temperature, max_tokens,
            num_ctx=num_ctx, keep_alive=keep_alive,
        )


class _SkippedGate(LayaService):
    async def decide(self, state: dict[str, Any], on_call: Any = None) -> LayaDecision:
        return LayaDecision(engine="skipped", skipped_reason="nothing classified this request")


class _NoToolsAtAll(_ToolProvider):
    @property
    def supports_tools(self) -> bool:
        return False


class TurnEndCase(ConductorTestCase):
    def _events(self) -> list[Any]:
        return list(self.goals.events_after(self.goal.id, 0))

    def _replies(self) -> list[str]:
        return [e.payload["message"] for e in self._events() if e.type == "log" and e.payload.get("turn")]

    def _warnings(self) -> list[str]:
        return [e.payload["message"] for e in self._events()
                if e.type == "log" and e.payload.get("level") == "warn"]

    def _errors(self) -> list[dict[str, Any]]:
        return [e.payload for e in self._events() if e.type == "error"]

    def _recipe_ran(self, provider: _ToolProvider) -> bool:
        return any(
            role in system.lower() for system, _ in provider.seen_prompts for role in RECIPE_ROLES
        )


class TestAChangeRequestTheConductorCouldNotFinish(TurnEndCase):
    async def test_a_dead_model_fails_the_turn_plainly_and_runs_no_pipeline(self) -> None:
        provider = _FailingConductor()
        executor = self._executor(provider, laya=_ChangeGate())

        await executor.run_chat(self.goal.id)

        self.assertEqual("FAILED", self.goals.get(self.goal.id).status)
        error = self._errors()[0]
        self.assertEqual("conductor_failed", error["code"])
        self.assertIn("upstream_error", error["message"], "the provider's code says what happened")
        self.assertNotIn("refused the tool call", error["message"], "the provider's own words are not ours")
        self.assertIn("nothing was changed", error["message"].lower())
        self.assertEqual([], self.goals.steps(self.goal.id), "a plan was made by something other than the conductor")
        self.assertFalse(self._recipe_ran(provider), "the recipe ran over a conductor that failed")

    async def test_a_silent_model_fails_the_turn_and_says_it_said_nothing(self) -> None:
        provider = _ToolProvider(replies=[ToolReply(text=""), ToolReply(text="")])
        executor = self._executor(provider, laya=_ChangeGate())

        await executor.run_chat(self.goal.id)

        self.assertEqual("FAILED", self.goals.get(self.goal.id).status)
        error = self._errors()[0]
        self.assertEqual("conductor_failed", error["code"])
        self.assertIn("said nothing", error["message"])
        self.assertEqual([], self.goals.steps(self.goal.id))
        self.assertFalse(self._recipe_ran(provider))

    async def test_a_run_that_used_every_call_says_what_it_has_and_that_nothing_was_changed(self) -> None:
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(text="I read app.py but did not get as far as a plan."),
        ])
        self.settings.set_int("conductor_max_turns", 1)
        executor = self._executor(provider, laya=_ChangeGate())
        executor.settings = self.settings

        await executor.run_chat(self.goal.id)

        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)
        self.assertEqual(1, len(self._replies()))
        self.assertIn("did not get as far as a plan", self._replies()[0], "its words are the turn's reply")
        warning = " ".join(self._warnings())
        self.assertIn("ran out of calls", warning)
        self.assertIn("nothing was changed", warning.lower())
        self.assertEqual([], self.goals.steps(self.goal.id))
        self.assertFalse(self._recipe_ran(provider))

    async def test_a_plan_made_before_the_conductor_stopped_still_stands(self) -> None:
        # Unchanged, and the control for the three above: a conductor that got as far as a plan produced
        # the turn's result, whatever happened to it afterwards.
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("recon", task="look")]),
            ToolReply(tool_calls=[_call("plan", task="change it")]),
            ToolReply(text=""),
        ])
        executor = self._executor(provider, laya=_ChangeGate())

        await executor.run_chat(self.goal.id)

        self.assertEqual("PENDING", self.goals.get(self.goal.id).status)
        self.assertEqual(["S1"], [s.title for s in self.goals.steps(self.goal.id)])


class TestAnythingElseGetsThePlainReply(TurnEndCase):
    async def test_a_question_whose_conductor_died_is_answered_by_the_plain_call(self) -> None:
        executor = self._executor(_FailingConductorThatAnswersPlainly())

        await executor.run_chat(self.goal.id)

        self.assertEqual([PLAIN_ANSWER], self._replies())
        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)
        self.assertEqual([], self.goals.steps(self.goal.id))
        self.assertTrue(any("did not finish" in w for w in self._warnings()), self._warnings())

    async def test_a_greeting_the_gate_could_not_label_is_answered_plainly_not_planned(self) -> None:
        # `other` used to run the whole pipeline when the conductor failed, because planning is a superset of
        # answering. A conductor install no longer has a pipeline to fall back to.
        provider = _FailingConductorThatAnswersPlainly()
        executor = self._executor(provider, laya=_OtherGate())

        await executor.run_chat(self.goal.id)

        self.assertEqual([PLAIN_ANSWER], self._replies())
        self.assertEqual([], self.goals.steps(self.goal.id))
        self.assertFalse(self._recipe_ran(provider))

    async def test_a_request_nothing_classified_is_answered_plainly_not_planned(self) -> None:
        provider = _FailingConductorThatAnswersPlainly()
        executor = self._executor(provider, laya=_SkippedGate())

        await executor.run_chat(self.goal.id)

        self.assertEqual([PLAIN_ANSWER], self._replies())
        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)
        self.assertFalse(self._recipe_ran(provider))


class TestNoConductorAtAllIsUnchanged(TurnEndCase):
    async def test_a_change_request_on_an_install_with_no_tool_capable_model_still_runs_the_recipe(self) -> None:
        provider = _NoToolsAtAll()
        executor = self._executor(provider, laya=_ChangeGate())

        await executor.run_chat(self.goal.id)

        self.assertEqual("PENDING", self.goals.get(self.goal.id).status)
        self.assertEqual(["S1"], [s.title for s in self.goals.steps(self.goal.id)])
        self.assertTrue(self._recipe_ran(provider), "the install with no conductor lost its driver")

    async def test_a_question_on_that_install_is_still_a_plain_answer(self) -> None:
        provider = _NoToolsAtAll()
        executor = self._executor(provider)

        await executor.run_chat(self.goal.id)

        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)
        self.assertEqual([], self.goals.steps(self.goal.id))
