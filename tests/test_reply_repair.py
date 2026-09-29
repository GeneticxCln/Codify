"""A reply that is not JSON is asked for again — once — before anything else happens (audit of
2026-09-29, H4).

`run_agent` moved straight from "the reply would not parse" to the fallback target, or, with none
configured, to a failed goal. There was no same-model re-ask ("that was not valid JSON: <why>; reply
again with the JSON only"), which is the standard remedy and costs one short call against a step's
worth of work. So a small model's single formatting slip ended a run that a second try would have saved.

The rules pinned here: one repair attempt, on the *same* target, and only for a reply that could not be
parsed; the first bad reply is recorded as a failed call (it is one) so the cost of the slip is visible;
a valid reply is never re-asked; prose (`raw_output`) is never re-asked; and after the repair fails the
existing fallback behaviour is exactly what it was.
"""

from __future__ import annotations

import json
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import tests.test_provider_fallback as base

from engine.providers import ProviderError

GOOD_PLAN = json.dumps({"steps": [{"title": "S1", "description": "d", "suggested_paths": []}]})
BAD_REPLY = "Sure! I would plan it like this, but here is no JSON at all."


class SequenceProvider(base.TargetProvider):
    """Replies from a script, one per *planner* call; the last entry repeats once it runs out.

    Planning also asks the librarian and the design role, through the same provider, and those get
    the neutral answer a role with nothing to say gives: the script belongs to the role under test,
    and so do the call counts (`planner_calls`, `planner_prompts`).
    """

    def __init__(self, name: str, replies: list[str | ProviderError]) -> None:
        super().__init__(name)
        self.script = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    @property
    def planner_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["role"] == "planner"]

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        self.calls.append({"role": self.role, "provider": self.name, "model": model,
                           "temperature": temperature, "max_tokens": max_tokens})
        if self.role != "planner":
            return json.dumps({})
        self.prompts.append(user_prompt)
        self.systems.append(system_prompt)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, ProviderError):
            raise ProviderError(step.code, step.message)
        return step


class ReAskCase(base.FallbackTestCase):
    def script_primary(self, *replies: str | ProviderError) -> SequenceProvider:
        provider = SequenceProvider("anthropic", list(replies))
        self.factory.providers["anthropic"] = provider
        self.primary = provider
        return provider

    def failed_calls(self, goal_id: str) -> list[dict[str, Any]]:
        return self._events(goal_id, "agent_call_failed")


class TestTheReAsk(ReAskCase):
    async def test_a_bad_reply_is_asked_for_again_and_the_second_one_is_used(self) -> None:
        primary = self.script_primary(BAD_REPLY, GOOD_PLAN)
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(self._errors(goal.id), [], "a recoverable slip failed the goal")
        self.assertEqual(2, len(primary.planner_calls), "the same target should have been asked exactly twice")
        self.assertEqual([], self._events(goal.id, "provider_fallback"), "the repair should not have needed a fallback")
        self.assertEqual(1, len(self.goals.steps(goal.id)))

    async def test_the_repair_prompt_says_what_was_wrong_and_shows_what_was_said(self) -> None:
        primary = self.script_primary(BAD_REPLY, GOOD_PLAN)
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        first, second = primary.prompts
        self.assertTrue(second.startswith(first), "the repair must carry the original task unchanged")
        self.assertIn("could not be used", second)
        self.assertIn("no JSON", second, "the parser's reason for refusing was not passed on")
        self.assertIn(BAD_REPLY, second, "the model was not shown its own previous reply")
        self.assertIn("JSON", second.split("could not be used", 1)[1])
        self.assertEqual(primary.systems[0], primary.systems[1], "the role's own instructions must not change")

    async def test_the_first_bad_reply_is_recorded_as_a_failed_call_that_was_retried(self) -> None:
        self.script_primary(BAD_REPLY, GOOD_PLAN)
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        failed = self.failed_calls(goal.id)
        self.assertEqual(1, len(failed))
        self.assertEqual("agent_output_invalid", failed[0]["code"])
        self.assertTrue(failed[0]["retrying"], "the record does not say a repair followed")
        self.assertEqual("planner", failed[0]["role"])

    async def test_a_valid_reply_is_never_asked_for_twice(self) -> None:
        primary = self.script_primary(GOOD_PLAN)
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(1, len(primary.planner_calls))
        self.assertEqual([], self.failed_calls(goal.id))

    async def test_a_reasoning_models_reply_needs_no_second_call_at_all(self) -> None:
        # The parse itself now copes with a think block, so the re-ask is not spent on it.
        primary = self.script_primary('<think>plan it as {"steps": [...]}, briefly</think>\n' + GOOD_PLAN)
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(1, len(primary.planner_calls))
        self.assertEqual(self._errors(goal.id), [])


class TestWhenTheRepairFailsToo(ReAskCase):
    async def test_the_fallback_then_runs_exactly_as_it_did_before(self) -> None:
        primary = self.script_primary(BAD_REPLY)  # bad every time
        self._give_planner_a_fallback()
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(2, len(primary.planner_calls), "the primary is asked at most twice, never more")
        self.assertEqual(1, len([c for c in self.second.calls if c['role'] == 'planner']))
        self.assertEqual(self._errors(goal.id), [])
        self.assertEqual("agent_output_invalid", self._events(goal.id, "provider_fallback")[0]["code"])

    async def test_with_no_fallback_the_goal_fails_and_the_error_says_a_repair_was_tried(self) -> None:
        primary = self.script_primary(BAD_REPLY)
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(2, len(primary.planner_calls))
        errors = self._errors(goal.id)
        self.assertEqual(1, len(errors))
        self.assertEqual("agent_output_invalid", errors[0]["code"])
        self.assertIn("after one repair attempt", errors[0]["message"])
        self.assertEqual("FAILED", self.goals.get(goal.id).status)

    async def test_a_provider_error_on_the_repair_call_is_a_provider_failure(self) -> None:
        # The second call can fail for a reason that has nothing to do with JSON. That is the
        # provider's failure, reported under the provider's code, and the fallback rule for it
        # is the ordinary one.
        primary = self.script_primary(BAD_REPLY, ProviderError("provider_unreachable", "connection refused"))
        self._give_planner_a_fallback()
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(2, len(primary.planner_calls))
        self.assertEqual(1, len([c for c in self.second.calls if c['role'] == 'planner']), "an unreachable endpoint should still reach the fallback")
        self.assertEqual("provider_unreachable", self._events(goal.id, "provider_fallback")[0]["code"])


class TestProseIsNeverReAsked(ReAskCase):
    async def test_a_raw_output_call_makes_exactly_one_call(self) -> None:
        primary = self.script_primary("Hello! That is a perfectly good answer.")
        goal = self._plan()

        said = await self.executor.orchestrator.run_agent(
            "planner", goal.id, None, "say hello", raw_output=True,
        )

        self.assertEqual("Hello! That is a perfectly good answer.", said)
        self.assertEqual(1, len(primary.planner_calls))


class TestTheRepairIsBounded(ReAskCase):
    async def test_no_target_is_ever_asked_more_than_twice_for_one_call(self) -> None:
        primary = self.script_primary(BAD_REPLY)
        self._give_planner_a_fallback()
        self.second.reply = BAD_REPLY
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(2, len(primary.planner_calls))
        self.assertEqual(2, len([c for c in self.second.calls if c['role'] == 'planner']))
        self.assertEqual("FAILED", self.goals.get(goal.id).status)


class TestAReplyOfTheWrongShape(ReAskCase):
    """A reply that is valid JSON but not the document the role's contract asks for.

    Found by the first real-model benchmark run: a fixer reply whose `files` held lists, not objects, raised
    `AttributeError` out of the parser and took the whole run down — to a user it would have read
    `internal_error`, which blames Codify for a model's slip. Every role's contract is one JSON *object*, so
    anything else is the same kind of failure as a reply that does not parse, and gets the same one re-ask.
    """

    async def test_a_bare_list_is_asked_for_again_as_an_object(self) -> None:
        primary = self.script_primary(json.dumps([{"title": "S1"}]), GOOD_PLAN)
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(2, len(primary.planner_calls))
        self.assertEqual(self._errors(goal.id), [], "a recoverable slip failed the goal")
        self.assertIn("JSON object", primary.prompts[1])

    async def test_steps_that_are_not_objects_are_asked_for_again_not_a_crash(self) -> None:
        primary = self.script_primary(json.dumps({"steps": ["do the thing", "then the other"]}), GOOD_PLAN)
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(2, len(primary.planner_calls))
        self.assertEqual(self._errors(goal.id), [])
        self.assertEqual(1, len(self.goals.steps(goal.id)))

    async def test_a_plan_with_no_steps_gets_the_engines_reason_back(self) -> None:
        primary = self.script_primary(json.dumps({"steps": []}), GOOD_PLAN)
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(2, len(primary.planner_calls))
        self.assertIn("1..20 steps", primary.prompts[1], "the contract's own reason was not passed on")
        self.assertEqual(self._errors(goal.id), [])

    async def test_a_wrong_shape_twice_fails_as_invalid_output_never_as_an_internal_error(self) -> None:
        primary = self.script_primary(json.dumps({"steps": ["just words"]}))
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(2, len(primary.planner_calls))
        errors = self._errors(goal.id)
        self.assertEqual(1, len(errors))
        self.assertEqual("agent_output_invalid", errors[0]["code"])
        self.assertIn("after one repair attempt", errors[0]["message"])
        self.assertEqual("FAILED", self.goals.get(goal.id).status)

    async def test_a_consult_is_still_not_mistaken_for_a_bad_plan(self) -> None:
        # The planner's other legitimate reply: ask the librarian something. It has no steps and must not be
        # refused for that.
        (self.root / "README.md").write_text("# hello\n", encoding="utf-8")
        consult = json.dumps({"steps": [], "consult": {"reads": ["README.md"]}})
        primary = self.script_primary(consult, GOOD_PLAN)
        goal = self._plan()

        await self.executor.run_planning(goal.id)

        self.assertEqual(2, len(primary.planner_calls), "a consult and then the plan: two calls, no repair")
        self.assertEqual(self._events(goal.id, "plan_consult")[0]["refused"], 0)
        self.assertEqual([], self.failed_calls(goal.id))
        self.assertEqual(self._errors(goal.id), [])


if __name__ == "__main__":
    import unittest

    unittest.main()
