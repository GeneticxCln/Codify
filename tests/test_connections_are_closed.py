"""A database connection is closed on every road out (review of 2026-09-29, finding 5).

`db.connect` opened the store and then ran the schema and every migration; when one of them raised, the
connection it had opened was never closed. `lifespan` had the same shape one level up: `conn.close()` sat
after the `try/finally` around the `yield`, so anything that raised between opening the store and reaching
that `try` — a service constructor, the orphan rescue — leaked it. That is the `ResourceWarning: unclosed
database` in the test output, and on a real boot it is a handle on a store the process is about to exit
around, which is harmless until the same code is used by something that does not exit.

The check is on the connection itself: a closed SQLite connection refuses every statement.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import engine.app as engine_app
import engine.db as engine_db


def is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


class Spy:
    """Records the connections a module opened, and lets a test ask which are still open."""

    def __init__(self) -> None:
        self.opened: list[sqlite3.Connection] = []

    def wrap(self, real: Any) -> Any:
        def opened(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            conn: sqlite3.Connection = real(*args, **kwargs)
            self.opened.append(conn)
            return conn

        return opened

    def still_open(self) -> list[sqlite3.Connection]:
        return [c for c in self.opened if not is_closed(c)]


class TestConnect(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "t.db"

    def test_a_migration_that_fails_does_not_leave_the_connection_open(self) -> None:
        spy = Spy()
        with mock.patch("sqlite3.connect", spy.wrap(sqlite3.connect)), \
                mock.patch.object(engine_db, "migrate_agent_roles", side_effect=RuntimeError("migration failed")):
            with self.assertRaises(RuntimeError):
                engine_db.connect(self.db)

        self.assertEqual(1, len(spy.opened))
        self.assertEqual([], spy.still_open(), "the connection was left open by the failed migration")

    def test_a_schema_that_fails_does_not_leave_the_connection_open(self) -> None:
        spy = Spy()
        with mock.patch("sqlite3.connect", spy.wrap(sqlite3.connect)), \
                mock.patch.object(engine_db, "SCHEMA", "CREATE TABLE this is not sql;"):
            with self.assertRaises(sqlite3.Error):
                engine_db.connect(self.db)

        self.assertEqual([], spy.still_open())

    def test_a_connection_that_succeeds_is_open(self) -> None:
        conn = engine_db.connect(self.db)
        self.addCleanup(conn.close)

        self.assertFalse(is_closed(conn))


class TestLifespan(unittest.IsolatedAsyncioTestCase):
    def patched(self, spy: Spy) -> Any:
        return mock.patch.object(engine_app, "connect", spy.wrap(engine_db.connect))

    async def test_a_startup_that_fails_after_the_store_is_open_closes_it(self) -> None:
        spy = Spy()
        with self.patched(spy), mock.patch.object(engine_app, "ExecutorService", side_effect=RuntimeError("no executor")):
            with self.assertRaises(RuntimeError):
                async with engine_app.lifespan(engine_app.app):
                    self.fail("the app must not start")

        self.assertEqual(1, len(spy.opened))
        self.assertEqual([], spy.still_open(), "a failed start leaked the store's connection")

    async def test_a_clean_shutdown_closes_it(self) -> None:
        spy = Spy()
        with self.patched(spy):
            async with engine_app.lifespan(engine_app.app):
                self.assertEqual(1, len(spy.still_open()))

        self.assertEqual([], spy.still_open())

    async def test_a_shutdown_step_that_raises_still_closes_it(self) -> None:
        spy = Spy()
        with self.patched(spy), mock.patch.object(engine_app, "_truncate_wal", side_effect=RuntimeError("wal")):
            with self.assertRaises(RuntimeError):
                async with engine_app.lifespan(engine_app.app):
                    pass

        self.assertEqual([], spy.still_open())


if __name__ == "__main__":
    unittest.main()
