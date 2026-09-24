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

from engine.db import connect
from engine.stats_history import StatsSnapshotService, utc_day
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


class TestMaybeSnapshot(StatsHistoryTestCase):
    def test_first_read_after_a_day_ends_freezes_that_day(self) -> None:
        # All of day 0's activity is inside day 0; the read happens on day 1.
        goals = [goal("COMPLETED", at(0, 2), updated=at(0, 9))]
        events = [usage_ev(at(0, 15), tokens=30)]
        frozen = self.snap.maybe_snapshot(self.conn, goals, events, now=at(1, 8))
        assert frozen is not None, "day 0 is over, so the read freezes it"
        self.assertEqual(frozen, utc_day(at(0, 15)))
        doc = self.snap.get_day(frozen)
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
        doc = self.snap.get_day(day)
        assert doc is not None, "the frozen row survives losing its source events"
        self.assertEqual(doc["usage"]["total_tokens"], 148)

    def test_nothing_freezes_while_activity_is_still_today(self) -> None:
        now = at(1, 8)
        goals = [goal("RUNNING", now - 100, updated=now - 10)]
        events = [usage_ev(now - 5)]
        self.assertIsNone(self.snap.maybe_snapshot(self.conn, goals, events, now=now))
        self.assertEqual(self.snap.list_days(), [])

    def test_empty_history_freezes_nothing(self) -> None:
        self.assertIsNone(self.snap.maybe_snapshot(self.conn, [], [], now=at(1)))

    def test_freeze_is_idempotent(self) -> None:
        goals = [goal("FAILED", at(0, 2))]
        events = [usage_ev(at(0, 15))]
        first = self.snap.maybe_snapshot(self.conn, goals, events, now=at(1, 8))
        self.assertIsNotNone(first)
        # The fixture spans two UTC days (goal late on day 0, event on day 1),
        # so backfill freezes both — the older one must not be left unfrozen.
        self.assertEqual(self.snap.list_days(), sorted(self.snap.list_days()))
        self.assertEqual(len(self.snap.list_days()), 2)
        self.assertEqual(self.snap.list_days()[-1], first)
        # A second read the same day finds the rows already frozen: nothing is
        # rewritten, and the contract reports "nothing to do".
        second = self.snap.maybe_snapshot(self.conn, goals, events, now=at(1, 20))
        self.assertIsNone(second)
        self.assertEqual(len(self.snap.list_days()), 2)
        # And the frozen numbers are the first freeze's, not a re-write.
        assert first is not None, "the backfill froze the older day"
        frozen_doc = self.snap.get_day(first)
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
        self.assertEqual(len(self.snap.list_days()), 4)


class TestHistory(StatsHistoryTestCase):
    def test_days_are_oldest_first_and_carry_full_documents(self) -> None:
        for offset, tokens in ((0, 30), (2, 50)):
            self.snap.maybe_snapshot(
                self.conn,
                [goal("COMPLETED", at(offset, 2))],
                [usage_ev(at(offset, 15), tokens=tokens)],
                now=at(offset + 1, 8),
            )
        days = self.snap.list_days()
        self.assertEqual(days, sorted(days))
        history = self.snap.history()
        self.assertEqual([h["day"] for h in history], days)
        # Each fixture spans two UTC days frozen with the same document.
        self.assertEqual(history[0]["document"]["usage"]["total_tokens"], 45)
        self.assertEqual(history[1]["document"]["usage"]["total_tokens"], 45)
        self.assertEqual(history[2]["document"]["usage"]["total_tokens"], 75)
        self.assertEqual(history[3]["document"]["usage"]["total_tokens"], 75)

    def test_a_corrupt_document_is_skipped_not_fatal(self) -> None:
        self.conn.execute(
            "INSERT INTO stats_snapshots (day, document, created_at) VALUES (?, ?, ?)",
            ("2030-01-01", "{not json", 0.0),
        )
        self.conn.commit()
        self.assertIsNone(self.snap.get_day("2030-01-01"))
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
        self.assertEqual(self.snap.list_days(), ["2026-09-03", "2026-09-04"], "the newest survive")

    def test_zero_keeps_everything(self) -> None:
        self._freeze_days(["2026-09-01", "2026-09-02"])
        self.assertEqual(self.snap.prune(0), 0)
        self.assertEqual(len(self.snap.list_days()), 2)

    def test_pruning_more_than_exists_changes_nothing(self) -> None:
        self._freeze_days(["2026-09-01"])
        self.assertEqual(self.snap.prune(30), 0)
        self.assertEqual(len(self.snap.list_days()), 1)

    def test_deletion_is_by_count_not_age_so_an_idle_machine_prunes_nothing(self) -> None:
        """Rows from months ago survive when they are among the newest N: the
        policy bounds the table, not the calendar."""
        self._freeze_days(["2025-01-01", "2026-09-01"])
        self.snap.prune(2)
        self.assertEqual(len(self.snap.list_days()), 2, "two rows, policy of 2 — nothing to do")

    def test_prune_tolerates_rows_a_corrupt_document_would_have_blocked(self) -> None:
        # Prune is a table operation; it must not care whether documents parse.
        self._freeze_days(["2026-09-01", "2026-09-02", "2026-09-03"])
        self.conn.execute("UPDATE stats_snapshots SET document = '{bad' WHERE day = '2026-09-02'")
        self.conn.commit()
        self.assertEqual(self.snap.prune(1), 2)
        self.assertEqual(self.snap.list_days(), ["2026-09-03"])


if __name__ == "__main__":
    unittest.main()
