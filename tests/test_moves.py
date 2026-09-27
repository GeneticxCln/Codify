"""The moves, and the conductor being the thing that decides.

`tests/test_conductor.py` covers the loop and the translations. This file covers
what changed when the conductor stopped being a dispatcher with one
whole-pipeline button and became the component that chooses the sequence:

- every move on the menu is actually wired, and nothing is wired that is not
  offered,
- the menu reflects the state it is in right now rather than being a fixed list,
- a turn can produce a plan without the engine's sequence running at all,
- a conductor that *declines* is obeyed, and a conductor that *fails* is caught.

That last pair is the distinction the whole change turns on, so each has its own
test rather than one test asserting both.
"""

from __future__ import annotations

import unittest
from typing import Any

from engine.conductor import BASE_TOOLS, STEP_TOOLS, TOOLS, Conductor
from engine.laya import LayaDecision, LayaService
from engine.providers import ProviderError
from engine.toolcall import ToolReply
from tests.test_conductor import ConductorTestCase, _call, _PlainProvider, _ToolProvider


class _ChangeGate(LayaService):
    """A gate that classifies the request as a change rather than a question."""

    async def decide(self, state: dict[str, Any], on_call: Any = None) -> LayaDecision:
        return LayaDecision(
            engine="sdk",
            answers={"intent": {"choice": "code_change", "confidence": 0.99}},
        )


class _FailingConductor(_ToolProvider):
    """The model endpoint refuses the *tool* call, and ordinary calls still work.

    That is the shape of a real failure: the provider is configured and answers
    role calls, but `complete_with_tools` — the one call that has to carry the
    move menu — errors out. It is the case the fallback exists for.
    """

    async def complete_with_tools(
        self, system_prompt: str, messages: list[dict[str, Any]],
        tools: list[Any], model: str, temperature: float, max_tokens: int,
    ) -> ToolReply:
        raise ProviderError("upstream_error", "the endpoint refused the tool call")


class TestTheMenuIsTheDispatchTable(ConductorTestCase):
    async def test_every_offered_move_is_wired(self) -> None:
        # A move on the menu with no handler is a model calling a tool that
        # cannot work — and the refusal it gets back lists the move as
        # available. The two sets have to be the same set.
        table = self._dispatch(self.goal.id)
        self.assertEqual(sorted(table), sorted(t.name for t in TOOLS))

    def test_the_read_tools_are_in_every_configuration(self) -> None:
        # A conductor with its move budget spent can still answer from what it
        # reads. Cutting these too would leave it mute rather than merely idle.
        base = {t.name for t in BASE_TOOLS}
        self.assertLessEqual(
            {"read_file", "search_code", "git_history", "run_command", "use_skill"},
            base,
        )
        self.assertEqual(
            {t.name for t in STEP_TOOLS}, {"write", "verify", "review", "summarize"},
        )


class TestTheMenuFollowsTheState(ConductorTestCase):
    async def test_step_moves_appear_only_once_a_step_exists(self) -> None:
        executor = self._executor(_ToolProvider())
        before = [t.name for t in executor.conductor_menu(self.goal.id)()]
        self.assertNotIn("write", before, "a move that cannot work was offered")
        self.assertIn("plan", before)

        table = self._dispatch(self.goal.id)
        await table["recon"]({"task": "find the parser"})
        await table["plan"]({"task": "change the parser"})

        after = [t.name for t in executor.conductor_menu(self.goal.id)()]
        self.assertIn("write", after)
        self.assertIn("summarize", after)

    async def test_the_stage_moves_are_dropped_when_the_move_budget_is_spent(self) -> None:
        # Dropped from the menu rather than refused at call time: a model that
        # is still offered `write` keeps asking for it, and a refusal it can
        # retry costs a turn every time.
        conductor = Conductor(
            provider=None, model="m", workspace_root=str(self.repo),
            dispatch={}, system_prompt="s",
            menu=lambda: list(TOOLS), max_moves=0,
        )
        conductor._refresh_menu()
        offered = [t.name for t in conductor.tools]
        for gone in ("write", "verify", "review", "summarize", "recon", "plan"):
            self.assertNotIn(gone, offered, f"{gone} is still offered with no budget")
        self.assertIn("read_file", offered)


class TestATurnCanPlanThroughTheConductor(ConductorTestCase):
    async def test_a_change_request_plans_without_the_engine_sequence(self) -> None:
        # The headline of the change: a turn that needs a plan gets one chosen
        # by the conductor, through moves it called, with no `delegate` and no
        # hardcoded order anywhere on the path.
        provider = _ToolProvider([
            ToolReply(text="", tool_calls=[_call("recon", task="find the parser")]),
            ToolReply(text="", tool_calls=[_call("plan", task="rename parse")]),
            ToolReply(text="I have planned it. Approve the plan and I will make it."),
        ])
        executor = self._executor(provider, laya=_ChangeGate())
        await executor.run_chat(self.goal.id)

        steps = self.goals.steps(self.goal.id)
        self.assertEqual([s.title for s in steps], ["S1"])
        # PENDING, not COMPLETED. The plan is what the user is being asked to
        # approve, and marking the turn finished would clear the very state the
        # approval gate reads.
        self.assertEqual(self.goals.get(self.goal.id).status, "PENDING")
        self.assertEqual(provider.seen_tools[0], [t.name for t in BASE_TOOLS])
        # And the run was measured as the pipeline's own stages, not as some
        # new kind of thing the stats screen would have to learn about.
        stages = {
            (e.payload or {}).get("stage")
            for e in self.goals.events_after(self.goal.id, 0)
            if e.type == "stage_result"
        }
        self.assertLessEqual({"librarian", "planner"}, stages)

    async def test_it_never_offers_a_step_move_before_a_step_exists(self) -> None:
        # The menu as the model was *shown* it, recorded by the double.
        provider = _ToolProvider([
            ToolReply(text="", tool_calls=[_call("recon", task="look")]),
            ToolReply(text="Nothing needs changing."),
        ])
        executor = self._executor(provider, laya=_ChangeGate())
        await executor.run_chat(self.goal.id)
        self.assertTrue(provider.seen_tools)
        for offered in provider.seen_tools:
            self.assertNotIn("write", offered)


class TestDecliningIsObeyedAndFailingIsCaught(ConductorTestCase):
    async def test_a_conductor_that_declines_to_plan_is_honoured(self) -> None:
        # It answered instead of planning, having judged that no change was
        # needed. Overriding that with the recipe would make the brain a
        # suggestion and would hand the user a plan they did not ask for.
        #
        # Two replies because a change request now arrives with one reminder
        # armed. Declining *through* the reminder is what makes this a decision
        # rather than an oversight, and it is the case that has to be obeyed.
        provider = _ToolProvider([
            ToolReply(
                text="That change would break the public API, so I have not planned it."
            ),
            ToolReply(
                text="Still no: the public API would break. There is nothing to plan."
            ),
        ])
        executor = self._executor(provider, laya=_ChangeGate())
        await executor.run_chat(self.goal.id)

        self.assertEqual(self.goals.steps(self.goal.id), [])
        self.assertEqual(self.goals.get(self.goal.id).status, "COMPLETED")
        answers = [
            str((e.payload or {}).get("message") or "")
            for e in self.goals.events_after(self.goal.id, 0)
            if e.type == "log" and (e.payload or {}).get("turn")
        ]
        # The final answer is the second reply: the first was streamed as a
        # delta and then superseded, which is what the transcript is meant to
        # show. Both name the reason, and the reason is what has to survive.
        self.assertTrue(
            any("public API" in a for a in answers),
            f"the conductor's answer never reached the transcript: {answers}",
        )

    async def test_the_reminder_is_given_once_and_not_into_a_loop(self) -> None:
        # Bounded by construction: a model that keeps declining must not be
        # asked again and again. Two replies are supplied and a third would be
        # "out of things to say", so a second reminder would show up as that
        # text being the answer.
        provider = _ToolProvider([
            ToolReply(text="No change needed, here is why."),
            ToolReply(text="Correct, still nothing to do."),
        ])
        executor = self._executor(provider, laya=_ChangeGate())
        await executor.run_chat(self.goal.id)
        reminders = sum(
            str(m.get("content") or "").count("You have not done anything yet")
            for batch in provider.seen_messages
            for m in batch
        )
        self.assertEqual(reminders, 1, "the reminder was given more than once")
        answers = [
            str((e.payload or {}).get("message") or "")
            for e in self.goals.events_after(self.goal.id, 0)
            if e.type == "log" and (e.payload or {}).get("turn")
        ]
        self.assertTrue(
            any("nothing to do" in a for a in answers),
            f"the model's second answer never reached the transcript: {answers}",
        )

    async def test_a_model_that_only_narrates_the_plan_gets_one_reminder(self) -> None:
        # The exact live failure this was built for. Against a real 7B, the
        # conductor called `use_skill`, said "Let's start with the `recon` step",
        # and stopped — announcing a move instead of making it, leaving the user
        # with a description of work nobody had done. One reminder is enough,
        # and this pins that it is given.
        provider = _ToolProvider([
            ToolReply(text="Understood. Let's start with the recon step."),
            ToolReply(text="", tool_calls=[_call("recon", task="read greeter.py")]),
            ToolReply(text="", tool_calls=[_call("plan", task="add a docstring")]),
            ToolReply(text="Planned. Approve it and I will make the change."),
        ])
        executor = self._executor(provider, laya=_ChangeGate())
        await executor.run_chat(self.goal.id)
        self.assertEqual(
            [s.title for s in self.goals.steps(self.goal.id)], ["S1"],
            "the reminder did not turn narration into a plan",
        )
        self.assertEqual(self.goals.get(self.goal.id).status, "PENDING")

    async def test_a_conductor_that_fails_falls_back_to_the_recipe(self) -> None:
        # No answer and no plan. The engine runs the sequence it would have run
        # before the conductor existed, and says that it did. This is the floor
        # that makes "the conductor decides" safe to default on.
        executor = self._executor(_FailingConductor(), laya=_ChangeGate())
        await executor.run_chat(self.goal.id)

        self.assertEqual(
            [s.title for s in self.goals.steps(self.goal.id)], ["S1"],
            "the fallback did not produce the pipeline's plan",
        )
        self.assertEqual(self.goals.get(self.goal.id).status, "PENDING")
        text = " ".join(
            str((e.payload or {}).get("message") or "")
            for e in self.goals.events_after(self.goal.id, 0)
            if e.type == "log"
        )
        self.assertIn("did not finish", text)
        self.assertIn("standard sequence", text)

    async def test_a_question_still_degrades_to_a_plain_answer(self) -> None:
        # The conductor is an upgrade and never a prerequisite (docs/09 §10.9).
        # An install where the tool loop cannot run must still answer "what does
        # this do?" without running a pipeline.
        executor = self._executor(_FailingConductor())
        await executor.run_chat(self.goal.id)
        self.assertEqual(self.goals.get(self.goal.id).status, "COMPLETED")
        self.assertEqual(self.goals.steps(self.goal.id), [])


class TestWhoDrivesAnApprovedPlan(ConductorTestCase):
    async def test_the_conductor_drives_when_there_is_one(self) -> None:
        executor = self._executor(_ToolProvider())
        self.assertTrue(executor.conductor_can_drive(self.goal.id))

    async def test_a_provider_without_tools_leaves_the_recipe_in_charge(self) -> None:
        executor = self._executor(_PlainProvider())
        self.assertFalse(executor.conductor_can_drive(self.goal.id))

    async def test_the_switch_turns_it_off_without_a_rebuild(self) -> None:
        # The escape hatch for a model that is not yet good at this. It has to
        # exist, or the only way back is a code change.
        executor = self._executor(_ToolProvider())
        executor.settings = self.settings
        self.assertTrue(executor.conductor_can_drive(self.goal.id))
        self.settings.set_int("conductor_drives_execution", 0)
        self.assertFalse(executor.conductor_can_drive(self.goal.id))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
