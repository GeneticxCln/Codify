"""Daily stats snapshots: the trend that survives a restart.

The stats overview recomputes from the live log; these tests pin the boundary
where a day's final numbers freeze into a row that outlives it — what triggers
the freeze, what does not, and what a reader is promised about the frozen
document. The freeze also has to be trigger-agnostic: a read endpoint, a test,
and a future cron must all be able to call `maybe_snapshot` and get the same
decision for the same data.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.db import connect
from engine import stats_history
from engine.stats_history import StatsSnapshotService, active_days, utc_day
from typing import Any

# A day/hour clock anchored at 00:30 UTC so `at(n, h)` provably stays inside
# calendar day n for every h in 0..23. The freeze boundary is a *calendar*
# boundary, and the tests have to say which side of it each timestamp is on —
# an anchor at any other hour silently shifts half the scenarios into the
# next day and the tests start pinning the wrong thing.
BASE = 1_789_939_800.0  # 2026-09-21 00:30:00 UTC (a Monday)


def at(day: int, hour: int = 12) -> float:
    return BASE + day * 86400.0 + hour * 3600.0


def goal(status: str, created: float, updated: float | None = None) -> dict[str, Any]:
    return {
        "id": f"g-{status}-{created}",
        "status": status,
        "created_at": created,
        "updated_at": updated if updated is not None else created,
    }


def usage_ev(ts: float, tokens: int = 10) -> dict[str, Any]:
    return {
        "type": "usage",
        "timestamp": ts,
        "payload": {
            "role": "fixer", "provider": "p", "model": "m",
            "input_tokens": tokens, "output_tokens": tokens // 2,
            "total_tokens": tokens + tokens // 2, "duration_ms": 5,
        },
    }


class StatsHistoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.temp_dir.name) / "t.db")
        self.snap = StatsSnapshotService(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def stored_days(self) -> list[str]:
        """The days that have a row, oldest first, whatever the rows hold.

        Read from the table: the service's own reader is `history()`, which skips a row it cannot parse, and a test
        about what is *stored* has to see that row too.
        """
        return [row["day"] for row in self.conn.execute("SELECT day FROM stats_snapshots ORDER BY day")]

    def stored_day(self, day: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT document FROM stats_snapshots WHERE day = ?", (day,)).fetchone()
        if row is None:
            return None
        document: dict[str, Any] = json.loads(row["document"])
        return document


class TestMaybeSnapshot(StatsHistoryTestCase):
    def test_first_read_after_a_day_ends_freezes_that_day(self) -> None:
        # All of day 0's activity is inside day 0; the read happens on day 1.
        goals = [goal("COMPLETED", at(0, 2), updated=at(0, 9))]
        events = [usage_ev(at(0, 15), tokens=30)]
        frozen = self.snap.maybe_snapshot(self.conn, goals, events, now=at(1, 8))
        assert frozen is not None, "day 0 is over, so the read freezes it"
        self.assertEqual(frozen, utc_day(at(0, 15)))
        doc = self.stored_day(frozen)
        assert doc is not None, "the day just frozen is readable"
        self.assertEqual(doc["goals"]["succeeded"], 1)
        self.assertEqual(doc["usage"]["total_tokens"], 45, "the frozen document is the full overview")

    def test_a_frozen_document_does_not_move_when_the_log_is_pruned(self) -> None:
        """The point of the table: yesterday's numbers survive losing the log
        they were computed from."""
        goals = [goal("COMPLETED", at(0, 2))]
        events = [usage_ev(at(0, 15), tokens=99)]
        day = self.snap.maybe_snapshot(self.conn, goals, events, now=at(1, 8))
        assert day is not None, "day 0 had activity, so it freezes"
        # The details are gone; only the frozen row remembers them.
        doc = self.stored_day(day)
        assert doc is not None, "the frozen row survives losing its source events"
        self.assertEqual(doc["usage"]["total_tokens"], 148)

    def test_nothing_freezes_while_activity_is_still_today(self) -> None:
        now = at(1, 8)
        goals = [goal("RUNNING", now - 100, updated=now - 10)]
        events = [usage_ev(now - 5)]
        self.assertIsNone(self.snap.maybe_snapshot(self.conn, goals, events, now=now))
        self.assertEqual(self.stored_days(), [])

    def test_empty_history_freezes_nothing(self) -> None:
        self.assertIsNone(self.snap.maybe_snapshot(self.conn, [], [], now=at(1)))

    def test_freeze_is_idempotent(self) -> None:
        goals = [goal("FAILED", at(0, 2))]
        events = [usage_ev(at(0, 15))]
        first = self.snap.maybe_snapshot(self.conn, goals, events, now=at(1, 8))
        self.assertIsNotNone(first)
        # The fixture spans two UTC days (goal late on day 0, event on day 1),
        # so backfill freezes both — the older one must not be left unfrozen.
        self.assertEqual(self.stored_days(), sorted(self.stored_days()))
        self.assertEqual(len(self.stored_days()), 2)
        self.assertEqual(self.stored_days()[-1], first)
        # A second read the same day finds the rows already frozen: nothing is
        # rewritten, and the contract reports "nothing to do".
        second = self.snap.maybe_snapshot(self.conn, goals, events, now=at(1, 20))
        self.assertIsNone(second)
        self.assertEqual(len(self.stored_days()), 2)
        # And the frozen numbers are the first freeze's, not a re-write.
        assert first is not None, "the backfill froze the older day"
        frozen_doc = self.stored_day(first)
        assert frozen_doc is not None
        self.assertEqual(frozen_doc["usage"]["calls"], 1)

    def test_a_day_with_no_activity_gets_no_row(self) -> None:
        """Sparse by design: a gap in the days means nothing happened, which the
        reader can infer without a table of zeroes claiming it as fact."""
        self.snap.maybe_snapshot(
            self.conn,
            [goal("COMPLETED", at(0, 2))],
            [usage_ev(at(0, 15))],
            now=at(1, 8),
        )
        # Two days later, with nothing in between: only the active days exist.
        self.snap.maybe_snapshot(
            self.conn,
            [goal("COMPLETED", at(3, 2))],
            [usage_ev(at(3, 15))],
            now=at(4, 8),
        )
        # Each fixture spans two UTC days, so backfill freezes both active days
        # per call — four rows, and no zeroed row for the gap in between.
        self.assertEqual(len(self.stored_days()), 4)


class TestFindingTheActiveDays(StatsHistoryTestCase):
    """`maybe_snapshot` runs on every stats read and used to format a date for every event it was handed.

    Up to 20,000 events, a `datetime` and a `strftime` each, on the event loop, on every call — 40 to 100 ms
    measured at the cap, on reads where every past day was already frozen and there was nothing to do. Found
    while moving the stats routes off the loop (`docs/03` §3 4.3), whose remaining stall it was.
    """

    def test_a_read_with_nothing_to_freeze_formats_a_date_per_day_not_per_event(self) -> None:
        goals = [goal("COMPLETED", at(0, 2))]
        events = [usage_ev(at(0, 3 + i % 20)) for i in range(5000)] + [usage_ev(at(1, 5))]
        self.snap.maybe_snapshot(self.conn, goals, events, now=at(2, 8))  # freezes days 0 and 1
        calls = 0
        real = stats_history.utc_day

        def counting(ts: float) -> str:
            nonlocal calls
            calls += 1
            return real(ts)

        with patch.object(stats_history, "utc_day", counting):
            again = self.snap.maybe_snapshot(self.conn, goals, events, now=at(2, 20))

        self.assertIsNone(again, "everything was already frozen")
        self.assertLessEqual(calls, 10, f"{calls} dates were formatted for 5,001 events on two days")

    def test_the_days_are_the_ones_reading_every_timestamp_would_find(self) -> None:
        # Midnights, the milliseconds either side of them, the epoch, a missing stamp and one before it.
        stamps = [0.0, 86399.999, 86400.0, 86400.001, -1.0, -86400.0, -86400.001, at(0, 0), at(0, 23),
                  at(1, 0), at(1, 23), at(400, 11)]
        goals = [goal("COMPLETED", ts) for ts in stamps[:6]]
        events: list[dict[str, Any]] = [usage_ev(ts) for ts in stamps[6:]]
        events.append({"type": "usage", "timestamp": None, "payload": {}})

        found = active_days(goals, events)

        expected = sorted({utc_day(g["created_at"]) for g in goals}
                          | {utc_day(e["timestamp"] or 0.0) for e in events})
        self.assertEqual(expected, found)

    def test_a_goal_is_placed_by_when_it_was_last_touched(self) -> None:
        # `updated_at` wins over `created_at`, and a goal with neither is on the epoch's day.
        goals: list[dict[str, Any]] = [
            {"id": "a", "status": "COMPLETED", "created_at": at(0, 2), "updated_at": at(3, 2)},
            {"id": "b", "status": "COMPLETED", "created_at": None, "updated_at": None},
        ]

        self.assertEqual(sorted({utc_day(at(3, 2)), utc_day(0.0)}), active_days(goals, []))


class TestHistory(StatsHistoryTestCase):
    def test_days_are_oldest_first_and_carry_full_documents(self) -> None:
        for offset, tokens in ((0, 30), (2, 50)):
            self.snap.maybe_snapshot(
                self.conn,
                [goal("COMPLETED", at(offset, 2))],
                [usage_ev(at(offset, 15), tokens=tokens)],
                now=at(offset + 1, 8),
            )
        days = self.stored_days()
        self.assertEqual(days, sorted(days))
        history = self.snap.history()
        self.assertEqual([h["day"] for h in history], days)
        # Each fixture spans two UTC days, and each day's document is the numbers as *that day* ended: the
        # first day of a pair has its goal and no model call yet, the second has the call. They used to be
        # one document frozen twice, which gave the first day spend that had not happened.
        self.assertEqual(history[0]["document"]["usage"]["total_tokens"], 0)
        self.assertEqual(history[1]["document"]["usage"]["total_tokens"], 45)
        self.assertEqual(history[2]["document"]["usage"]["total_tokens"], 0)
        self.assertEqual(history[3]["document"]["usage"]["total_tokens"], 75)

    def test_a_corrupt_document_is_skipped_not_fatal(self) -> None:
        self.conn.execute(
            "INSERT INTO stats_snapshots (day, document, created_at) VALUES (?, ?, ?)",
            ("2030-01-01", "{not json", 0.0),
        )
        self.conn.commit()
        self.assertEqual(self.snap.history(), [], "one bad row must not take the trend down")


class TestPrune(StatsHistoryTestCase):
    def _freeze_days(self, days: list[str]) -> None:
        """Insert snapshot rows directly — pruning is a table concern, and the
        freeze boundary is another test's job."""
        for i, day in enumerate(days):
            self.conn.execute(
                "INSERT INTO stats_snapshots (day, document, created_at) VALUES (?, ?, ?)",
                (day, json.dumps({"goals": {"goals": i}}), 1000.0 + i),
            )
        self.conn.commit()

    def test_keeps_the_newest_n_rows(self) -> None:
        self._freeze_days(["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"])
        deleted = self.snap.prune(2)
        self.assertEqual(deleted, 2)
        self.assertEqual(self.stored_days(), ["2026-09-03", "2026-09-04"], "the newest survive")

    def test_zero_keeps_everything(self) -> None:
        self._freeze_days(["2026-09-01", "2026-09-02"])
        self.assertEqual(self.snap.prune(0), 0)
        self.assertEqual(len(self.stored_days()), 2)

    def test_pruning_more_than_exists_changes_nothing(self) -> None:
        self._freeze_days(["2026-09-01"])
        self.assertEqual(self.snap.prune(30), 0)
        self.assertEqual(len(self.stored_days()), 1)

    def test_deletion_is_by_count_not_age_so_an_idle_machine_prunes_nothing(self) -> None:
        """Rows from months ago survive when they are among the newest N: the
        policy bounds the table, not the calendar."""
        self._freeze_days(["2025-01-01", "2026-09-01"])
        self.snap.prune(2)
        self.assertEqual(len(self.stored_days()), 2, "two rows, policy of 2 — nothing to do")

    def test_prune_tolerates_rows_a_corrupt_document_would_have_blocked(self) -> None:
        # Prune is a table operation; it must not care whether documents parse.
        self._freeze_days(["2026-09-01", "2026-09-02", "2026-09-03"])
        self.conn.execute("UPDATE stats_snapshots SET document = '{bad' WHERE day = '2026-09-02'")
        self.conn.commit()
        self.assertEqual(self.snap.prune(1), 2)
        self.assertEqual(self.stored_days(), ["2026-09-03"])


if __name__ == "__main__":
    unittest.main()


# Real UTC midnights. `BASE` above is not one (it sits at 21:30 UTC the evening before, which is why its
# fixtures straddle two days), and the tests below are about which side of a day's end things fall on.
MIDNIGHT = 1_789_948_800.0  # 2026-09-21 00:00:00 UTC


def noon(day: int) -> float:
    return MIDNIGHT + day * 86400.0 + 12 * 3600.0


class TestADayIsFrozenAsItEnded(StatsHistoryTestCase):
    def test_the_fixture_clock_is_a_midnight(self) -> None:
        self.assertEqual(utc_day(MIDNIGHT), "2026-09-21")
        self.assertEqual(utc_day(MIDNIGHT - 1), "2026-09-20")

    def test_a_day_older_than_the_trend_span_still_has_its_own_row(self) -> None:
        """A backfilled day's document came from everything, and the trend only carries the newest 30 days, so a
        day older than that was frozen without its own row and the chart read "0 started" for real goals."""
        goals = [goal("COMPLETED", noon(0) + i) for i in range(3)] + [goal("COMPLETED", noon(50))]
        events = [usage_ev(noon(0) + 5), usage_ev(noon(50))]
        self.snap.maybe_snapshot(self.conn, goals, events, now=noon(51))
        first = self.snap.history()[0]
        self.assertEqual(first["day"], utc_day(noon(0)))
        row = next((r for r in first["document"]["daily"] if r["date"] == first["day"]), None)
        assert row is not None, "the day's own row is missing from its own document"
        self.assertEqual(row["created"], 3)
        self.assertEqual(row["calls"], 1)

    def test_yesterdays_document_does_not_hold_what_happened_today(self) -> None:
        goals = [goal("COMPLETED", noon(0)), goal("RUNNING", noon(1))]
        events = [usage_ev(noon(0), tokens=20), usage_ev(noon(1), tokens=200)]
        frozen = self.snap.maybe_snapshot(self.conn, goals, events, now=noon(1) + 600)
        assert frozen is not None, "yesterday had activity, so it freezes"
        self.assertEqual(frozen, utc_day(noon(0)))
        doc = self.stored_day(frozen)
        assert doc is not None
        self.assertEqual(doc["goals"]["goals"], 1, "today's goal is in yesterday's frozen numbers")
        self.assertEqual(doc["usage"]["calls"], 1, "today's model call is in yesterday's frozen numbers")

    def test_a_goal_that_finished_after_the_day_is_still_in_flight_in_that_days_document(self) -> None:
        """The log keeps a goal's last status, not the one it had that evening, so a goal that ended on day 2 is not
        counted as a success in day 0's numbers."""
        goals = [goal("COMPLETED", noon(0), updated=noon(2))]
        events = [usage_ev(noon(0)), usage_ev(noon(2))]  # a goal is placed by its last change, so day 0 needs a call
        self.snap.maybe_snapshot(self.conn, goals, events, now=noon(3))
        by_day = {h["day"]: h["document"] for h in self.snap.history()}
        first = by_day[utc_day(noon(0))]["goals"]
        self.assertEqual((first["succeeded"], first["active"]), (0, 1))
        last = by_day[utc_day(noon(2))]["goals"]
        self.assertEqual((last["succeeded"], last["active"]), (1, 0))


class TestRetentionDoesNotCauseRework(StatsHistoryTestCase):
    def history_for(self, days: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return (
            [goal("COMPLETED", noon(d)) for d in range(days)],
            [usage_ev(noon(d)) for d in range(days)],
        )

    def test_a_day_the_policy_would_prune_is_not_frozen_to_be_pruned(self) -> None:
        """With more active days than the retention keeps, every read froze the older ones and `prune` deleted them
        again, so the next read froze them again: about 25 seconds of thrown-away work per Stats open at 400 days."""
        goals, events = self.history_for(10)
        now = noon(10)
        self.assertEqual(len(self.snap.pending_days(goals, events, now, keep=3)), 3, "only the newest 3 are candidates")
        self.snap.maybe_snapshot(self.conn, goals, events, now, keep=3)
        self.snap.prune(3)
        self.assertEqual(len(self.stored_days()), 3)
        self.assertEqual(self.snap.pending_days(goals, events, now, keep=3), [], "the second read has nothing to redo")

    def test_a_lowered_policy_prunes_once_and_nothing_comes_back(self) -> None:
        goals, events = self.history_for(8)
        now = noon(8)
        self.snap.maybe_snapshot(self.conn, goals, events, now)  # kept everything so far
        self.assertEqual(len(self.stored_days()), 8)
        self.snap.prune(3)
        self.assertEqual(len(self.stored_days()), 3)
        self.assertEqual(self.snap.pending_days(goals, events, now, keep=3), [], "the 5 pruned days are asked for again")

    def test_no_policy_keeps_asking_for_every_unfrozen_day(self) -> None:
        goals, events = self.history_for(5)
        self.assertEqual(len(self.snap.pending_days(goals, events, noon(5), keep=0)), 5)
        self.assertEqual(len(self.snap.pending_days(goals, events, noon(5), keep=None)), 5)

    def test_the_newest_days_go_first_and_a_read_owes_at_most_a_month(self) -> None:
        goals, events = self.history_for(40)
        now = noon(40)
        first = self.snap.pending_days(goals, events, now)
        self.assertEqual(len(first), stats_history.MAX_FREEZE_PER_READ)
        self.assertEqual(first[0], utc_day(noon(39)), "the newest day is first, so the chart is useful at once")
        self.snap.maybe_snapshot(self.conn, goals, events, now)
        rest = self.snap.pending_days(goals, events, now)
        self.assertEqual(len(rest), 40 - stats_history.MAX_FREEZE_PER_READ)
        self.snap.maybe_snapshot(self.conn, goals, events, now)
        self.assertEqual(self.snap.pending_days(goals, events, now), [])
        self.assertEqual(len(self.stored_days()), 40)
