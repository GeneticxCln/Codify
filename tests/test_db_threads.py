"""One thread's transaction is its own.

`db.connect` returns one handle that every service holds, and the engine uses it from the event loop, from
`asyncio.to_thread` workers, and from the daemon threads behind the gate and the conductor. SQLite serialises
each C call, but a transaction is three calls — BEGIN, execute, COMMIT — so with a single connection two
threads interleave them: one thread's `commit()` ends another thread's half-finished transaction, and the
second thread's `rollback()` then has nothing left to undo.

These tests are the defect made deterministic. On one shared connection the first one finds the writer's row
still there after its own rollback, and the second one raises "cannot start a transaction within a
transaction" in the second thread. With a connection per thread both pass, and the stress case checks that
concurrent writers neither lose a committed row nor keep a rolled-back one.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.db import connect


class TestTransactionsArePerThread(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE probe (id TEXT PRIMARY KEY)")
        self.conn.commit()

    def _ids(self) -> list[str]:
        """What a *separate* connection can see, so a commit is visible and an open transaction is not."""
        reader = sqlite3.connect(self.path)
        try:
            return [str(row[0]) for row in reader.execute("SELECT id FROM probe ORDER BY id")]
        finally:
            reader.close()

    def test_a_commit_in_one_thread_does_not_commit_another_threads_write(self) -> None:
        inserted = threading.Event()
        committed = threading.Event()
        failures: list[BaseException] = []

        def writer() -> None:
            try:
                self.conn.execute("INSERT INTO probe (id) VALUES ('writer')")
                inserted.set()
                if not committed.wait(10):
                    raise TimeoutError("the committer never answered")
                # Its own write, still uncommitted: this is what makes the row go away.
                self.conn.rollback()
            except BaseException as exc:  # noqa: BLE001 — surfaced on the test thread below
                failures.append(exc)
                inserted.set()
                committed.set()

        def committer() -> None:
            try:
                if not inserted.wait(10):
                    raise TimeoutError("the writer never inserted")
                # No write of its own. On a single shared connection this commit ends the writer's
                # implicit transaction; per thread it ends this thread's (empty) one and nothing else.
                self.conn.commit()
            except BaseException as exc:  # noqa: BLE001 — surfaced on the test thread below
                failures.append(exc)
            finally:
                committed.set()

        threads = [
            threading.Thread(target=writer, name="writer"),
            threading.Thread(target=committer, name="committer"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)

        self.assertFalse(any(thread.is_alive() for thread in threads), "a thread hung")
        self.assertEqual([], failures)
        self.assertEqual([], self._ids(), "the writer's rollback was undone by another thread's commit")

    def test_two_threads_can_hold_a_transaction_at_the_same_time(self) -> None:
        both_in: list[threading.Event] = [threading.Event(), threading.Event()]
        release = threading.Event()
        failures: list[BaseException] = []

        def holder(index: int) -> None:
            try:
                self.conn.execute("BEGIN")
                self.conn.execute("SELECT id FROM probe").fetchall()
                both_in[index].set()
                if not release.wait(10):
                    raise TimeoutError("never released")
                self.conn.rollback()
            except BaseException as exc:  # noqa: BLE001 — surfaced on the test thread below
                failures.append(exc)
                both_in[index].set()
                release.set()

        threads = [
            threading.Thread(target=holder, args=(index,), name=f"holder-{index}")
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        overlapped = all(event.wait(10) for event in both_in)
        release.set()
        for thread in threads:
            thread.join(15)

        self.assertTrue(overlapped, "the second transaction could not start while the first was open")
        self.assertFalse(any(thread.is_alive() for thread in threads), "a thread hung")
        self.assertEqual([], failures)

    def test_many_threads_writing_keep_every_transaction(self) -> None:
        workers, batches = 8, 25
        failures: list[BaseException] = []

        def work(worker: int) -> None:
            try:
                for batch in range(batches):
                    self.conn.execute(
                        "INSERT INTO probe (id) VALUES (?)", (f"{worker:02d}-{batch:03d}",),
                    )
                    self.conn.commit()
            except BaseException as exc:  # noqa: BLE001 — surfaced on the test thread below
                failures.append(exc)

        threads = [
            threading.Thread(target=work, args=(worker,), name=f"worker-{worker}")
            for worker in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertFalse(any(thread.is_alive() for thread in threads), "a thread hung")
        self.assertEqual([], failures)
        self.assertEqual(
            sorted(f"{worker:02d}-{batch:03d}" for worker in range(workers) for batch in range(batches)),
            self._ids(),
            "a committed write was lost",
        )

    def test_a_cursor_uses_the_calling_threads_connection(self) -> None:
        failures: list[BaseException] = []

        def writer() -> None:
            try:
                self.conn.cursor().execute("INSERT INTO probe (id) VALUES ('cursor-writer')")
                self.conn.commit()
            except BaseException as exc:  # noqa: BLE001 — surfaced on the test thread below
                failures.append(exc)

        thread = threading.Thread(target=writer, name="cursor-writer")
        thread.start()
        thread.join(15)

        self.assertFalse(thread.is_alive(), "a thread hung")
        self.assertEqual([], failures)
        self.assertEqual(
            ["cursor-writer"], self._ids(),
            "the cursor's write was committed on another thread's connection",
        )

    def test_in_transaction_reads_the_asking_threads_connection(self) -> None:
        seen: list[bool] = []
        failures: list[BaseException] = []

        def holder() -> None:
            try:
                self.conn.execute("BEGIN")
                seen.append(self.conn.in_transaction)
                self.conn.rollback()
            except BaseException as exc:  # noqa: BLE001 — surfaced on the test thread below
                failures.append(exc)

        thread = threading.Thread(target=holder, name="transaction-holder")
        thread.start()
        thread.join(15)

        self.assertFalse(thread.is_alive(), "a thread hung")
        self.assertEqual([], failures)
        self.assertEqual([True], seen)

    def test_a_close_from_another_thread_closes_the_workers_connection(self) -> None:
        inserted = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        def writer() -> None:
            try:
                self.conn.execute("INSERT INTO probe (id) VALUES ('uncommitted')")
                inserted.set()
                if not release.wait(10):
                    raise TimeoutError("the closer never answered")
                try:
                    self.conn.commit()
                except sqlite3.ProgrammingError:
                    return
                raise AssertionError("a commit after close did not raise")
            except BaseException as exc:  # noqa: BLE001 — surfaced on the test thread below
                failures.append(exc)
                inserted.set()
                release.set()

        thread = threading.Thread(target=writer, name="close-writer")
        thread.start()
        self.assertTrue(inserted.wait(10), "the writer never inserted")

        self.conn.close()
        release.set()
        thread.join(15)

        self.assertFalse(thread.is_alive(), "a thread hung")
        self.assertEqual([], failures)
        self.assertEqual([], self._ids(), "close left the worker's uncommitted row")
        with self.assertRaises(sqlite3.ProgrammingError):
            self.conn.execute("SELECT 1")
