"""The conductor's `verify` runs the project's own tests only while the goal is still approved.

`verify` read the goal's stored status once, on entry, and then did two slow things before the process started: it
asked the verifier model what to run (minutes, with a local model) and it waited for the workspace's sandbox lock
(another goal's tests can hold it for minutes). A Pause or Cancel pressed during either wait changed nothing, and the
project's code ran after the person had taken the approval back. `run_command` closed the same gap in the lock wait
(`tests/test_conductor.py`); `verify` reaches the same process through the shared `_verifier`, so it has to ask too
(docs/03 section 1.4, CLAUDE.md invariant 6).

The recipe's own `run_step` keeps its documented rule: a Pause stops the goal between steps and a running step finishes
(`executor_core._cancelled`), so the same `_verifier` is called there without the question.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Callable
from typing import Any

from tests.test_conductor_moves_state import A_V2, MovesCase, _Counting


class _VerifierThatProposes(_Counting):
    """A scripted provider whose first verifier call runs a hook (the person pressing a button) and proposes a command."""

    def __init__(self, fixer_replies: list[Any], argv: list[str], during_model_call: Callable[[], None] | None) -> None:
        super().__init__(fixer_replies)
        self.argv = argv
        self.during_model_call = during_model_call
        self.verifier_calls = 0

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
        json_schema: dict[str, Any] | None = None, thinking_effort: str | None = None,
    ) -> str:
        if "You are Codify Verifier" in system_prompt:
            self.verifier_calls += 1
            if self.verifier_calls == 1:
                if self.during_model_call is not None:
                    self.during_model_call()
                return json.dumps({"argv": self.argv, "verdict": None, "explanation": None})
            return json.dumps({"argv": None, "verdict": "pass", "explanation": "ok"})
        return await super().complete(
            system_prompt, user_prompt, model, temperature, max_tokens, num_ctx=num_ctx, keep_alive=keep_alive,
        )


class TestVerifyRunsOnlyWhileApproved(MovesCase):
    verifier_provider: _VerifierThatProposes

    def _arrange(self, during_model_call: Callable[[], None] | None) -> Any:
        """An approved goal with one written-to step and a `mark.py` that leaves a file when the project's code runs."""
        mark = self.root / "MARK-project-code-ran"
        (self.root / "mark.py").write_text(
            f"from pathlib import Path\nPath({str(mark)!r}).write_text('ran')\n", encoding="utf-8",
        )
        provider = _VerifierThatProposes([A_V2], ["python3", "mark.py"], during_model_call)
        self.provider = provider
        self.verifier_provider = provider
        from engine.executor import ExecutorService
        from engine.models import ROLES, AgentConfigUpdate
        from engine.providers import Keychain
        from engine.sandbox import SandboxService
        from engine.services import AgentRegistryService
        from tests.test_fixer_reask import Factory, Gate

        registry = AgentRegistryService(self.conn, Factory(provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="test-model"))
        self.executor = ExecutorService(self.goals, self.workspaces, registry, SandboxService(), laya=Gate())
        self.repo()
        return mark

    def _take_back(self, status: str) -> Callable[[], None]:
        def press() -> None:
            self.goals.update_status(self.goal.id, self.goals.get(self.goal.id).version, status)

        return press

    async def _verify_after_the_person_takes_the_approval_back(self, taken_back: str) -> None:
        mark = self._arrange(None)
        self.verifier_provider.during_model_call = self._take_back(taken_back)
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "change a"})

        out = await table["verify"]({"step_id": step_id})

        self.assertFalse(mark.exists(), "the project's code ran after the person took the approval back")
        self.assertTrue(out.startswith("Nothing was run"), out)
        self.assertEqual(taken_back, self.goals.get(self.goal.id).status)
        self.assertEqual([], self.events("test_result"), "a run that never happened was recorded as a result")
        outcomes = [e["outcome"] for e in self.events("stage_result") if e.get("stage") == "verifier"]
        self.assertEqual(["cancelled"], outcomes, "a withdrawn run is the person's act, not an invalid verifier reply")

    async def test_a_pause_during_the_verifiers_model_call_stops_the_project_code(self) -> None:
        await self._verify_after_the_person_takes_the_approval_back("PAUSED")

    async def test_a_cancel_during_the_verifiers_model_call_stops_the_project_code(self) -> None:
        await self._verify_after_the_person_takes_the_approval_back("CANCELLED")

    async def test_a_pause_while_the_command_waits_for_the_lock_stops_it(self) -> None:
        mark = self._arrange(None)
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "change a"})
        lock = self.executor.sandbox_lock(str(self.root))

        await lock.acquire()
        try:
            waiting = asyncio.create_task(table["verify"]({"step_id": step_id}))
            await asyncio.sleep(0.3)  # approved when the model answered, and now queued behind another goal's tests
            self.assertFalse(waiting.done())
            self._take_back("PAUSED")()
        finally:
            lock.release()
        out = await waiting

        self.assertFalse(mark.exists(), "the project's code ran after the person paused while it waited for the lock")
        self.assertTrue(out.startswith("Nothing was run"), out)

    async def test_a_goal_that_stays_approved_still_runs_its_tests(self) -> None:
        mark = self._arrange(None)
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "change a"})

        out = await table["verify"]({"step_id": step_id})

        self.assertTrue(mark.exists(), "an approved goal could not run its project's command")
        self.assertTrue(json.loads(out)["passed"], out)

    async def test_the_recipes_verifier_is_not_asked_the_question(self) -> None:
        # The control: `run_step` calls `_verifier` as it always did. A Pause there stops the goal between steps and a
        # running step finishes, which is documented (`executor_core._cancelled`) and not changed here.
        mark = self._arrange(None)
        self.verifier_provider.during_model_call = self._take_back("PAUSED")
        step_id = self.approve()
        step = self.goals.steps(self.goal.id)[0]
        self.assertEqual(step_id, step.id)

        workspace = self.workspaces.get(self.goals.get(self.goal.id).workspace_id)
        await self.executor._verifier(self.goal.id, step, workspace, None)

        self.assertTrue(mark.exists(), "the recipe's verifier changed behaviour: a running step is meant to finish")


if __name__ == "__main__":
    unittest.main()
