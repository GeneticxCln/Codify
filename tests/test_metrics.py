from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from engine import metrics
from engine.db import connect
from engine.executor import STAGE_OUTCOMES, ExecutorService
from engine.models import ROLES, AgentConfig, AgentConfigUpdate, GoalCreate, WorkspaceCreate
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService

from tests.test_design_role import SkippedGate  # noqa: F401 — the double is shared


def stage_event(
    stage: str, outcome: str, ts: float = 1_700_000_000.0, step_id: str | None = None,
    role: str | None = None, tokens: int = 0, calls: int = 0, duration_ms: int = 100,
) -> dict[str, Any]:
    return {
        "type": "stage_result",
        "goal_id": "g1",
        "step_id": step_id,
        "sequence": 1,
        "timestamp": ts,
        "payload": {
            "stage": stage, "role": role or stage, "ordinal": 1, "outcome": outcome,
            "detail": None, "duration_ms": duration_ms, "tokens": tokens, "calls": calls,
        },
    }


def usage_event(role: str, tokens: int, ts: float = 1_700_000_000.0) -> dict[str, Any]:
    half = tokens // 2
    return {
        "type": "usage",
        "goal_id": "g1",
        "step_id": None,
        "sequence": 1,
        "timestamp": ts,
        "payload": {
            "role": role, "provider": "ollama", "model": "m",
            "input_tokens": half, "output_tokens": tokens - half,
            "total_tokens": tokens, "duration_ms": 10,
        },
    }


class OutcomeVocabularyCase(unittest.TestCase):
    """The vocabulary is a contract with the UI, so it cannot drift from the
    engine that writes it."""

    def test_every_stage_is_a_role_the_engine_actually_runs(self) -> None:
        for stage in STAGE_OUTCOMES:
            self.assertIn(stage, ROLES, f"{stage} is measured but is not a role")

    def test_the_executor_publishes_every_stage_it_declares(self) -> None:
        """Re-derived from the executor's own call sites, the way
        `tests/test_versioned.py` re-derives the status matrix: adding a stage
        to the vocabulary without measuring it is a row that can only ever be
        zero, and this fails instead."""
        import inspect

        source = inspect.getsource(ExecutorService)
        published = set(re.findall(r'self\._stage\(\s*goal_id,\s*"([a-z]+)"', source))
        self.assertEqual(
            published, set(STAGE_OUTCOMES),
            "a stage in the outcome vocabulary is not measured by the executor, "
            "or a measured stage is missing from the vocabulary",
        )

    def test_every_declared_outcome_is_from_the_documented_set(self) -> None:
        for stage, outcomes in STAGE_OUTCOMES.items():
            self.assertIn("unavailable", outcomes, f"{stage} needs a default outcome")
            for outcome in outcomes:
                self.assertIsInstance(outcome, str)
                self.assertTrue(outcome.strip())

    def test_the_success_table_only_names_outcomes_that_exist(self) -> None:
        for stage, good in metrics.STAGE_SUCCESS_OUTCOMES.items():
            self.assertIn(stage, STAGE_OUTCOMES, f"{stage} is not a measured stage")
            for outcome in good:
                self.assertIn(
                    outcome, STAGE_OUTCOMES[stage],
                    f"{stage}: {outcome!r} is a success the vocabulary does not have",
                )


class RoleSuccessRateCase(unittest.TestCase):
    def test_a_role_that_never_ran_is_null_not_zero(self) -> None:
        """An install that has only ever planned has not proved its scribe
        broken. 0% would say exactly that."""
        rates = metrics.role_success_rate([stage_event("planner", "plan")])
        self.assertEqual(rates["planner"]["success_rate"], 100)
        self.assertIsNone(rates["scribe"]["success_rate"])
        self.assertEqual(rates["scribe"]["runs"], 0)

    def test_a_rate_is_over_outcomes_that_finished(self) -> None:
        events = [
            stage_event("verifier", "pass"),
            stage_event("verifier", "pass"),
            stage_event("verifier", "fail"),
            stage_event("verifier", "cancelled"),
        ]
        rates = metrics.role_success_rate(events)["verifier"]
        self.assertEqual(rates["runs"], 4)
        # Cancelled is not in the denominator: it has not happened yet.
        self.assertEqual(rates["cancelled"], 1)
        self.assertEqual((rates["succeeded"], rates["failed"]), (2, 1))
        self.assertEqual(rates["success_rate"], 67)

    def test_a_verifier_that_fails_is_a_role_working_not_a_role_broken(self) -> None:
        """The rate is about the role, not the goal: reporting a healthy
        verifier as broken is the failure mode this split exists to avoid. The
        two failures below are both failures, and the histogram is what tells a
        reader which one it was looking at."""
        reported = metrics.role_success_rate([stage_event("verifier", "fail")])["verifier"]
        self.assertEqual(reported["failed"], 1)
        self.assertEqual(reported["success_rate"], 0)
        broken = metrics.role_success_rate([stage_event("verifier", "invalid")])["verifier"]
        self.assertEqual(broken["failed"], 1)
        self.assertNotEqual(
            broken["outcomes"], reported["outcomes"],
            "two failures that name themselves differently must be distinguishable",
        )
        self.assertEqual(reported["outcomes"], {"fail": 1})
        self.assertEqual(broken["outcomes"], {"invalid": 1})

    def test_the_outcome_histogram_is_always_shipped(self) -> None:
        rates = metrics.role_success_rate([stage_event("critic", "approve")])
        for stage, entry in rates.items():
            self.assertIn("outcomes", entry, f"{stage} hides the evidence for its rate")

    def test_tokens_come_from_usage_and_latency_from_the_stage(self) -> None:
        events = [
            usage_event("fixer", 1000),
            stage_event("fixer", "wrote", duration_ms=200, tokens=1000, calls=1),
            stage_event("fixer", "wrote", duration_ms=400, tokens=2000, calls=1),
        ]
        rates = metrics.role_success_rate(events)["fixer"]
        self.assertEqual(rates["tokens"], 1000, "usage events, not summed stage payloads")
        self.assertEqual(rates["avg_duration_ms"], 300)
        self.assertEqual(rates["p95_duration_ms"], 400)

    def test_the_window_anchors_on_wall_clock_when_given_one(self) -> None:
        now = 1_700_000_000.0
        events = [
            stage_event("planner", "plan", ts=now - 86400),
            stage_event("planner", "plan", ts=now - 10 * 86400),
        ]
        rates = metrics.role_success_rate(events, 7, now=now)["planner"]
        self.assertEqual(rates["runs"], 1, "a 7-day window excludes a 10-day-old run")

    def test_without_a_clock_the_newest_event_anchors_the_window(self) -> None:
        events = [
            stage_event("planner", "plan", ts=1_700_000_000.0),
            stage_event("planner", "plan", ts=1_699_000_000.0),
        ]
        rates = metrics.role_success_rate(events, 7)["planner"]
        self.assertEqual(rates["runs"], 1)

    def test_window_zero_is_all_time(self) -> None:
        events = [stage_event("planner", "plan", ts=1.0)]
        self.assertEqual(metrics.role_success_rate(events, 0, now=2e9)["planner"]["runs"], 1)


class PercentileCase(unittest.TestCase):
    def test_nearest_rank_never_invents_a_duration(self) -> None:
        self.assertIsNone(metrics.percentile([], 95))
        self.assertEqual(metrics.percentile([10], 95), 10)
        self.assertEqual(metrics.percentile([10, 20, 30, 40], 95), 40)
        self.assertEqual(metrics.percentile([10, 20, 30, 40], 50), 20)
        self.assertEqual(metrics.percentile([10, 20, 30, 40], 0), 10)

    def test_a_list_of_one_measurements_p95_is_that_measurement(self) -> None:
        self.assertEqual(metrics.percentile([7, 900], 95), 900)


class StageCostCase(unittest.TestCase):
    def test_every_stage_is_a_row_even_before_it_has_run(self) -> None:
        rows = metrics.stage_costs([stage_event("planner", "plan", tokens=100)])
        self.assertEqual(
            [r["stage"] for r in rows], list(metrics.STAGE_ORDER),
            "a stage that appears only once it has data reads as not-in-pipeline",
        )
        self.assertEqual(rows[0]["runs"], 0)

    def test_shares_add_up_to_the_window(self) -> None:
        rows = metrics.stage_costs([
            stage_event("planner", "plan", tokens=750),
            stage_event("fixer", "wrote", tokens=250),
        ])
        by_stage = {r["stage"]: r for r in rows}
        self.assertEqual(by_stage["planner"]["token_share"], 75)
        self.assertEqual(by_stage["fixer"]["token_share"], 25)
        self.assertEqual(sum(r["token_share"] for r in rows), 100)

    def test_shares_add_up_even_when_rounding_would_not(self) -> None:
        """Six equal stages each round 16.67 to 17 and sum to 102. A column
        that does not add up is how a reader stops trusting the column."""
        events = [
            stage_event(stage, "plan" if stage == "planner" else "wrote", tokens=150)
            for stage in ("librarian", "planner", "fixer", "verifier", "critic", "scribe")
        ]
        rows = metrics.stage_costs(events)
        self.assertEqual(sum(r["token_share"] for r in rows), 100)

    def test_a_window_with_no_spend_shares_zero_rather_than_dividing(self) -> None:
        rows = metrics.stage_costs([stage_event("planner", "plan")])
        self.assertEqual(sum(r["token_share"] for r in rows), 0)
        self.assertIsNone(rows[0]["avg_duration_ms"])

    def test_latency_is_the_stage_wall_clock_not_the_sum_of_its_calls(self) -> None:
        """A stage's cost is the call plus the engine work around it, so summing
        the usage events' durations would understate every stage that runs a
        sandboxed command."""
        rows = {
            r["stage"]: r for r in metrics.stage_costs([
                stage_event("verifier", "pass", duration_ms=5000, tokens=10, calls=1),
            ])
        }
        self.assertEqual(rows["verifier"]["avg_duration_ms"], 5000)
        self.assertEqual(rows["verifier"]["p95_duration_ms"], 5000)


class FailureBreakdownCase(unittest.TestCase):
    def _error(self, code: str, role: str | None, ts: float = 1.7e9) -> dict[str, Any]:
        return {
            "type": "error", "goal_id": "g1", "step_id": None, "sequence": 1,
            "timestamp": ts,
            "payload": {"code": code, "message": f"{code} happened", "role": role},
        }

    def _call_failed(self, code: str, role: str) -> dict[str, Any]:
        return {
            "type": "agent_call_failed", "goal_id": "g1", "step_id": None,
            "sequence": 2, "timestamp": 1.7e9,
            "payload": {"code": code, "message": code, "role": role},
        }

    def test_an_install_that_never_failed_says_so_rather_than_showing_zeros(self) -> None:
        out = metrics.failure_breakdown([])
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["by_code"], {})
        self.assertIsNone(out["recovery_rate"], "no retries is not a perfect record")

    def test_goal_and_call_failures_are_both_counted(self) -> None:
        """They fail at different scales: a provider refused all week should not
        hide behind goals that happened to pass."""
        out = metrics.failure_breakdown([
            self._error("tests_failed", "verifier"),
            self._call_failed("provider_unreachable", "planner"),
        ])
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["by_code"], {"tests_failed": 1, "provider_unreachable": 1})
        self.assertEqual(out["by_role"], {"verifier": 1, "planner": 1})

    def test_causes_carry_the_most_recent_message_and_are_ranked(self) -> None:
        out = metrics.failure_breakdown([
            self._error("boom", "fixer", ts=1.0),
            self._error("boom", "fixer", ts=2.0),
            self._error("rare", "fixer", ts=3.0),
        ])
        self.assertEqual([c["code"] for c in out["causes"]], ["boom", "rare"])
        self.assertEqual(out["causes"][0]["count"], 2)

    def test_stages_are_ranked_by_how_often_they_failed(self) -> None:
        out = metrics.failure_breakdown([
            stage_event("fixer", "wrote"),
            stage_event("fixer", "invalid"),
            stage_event("verifier", "pass"),
            stage_event("verifier", "invalid"),
            stage_event("verifier", "invalid"),
            stage_event("planner", "cancelled"),
        ])
        # `wrote`/`pass` are successes and `cancelled` is unfinished: neither
        # is a stage failure.
        self.assertEqual(set(out["by_stage"]), {"fixer", "verifier"})
        self.assertEqual(out["by_stage"]["verifier"]["failed"], 2)
        self.assertEqual(out["by_stage"]["verifier"]["outcomes"], {"invalid": 2})

    def test_a_retry_the_step_got_past_is_a_recovery(self) -> None:
        events = [
            {"type": "fix_retry", "goal_id": "g1", "step_id": "s1", "sequence": 1,
             "timestamp": 1.7e9, "payload": {"attempt": 1}},
            {"type": "test_result", "goal_id": "g1", "step_id": "s1", "sequence": 2,
             "timestamp": 1.7e9, "payload": {"verdict": "pass"}},
            {"type": "fix_retry", "goal_id": "g1", "step_id": "s2", "sequence": 3,
             "timestamp": 1.7e9, "payload": {"attempt": 1}},
            {"type": "test_result", "goal_id": "g1", "step_id": "s2", "sequence": 4,
             "timestamp": 1.7e9, "payload": {"verdict": "fail"}},
        ]
        out = metrics.failure_breakdown(events)
        self.assertEqual(out["retries"], 2)
        self.assertEqual(out["recovered"], 1)
        self.assertEqual(out["recovery_rate"], 50)

    def test_recovery_is_paired_per_step_not_per_goal(self) -> None:
        """Step s1 recovered; s2's later pass is s1's, not its own — counting
        that as s2 recovering is how a broken loop reports a good record."""
        events = [
            {"type": "fix_retry", "goal_id": "g1", "step_id": "s1", "sequence": 1,
             "timestamp": 1.7e9, "payload": {}},
            {"type": "fix_retry", "goal_id": "g1", "step_id": "s2", "sequence": 2,
             "timestamp": 1.7e9, "payload": {}},
            {"type": "test_result", "goal_id": "g1", "step_id": "s1", "sequence": 3,
             "timestamp": 1.7e9, "payload": {"verdict": "pass"}},
        ]
        out = metrics.failure_breakdown(events)
        self.assertEqual((out["retries"], out["recovered"]), (2, 1))

    def test_a_second_retry_on_the_same_step_is_one_retry(self) -> None:
        events = [
            {"type": "fix_retry", "goal_id": "g1", "step_id": "s1", "sequence": 1,
             "timestamp": 1.7e9, "payload": {}},
            {"type": "fix_retry", "goal_id": "g1", "step_id": "s1", "sequence": 2,
             "timestamp": 1.7e9, "payload": {}},
            {"type": "test_result", "goal_id": "g1", "step_id": "s1", "sequence": 3,
             "timestamp": 1.7e9, "payload": {"verdict": "pass"}},
        ]
        out = metrics.failure_breakdown(events)
        self.assertEqual((out["retries"], out["recovered"]), (1, 1))

    def test_a_completed_step_counts_as_recovered_too(self) -> None:
        events = [
            {"type": "fix_retry", "goal_id": "g1", "step_id": "s1", "sequence": 1,
             "timestamp": 1.7e9, "payload": {}},
            {"type": "step_status", "goal_id": "g1", "step_id": "s1", "sequence": 2,
             "timestamp": 1.7e9, "payload": {"status": "COMPLETED"}},
        ]
        self.assertEqual(metrics.failure_breakdown(events)["recovered"], 1)


class UsageReportingProvider(BaseProvider):
    """A provider that reports usage, so the stage's token attribution has
    something real to sum. The MockProvider in `tests/test_design_role.py`
    reports nothing, which is a legitimate provider behaviour and would make
    every token number in this file read as zero."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.role: str | None = None

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
        if self.usage_sink is not None:
            self.usage_sink({"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
        return json.dumps(self.responses.get(self.role or "", {}))


class UsageFactory(ProviderFactory):
    """Routes by the role the registry asked for, the same way the real
    factory does — a provider that guessed from the prompt would attribute
    every call to whichever role's name the prompt happened to mention."""

    def __init__(self, provider: UsageReportingProvider, keychain: Keychain) -> None:
        super().__init__(keychain)
        self.provider = provider

    def build(self, config: AgentConfig) -> UsageReportingProvider:
        self.provider.role = config.role
        return self.provider


class _Rig(unittest.IsolatedAsyncioTestCase):
    RESPONSES: dict[str, Any] = {}

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.provider = UsageReportingProvider(dict(self.RESPONSES))
        self.registry = AgentRegistryService(
            self.conn, UsageFactory(self.provider, Keychain()), Keychain()
        )
        for role in ROLES:
            self.registry.set_config(role, AgentConfigUpdate(provider="ollama", model_name="m"))
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=SkippedGate()
        )
        self.ws = self.workspaces.create(
            WorkspaceCreate(name="WS", root_path=str(self.root))
        )

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def stages(self, goal_id: str) -> list[tuple[str, str]]:
        return [
            (str(ev.payload.get("stage")), str(ev.payload.get("outcome")))
            for ev in self.goals.events_after(goal_id, 0) if ev.type == "stage_result"
        ]


RESPONSES: dict[str, Any] = {
    "librarian": {"summary": "s", "files": [], "conventions": [], "enough": True},
    "design": {"applies": False},
    "planner": {"steps": [{"title": "S1", "description": "d", "suggested_paths": ["a.txt"]}]},
    "fixer": {"files": [{"path": "a.txt", "action": "create", "content": "hi\n"}]},
    "verifier": {"argv": None, "verdict": "pass", "explanation": "ok"},
    "critic": {"decision": "approve", "reasons": []},
    "scribe": {"summary": "did", "commit_message": "feat: a"},
}


class MeasuredRunCase(_Rig):
    """The whole point: a real run publishes what each stage achieved, with
    the spend the providers actually reported."""

    RESPONSES = RESPONSES

    async def test_a_complete_run_measures_every_stage_it_ran(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="")
        )
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, self.goals.get(goal.id).version, "RUNNING")
        await self.executor.run_step(goal.id, step.id)

        self.assertEqual(self.stages(goal.id), [
            ("laya", "skipped"),
            ("librarian", "pack"),
            ("design", "declined"),
            ("planner", "plan"),
            ("fixer", "wrote"),
            ("verifier", "pass"),
            ("critic", "approve"),
            ("scribe", "not_a_repo"),
        ])

    async def test_the_numbers_the_dashboard_renders_come_from_the_run(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="")
        )
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, self.goals.get(goal.id).version, "RUNNING")
        await self.executor.run_step(goal.id, step.id)

        events = [
            {"type": ev.type, "payload": ev.payload, "timestamp": ev.timestamp,
             "goal_id": goal.id, "step_id": ev.step_id}
            for ev in self.goals.events_after(goal.id, 0)
        ]
        rates = metrics.role_success_rate(events, now=time_now())
        self.assertEqual(rates["planner"]["success_rate"], 100)
        self.assertEqual(rates["design"]["success_rate"], 100, "declining is its job")
        self.assertEqual(rates["verifier"]["tokens"], 150, "the usage event it read")
        self.assertEqual(rates["scribe"]["tokens"], 150)
        self.assertEqual(rates["laya"]["tokens"], 0, "a skipped gate spends nothing")
        for stage in ("planner", "fixer", "verifier", "critic", "scribe"):
            self.assertIsNotNone(
                rates[stage]["avg_duration_ms"],
                f"{stage} was measured but published no duration",
            )

        rows = {r["stage"]: r for r in metrics.stage_costs(events, now=time_now())}
        self.assertEqual(sum(r["token_share"] for r in rows.values()), 100)
        self.assertEqual(rows["verifier"]["calls"], 1)

    async def test_a_step_that_fails_verification_is_measured_as_a_fail(self) -> None:
        """A verifier that returns `fail` and raises is a stage that worked; the
        mapping is what keeps its success rate honest."""
        self.provider.responses["verifier"] = {
            "argv": None, "verdict": "fail", "explanation": "tests are red",
        }
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="")
        )
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, self.goals.get(goal.id).version, "RUNNING")
        await self.executor.run_step(goal.id, step.id)

        stages = self.stages(goal.id)
        self.assertIn(("verifier", "fail"), stages)
        self.assertEqual(
            [s for s in stages if s[0] == "verifier"],
            [("verifier", "fail"), ("verifier", "fail")],
            "one failed verification and the one retry that failed again",
        )

    async def test_a_critic_that_asks_for_changes_is_measured_as_such(self) -> None:
        self.provider.responses["critic"] = {
            "decision": "request-changes", "reasons": ["no"],
        }
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="")
        )
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, self.goals.get(goal.id).version, "RUNNING")
        await self.executor.run_step(goal.id, step.id)

        self.assertIn(("critic", "request_changes"), self.stages(goal.id))
        self.assertEqual(self.goals.get(goal.id).status, "PAUSED")

    async def test_a_stage_that_cannot_run_is_measured_as_unavailable(self) -> None:
        """A role with no model configured is `unavailable`, which is a
        different problem from a role whose prompt needs work, and the two must
        not read as one number."""
        self.registry.set_config("design", AgentConfigUpdate(provider="ollama", model_name=""))
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="")
        )
        await self.executor.run_planning(goal.id)
        self.assertIn(("design", "unavailable"), self.stages(goal.id))
        self.assertEqual([s.title for s in self.goals.steps(goal.id)], ["S1"])

    async def test_a_librarian_stopped_by_its_cap_is_an_incomplete_pack(self) -> None:
        """The difference between "the workspace had nothing more" and "the
        engine stopped asking" is a different outcome, not a detail."""
        self.provider.responses["librarian"] = {
            "summary": "s", "files": [], "enough": False,
            "reads": ["a.txt"], "searches": ["x"], "git": [["log"]], "run": [["ls"]],
        }
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="")
        )
        await self.executor.run_planning(goal.id)
        self.assertIn(("librarian", "incomplete"), self.stages(goal.id))
        self.assertTrue(
            self.goals.events_after(goal.id, 0)[-1].payload.get("capped")
            or any(
                ev.type == "library_evidence" and ev.payload.get("capped")
                for ev in self.goals.events_after(goal.id, 0)
            )
        )

    async def test_a_replayed_step_is_measured_without_double_counting_the_fixer(
        self,
    ) -> None:
        self.provider.responses["planner"] = {
            "steps": [{"title": "S1", "description": "d", "suggested_paths": []}],
        }
        self.provider.responses["fixer"] = {
            "files": [{"path": "a.txt", "action": "create", "content": "hi\n"}],
        }
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", dry_run=True)
        )
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, self.goals.get(goal.id).version, "RUNNING")
        await self.executor.run_step(goal.id, step.id)
        self.assertTrue(
            self.goals.has_proposed_files(goal.id),
            "a dry run must have stored its proposal",
        )
        # The driver loop, not run_step, closes the goal out — Apply is only
        # legal from a terminal status.
        self.goals.update_status(goal.id, self.goals.get(goal.id).version, "COMPLETED")

        applied = await self.executor.apply_goal(goal.id)
        self.assertIn(("fixer", "replayed"), self.stages(goal.id))
        self.assertEqual(applied.status, "COMPLETED")


def time_now() -> float:
    import time

    return time.time()


if __name__ == "__main__":
    unittest.main()
