"""One writer per goal, and one plan per goal (review of 2026-09-29, findings 2 and 3).

**Two writers.** The conductor's `plan` move leaves the goal `PENDING` *while the turn is still running* —
it goes on to write its answer. `PENDING` is what `POST /goals/{id}/start` accepts, so pressing Start then
began a second driver on a goal whose turn was still going: the turn's conductor and the resumed one both
write the same steps, and the turn's own `write` gate opens the moment the status reads `RUNNING`. Delete was
allowed for the same reason (the goal was not `PLANNING` or `RUNNING`). A turn and a planning run now hold
the goal's driver claim for as long as they run, and Start and Delete both consult it.

**Two plans.** A conductor whose provider fails *after* `plan` returned `planned=False` — the failure path
assumed nothing had been planned — so `run_chat` fell through to the standard sequence and inserted a second
full plan next to the first. Approving it ran every step twice. The plan that exists is the plan, and asking
for another one (from the loop, or by the fall-through) is refused.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from httpx import ASGITransport

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from tests.test_conductor import ConductorTestCase, _call, _DyingProvider, _ToolProvider

from engine.app import BOOT_TOKEN, app
from engine.laya import LayaDecision, LayaService
from engine.toolcall import ToolReply as _ToolReply


class _ChangeGate(LayaService):
    """A gate that reads every request as a change, so the fall-through (the standard sequence) is the road taken."""

    async def decide(self, state: dict[str, Any], on_call: Any = None) -> LayaDecision:
        return LayaDecision(
            engine="sdk", answers={"intent": {"choice": "code_change", "confidence": 0.99}},
        )


def plan_then_die() -> _DyingProvider:
    """Recon, plan — then the provider refuses the third call."""
    return _DyingProvider(
        replies=[
            _ToolReply(text="", tool_calls=[_call("recon", task="find the parser")]),
            _ToolReply(text="", tool_calls=[_call("plan", task="add a test")]),
        ],
        fail_on={3: "provider_unreachable"},
    )


class TestOnePlanPerGoal(ConductorTestCase):
    async def test_a_conductor_that_dies_after_planning_keeps_its_plan_and_plans_nothing_more(self) -> None:
        executor = self._executor(plan_then_die(), laya=_ChangeGate())

        await executor.run_chat(self.goal.id)

        steps = self.goals.steps(self.goal.id)
        self.assertEqual(1, len(steps), f"the goal was planned twice: {[s.title for s in steps]}")
        self.assertEqual("PENDING", self.goals.get(self.goal.id).status)

    async def test_the_person_is_told_the_plan_stands_and_why_the_turn_stopped(self) -> None:
        executor = self._executor(plan_then_die(), laya=_ChangeGate())

        await executor.run_chat(self.goal.id)

        warnings = [e.payload["message"] for e in self.goals.events_after(self.goal.id, 0)
                    if e.type == "log" and e.payload.get("level") == "warn"]
        self.assertTrue(any("plan" in w and "stands" in w for w in warnings), warnings)
        self.assertFalse(any("standard sequence" in w for w in warnings), "the fall-through still ran")

    async def test_the_planning_pipeline_will_not_plan_a_goal_that_already_has_a_plan(self) -> None:
        executor = self._executor(_ToolProvider(), laya=_ChangeGate())
        executor._insert_steps(self.goal.id, [{"title": "Existing", "description": "d", "suggested_paths": []}])

        await executor.run_planning(self.goal.id)

        self.assertEqual(["Existing"], [s.title for s in self.goals.steps(self.goal.id)])

    async def test_a_second_plan_move_is_refused_and_leaves_the_first_alone(self) -> None:
        table = self._dispatch(self.goal.id)
        await table["recon"]({"task": "find the parser"})
        await table["plan"]({"task": "add a test"})

        second = await table["plan"]({"task": "add another test"})

        self.assertIn("already has a plan", second)
        self.assertEqual(1, len(self.goals.steps(self.goal.id)))


class TestOneDriverPerGoal(ConductorTestCase):
    async def test_a_turn_holds_the_goal_for_as_long_as_it_runs(self) -> None:
        held = asyncio.Event()
        release = asyncio.Event()

        class Slow(_ToolProvider):
            async def complete_with_tools(self, *args: Any, **kwargs: Any) -> Any:
                held.set()
                await release.wait()
                return _ToolReply(text="done")

        executor = self._executor(Slow())
        run = asyncio.create_task(executor.run_chat(self.goal.id))
        await asyncio.wait_for(held.wait(), 10)

        self.assertTrue(executor.is_driving(self.goal.id), "a running turn is not a driver of its goal")
        release.set()
        await asyncio.wait_for(run, 10)
        self.assertFalse(executor.is_driving(self.goal.id), "the claim outlived the turn")

    async def test_the_claim_is_released_when_the_turn_blows_up(self) -> None:
        executor = self._executor(_ToolProvider())

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

        executor._conduct = boom  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            await executor.run_chat(self.goal.id)

        self.assertFalse(executor.is_driving(self.goal.id))

    async def test_planning_holds_the_goal_while_it_plans(self) -> None:
        asked = asyncio.Event()
        release = asyncio.Event()

        class SlowPlanner(_ToolProvider):
            async def complete(self, system_prompt: str, *args: Any, **kwargs: Any) -> str:
                if "you are codify planner" in system_prompt.lower():
                    asked.set()
                    await release.wait()
                return await super().complete(system_prompt, *args, **kwargs)

        executor = self._executor(SlowPlanner(), laya=_ChangeGate())
        run = asyncio.create_task(executor.run_planning(self.goal.id))
        await asyncio.wait_for(asked.wait(), 10)

        self.assertTrue(executor.is_driving(self.goal.id))
        release.set()
        await asyncio.wait_for(run, 10)
        self.assertFalse(executor.is_driving(self.goal.id))


class TestTheRoutesConsultTheClaim(ConductorTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.executor = self._executor(_ToolProvider())
        app.state.goals = self.goals
        app.state.executor = self.executor
        app.state.token = BOOT_TOKEN
        # A goal a turn has just planned: PENDING, with a step, and the turn still running.
        self.executor._insert_steps(self.goal.id, [{"title": "S1", "description": "d", "suggested_paths": []}])
        self.executor._set_status(self.goal.id, "PENDING", None)
        assert self.executor.claim_driver(self.goal.id)
        self.addCleanup(self.executor.release_driver, self.goal.id)
        self.client = httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t",
            headers={"Authorization": f"Bearer {BOOT_TOKEN}"},
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_start_is_refused_while_the_turn_is_still_running(self) -> None:
        version = self.goals.get(self.goal.id).version

        r = await self.client.post(f"/goals/{self.goal.id}/start", json={"expected_version": version})

        self.assertEqual(409, r.status_code, r.text)
        self.assertEqual("driver_busy", r.json()["code"])
        self.assertEqual("PENDING", self.goals.get(self.goal.id).status, "a refused start still moved the goal")

    async def test_delete_is_refused_while_the_turn_is_still_running(self) -> None:
        r = await self.client.delete(f"/goals/{self.goal.id}")

        self.assertEqual(409, r.status_code, r.text)
        self.assertEqual("goal_in_progress", r.json()["code"])
        self.assertEqual(self.goal.id, self.goals.get(self.goal.id).id)

    async def test_cancel_is_still_allowed_mid_turn(self) -> None:
        version = self.goals.get(self.goal.id).version

        r = await self.client.post(f"/goals/{self.goal.id}/cancel", json={"expected_version": version})

        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("CANCELLED", self.goals.get(self.goal.id).status)

    async def test_start_works_once_the_turn_has_finished(self) -> None:
        self.executor.release_driver(self.goal.id)
        version = self.goals.get(self.goal.id).version

        # Only the route's own answer is asserted: the background driver it spawns is the pipeline's.
        r = await self.client.post(f"/goals/{self.goal.id}/start", json={"expected_version": version})
        await asyncio.sleep(0)

        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(json.loads(r.text)["status"], "RUNNING")
        self.executor.claim_driver(self.goal.id)  # so the cleanup above finds what it expects


if __name__ == "__main__":
    import unittest

    unittest.main()
