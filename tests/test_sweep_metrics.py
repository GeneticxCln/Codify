"""`_sweep_metrics` bounds its read from the old end, not the new one.

The sweep feeds `/stats/failures` and the stage/role metrics. It is capped so a
huge event table cannot stall a request, and the cap used to keep the *first*
rows ever written: past the limit the views served ancient history and never
said so. These tests run the real query against a real table.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import app as engine_app
from engine.db import connect


class TestSweepMetricsWindow(unittest.IsolatedAsyncioTestCase):
    def _conn_with_events(self, count: int) -> sqlite3.Connection:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = connect(Path(tmp.name) / "codify.db")
        self.addCleanup(conn.close)
        # The events reference goals; the sweep does not care, and this test is
        # about which rows come back, not about the parents they would need.
        conn.execute("PRAGMA foreign_keys = OFF")
        for i in range(count):
            conn.execute(
                "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"e{i}", "g", None, "usage", json.dumps({"n": i}), float(i), i),
            )
        conn.commit()
        return conn

    async def test_past_the_limit_the_newest_rows_win(self) -> None:
        conn = self._conn_with_events(10)
        with patch.object(engine_app, "_METRIC_SWEEP_LIMIT", 4):
            rows = await engine_app._sweep_metrics(conn)
        self.assertEqual(
            [6, 7, 8, 9], [r["payload"]["n"] for r in rows],
            "the sweep must keep the newest rows, not the first ones ever written",
        )

    async def test_rows_stay_chronological(self) -> None:
        # `recovery_counts` walks the list in order and only counts a finish
        # that arrives after its retry, so newest-first would misclassify.
        conn = self._conn_with_events(10)
        with patch.object(engine_app, "_METRIC_SWEEP_LIMIT", 4):
            rows = await engine_app._sweep_metrics(conn)
        stamps = [r["timestamp"] for r in rows]
        self.assertEqual(sorted(stamps), stamps)

    async def test_the_sweep_is_served_by_the_time_index_not_a_full_sort(self) -> None:
        # 300,000 events took 235 ms to sweep before the index and 29 ms after; a
        # plan is a fact about the query and the schema, where a timing is a fact
        # about the machine, so this asserts the plan.
        conn = self._conn_with_events(3)
        plan = " ".join(
            str(row["detail"])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN " + engine_app._metric_sweep_sql(), engine_app._METRIC_EVENT_TYPES
            )
        )
        self.assertIn("idx_events_time", plan)

    async def test_under_the_limit_nothing_is_dropped(self) -> None:
        conn = self._conn_with_events(3)
        rows = await engine_app._sweep_metrics(conn)
        self.assertEqual([0, 1, 2], [r["payload"]["n"] for r in rows])


if __name__ == "__main__":
    unittest.main()
