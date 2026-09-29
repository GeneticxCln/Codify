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

from scripts.drive_a_turn import turn_tally


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
