"""Deep review inside `ExecutorService._critic` (docs/21 P1).

Three promises, each pinned where a refactor would break it:

* **The standard path is byte-identical.** `review_deep` off must send the critic exactly the prompt it sent before
  lenses existed. The golden below was written against the unmodified code.
* **The deep path is bounded and honest.** Two model lenses and the critic, a lens that fails is `not_run` and
  never retried, a failed lens is never reported as a clean one, and the critic still decides.
* **A lens sees only the packet.** The librarian's evidence reaches the critic and never a lens.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.test_executor import _QueuedFactory, _QueuedProvider, _SkippedGate, configure_every_role

from engine import review
from engine.db import connect
from engine.executor import ExecutorService
from engine.executor_support import CriticRejection
from engine.fs import FileSystemService
from engine.models import GoalCreate, PlanStep, WorkspaceCreate
from engine.providers import Keychain
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, SettingsService, WorkspaceService

EVIDENCE = {"summary": "LIBRARIAN-EVIDENCE-7731"}
OUTCOME = {"argv": ["pytest"], "ran": True, "verdict": "pass", "exit_code": 0, "explanation": "ok"}
DIFFS = [{"path": "a.py", "action": "create", "unified_diff": "+x = 1"}]

# What the critic was sent before lenses existed, for the inputs above, character for character.
GOLDEN_STANDARD_PROMPT = (
    "Step: T\nD\n\n"
    "Test verdict: pass — ran `pytest`, exit 0. ok\n\n"
    "What the librarian found:\nSummary: LIBRARIAN-EVIDENCE-7731\n\n"
    "Diffs:\nFile: a.py (create)\n+x = 1"
)

FINDING = {
    "severity": "major", "category": "call-site", "scope": "a.py:1",
    "evidence": "x is never read", "recommendation": "remove it",
}
APPROVE = {"decision": "approve", "reasons": []}


class _Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve() / "work"
        self.root.mkdir()
        self.conn = connect(Path(self.temp_dir.name) / "t.db")
        self.provider = _QueuedProvider()
        self.registry = AgentRegistryService(self.conn, _QueuedFactory(self.provider, Keychain()), Keychain())
        configure_every_role(self.registry)
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.settings = SettingsService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        self.executor.settings = self.settings
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def _step(self) -> tuple[str, PlanStep]:
        self.provider.by_role = {
            "planner": [{"steps": [{"title": "T", "description": "D", "suggested_paths": []}]}],
        }
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="G", description="the whole request"))
        await self.executor.run_planning(goal.id)
        self.goals.update_status(goal.id, goal.version + 1, "RUNNING")
        return goal.id, self.goals.steps(goal.id)[0]

    async def _critic(self, replies: dict[str, list[Any]]) -> tuple[str, PlanStep]:
        goal_id, step = await self._step()
        self.provider.by_role = {role: list(queue) for role, queue in replies.items()}
        self.provider.calls.clear()
        await self.executor._critic(
            goal_id, step, FileSystemService(str(self.root)), DIFFS, EVIDENCE, OUTCOME, ws_root=str(self.root),
        )
        return goal_id, step

    def critic_calls(self) -> list[tuple[str, str, str]]:
        return self.provider.role_calls("critic")

    def events(self, goal_id: str, kind: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.goals.events_after(goal_id, 0) if e.type == kind]


class TestTheStandardPathIsUnchanged(_Harness):
    async def test_the_critic_is_sent_exactly_the_prompt_it_always_was(self) -> None:
        await self._critic({"critic": [APPROVE]})
        calls = self.critic_calls()
        self.assertEqual(1, len(calls))
        self.assertEqual(GOLDEN_STANDARD_PROMPT, calls[0][2])

    async def test_off_by_default_and_no_lens_event_is_published(self) -> None:
        self.assertEqual(0, self.settings.get_int("review_deep"))
        goal_id, _ = await self._critic({"critic": [APPROVE]})
        self.assertEqual([], self.events(goal_id, "review_lens"))
        self.assertEqual([], self.events(goal_id, "review_report"))


class TestDeepReview(_Harness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.settings.set_int("review_deep", 1)

    async def test_two_lenses_then_the_critic_with_the_report_appended(self) -> None:
        goal_id, _ = await self._critic({"critic": [
            {"findings": [FINDING]}, {"findings": []}, APPROVE,
        ]})
        calls = self.critic_calls()
        self.assertEqual(3, len(calls), "two model lenses and the critic")
        self.assertIn("zoom-out reviewer", calls[0][1])
        self.assertIn("comprehension reviewer", calls[1][1])
        critic_prompt = calls[2][2]
        self.assertTrue(critic_prompt.startswith(GOLDEN_STANDARD_PROMPT), "the critic still gets its own prompt")
        self.assertIn("Independent review findings", critic_prompt)
        self.assertIn("a.py:1", critic_prompt)
        self.assertIn("not a verdict", critic_prompt)

    async def test_a_lens_never_sees_the_authors_evidence(self) -> None:
        await self._critic({"critic": [{"findings": []}, {"findings": []}, APPROVE]})
        for _role, system, user in self.critic_calls()[:2]:
            self.assertNotIn("LIBRARIAN-EVIDENCE-7731", user)
            self.assertNotIn("LIBRARIAN-EVIDENCE-7731", system)
            self.assertIn("the whole request", user, "the original request is in the packet")
            self.assertIn("+x = 1", user, "and so is the diff")

    async def test_every_lens_is_recorded_with_its_status(self) -> None:
        goal_id, _ = await self._critic({"critic": [{"findings": [FINDING]}, {"findings": []}, APPROVE]})
        lenses = self.events(goal_id, "review_lens")
        self.assertEqual(["zoom-out", "comprehension", "readiness"], [e["lens"] for e in lenses])
        self.assertEqual(["ran", "ran", "ran"], [e["status"] for e in lenses])
        self.assertEqual([1, 1, 0], [e["calls"] for e in lenses], "readiness is answered without a model")
        self.assertEqual(1, lenses[0]["findings"])
        report = self.events(goal_id, "review_report")
        self.assertEqual(1, len(report))
        self.assertEqual("3 of 3 lenses ran", report[0]["status"])

    async def test_a_failed_lens_is_not_run_not_retried_and_never_reported_clean(self) -> None:
        # An unusable reply is asked once more by the existing re-ask; two unusable replies fail the lens.
        goal_id, _ = await self._critic({"critic": [
            {"nonsense": True}, {"nonsense": True},   # zoom-out: both unusable
            {"findings": []},                          # comprehension: ran, found nothing
            APPROVE,
        ]})
        lenses = {e["lens"]: e for e in self.events(goal_id, "review_lens")}
        self.assertEqual("not_run", lenses["zoom-out"]["status"])
        self.assertEqual("ran", lenses["comprehension"]["status"])
        report = self.events(goal_id, "review_report")[0]
        self.assertEqual("2 of 3 lenses ran — zoom-out not run", report["status"])

    async def test_if_every_model_lens_fails_the_standard_prompt_is_used_and_the_event_says_so(self) -> None:
        goal_id, _ = await self._critic({"critic": [
            {"bad": 1}, {"bad": 1}, {"bad": 1}, {"bad": 1}, APPROVE,
        ]})
        critic_prompt = self.critic_calls()[-1][2]
        self.assertEqual(GOLDEN_STANDARD_PROMPT, critic_prompt)
        report = self.events(goal_id, "review_report")[0]
        self.assertIn("fell back", report["detail"])

    async def test_the_critic_still_decides(self) -> None:
        goal_id, step = await self._step()
        self.provider.by_role = {"critic": [
            {"findings": []}, {"findings": []},
            {"decision": "request-changes", "reasons": ["nope"]},
        ]}
        with self.assertRaises(CriticRejection):
            await self.executor._critic(
                goal_id, step, FileSystemService(str(self.root)), DIFFS, EVIDENCE, OUTCOME, ws_root=str(self.root),
            )

    async def test_blocker_findings_do_not_reject_a_step_on_their_own(self) -> None:
        blocker = dict(FINDING, severity="blocker")
        goal_id, step = await self._critic({"critic": [{"findings": [blocker]}, {"findings": []}, APPROVE]})
        self.assertNotEqual("FAILED", self.goals.steps(goal_id)[0].status)

    async def test_the_readiness_lens_needs_no_model_call(self) -> None:
        await self._critic({"critic": [{"findings": []}, {"findings": []}, APPROVE]})
        self.assertEqual(3, len(self.critic_calls()), "no fourth call for readiness")
        self.assertTrue(review.READINESS not in review.MODEL_LENSES)


if __name__ == "__main__":
    unittest.main()
