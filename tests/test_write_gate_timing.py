"""The write gate is asked where the write happens, not only before the wait (review of 2026-09-29, finding 1).

`write` checked the goal's stored status *before* the fixer's model call, and `fs.apply` ran after the reply
with nothing re-checking it. A local model takes minutes to answer, and that is exactly when a person presses
Cancel or Pause: the goal read CANCELLED, the reply arrived, and the files were written anyway. The gate is
the engine's only defence against a write nobody approved (invariant 9), so it has to be true at the moment of
the write — which is the moment `fs.apply` runs, on both roads to it (the conductor's `write` and the
pipeline's step runner).

Each test holds the fixer's reply back, changes the goal while it is held, and then lets it through.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from tests.test_fixer_reask import SOURCE, Factory, Gate, ReAskCase, ScriptedProvider

from engine.executor import ExecutorService
from engine.models import ROLES, AgentConfigUpdate
from engine.providers import Keychain
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService
from engine.skills import load_skills

REWRITE = {"files": [{"path": "a.py", "action": "update", "content": "x = 1\n"}]}


class HeldFixer(ScriptedProvider):
    """A provider whose fixer does not answer until told to, and says when it was asked."""

    def __init__(self) -> None:
        super().__init__([REWRITE])
        self.asked = asyncio.Event()
        self.answer = asyncio.Event()

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        if "You are Codify Fixer" in system_prompt:
            self.asked.set()
            await self.answer.wait()
        return await super().complete(
            system_prompt, user_prompt, model, temperature, max_tokens, num_ctx=num_ctx, keep_alive=keep_alive,
        )


class GateCase(ReAskCase):
    def hold(self) -> HeldFixer:
        provider = HeldFixer()
        registry = AgentRegistryService(self.conn, Factory(provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="test-model"))
        self.executor = ExecutorService(self.goals, self.workspaces, registry, SandboxService(), laya=Gate())
        self.provider = provider
        return provider

    def approve_and_plan(self) -> str:
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        self.executor._insert_steps(
            self.goal.id, [{"title": "Rewrite", "description": "rewrite a.py", "suggested_paths": ["a.py"]}]
        )
        return self.goals.steps(self.goal.id)[0].id

    def move_goal_to(self, status: str) -> None:
        self.goals.update_status(self.goal.id, self.goals.get(self.goal.id).version, status)

    def diffs(self) -> list[dict[str, Any]]:
        return self.events("diff")


class TestThePipelineRoad(GateCase):
    async def held_then(self, status: str) -> None:
        provider = self.hold()
        step_id = self.approve_and_plan()
        run = asyncio.create_task(self.executor.run_step(self.goal.id, step_id))
        await asyncio.wait_for(provider.asked.wait(), 10)

        self.move_goal_to(status)
        provider.answer.set()
        await asyncio.wait_for(run, 10)

    async def test_a_goal_cancelled_while_the_fixer_thinks_is_not_written_to(self) -> None:
        await self.held_then("CANCELLED")

        self.assertEqual(SOURCE, self.file, "a cancelled goal's files were written when the reply arrived")
        self.assertEqual([], self.diffs())
        self.assertEqual("CANCELLED", self.goals.get(self.goal.id).status)

    async def test_a_goal_paused_while_the_fixer_thinks_is_not_written_to(self) -> None:
        await self.held_then("PAUSED")

        self.assertEqual(SOURCE, self.file, "a paused goal's files were written when the reply arrived")
        self.assertEqual([], self.diffs())
        self.assertEqual("PAUSED", self.goals.get(self.goal.id).status)

    async def test_a_withdrawn_write_is_not_reported_as_the_models_failure(self) -> None:
        await self.held_then("CANCELLED")

        self.assertEqual([], self.events("error"), "the person's cancel was reported as an error")
        self.assertEqual([], [e for e in self.events("agent_call_failed") if e["role"] == "fixer"])

    async def test_a_goal_left_alone_is_written_as_before(self) -> None:
        provider = self.hold()
        step_id = self.approve_and_plan()
        run = asyncio.create_task(self.executor.run_step(self.goal.id, step_id))
        await asyncio.wait_for(provider.asked.wait(), 10)
        provider.answer.set()
        await asyncio.wait_for(run, 10)

        self.assertEqual("x = 1\n", self.file)


class TestTheConductorRoad(GateCase):
    async def held_write_then(self, status: str) -> str:
        provider = self.hold()
        step_id = self.approve_and_plan()
        goal = self.goals.get(self.goal.id)
        table = self.executor._conductor_dispatch(goal.id, goal, str(self.root), load_skills(str(self.root)))
        call = asyncio.create_task(table["write"]({"step_id": step_id, "instructions": "rewrite it"}))
        await asyncio.wait_for(provider.asked.wait(), 10)

        self.move_goal_to(status)
        provider.answer.set()
        return await asyncio.wait_for(call, 10)

    async def test_a_cancel_during_the_write_move_stops_the_write(self) -> None:
        told = await self.held_write_then("CANCELLED")

        self.assertEqual(SOURCE, self.file, "the conductor's write landed after the goal was cancelled")
        self.assertIn("Nothing was written", told)

    async def test_a_pause_during_the_write_move_stops_the_write(self) -> None:
        told = await self.held_write_then("PAUSED")

        self.assertEqual(SOURCE, self.file)
        self.assertIn("Nothing was written", told)

    async def test_an_approved_goal_is_still_written_by_the_conductor(self) -> None:
        provider = self.hold()
        step_id = self.approve_and_plan()
        goal = self.goals.get(self.goal.id)
        table = self.executor._conductor_dispatch(goal.id, goal, str(self.root), load_skills(str(self.root)))
        call = asyncio.create_task(table["write"]({"step_id": step_id, "instructions": "rewrite it"}))
        await asyncio.wait_for(provider.asked.wait(), 10)
        provider.answer.set()

        told = await asyncio.wait_for(call, 10)

        self.assertEqual("x = 1\n", self.file)
        self.assertEqual(["a.py"], [c["path"] for c in json.loads(told)["changed"]])


if __name__ == "__main__":
    import unittest

    unittest.main()
