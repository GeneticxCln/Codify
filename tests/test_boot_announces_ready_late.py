"""The boot handshake is printed only once the engine can actually start (review of 2026-09-29).

`serve()` bound the port and printed `CODIFY_ENGINE token=… port=…` — "I am ready" — and only then handed the
socket to uvicorn, whose startup is where the state store is opened. With a corrupt database the engine
announced itself, then failed its own startup and exited 3. A shell reading the handshake sees a live engine and
connects to a socket nobody will ever serve. So the store is opened (and closed) *before* the announcement: a
store that cannot be opened is a failed boot with nothing on stdout, which is what the shell already reads as one.
"""

from __future__ import annotations

import contextlib
import io
import os
import socket
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import engine.app as app_module


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestReadyIsAnnouncedLate(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "state"
        self.home.mkdir(mode=0o700)
        self.started: list[Any] = []

    def boot(self) -> tuple[io.StringIO, io.StringIO]:
        started = self.started

        class FakeServer:
            def __init__(self, config: Any) -> None:
                self.config = config

            def run(self, sockets: Any = None) -> None:
                started.append(sockets)
                for s in sockets or ():
                    s.close()

        fake = types.ModuleType("uvicorn")
        fake.Config = lambda *a, **k: types.SimpleNamespace()  # type: ignore[attr-defined]
        fake.Server = FakeServer  # type: ignore[attr-defined]
        env = {
            "CODIFY_HOME": str(self.home),
            "CODIFY_DB": str(self.home / "codify.db"),
            "CODIFY_SECRETS": str(self.home / "secrets.json"),
        }
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env), patch.dict(sys.modules, {"uvicorn": fake}), \
                patch.object(app_module, "pick_port", return_value=free_port()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                app_module.serve()
            finally:
                self.out, self.err = out, err
        return out, err

    def test_a_store_that_can_be_opened_is_announced_as_before(self) -> None:
        out, _ = self.boot()

        self.assertIn("CODIFY_ENGINE token=", out.getvalue())
        self.assertEqual(1, len(self.started), "the server was not started")

    def test_a_corrupt_store_is_never_announced_as_ready(self) -> None:
        (self.home / "codify.db").write_bytes(b"this is not a database\n" * 200)

        with self.assertRaises(sqlite3.DatabaseError):
            self.boot()

        self.assertNotIn("CODIFY_ENGINE", self.out.getvalue(), "an engine that cannot start announced itself")
        self.assertEqual([], self.started, "the server was started on a store that cannot be opened")

    def test_the_refusal_says_which_file_and_why(self) -> None:
        (self.home / "codify.db").write_bytes(b"this is not a database\n" * 200)

        with self.assertRaises(sqlite3.DatabaseError):
            self.boot()

        self.assertIn(str(self.home / "codify.db"), self.err.getvalue())
        self.assertIn("cannot open", self.err.getvalue())

    def test_the_preflight_does_not_leave_a_connection_open(self) -> None:
        opened: list[sqlite3.Connection] = []
        real = sqlite3.connect

        def spy(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            conn: sqlite3.Connection = real(*args, **kwargs)
            opened.append(conn)
            return conn

        with patch("sqlite3.connect", spy):
            self.boot()

        self.assertTrue(opened, "the store was never opened before the announcement")
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
