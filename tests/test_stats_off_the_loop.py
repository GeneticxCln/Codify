"""The stats routes do their reading and their arithmetic off the event loop.

`docs/03` §3 4.3 measured it: at the sweep's cap, `/stats/failures` held the loop for about 145 ms —
the query, the JSON parse of 20,000 rows, the aggregation — and every goal streaming at that moment
stalled with it. The cost plateaus, so it was recorded as a decision to wait for a reason; the reason
was asking for everything to be finished. The read now runs on a worker thread over a second, read-only
connection (WAL lets it read while the engine writes), and the aggregation follows it there.

What is asserted is a fact about *where things run*, not a timing: a stopwatch is a fact about the
machine, and this suite runs on loaded ones. The one behavioural test blocks a read on an event that only
the loop can set, so it can pass only if the loop is free while the read is running.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from httpx import ASGITransport

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine import app as engine_app
from engine.app import BOOT_TOKEN, app
from engine.db import SCHEMA, connect
from engine.services import SettingsService
from engine.stats_history import StatsSnapshotService
from engine.stats_import import StatsImportService
from engine.trace import TraceService


def _add_events(conn: sqlite3.Connection, count: int, start: int = 0) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    for i in range(start, start + count):
        conn.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"e{i}", "g", None, "usage", json.dumps({"n": i, "role": "fixer"}), float(i), i),
        )
    conn.commit()


class _Stats(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = connect(Path(tmp.name) / "codify.db")
        self.addCleanup(self.conn.close)
        _add_events(self.conn, 5)
        app.state.conn = self.conn
        app.state.token = BOOT_TOKEN
        app.state.settings = SettingsService(self.conn)
        app.state.stats_snapshots = StatsSnapshotService(self.conn)
        app.state.stats_imports = StatsImportService(self.conn)
        app.state.traces = TraceService(self.conn)
        self.client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}

    async def get_both(self) -> None:
        for route in ("/stats/overview", "/stats/failures"):
            response = await self.client.get(route, headers=self.headers)
            self.assertEqual(200, response.status_code, response.text)


class TestTheReadsLeaveTheLoop(_Stats):
    async def test_each_read_runs_on_a_worker_thread_over_a_connection_of_its_own(self) -> None:
        seen: list[tuple[str, int, sqlite3.Connection]] = []

        def spy(name: str) -> Any:
            real = getattr(engine_app, name)

            def inner(conn: sqlite3.Connection) -> Any:
                seen.append((name, threading.get_ident(), conn))
                return real(conn)

            return inner

        with (
            patch.object(engine_app, "_load_stats", spy("_load_stats")),
            patch.object(engine_app, "_load_metrics", spy("_load_metrics")),
        ):
            await self.get_both()

        self.assertEqual({"_load_stats", "_load_metrics"}, {name for name, _, _ in seen})
        loop_thread = threading.get_ident()
        for name, thread, conn in seen:
            self.assertNotEqual(loop_thread, thread, f"{name} ran on the event loop's thread")
            self.assertIsNot(self.conn, conn, f"{name} read through the engine's own connection")

    async def test_a_read_that_takes_time_does_not_hold_the_loop(self) -> None:
        release = threading.Event()
        released: list[bool] = []
        real = engine_app._load_metrics

        def slow(conn: sqlite3.Connection) -> Any:
            # Blocks this thread until the loop sets `release`. On the loop's own thread that is a
            # deadlock the timeout turns into a failure: the loop could never reach `release.set()`.
            released.append(release.wait(timeout=5))
            return real(conn)

        with patch.object(engine_app, "_load_metrics", slow):
            request = asyncio.create_task(self.client.get("/stats/failures", headers=self.headers))
            await asyncio.sleep(0.2)
            release.set()
            response = await request

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([True], released, "the loop was held while the read waited")

    async def test_the_aggregation_runs_off_the_loop_too(self) -> None:
        threads: dict[str, int] = {}

        def watch(name: str) -> Any:
            real = getattr(engine_app, name)

            def inner(*args: Any, **kwargs: Any) -> Any:
                threads[name] = threading.get_ident()
                return real(*args, **kwargs)

            return inner

        names = ("build_overview", "stage_costs", "role_success_rate", "failure_breakdown")
        with (
            patch.object(engine_app, names[0], watch(names[0])),
            patch.object(engine_app, names[1], watch(names[1])),
            patch.object(engine_app, names[2], watch(names[2])),
            patch.object(engine_app, names[3], watch(names[3])),
        ):
            await self.get_both()

        self.assertEqual(set(names), set(threads))
        loop_thread = threading.get_ident()
        for name, thread in threads.items():
            self.assertNotEqual(loop_thread, thread, f"{name} ran on the event loop's thread")


class TestTheFreezeLeavesTheLoopToo(_Stats):
    """The daily freeze was the part of the overview that still ran on the loop.

    It built one full overview per unfrozen day, synchronously, between the read and the aggregation. With more
    history than the retention keeps, it also did so again on every read (see `tests/test_stats_history.py`),
    and `/health` measured 23 seconds late while the Stats drawer opened over 400 days of history.
    """

    async def test_the_arithmetic_for_the_days_owed_runs_on_a_worker_thread(self) -> None:
        threads: list[int] = []
        real = StatsSnapshotService.build_documents

        def spy(goals: Any, events: Any, days: Any) -> Any:
            threads.append(threading.get_ident())
            return real(goals, events, days)

        with patch.object(StatsSnapshotService, "build_documents", staticmethod(spy)):
            await self.get_both()

        self.assertTrue(threads, "no day was owed, so the test proves nothing about where the freeze runs")
        self.assertNotIn(threading.get_ident(), threads, "the freeze's arithmetic ran on the event loop's thread")
        self.assertGreaterEqual(self.conn.execute("SELECT COUNT(*) FROM stats_snapshots").fetchone()[0], 1)

    async def test_a_second_read_owes_nothing(self) -> None:
        builds: list[list[str]] = []
        real = StatsSnapshotService.build_documents

        def spy(goals: Any, events: Any, days: Any) -> Any:
            builds.append(list(days))
            return real(goals, events, days)

        with patch.object(StatsSnapshotService, "build_documents", staticmethod(spy)):
            await self.get_both()
            first = len(builds)
            await self.get_both()

        self.assertEqual(1, first)
        self.assertEqual(first, len(builds), "the days frozen by the first read were built again by the second")


    async def test_a_history_longer_than_the_retention_is_not_frozen_again_on_every_read(self) -> None:
        """The thrash: with more active days than `stats_retention_days` keeps, each read froze the older days and
        `prune` deleted them, so the next read froze them again. Measured at 400 days of history and the default 90,
        that was 25 seconds of discarded work on every open of the Stats drawer."""
        for day in range(1, 8):
            _add_events(self.conn, 1, start=100 + day)  # placeholder ids; the timestamp is set below
            self.conn.execute("UPDATE events SET timestamp = ? WHERE id = ?", (day * 86400.0 + 600.0, f"e{100 + day}"))
        self.conn.commit()
        app.state.settings.set_int("stats_retention_days", 2)
        builds: list[list[str]] = []
        real = StatsSnapshotService.build_documents

        def spy(goals: Any, events: Any, days: Any) -> Any:
            builds.append(list(days))
            return real(goals, events, days)

        with patch.object(StatsSnapshotService, "build_documents", staticmethod(spy)):
            await self.get_both()
            self.assertEqual([2], [len(b) for b in builds], "the first read froze more days than the policy keeps")
            await self.get_both()
            await self.get_both()

        self.assertEqual(1, len(builds), "a later read froze days again that the policy had just pruned")
        self.assertEqual(2, self.conn.execute("SELECT COUNT(*) FROM stats_snapshots").fetchone()[0])


class TestTheOverviewSaysWhatItCovers(_Stats):
    async def overview(self) -> dict[str, Any]:
        response = await self.client.get("/stats/overview?window=0", headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        body: dict[str, Any] = response.json()
        return body

    async def test_a_history_inside_the_caps_is_complete(self) -> None:
        coverage = (await self.overview())["coverage"]
        self.assertFalse(coverage["truncated"])
        self.assertIsNone(coverage["since"])
        self.assertEqual(5, coverage["events"])
        self.assertEqual(engine_app._STATS_EVENT_LIMIT, coverage["event_cap"])
        self.assertEqual(engine_app._STATS_GOAL_LIMIT, coverage["goal_cap"])

    async def test_a_history_past_the_event_cap_says_so_and_from_when_it_is_complete(self) -> None:
        """"All" over 30,000 model calls counted the newest 20,000 and said nothing: 63% of the true total in the
        large-history measurement. The numbers are right from the oldest event the read kept."""
        with patch.object(engine_app, "_STATS_EVENT_LIMIT", 3):
            coverage = (await self.overview())["coverage"]
        self.assertTrue(coverage["truncated"])
        self.assertEqual(3, coverage["events"])
        self.assertEqual(3, coverage["event_cap"])
        self.assertEqual(2.0, coverage["since"], "events 2, 3 and 4 are the newest three; complete from the oldest of them")

    async def test_a_history_past_the_goal_cap_says_so_too(self) -> None:
        self.conn.execute("INSERT INTO workspaces (id, name, root_path, created_at) VALUES ('w0', 'w', '/w0', 0.0)")
        for i in range(4):
            self.conn.execute(
                "INSERT INTO goals (id, workspace_id, title, description, status, created_at, updated_at)"
                " VALUES (?, 'w0', 't', '', 'COMPLETED', ?, ?)",
                (f"g{i}", 100.0 + i, 100.0 + i),
            )
        self.conn.commit()
        with patch.object(engine_app, "_STATS_GOAL_LIMIT", 2):
            coverage = (await self.overview())["coverage"]
        self.assertTrue(coverage["truncated"])
        self.assertEqual(2, coverage["goals"])
        self.assertEqual(102.0, coverage["since"], "the two newest goals are 102 and 103; complete from the older of them")


class TestTheSecondConnectionIsSafe(_Stats):
    async def test_it_cannot_write(self) -> None:
        outcome: list[str] = []
        real = engine_app._load_metrics

        def probe(conn: sqlite3.Connection) -> Any:
            try:
                conn.execute("DELETE FROM events")
            except sqlite3.OperationalError as exc:
                outcome.append(str(exc))
            else:
                outcome.append("the read connection could write")
            return real(conn)

        with patch.object(engine_app, "_load_metrics", probe):
            await self.client.get("/stats/failures", headers=self.headers)

        self.assertEqual(1, len(outcome))
        self.assertIn("readonly", outcome[0])
        self.assertEqual(5, self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    async def test_it_is_closed_when_the_read_is_done(self) -> None:
        opened: list[sqlite3.Connection] = []
        real = engine_app._load_stats

        def keep(conn: sqlite3.Connection) -> Any:
            opened.append(conn)
            return real(conn)

        with patch.object(engine_app, "_load_stats", keep):
            await self.client.get("/stats/overview", headers=self.headers)

        self.assertEqual(1, len(opened))
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    async def test_it_is_closed_when_the_read_fails_too(self) -> None:
        opened: list[sqlite3.Connection] = []

        def boom(conn: sqlite3.Connection) -> Any:
            opened.append(conn)
            raise RuntimeError("the read failed")

        with patch.object(engine_app, "_load_metrics", boom), self.assertRaises(RuntimeError):
            await engine_app._sweep_metrics(self.conn)

        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    async def test_it_sees_what_the_engine_committed_a_moment_ago(self) -> None:
        # The engine commits every write, so a WAL reader on another connection sees it.
        _add_events(self.conn, 3, start=5)

        rows = await engine_app._sweep_metrics(self.conn)

        self.assertEqual(8, len(rows))

    async def test_a_store_that_is_not_a_file_is_read_where_it_is(self) -> None:
        # An in-memory database cannot be opened a second time, so the read stays on the caller's
        # connection rather than answering from an empty copy.
        memory = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(memory.close)
        memory.row_factory = sqlite3.Row
        memory.executescript(SCHEMA)
        _add_events(memory, 4)
        used: list[sqlite3.Connection] = []
        real = engine_app._load_metrics

        def spy(conn: sqlite3.Connection) -> Any:
            used.append(conn)
            return real(conn)

        with patch.object(engine_app, "_load_metrics", spy):
            rows = await engine_app._sweep_metrics(memory)

        self.assertEqual(4, len(rows))
        self.assertEqual([memory], used)


if __name__ == "__main__":
    unittest.main()
