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

from engine.conductor import BASE_TOOLS, PAGE_ACTION_NAMES, STEP_TOOLS, TOOLS, Conductor
from engine.laya import LayaDecision, LayaService
from engine.models import TurnCreate
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


class _OtherGate(LayaService):
    """A gate that cannot tell what the request is: greetings, thanks, anything unlabelled."""

    async def decide(self, state: dict[str, Any], on_call: Any = None) -> LayaDecision:
        return LayaDecision(
            engine="sdk",
            answers={"intent": {"choice": "other", "confidence": 0.9}},
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
        *, num_ctx: int | None = None, keep_alive: str | None = None,
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
        # `read_page` is here for the same reason as the rest: it reads, it
        # cannot write, and it is not one of the moves a step unlocks.
        # `recall` too, and the case is the sharpest of the set — it is the
        # only tool that reads this machine's own record of previous runs, so
        # removing it would leave a conductor that can see the current code and
        # nothing of what it has already learned about it. `recall_threads` is
        # the same claim one grain up: without it a new conversation can learn
        # what happened inside earlier runs but not that the conversations
        # themselves happened.
        base = {t.name for t in BASE_TOOLS}
        self.assertLessEqual(
            {
                "read_file", "search_code", "git_history", "run_command",
                "read_page", "navigate_page", "recall", "recall_threads",
                "use_skill",
            },
            base,
        )
        self.assertEqual(
            {t.name for t in STEP_TOOLS}, {"write", "verify", "review", "summarize", "todo"},
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
        # The base menu, and the question a turn may put to the person (`ask_user`, last: it is offered
        # whenever somebody is there to answer, which is on a turn and never during an approved run). Without
        # `fetch_page`: it is offered only once a person has allowed it in Settings (docs/12), and a fresh
        # install has not, so a tool that could only say "turned off" is not a slot the model is shown. Without
        # `navigate_page`, `click_page` and `type_page` either, for the same reason: acting on the person's
        # browser tab is off until they allow it (`page_actions`, docs/03 §1.6).
        self.assertEqual(
            provider.seen_tools[0],
            [
                *(t.name for t in BASE_TOOLS if t.name != "fetch_page" and t.name not in PAGE_ACTION_NAMES),
                "ask_user",
            ],
        )
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


class TestTheConductorCanDirectWhatItSummons(ConductorTestCase):
    """What the conductor writes on a move has to reach the sub-agent.

    `recon` and `design` both *require* a `task`, and both handlers logged it
    as delivered — "conductor sent the librarian: find the parser" — and then
    called `_librarian(goal_id, goal, ws)`, which had no parameter to receive
    it. The tool schema told the model it was directing a sub-agent, the log
    agreed, and the prompt was built from the goal alone. Three surfaces
    promised a control the engine did not have, which is a worse shape than
    having none: a model that reads a log and a schema has no reason to doubt
    the direction arrived.

    So these assert the prompt itself, not that the call happened. `write` and
    `plan` already carried their text through and are the convention being
    copied here.
    """

    ASK = "find where the config file is parsed"
    DIRECTION = "keep the existing public function names"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # A goal whose text is a substring of half this file proves nothing.
        # "hi" is three characters and the harness's own default.
        self.asked = self.goals.create_turn(
            self.thread.id,
            TurnCreate(prompt="Widen the config parser to accept nested keys"),
        )

    def _prompt_for(self, provider: _ToolProvider, role: str) -> str:
        for system, user in provider.seen_prompts:
            if f"you are codify {role}" in system.lower():
                return user
        self.fail(f"the {role} role was never called, so there is no prompt to read")

    async def test_the_librarian_reads_what_the_conductor_asked_it_to_find(self) -> None:
        provider = _ToolProvider([
            ToolReply(text="", tool_calls=[_call("recon", task=self.ASK)]),
            ToolReply(text="I found the parser and stopped there."),
        ])
        executor = self._executor(provider, laya=_ChangeGate())
        await executor.run_chat(self.asked.id)
        self.assertIn(self.ASK, self._prompt_for(provider, "librarian"))

    async def test_the_designer_reads_what_the_conductor_asked_it_to_decide(self) -> None:
        provider = _ToolProvider([
            ToolReply(text="", tool_calls=[_call("recon", task="look around")]),
            ToolReply(text="", tool_calls=[_call("design", task=self.DIRECTION)]),
            ToolReply(text="I would keep the public names and add a nested read."),
        ])
        executor = self._executor(provider, laya=_ChangeGate())
        await executor.run_chat(self.asked.id)
        self.assertIn(self.DIRECTION, self._prompt_for(provider, "design"))

    async def test_the_goal_is_still_what_the_user_asked_for(self) -> None:
        # Beside the goal, never instead of it. A conductor that could rewrite
        # the goal could send a sub-agent after something the user never asked
        # for, and the user's own words would be gone from the prompt.
        provider = _ToolProvider([
            ToolReply(text="", tool_calls=[_call("recon", task=self.ASK)]),
            ToolReply(text="I found the parser."),
        ])
        executor = self._executor(provider, laya=_ChangeGate())
        await executor.run_chat(self.asked.id)
        prompt = self._prompt_for(provider, "librarian")
        self.assertIn("Widen the config parser to accept nested keys", prompt)
        # Labelled, so the sub-agent can tell whose ask it is reading.
        self.assertIn("in addition to the goal above", prompt)

    async def test_the_designer_still_hears_the_workspace_contract_last(self) -> None:
        # The ask narrows what is decided; the workspace's own DESIGN.md still
        # outranks it. Order is the whole mechanism, so it is asserted.
        (self.repo / "DESIGN.md").write_text("# Brand\nOne font. No purple.\n")
        provider = _ToolProvider([
            ToolReply(text="", tool_calls=[_call("recon", task="look around")]),
            ToolReply(text="", tool_calls=[_call("design", task=self.DIRECTION)]),
            ToolReply(text="The workspace brand already answers that."),
        ])
        executor = self._executor(provider, laya=_ChangeGate())
        await executor.run_chat(self.asked.id)
        prompt = self._prompt_for(provider, "design")
        self.assertIn(self.DIRECTION, prompt)
        self.assertIn("BINDING", prompt)
        self.assertLess(
            prompt.index(self.DIRECTION), prompt.index("BINDING"),
            "the conductor's ask must not come after the binding contract",
        )

    async def test_an_engine_run_asks_for_nothing(self) -> None:
        # The engine's own path has no conductor and no ask, and the default
        # has to be a prompt byte-identical to the one before this change —
        # otherwise a goal that never involved a conductor would have started
        # carrying a line about one.
        provider = _ToolProvider()
        executor = self._executor(provider)
        await executor._librarian(self.asked.id, self.goals.get(self.asked.id), self.ws)
        prompt = self._prompt_for(provider, "librarian")
        self.assertNotIn("The conductor asked", prompt)
        self.assertIn("Widen the config parser", prompt)


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

    async def test_a_conductor_that_fails_on_a_change_does_not_run_the_recipe_over_it(self) -> None:
        # No answer and no plan on a request the gate read as a change. There used to be a floor here: the
        # engine ran the sequence it would have run before the conductor existed. That second pipeline is
        # gone on an install that has a conductor (docs/09 §10.14). The turn fails plainly, says nothing
        # was changed, and plans nothing. `tests/test_conductor_turn_end.py` holds the whole contract.
        executor = self._executor(_FailingConductor(), laya=_ChangeGate())
        await executor.run_chat(self.goal.id)

        self.assertEqual(self.goals.steps(self.goal.id), [], "a plan appeared that the conductor did not make")
        self.assertEqual(self.goals.get(self.goal.id).status, "FAILED")

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


class TestTheGateLabelIsAdviceNotAnOrder(ConductorTestCase):
    """What the gate's verdict may and may not make the conductor do.

    Found live: the SDK gate labelled "hi" `code_change` (it was handed
    `mode: direct-apply`, an execution setting that primes it toward "apply"),
    and the engine treated every label but `question` as "the user wants the
    workspace changed": the conductor was told to load `ship-a-change`, to
    prefer acting over asking, and was armed with a reminder forbidding it to
    ask anything. A 7B obeyed the more specific text over its own system prompt
    ("answer directly whenever the workspace does not need to change") and spent
    78 s sending a librarian to analyse the repository for a greeting.
    """

    _BRIEF_CHANGE_ORDERS = ("Prefer acting over asking", "You have not done anything yet")

    def _sent(self, provider: _ToolProvider) -> str:
        return "\n".join(
            str(m.get("content") or "") for batch in provider.seen_messages for m in batch
        )

    async def test_a_greeting_labelled_other_is_answered_and_not_briefed_as_a_change(self) -> None:
        provider = _ToolProvider([ToolReply(text="Hi! How can I help you today?")])
        executor = self._executor(provider, laya=_OtherGate())
        await executor.run_chat(self.goal.id)

        sent = self._sent(provider)
        for order in self._BRIEF_CHANGE_ORDERS:
            self.assertNotIn(order, sent, f"an unlabelled request was ordered: {order!r}")
        self.assertNotIn("use_skill` and follow it", sent)
        self.assertEqual(len(provider.seen_messages), 1, "an answer needed exactly one model call")
        self.assertEqual(self.goals.steps(self.goal.id), [])
        self.assertEqual(self.goals.get(self.goal.id).status, "COMPLETED")

    async def test_a_plain_answer_to_an_unlabelled_request_is_not_warned_about(self) -> None:
        # The warning says "the gate read this as X and the conductor finished
        # without planning anything: no file was changed". For `other` that is
        # noise that reads as a failure on a turn that did exactly what was asked.
        provider = _ToolProvider([ToolReply(text="Hi! How can I help you today?")])
        executor = self._executor(provider, laya=_OtherGate())
        await executor.run_chat(self.goal.id)
        warnings = [
            str((e.payload or {}).get("message") or "")
            for e in self.goals.events_after(self.goal.id, 0)
            if e.type == "log" and (e.payload or {}).get("level") == "warn"
        ]
        self.assertEqual([w for w in warnings if "the gate read this as" in w], [])

    async def test_a_change_label_still_points_at_the_recipe_but_says_it_is_a_guess(self) -> None:
        provider = _ToolProvider([ToolReply(text="No change is needed."), ToolReply(text="Still none.")])
        executor = self._executor(provider, laya=_ChangeGate())
        await executor.run_chat(self.goal.id)
        sent = self._sent(provider)
        self.assertIn("ship-a-change", sent)
        self.assertIn("guess", sent, "the label was presented as fact, not as a small classifier's guess")
        self.assertIn("greeting", sent, "the brief never says a plain message may just be answered")
        self.assertIn("You have not done anything yet", sent, "a real change request keeps its reminder")

    async def test_ops_command_is_briefed_like_a_change(self) -> None:
        brief = self._executor(_ToolProvider(), laya=_OtherGate())._intent_brief("ops_command")
        self.assertIn("ship-a-change", brief)

    async def test_an_unlabelled_or_unknown_intent_gets_no_change_orders(self) -> None:
        executor = self._executor(_ToolProvider(), laya=_OtherGate())
        for intent in ("other", "", "something-new"):
            brief = executor._intent_brief(intent)
            for order in self._BRIEF_CHANGE_ORDERS:
                self.assertNotIn(order, brief, f"intent {intent!r}")
            self.assertIsNone(executor._intent_nudge(intent), f"intent {intent!r} armed a reminder")
        self.assertIsNone(executor._intent_nudge("question"))
        self.assertIsNotNone(executor._intent_nudge("code_change"))
        self.assertIsNotNone(executor._intent_nudge("ops_command"))

    async def test_the_prompt_tells_the_conductor_to_do_the_work_itself_and_only_names_real_tools(self) -> None:
        # The conductor is the main agent: it has read, search, git and
        # allowlisted-command tools of its own, and the sub-agents are for what is
        # broad or what the user asks for. And a prompt that names a tool the
        # menu does not offer sends a 7B looking for it, so every name in
        # backticks must be a real one.
        import re

        from engine.chat_prompts import CONDUCTOR_SYSTEM_PROMPT

        self.assertIn("Do the work yourself", CONDUCTOR_SYSTEM_PROMPT)
        self.assertIn("A greeting", CONDUCTOR_SYSTEM_PROMPT)
        # Tool names, plus the parameter names the prompt legitimately mentions
        # (`task` is what a move is given, not a tool).
        offered = {t.name for t in TOOLS} | {
            param for t in TOOLS for param in (t.parameters.get("properties") or {})
        }
        named = set(re.findall(r"`([a-z_]+)`", CONDUCTOR_SYSTEM_PROMPT))
        self.assertTrue(named, "the scan found no tool names to check")
        self.assertEqual(sorted(named - offered), [], "the prompt names tools that are not on the menu")

    async def test_recon_says_what_it_is_not_for(self) -> None:
        recon = next(t for t in TOOLS if t.name == "recon")
        self.assertIn("NOT for a question you can answer", recon.description)
        self.assertIn("never for a greeting", recon.description)

