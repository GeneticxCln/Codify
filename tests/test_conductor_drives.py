"""After Start, the conductor finishes each step, or the goal pauses and says why.

`_run_steps_locked` ran the conductor once over the whole plan and then walked every step that was not
COMPLETED through the engine's own sequence, so "the conductor drives execution" was a switch that could
double-write a step: the conductor wrote it, ran out of calls before `summarize`, and the sweep ran the fixer
again from scratch. The conductor was in charge only until it was inconvenient.

Now the driver gives the conductor one open step at a time, each with its own budget, and judges by the step's
stored status and nothing the conductor said. A step the conductor did not finish leaves the goal PAUSED with a
reason from `models.PAUSE_CODES`; only the person's Start resumes it. The recipe remains for installs that have
no tool-capable model, for `parallel` goals, and for `POST /goals` planning.

These run the real driver (`_run_steps_locked`), the real executor and a real git repository, against a scripted
tool-calling model, and assert on what the fixer was asked, what git holds and what status the goal ended in.
"""

from __future__ import annotations

import types
from typing import Any
from unittest import mock

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import _run_steps_locked
from engine.conductor_tools import ConductorTools
from engine.executor import ExecutorService
from engine.models import ROLES, AgentConfigUpdate
from engine.providers import Keychain, ProviderError
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, SettingsService
from engine.toolcall import ToolCall, ToolReply, ToolSpec
from tests.test_conductor_moves_state import A_V2, B_V1, MovesCase, _git
from tests.test_failure_visibility import _VerifierScript
from tests.test_fixer_reask import Factory, Gate


def _call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"c_{name}_{len(arguments)}", name=name, arguments=arguments)


def _done(step_id: str) -> list[ToolReply]:
    """What a conductor does for one step that goes right, then the sentence it ends on."""
    return [
        ToolReply(tool_calls=[_call("write", step_id=step_id, instructions="make the change")]),
        ToolReply(tool_calls=[_call("verify", step_id=step_id)]),
        ToolReply(tool_calls=[_call("review", step_id=step_id)]),
        ToolReply(tool_calls=[_call("summarize", step_id=step_id)]),
        ToolReply(text="That step is done."),
    ]


class _Driving(_VerifierScript):
    """A provider that is both the eight roles' scripted model and a tool-calling conductor.

    `tool_replies` is consumed one entry per `complete_with_tools`; an exception in the list is raised, which is
    how a provider that dies mid-run is scripted. The run ends with a plain sentence when the script does.
    """

    def __init__(self, fixer_replies: list[Any]) -> None:
        super().__init__(fixer_replies)
        self.tool_replies: list[Any] = []
        self.seen_messages: list[list[dict[str, Any]]] = []
        self.on_call: Any = None

    @property
    def supports_tools(self) -> bool:
        return True

    async def complete_with_tools(
        self, system_prompt: str, messages: list[dict[str, Any]], tools: list[ToolSpec], model: str,
        temperature: float, max_tokens: int, *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> ToolReply:
        self.seen_messages.append([dict(m) for m in messages])
        if self.on_call is not None:
            self.on_call()
        if not self.tool_replies:
            return ToolReply(text="(the script ended)")
        reply = self.tool_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, ToolReply)
        return reply


class DriveCase(MovesCase):
    """Two steps over a.py and b.py, in a real repository, approved and ready for the driver."""

    def build(self, *fixer_replies: Any) -> _Driving:
        provider = _Driving(list(fixer_replies))
        provider.verifier_replies = []
        self.provider = provider
        registry = AgentRegistryService(self.conn, Factory(provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="test-model"))
        self.executor = ExecutorService(self.goals, self.workspaces, registry, SandboxService(), laya=Gate())
        self.settings = SettingsService(self.conn)
        self.executor.settings = self.settings
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(executor=self.executor, goals=self.goals))
        return provider

    def two_steps(self) -> tuple[str, str]:
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        self.executor._insert_steps(self.goal.id, [
            {"title": "Change a", "description": "change a.py", "suggested_paths": ["a.py"]},
            {"title": "Change b", "description": "change b.py", "suggested_paths": ["b.py"]},
        ])
        first, second = self.goals.steps(self.goal.id)
        return first.id, second.id

    def one_step(self) -> str:
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        self.executor._insert_steps(self.goal.id, [
            {"title": "Change a", "description": "change a.py", "suggested_paths": ["a.py"]},
        ])
        return self.goals.steps(self.goal.id)[0].id

    async def drive(self) -> None:
        await _run_steps_locked(self.app, self.goal.id)  # type: ignore[arg-type]

    def status(self) -> str:
        return self.goals.get(self.goal.id).status

    def pauses(self) -> list[dict[str, Any]]:
        return [e.payload for e in self.goals.events_after(self.goal.id, 0)
                if e.type == "goal_status" and e.payload["status"] == "PAUSED"]

    def statuses(self) -> list[str]:
        return [s.status for s in self.goals.steps(self.goal.id)]

    def fixer_calls(self) -> int:
        return len(self.provider.fixer_prompts)

    def resume(self) -> None:
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")


class TestTheConductorFinishesTheSteps(DriveCase):
    async def test_two_steps_are_two_commits_and_one_fixer_call_each(self) -> None:
        provider = self.build(A_V2, B_V1)
        self.repo()
        first, second = self.two_steps()
        provider.tool_replies = _done(first) + _done(second)

        await self.drive()

        self.assertEqual("COMPLETED", self.status())
        self.assertEqual(["COMPLETED", "COMPLETED"], self.statuses())
        self.assertEqual(2, self.fixer_calls(), "a step was written more than once")
        self.assertEqual(3, int(_git(self.root, "rev-list", "--count", "HEAD").strip()), "first + two steps")
        self.assertEqual([], self.pauses())

    async def test_each_step_is_a_run_of_its_own_with_its_own_prompt(self) -> None:
        provider = self.build(A_V2, B_V1)
        self.repo()
        first, second = self.two_steps()
        provider.tool_replies = _done(first) + _done(second)

        await self.drive()

        opening = [msgs[-1]["content"] for msgs in provider.seen_messages if msgs[-1]["role"] == "user"]
        starts = [c for c in opening if "You are working on one step" in c]
        self.assertEqual(2, len(starts))
        self.assertIn("change a.py", starts[0])
        self.assertIn(first, starts[0])
        self.assertNotIn(f"step {second}", starts[0], "the run for step one was handed step two to do")
        self.assertIn("Change b", starts[0], "the other steps are named so the run keeps the whole in view")
        self.assertIn("change b.py", starts[1])


class TestAConductorThatDoesNotFinishPausesTheGoal(DriveCase):
    async def test_stopping_early_pauses_with_conductor_stopped_and_the_recipe_does_not_finish_it(self) -> None:
        provider = self.build(A_V2, B_V1)
        self.repo()
        self.one_step()
        provider.tool_replies = [ToolReply(text="I will get to this later.")]

        await self.drive()

        self.assertEqual("PAUSED", self.status())
        self.assertEqual(["conductor_stopped"], [p["reason_code"] for p in self.pauses()])
        self.assertEqual(0, self.fixer_calls(), "the engine's sweep wrote a step the conductor did not")
        self.assertNotIn("COMPLETED", self.statuses())

    async def test_running_out_of_calls_pauses_with_conductor_budget(self) -> None:
        provider = self.build(A_V2, B_V1)
        self.repo()
        first = self.one_step()
        self.settings.set_int("conductor_max_turns", 2)
        provider.tool_replies = [
            ToolReply(tool_calls=[_call("write", step_id=first, instructions="go")]),
            ToolReply(tool_calls=[_call("verify", step_id=first)]),
            ToolReply(tool_calls=[_call("review", step_id=first)]),
        ]

        await self.drive()

        self.assertEqual("PAUSED", self.status())
        self.assertEqual(["conductor_budget"], [p["reason_code"] for p in self.pauses()])
        self.assertEqual(1, self.fixer_calls(), "the step was written once and then left alone, not re-run")

    async def test_start_resumes_at_that_step_and_does_not_write_it_again(self) -> None:
        provider = self.build(A_V2, B_V1)
        self.repo()
        first = self.one_step()
        self.settings.set_int("conductor_max_turns", 2)
        provider.tool_replies = [
            ToolReply(tool_calls=[_call("write", step_id=first, instructions="go")]),
            ToolReply(tool_calls=[_call("verify", step_id=first)]),
            ToolReply(tool_calls=[_call("review", step_id=first)]),
        ]
        await self.drive()
        self.assertEqual("PAUSED", self.status())

        # The person presses Start. The files the first run wrote are on disk and in the step's diff events,
        # so the conductor can verify, review and record them without a second write.
        self.settings.set_int("conductor_max_turns", 14)
        self.resume()
        provider.tool_replies = [
            ToolReply(tool_calls=[_call("verify", step_id=first)]),
            ToolReply(tool_calls=[_call("review", step_id=first)]),
            ToolReply(tool_calls=[_call("summarize", step_id=first)]),
            ToolReply(text="Finished."),
        ]
        await self.drive()

        self.assertEqual("COMPLETED", self.status())
        self.assertEqual(1, self.fixer_calls(), "the resume wrote the step again")
        self.assertEqual({"a.py"}, self.committed_files())

    async def test_a_provider_that_dies_pauses_the_goal_it_does_not_fail_it(self) -> None:
        provider = self.build(A_V2, B_V1)
        self.repo()
        self.one_step()
        provider.tool_replies = [ProviderError("provider_unreachable", "the endpoint refused the connection")]

        await self.drive()

        self.assertEqual("PAUSED", self.status(), "a dead model is not a failed goal")
        pause = self.pauses()[0]
        self.assertEqual("conductor_provider", pause["reason_code"])
        self.assertIn("provider_unreachable", pause["reason"], "the code is engine vocabulary and helps")
        self.assertNotIn("refused the connection", pause["reason"], "the provider's own words are not ours")
        self.assertEqual(0, self.fixer_calls(), "the recipe took over for a dead conductor")

    async def test_a_conductor_that_finished_a_step_with_its_last_call_is_not_paused(self) -> None:
        # The step's stored status is the judge, not the sentence the conductor ended on: summarize was the
        # last thing it did, so the budget running out after it costs nothing.
        provider = self.build(A_V2, B_V1)
        self.repo()
        first = self.one_step()
        self.settings.set_int("conductor_max_turns", 4)
        provider.tool_replies = _done(first)[:4] + [ToolReply(tool_calls=[_call("read_file", path="a.py")])]

        await self.drive()

        self.assertEqual("COMPLETED", self.status())
        self.assertEqual([], self.pauses())


class TestTheEngineDoesNothingAfterTheConductor(DriveCase):
    async def test_a_conductor_that_returns_with_the_goal_running_is_not_followed_by_the_recipe(self) -> None:
        # The driver's own boundary: whatever the conductor layer does, the engine adds no second pass. Under
        # the real `run_conductor_resume` a run that ends with a step open always pauses the goal, so this
        # stubs it to return and leave the goal RUNNING with steps open, which is the case the old sweep
        # existed for.
        provider = self.build(A_V2, B_V1)
        self.repo()
        self.two_steps()
        calls: list[str] = []

        async def returns_having_done_nothing(goal_id: str, focus_step_id: str | None = None) -> None:
            calls.append(goal_id)

        with mock.patch.object(self.executor, "run_conductor_resume", returns_having_done_nothing):
            await self.drive()

        self.assertEqual([self.goal.id], calls)
        self.assertEqual(0, self.fixer_calls(), "the engine ran a step the conductor layer left open")
        self.assertEqual(["PENDING", "PENDING"], self.statuses())
        self.assertEqual("RUNNING", self.status())
        self.assertEqual([], provider.seen_messages)


class TestARetriedStepIsTakenFirst(DriveCase):
    async def test_the_run_starts_at_the_step_named_not_the_first_open_one(self) -> None:
        provider = self.build(A_V2, B_V1)
        self.repo()
        first, second = self.two_steps()
        provider.tool_replies = _done(second) + [ToolReply(text="I will leave the other one.")]

        await self.executor.run_conductor_resume(self.goal.id, focus_step_id=second)

        opening = provider.seen_messages[0][-1]["content"]
        self.assertIn(f"step {second}", opening, "the first run was not given the step the person named")
        self.assertIn("change b.py", opening)
        self.assertEqual("COMPLETED", self.goals.steps(self.goal.id)[1].status)
        # Then it carries on with what is still open, and pauses there because the script stops.
        self.assertEqual("PAUSED", self.status())
        pause = next(e for e in self.goals.events_after(self.goal.id, 0)
                     if e.type == "goal_status" and e.payload["status"] == "PAUSED")
        self.assertEqual("conductor_stopped", pause.payload["reason_code"])
        self.assertEqual(first, pause.step_id, "the pause names the step that was left open")


class TestACriticRejectionStopsTheRunUntilStart(DriveCase):
    async def test_the_pause_carries_the_reasons_to_the_next_run_and_the_write_then_proceeds(self) -> None:
        provider = self.build(A_V2, B_V1)
        self.repo()
        first = self.one_step()
        reasons = ["a.py: the new value has no explanation"]
        provider.roles["critic"] = {"decision": "request-changes", "reasons": reasons}
        provider.tool_replies = [
            ToolReply(tool_calls=[_call("write", step_id=first, instructions="go")]),
            ToolReply(tool_calls=[_call("verify", step_id=first)]),
            ToolReply(tool_calls=[_call("review", step_id=first)]),
            ToolReply(text="The critic wants changes; I have stopped."),
        ]
        provider.verifier_replies = [{"argv": None, "verdict": "pass", "explanation": "ok"}]

        await self.drive()

        self.assertEqual("PAUSED", self.status())
        self.assertEqual(["critic_rejected"], [p["reason_code"] for p in self.pauses()],
                         "one pause, and the critic's, not a second one for the conductor stopping")
        self.assertEqual(1, self.fixer_calls())

        # Start. The run for this step is told what the critic said, and the write is allowed again.
        provider.roles["critic"] = {"decision": "approve", "reasons": []}
        self.resume()
        provider.tool_replies = _done(first)
        await self.drive()

        told = [m["content"] for m in provider.seen_messages[-5] if m["role"] == "user"][-1]
        self.assertIn(reasons[0], told, "the step's run was not given the critic's reasons")
        self.assertEqual("COMPLETED", self.status())
        self.assertEqual(2, self.fixer_calls(), "after Start the conductor writes again, as the reasons ask")
        # An approval supersedes the notes of the rejection it answers, so a later run is not told to act
        # on them again.
        self.assertIsNone(self.goals.steps(self.goal.id)[0].review_notes)


class TestCancelAndTheRecipe(DriveCase):
    async def test_a_cancel_during_a_run_stays_cancelled_and_pauses_nothing(self) -> None:
        provider = self.build(A_V2, B_V1)
        first = self.one_step()
        provider.tool_replies = _done(first)

        def cancel() -> None:
            self.goals.update_status(self.goal.id, self.goals.get(self.goal.id).version, "CANCELLED")

        provider.on_call = cancel

        await self.drive()

        self.assertEqual("CANCELLED", self.status())
        self.assertEqual([], self.pauses())
        self.assertEqual(0, self.fixer_calls())

    async def test_a_parallel_goal_is_driven_by_the_engines_batcher_not_the_conductor(self) -> None:
        provider = self.build(A_V2, B_V1)
        self.conn.execute("UPDATE goals SET parallel=1 WHERE id=?", (self.goal.id,))
        self.conn.commit()
        self.two_steps()

        self.assertFalse(self.executor.conductor_can_drive(self.goal.id))
        await self.drive()

        self.assertEqual("COMPLETED", self.status())
        self.assertEqual([], provider.seen_messages, "a conductor was started for a goal the batcher drives")
        self.assertEqual(2, self.fixer_calls())

    async def test_an_install_with_no_tool_capable_model_still_runs_the_recipe(self) -> None:
        provider = self.build(A_V2, B_V1)
        self.settings.set_int("conductor_drives_execution", 0)
        self.two_steps()

        await self.drive()

        self.assertEqual("COMPLETED", self.status())
        self.assertEqual([], provider.seen_messages)


class TestAResumeKnowsWhatAnEarlierRunWrote(DriveCase):
    async def test_files_come_back_from_the_diff_events_but_a_verdict_and_an_approval_never_do(self) -> None:
        provider = self.build(A_V2, B_V1)
        self.repo()
        first = self.one_step()
        await ConductorTools.write(
            self._tools(), {"step_id": first, "instructions": "go"},
        )
        fresh = self._tools()  # a new run: nothing in memory, everything on the goal's event log

        verified = await fresh.verify({"step_id": first})
        unreviewed = await self._tools().summarize({"step_id": first})
        unverified = await self._tools().review({"step_id": first})

        self.assertIn("passed", verified, "the files were not recovered from the step's diff events")
        self.assertEqual(1, self.fixer_calls(), "recovering the files wrote them again")
        self.assertIn("not been reviewed", unreviewed, "an approval came back from nowhere")
        self.assertIn("verify", unverified, "a verdict came back from nowhere")
        self.assertEqual(0, provider.asked.get("critic", 0))

    def _tools(self) -> ConductorTools:
        from engine.skills import load_skills

        goal = self.goals.get(self.goal.id)
        return ConductorTools(self.executor, goal.id, goal, str(self.root), load_skills(str(self.root)))


class TestRetryOnAConductorInstall(DriveCase):
    async def test_retry_resumes_the_conductor_with_that_step_in_focus_and_never_runs_the_recipe_step(self) -> None:
        from engine.app import _retry_and_drive

        self.build(A_V2, B_V1)
        first, second = self.two_steps()
        calls: list[Any] = []

        async def resume(goal_id: str, focus_step_id: str | None = None) -> None:
            calls.append(("resume", focus_step_id))

        async def run_step(goal_id: str, step_id: str, stored_files: Any = None) -> None:
            calls.append(("run_step", step_id))

        with mock.patch.object(self.executor, "run_conductor_resume", resume), \
                mock.patch.object(self.executor, "run_step", run_step):
            self.executor.claim_driver(self.goal.id)
            await _retry_and_drive(self.app, self.goal.id, second)  # type: ignore[arg-type]

        self.assertEqual([("resume", second)], calls)
        self.assertFalse(self.executor.is_driving(self.goal.id), "the driver the retry claimed was not released")
        self.assertEqual(first, self.goals.steps(self.goal.id)[0].id)

    async def test_retry_keeps_the_critics_notes_for_the_conductor_that_will_act_on_them(self) -> None:
        self.build(A_V2, B_V1)
        first = self.one_step()
        self.executor._set_step(self.goal.id, self.goals.steps(self.goal.id)[0], "IN_PROGRESS",
                                review_notes="a.py: explain the value")
        self.executor.release_driver(self.goal.id)
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "PAUSED")

        self.executor.begin_retry(
            self.goal.id, first, self.goals.get(self.goal.id).version, keep_notes=True,
        )

        step = self.goals.steps(self.goal.id)[0]
        self.assertEqual("PENDING", step.status)
        self.assertEqual("a.py: explain the value", step.review_notes)
        self.executor.release_driver(self.goal.id)

    async def test_the_recipe_installs_retry_still_clears_the_notes(self) -> None:
        # The recipe's fixer never reads them, so a stale note would only sit on the timeline.
        self.build(A_V2, B_V1)
        first = self.one_step()
        self.settings.set_int("conductor_drives_execution", 0)
        self.executor._set_step(self.goal.id, self.goals.steps(self.goal.id)[0], "IN_PROGRESS",
                                review_notes="a.py: explain the value")
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "PAUSED")

        self.executor.begin_retry(self.goal.id, first, self.goals.get(self.goal.id).version, keep_notes=False)

        self.assertIsNone(self.goals.steps(self.goal.id)[0].review_notes)
        self.executor.release_driver(self.goal.id)


class TestTheDefaultBudgetIsFourteenCalls(DriveCase):
    async def test_a_step_is_four_moves_and_the_calls_around_them(self) -> None:
        from engine.conductor import DEFAULT_MAX_MOVES, DEFAULT_MAX_TURNS

        self.assertEqual(14, DEFAULT_MAX_TURNS)
        self.assertEqual(12, DEFAULT_MAX_MOVES)
        self.build(A_V2)
        self.assertEqual(14, self.settings.get_int("conductor_max_turns"))
