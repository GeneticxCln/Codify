"""`scripts/drive_a_turn.py` counts what a real model did, not only what it said (audit of 2026-09-29, 3.4).

Reading a transcript tells a person whether one turn went well. A baseline is a rate — how often the
conductor called a tool the engine had to refuse, how often a role's reply had to be asked for again, how
many turns ended in an answer, a plan, or a failure — and rates come from counting the run's own events,
which is what `turn_tally` does. It is a pure function over the events the ordinary `/goals/{id}/events`
route returns, so it is tested on events shaped exactly as the engine publishes them.
"""

from __future__ import annotations

import unittest
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from scripts.drive_a_turn import run_tally, turn_tally


def event(type_: str, **payload: Any) -> dict[str, Any]:
    return {"type": type_, "payload": payload}


class TestTurnTally(unittest.TestCase):
    def test_conductor_tool_calls_are_counted_by_name(self) -> None:
        tally = turn_tally([
            event("log", level="info", message="conductor called read_file({'path': 'a.py'})"),
            event("log", level="info", message="conductor called read_file({'path': 'b.py'})"),
            event("log", level="info", message="conductor called plan({})"),
            event("log", level="info", message="something else entirely"),
        ], "PENDING")

        self.assertEqual({"read_file": 2, "plan": 1}, tally["tools"])

    def test_failed_calls_are_counted_by_code_and_reasks_separately(self) -> None:
        tally = turn_tally([
            event("agent_call_failed", role="fixer", code="agent_output_invalid", retrying=True),
            event("agent_call_failed", role="fixer", code="agent_output_invalid"),
            event("agent_call_failed", role="critic", code="provider_http"),
        ], "FAILED")

        self.assertEqual({"agent_output_invalid": 2, "provider_http": 1}, tally["failed_calls"])
        self.assertEqual(1, tally["reasks"])

    def test_fallbacks_and_error_codes_are_counted(self) -> None:
        tally = turn_tally([
            event("provider_fallback", role="planner"),
            event("error", code="agent_output_invalid", message="x"),
        ], "FAILED")

        self.assertEqual(1, tally["fallbacks"])
        self.assertEqual(["agent_output_invalid"], tally["errors"])

    def test_the_outcome_is_an_answer_a_plan_or_a_failure_and_never_a_guess(self) -> None:
        answer = [event("log", level="info", message="It prints hello.", turn=True)]

        self.assertEqual("answered", turn_tally(answer, "COMPLETED")["outcome"])
        self.assertEqual("planned", turn_tally([], "PENDING")["outcome"])
        self.assertEqual("failed", turn_tally([], "FAILED")["outcome"])
        self.assertEqual("cancelled", turn_tally([], "CANCELLED")["outcome"])
        # Completed with nothing said is not an answer: it is the silent turn the script exits 1 for.
        self.assertEqual("silent", turn_tally([], "COMPLETED")["outcome"])

    def test_no_events_is_an_empty_tally_not_an_error(self) -> None:
        self.assertEqual(
            {"outcome": "silent", "tools": {}, "failed_calls": {}, "reasks": 0, "fallbacks": 0, "errors": []},
            turn_tally([], "COMPLETED"),
        )


if __name__ == "__main__":
    unittest.main()


class TestRunTally(unittest.TestCase):
    """What the conductor did *after* Start: `--approve` presses it and counts how the run ended.

    A turn ends in an answer or a plan; a run ends finished, paused with a reason, or failed. The pause code is
    the part worth counting, because a model that cannot finish a step is the rate a baseline is after
    (`docs/09` §10.14), and the code says which way it fell short.
    """

    def test_a_finished_run_is_completed_and_counts_the_moves_it_made(self) -> None:
        tally = run_tally([
            event("log", level="info", message="conductor called write({'step_id': 's1'})"),
            event("log", level="info", message="conductor called verify({'step_id': 's1'})"),
            event("log", level="info", message="conductor called review({'step_id': 's1'})"),
            event("log", level="info", message="conductor called summarize({'step_id': 's1'})"),
        ], "COMPLETED")

        self.assertEqual("completed", tally["outcome"])
        self.assertEqual({"write": 1, "verify": 1, "review": 1, "summarize": 1}, tally["tools"])
        self.assertIsNone(tally["pause"])

    def test_a_paused_run_says_which_way_it_fell_short(self) -> None:
        tally = run_tally([
            event("goal_status", status="RUNNING", version=3),
            event("goal_status", status="PAUSED", version=4, reason_code="conductor_budget", reason="It used its calls."),
        ], "PAUSED")

        self.assertEqual("paused:conductor_budget", tally["outcome"])
        self.assertEqual({"code": "conductor_budget", "reason": "It used its calls."}, tally["pause"])

    def test_the_newest_pause_is_the_one_that_counts(self) -> None:
        tally = run_tally([
            event("goal_status", status="PAUSED", version=4, reason_code="conductor_budget", reason="first"),
            event("goal_status", status="RUNNING", version=5),
            event("goal_status", status="PAUSED", version=6, reason_code="critic_rejected", reason="second"),
        ], "PAUSED")

        self.assertEqual("paused:critic_rejected", tally["outcome"])

    def test_a_pause_the_person_made_has_no_code_and_says_so(self) -> None:
        tally = run_tally([event("goal_status", status="PAUSED", version=4)], "PAUSED")

        self.assertEqual("paused", tally["outcome"])
        self.assertIsNone(tally["pause"])

    def test_a_failed_run_is_failed_and_keeps_its_error_codes(self) -> None:
        tally = run_tally([event("error", code="provider_error", message="x")], "FAILED")

        self.assertEqual("failed", tally["outcome"])
        self.assertEqual(["provider_error"], tally["errors"])

