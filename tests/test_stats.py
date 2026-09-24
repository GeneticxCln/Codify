"""The cross-goal statistics arithmetic, tested directly.

`engine/stats.py` is pure — no DB, no HTTP, and no clock *calls*; the wall
clock is injected as `now` — so these tests build plain goal/event dicts and
pin the numbers exactly. The honesty rules are the point: what counts as
success, what a window includes, and what an unmeasured duration must never
claim to be.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import unittest
from typing import Any

from engine.stats import (
    build_overview,
    daily_trend,
    normalize_window,
    summarize_goals,
    summarize_usage,
)


NOW = 1_800_000_000.0  # a fixed anchor; without an explicit `now`, windows derive it from the data


def goal(status: str, created: float, updated: float | None = None) -> dict[str, Any]:
    return {
        "id": f"g-{status}-{created}",
        "status": status,
        "created_at": created,
        "updated_at": updated if updated is not None else created,
    }


def usage_ev(role: str, provider: str, model: str, tokens: int, ts: float,
             duration_ms: float | None = 100) -> dict[str, Any]:
    return {
        "type": "usage",
        "timestamp": ts,
        "payload": {
            "role": role,
            "provider": provider,
            "model": model,
            "input_tokens": tokens,
            "output_tokens": tokens // 2,
            "total_tokens": tokens + tokens // 2,
            **({"duration_ms": duration_ms} if duration_ms is not None else {}),
        },
    }


def failure_ev(role: str, provider: str, model: str, code: str, ts: float) -> dict[str, Any]:
    return {
        "type": "agent_call_failed",
        "timestamp": ts,
        "payload": {
            "role": role, "provider": provider, "model": model,
            "target": "primary", "code": code, "message": code,
            "duration_ms": 50,
        },
    }


class TestSuccessRate(unittest.TestCase):
    def test_only_completed_counts_as_success(self) -> None:
        """Success is COMPLETED. A cancelled goal was stopped, not finished —
        folding it into the rate would flatter a setup that keeps getting
        interrupted, which is the opposite of what a rate is for."""
        goals = [
            goal("COMPLETED", NOW - 100),
            goal("COMPLETED", NOW - 90),
            goal("FAILED", NOW - 80),
            goal("CANCELLED", NOW - 70),
        ]
        out = summarize_goals(goals, window_days=0)
        self.assertEqual(out["succeeded"], 2)
        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["cancelled"], 1)
        self.assertEqual(out["success_rate"], 50)

    def test_active_goals_count_in_totals_but_never_in_the_rate(self) -> None:
        goals = [
            goal("COMPLETED", NOW - 100),
            goal("RUNNING", NOW - 10),
            goal("PLANNING", NOW - 5),
            goal("PAUSED", NOW - 3),
            goal("PENDING", NOW - 2),
        ]
        out = summarize_goals(goals, window_days=0)
        self.assertEqual(out["goals"], 5)
        self.assertEqual(out["active"], 4)
        self.assertEqual(out["succeeded"], 1)
        self.assertEqual(out["success_rate"], 100, "one terminal goal, and it succeeded")

    def test_no_terminal_goals_means_no_rate(self) -> None:
        out = summarize_goals([goal("RUNNING", NOW - 1)], window_days=0)
        self.assertIsNone(out["success_rate"], "a rate over zero terminal goals is a claim about nothing")

    def test_window_excludes_older_goals(self) -> None:
        goals = [
            goal("COMPLETED", NOW - 8 * 86400, updated=NOW - 8 * 86400 + 60),
            goal("COMPLETED", NOW - 1 * 86400, updated=NOW - 1 * 86400 + 60),
        ]
        out = summarize_goals(goals, window_days=7)
        self.assertEqual(out["goals"], 1)
        self.assertEqual(out["succeeded"], 1)

    def test_all_time_window_has_no_cutoff(self) -> None:
        goals = [goal("FAILED", NOW - 400 * 86400)]
        out = summarize_goals(goals, window_days=0)
        self.assertEqual(out["goals"], 1)


class TestUsageAggregation(unittest.TestCase):
    def test_tokens_split_per_role_and_per_model(self) -> None:
        events = [
            usage_ev("fixer", "anthropic", "claude", 100, NOW - 30, duration_ms=1200),
            usage_ev("fixer", "ollama", "qwen", 10, NOW - 20, duration_ms=8),
            usage_ev("planner", "anthropic", "claude", 50, NOW - 10, duration_ms=600),
        ]
        # 100 + 50 + 10 in / 50 + 25 + 5 out = 240 total.
        out = summarize_usage(events, window_days=0)
        self.assertEqual(out["calls"], 3)
        self.assertEqual(out["total_tokens"], 240)
        self.assertEqual(out["by_role"]["fixer"]["total_tokens"], 165)
        self.assertEqual(out["by_model"]["anthropic/claude"]["total_tokens"], 225)
        self.assertEqual(out["by_model"]["anthropic/claude"]["calls"], 2)

    def test_average_duration_averages_only_measured_calls(self) -> None:
        events = [
            usage_ev("fixer", "p", "m", 10, NOW - 3, duration_ms=100),
            usage_ev("fixer", "p", "m", 10, NOW - 2, duration_ms=300),
            usage_ev("fixer", "p", "m", 10, NOW - 1, duration_ms=None),
        ]
        out = summarize_usage(events, window_days=0)
        self.assertEqual(out["calls"], 3)
        self.assertEqual(out["avg_duration_ms"], 200, "average of the measured calls only")

    def test_no_durations_means_none_never_zero(self) -> None:
        """Events written before durations existed must read as unknown. A 0ms
        average would be a false claim about a real engine."""
        events = [usage_ev("scribe", "p", "m", 5, NOW - 1, duration_ms=None)]
        out = summarize_usage(events, window_days=0)
        self.assertIsNone(out["avg_duration_ms"])

    def test_failures_are_counted_and_never_add_tokens(self) -> None:
        events = [
            usage_ev("fixer", "p", "m", 10, NOW - 3, duration_ms=100),
            failure_ev("fixer", "p", "m", "provider_http", NOW - 2),
            failure_ev("scribe", "p", "m", "provider_unreachable", NOW - 1),
        ]
        out = summarize_usage(events, window_days=0)
        self.assertEqual(out["calls"], 1, "a failed call is not a usage call")
        self.assertEqual(out["failures"], 2)
        self.assertEqual(out["total_tokens"], 15)
        self.assertEqual(out["by_role"]["scribe"]["calls"], 1)
        self.assertEqual(out["by_role"]["scribe"]["total_tokens"], 0)
        # A role with only failures has no measured duration — none, not zero.
        self.assertIsNone(out["by_role"]["scribe"]["avg_duration_ms"])

    def test_window_excludes_older_events(self) -> None:
        events = [
            usage_ev("fixer", "p", "m", 1000, NOW - 20 * 86400, duration_ms=1),
            usage_ev("fixer", "p", "m", 10, NOW - 1 * 86400, duration_ms=2),
        ]
        out = summarize_usage(events, window_days=7)
        self.assertEqual(out["calls"], 1)
        self.assertEqual(out["total_tokens"], 15)


class TestDailyTrend(unittest.TestCase):
    def test_rows_are_sparse_sorted_and_split_created_from_outcome(self) -> None:
        day0 = NOW - 2 * 86400  # creation day
        day1 = NOW - 86400      # outcome day (same goal, finished later)
        goals = [goal("COMPLETED", day0, updated=day1)]
        events = [usage_ev("fixer", "p", "m", 30, day1, duration_ms=10)]
        out = build_overview(goals, events, window_days=0)
        daily = out["daily"]
        self.assertEqual([r["date"] for r in daily], sorted(r["date"] for r in daily))
        created_row = next(r for r in daily if r["created"] == 1)
        done_row = next(r for r in daily if r["succeeded"] == 1)
        self.assertIsNot(created_row, done_row, "a goal crossing midnight counts on each day it happened")
        self.assertEqual(done_row["total_tokens"], 45)

    def test_failed_and_cancelled_landing_days(self) -> None:
        day = NOW - 86400
        goals = [goal("FAILED", day), goal("CANCELLED", day + 60)]
        out = build_overview(goals, [], window_days=0)
        row = next(r for r in out["daily"] if r["failed"] or r["cancelled"])
        self.assertEqual(row["failed"], 1)
        self.assertEqual(row["cancelled"], 1)


class TestWindowHandling(unittest.TestCase):
    def test_normalize_accepts_choices_and_clamps_nonsense(self) -> None:
        self.assertEqual(normalize_window(1), 1)
        self.assertEqual(normalize_window(7), 7)
        self.assertEqual(normalize_window(30), 30)
        self.assertEqual(normalize_window(0), 0)
        self.assertEqual(normalize_window(13), 7)
        self.assertEqual(normalize_window("bogus"), 7)

    def test_overview_carries_window_and_generated_shape(self) -> None:
        out = build_overview([goal("COMPLETED", NOW)], [], window_days=7)
        self.assertEqual(out["window_days"], 7)
        self.assertIn("goals", out)
        self.assertIn("usage", out)
        self.assertIn("daily", out)

    def test_empty_history_is_all_zeros_and_no_claims(self) -> None:
        out = build_overview([], [], window_days=0)
        self.assertEqual(out["goals"]["goals"], 0)
        self.assertIsNone(out["goals"]["success_rate"])
        self.assertIsNone(out["usage"]["avg_duration_ms"])
        self.assertEqual(out["daily"], [])


class TestWallClockWindowAnchor(unittest.TestCase):
    """A window is "the last N days", not "the last N days of whatever ran".

    The fallback anchor (newest row in the data) exists for tests building a
    synthetic timeline. On a real request the endpoint injects `now`, and this
    is the contract that has to hold: an install that has been idle for months
    asking for 24h must see an EMPTY window, not its last run relabelled as
    recent with a 100% success rate.
    """

    STALE = 90 * 86400  # 90 days ago

    def test_idle_store_reports_an_empty_24h_window(self) -> None:
        goals = [goal("COMPLETED", NOW - self.STALE)]
        out = summarize_goals(goals, window_days=1, now=NOW)

        self.assertEqual(out["goals"], 0, "a 90-day-old goal is not in the last 24 hours")
        self.assertEqual(out["succeeded"], 0)
        # The rate is the claim that would survive a screenshot of the card.
        self.assertIsNone(
            out["success_rate"],
            "reporting 100% for a 90-day-old goal is the exact false claim this fixes",
        )

    def test_wall_clock_anchor_differs_from_the_data_anchor(self) -> None:
        """The bug itself, asserted in both modes so the fix cannot regress."""
        goals = [goal("COMPLETED", NOW - self.STALE)]

        stale_claim = summarize_goals(goals, window_days=1)
        self.assertEqual(
            stale_claim["goals"], 1,
            "the no-`now` fallback still slides the window onto stale data "
            "(tests rely on this; live callers must pass now)",
        )

        honest_claim = summarize_goals(goals, window_days=1, now=NOW)
        self.assertEqual(honest_claim["goals"], 0, "an injected wall clock empties the window")

    def test_recent_goals_are_still_counted_with_a_wall_clock(self) -> None:
        """The fix must not empty windows that genuinely have work in them."""
        goals = [
            goal("COMPLETED", NOW - 3600, updated=NOW - 3600),
            goal("FAILED", NOW - 2 * 3600, updated=NOW - 2 * 3600),
            goal("COMPLETED", NOW - 8 * 86400, updated=NOW - 8 * 86400),
        ]
        out = summarize_goals(goals, window_days=7, now=NOW)

        self.assertEqual(out["goals"], 2, "the 8-day-old goal is outside a 7-day window")
        self.assertEqual(out["succeeded"], 1)
        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["success_rate"], 50)

    def test_idle_store_reports_no_spend(self) -> None:
        events = [usage_ev("fixer", "p", "m", 1000, NOW - self.STALE, duration_ms=5)]
        out = summarize_usage(events, window_days=7, now=NOW)

        self.assertEqual(out["calls"], 0)
        self.assertEqual(out["total_tokens"], 0)
        self.assertIsNone(out["avg_duration_ms"])
        self.assertEqual(out["by_role"], {})

    def test_recent_spend_is_still_counted_with_a_wall_clock(self) -> None:
        events = [
            usage_ev("fixer", "p", "m", 1000, NOW - 60, duration_ms=5),
            usage_ev("fixer", "p", "m", 999, NOW - self.STALE, duration_ms=9),
        ]
        out = summarize_usage(events, window_days=7, now=NOW)

        self.assertEqual(out["calls"], 1, "only the recent call belongs in the window")
        self.assertEqual(out["total_tokens"], 1500)
        self.assertEqual(out["avg_duration_ms"], 5)

    def test_daily_trend_drops_stale_days(self) -> None:
        goals = [goal("COMPLETED", NOW - self.STALE, updated=NOW - self.STALE)]
        self.assertEqual(
            daily_trend(goals, [], window_days=7, now=NOW), [],
            "the chart must not draw a 90-day-old day inside a 7-day window",
        )
        self.assertNotEqual(daily_trend(goals, [], window_days=7), [])

    def test_all_time_ignores_the_anchor_entirely(self) -> None:
        """window=0 means "everything", so a stale store still reports."""
        goals = [goal("COMPLETED", NOW - 400 * 86400)]
        self.assertEqual(summarize_goals(goals, window_days=0, now=NOW)["goals"], 1)

    def test_overview_anchors_all_three_sections_consistently(self) -> None:
        """One `now` for goals, spend, and the chart — never a mixed window."""
        goals = [goal("COMPLETED", NOW - self.STALE, updated=NOW - self.STALE)]
        events = [usage_ev("fixer", "p", "m", 1000, NOW - self.STALE, duration_ms=5)]
        out = build_overview(goals, events, window_days=7, now=NOW)

        self.assertEqual(out["goals"]["goals"], 0)
        self.assertEqual(out["usage"]["calls"], 0)
        self.assertEqual(out["daily"], [])


if __name__ == "__main__":
    unittest.main()
