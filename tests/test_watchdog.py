"""The engine's half of "close the window, stop the engine".

The desktop shell already kills its child on a clean exit, and on a dropped handle.
The failure this suite exists for is the one a signal leaves behind: the window is
gone and `python3 -m engine` is still holding port 7430 and the SQLite file, so the
next launch has to fight it for both. `CODIFY_PARENT_PID` is the contract — the
shell passes its own pid, the engine watches it — so these tests pin the states that
matter: armed with a live pid, disarmed without one (a standalone run must behave
exactly as it did before this existed), and a polling loop that actually reaches its
handler instead of stopping after one look.

Watching is only half of it. A stop request only *starts* a shutdown, and that
shutdown is uvicorn's: it waits for in-flight work with no deadline of its own, so
an open event stream or a turn still generating held the SQLite file after the port
had already been released — the orphan as it was actually observed, reproduced
before this existed by holding one WebSocket open and sending SIGTERM. The other
half is therefore pinned here too: the deadline is armed *before* the graceful path
(both from a signal and from the parent's death), it ends the process through
`os._exit` and not through interpreter finalization, and it can be cancelled when
the graceful path finished inside the window. The launcher's wiring — the finite
timeout and the wrapped handler — is asserted against a real `main()` at the end.

Nothing here kills a process: the loop takes its predicate and its handler as
arguments, only the predicates themselves are exercised against real pids, and the
launcher test stubs `Server.run` so no port is served and no process exits.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import contextlib
import io
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import uvicorn

from engine import spawn_guard, watchdog
from engine.app import _truncate_wal, main


def _finished_child_pid() -> int:
    """The pid of a process that has certainly exited — the orphan's shape.

    Reaped and gone, which is the state a killed window leaves behind for the pid it
    passed to the engine.
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class ParentPidTests(unittest.TestCase):
    """Reading the contract out of the environment."""

    def test_the_pid_the_shell_passed_is_the_pid_that_gets_watched(self) -> None:
        self.assertEqual(4321, watchdog.parent_pid_from_env({watchdog.ENV_PARENT_PID: "4321"}))
        self.assertEqual(
            4321,
            watchdog.parent_pid_from_env({watchdog.ENV_PARENT_PID: " 4321 "}),
            "whitespace around the value is not a different pid",
        )

    def test_a_standalone_run_has_no_parent_to_watch(self) -> None:
        for value in ("", "   ", "not-a-pid", "0", "-7", "²", "12.5", "1e3"):
            self.assertIsNone(
                watchdog.parent_pid_from_env({watchdog.ENV_PARENT_PID: value}),
                f"{value!r} must not parse into a pid",
            )
        self.assertIsNone(
            watchdog.parent_pid_from_env({}),
            "an absent variable is the standalone case",
        )

    def test_the_real_environment_is_read_when_no_mapping_is_passed(self) -> None:
        with patch.dict(os.environ, {watchdog.ENV_PARENT_PID: "999"}):
            self.assertEqual(999, watchdog.parent_pid_from_env())


class InheritedWatchdogTests(unittest.TestCase):
    """The suite must not arrive already armed.

    `engine.app.serve` arms the parent watchdog from the environment, with no
    code call and no opt-out: a test that boots the engine inherits whatever
    `CODIFY_PARENT_PID` the developer's shell happens to carry. When that pid is
    gone — or was never this process's parent — `watchdog.terminate` runs, arms
    the hard deadline and SIGTERMs the runner. The suite would end mid-file at a
    signal, with an exit status that reads as success to a `make` that never saw
    a summary. That is the same failure as the `os._exit` in `main`, reached
    without any code changing.

    So the two parent-pid variables are cleared on every run, ahead of both
    branches of `activate` — including the branch that respects an externally set
    `CODIFY_HOME`, which is the CI case and the one where an inherited value is
    most likely.
    """

    def test_both_parent_variables_are_cleared(self) -> None:
        names = (watchdog.ENV_PARENT_PID, spawn_guard.ENV_PARENT_PID)
        self.assertNotEqual(
            names[0],
            names[1],
            "the engine's parent and the sandbox guard's parent are different "
            "variables, and the test is weaker if they are not",
        )
        with patch.dict(os.environ, {name: "4321" for name in names}):
            hermetic.disarm_parent_watchdogs()
            for name in names:
                self.assertNotIn(name, os.environ, f"{name} survived the disarm")

    def test_this_process_is_disarmed_right_now(self) -> None:
        """The invariant, asserted about the suite that is running it."""
        for name in (watchdog.ENV_PARENT_PID, spawn_guard.ENV_PARENT_PID):
            self.assertNotIn(
                name,
                os.environ,
                f"{name} is set in the test process — every test that boots the engine "
                "is running with a live parent-death watchdog behind it",
            )

    def test_the_disarm_is_idempotent(self) -> None:
        hermetic.disarm_parent_watchdogs()
        hermetic.disarm_parent_watchdogs()  # must not raise on an absent variable


class GoneTests(unittest.TestCase):
    """The two questions `parent_is_gone` asks, and why both are needed."""

    def test_a_live_pid_names_a_live_process_and_a_reaped_one_does_not(self) -> None:
        self.assertTrue(watchdog.process_exists(os.getpid()))
        self.assertFalse(watchdog.process_exists(_finished_child_pid()))

    def test_reparenting_alone_is_enough(self) -> None:
        # The pid still exists as far as `kill` is concerned, but it is not our parent
        # any more — the kernel reparents us the instant the parent dies, and that
        # answer cannot be defeated by the pid being reused.
        self.assertTrue(watchdog.parent_is_gone(7, parent_now=lambda: 8, exists=lambda pid: True))

    def test_a_missing_pid_alone_is_enough(self) -> None:
        # Still our parent according to `getppid()`, but the process is gone: the
        # window between death and reparenting, and the case where the variable names
        # a pid that was never our parent at all.
        self.assertTrue(watchdog.parent_is_gone(7, parent_now=lambda: 7, exists=lambda pid: False))

    def test_a_live_parent_still_our_own_is_not_gone(self) -> None:
        self.assertFalse(watchdog.parent_is_gone(7, parent_now=lambda: 7, exists=lambda pid: True))


class WatchdogLoopTests(unittest.TestCase):
    """Arming, polling, and the handler that ends the engine."""

    def test_the_handler_runs_once_the_watched_pid_is_gone(self) -> None:
        seen: list[int] = []
        fired = threading.Event()

        def on_death(pid: int) -> None:
            seen.append(pid)
            fired.set()

        thread = watchdog.start_parent_watchdog(
            4321, interval_s=0.01, on_parent_death=on_death, is_gone=lambda pid: True
        )
        self.assertIsNotNone(thread)
        self.assertTrue(fired.wait(2.0), "the handler has to run once the parent is gone")
        self.assertEqual([4321], seen, "the handler is told which pid died")

    def test_the_loop_polls_instead_of_stopping_after_one_look(self) -> None:
        looks: list[int] = []
        looked = threading.Event()

        def is_gone(pid: int) -> bool:
            looks.append(pid)
            looked.set()
            return False

        on_death = Mock()
        thread = watchdog.start_parent_watchdog(
            4321, interval_s=0.01, on_parent_death=on_death, is_gone=is_gone
        )
        self.assertIsNotNone(thread)
        self.assertTrue(looked.wait(2.0), "the first look happens immediately")
        for _ in range(200):
            if len(looks) >= 2:
                break
            time.sleep(0.01)
        self.assertGreaterEqual(len(looks), 2, "a live parent is checked again, not trusted once")
        on_death.assert_not_called()

    def test_a_standalone_run_arms_nothing(self) -> None:
        with patch.dict(os.environ, {}):
            os.environ.pop(watchdog.ENV_PARENT_PID, None)
            self.assertIsNone(
                watchdog.start_parent_watchdog(),
                "no pid in the environment means no watchdog thread at all",
            )

    def test_the_arming_line_goes_to_stderr_and_keeps_stdout_clean(self) -> None:
        """stdout is the boot handshake channel; the shell parses it line by line."""
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            thread = watchdog.start_parent_watchdog(
                4321, interval_s=0.01, on_parent_death=lambda pid: None, is_gone=lambda pid: True
            )
        self.assertIsNotNone(thread)
        self.assertEqual("", out.getvalue(), "nothing may be written to the handshake channel")
        self.assertIn("4321", err.getvalue())


class HardDeadlineTests(unittest.TestCase):
    """The bound behind the graceful path: a stop request has to end, not wait."""

    def test_the_deadline_outlasts_the_graceful_window(self) -> None:
        self.assertGreater(
            watchdog.HARD_EXIT_GRACE_S,
            watchdog.GRACEFUL_SHUTDOWN_S,
            "a deadline inside the graceful window would cut off a shutdown that was "
            "about to finish on its own",
        )

    def test_a_shutdown_that_never_finishes_is_ended_anyway(self) -> None:
        exits: list[int] = []
        done = threading.Event()

        def exit_fn() -> None:
            exits.append(1)
            done.set()

        timer = watchdog.arm_hard_deadline(0.05, exit_fn=exit_fn)
        self.assertTrue(done.wait(2.0), "nothing else was going to end this process")
        self.assertEqual([1], exits, "the deadline fires once, not once per look")
        self.assertTrue(timer.daemon, "the timer itself must never be a reason to linger")

    def test_a_shutdown_that_finishes_inside_the_window_is_not_cut_off(self) -> None:
        exit_fn = Mock()
        timer = watchdog.arm_hard_deadline(0.2, exit_fn=exit_fn)
        timer.cancel()
        time.sleep(0.35)
        exit_fn.assert_not_called()

    def test_the_deadline_says_why_it_fired(self) -> None:
        with (
            patch.object(watchdog, "hard_exit") as leave,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            timer = watchdog.arm_hard_deadline(0.05)
            timer.join(timeout=2.0)
            self.assertTrue(leave.called, "the deadline still ends the process")
        self.assertIn(
            "shutdown unfinished",
            err.getvalue(),
            "giving up in silence is indistinguishable from being killed",
        )

    def test_hard_exit_leaves_without_waiting_for_the_interpreter(self) -> None:
        # `os._exit` is the whole point: interpreter finalization joins the threads
        # that cannot be cancelled, and that join is where the orphan actually sat.
        with patch.object(os, "_exit") as leave:
            watchdog.hard_exit(3)
        leave.assert_called_once_with(3)

    def test_a_broken_stream_cannot_hold_the_database(self) -> None:
        class Broken:
            def flush(self) -> None:
                raise OSError("broken pipe")

        with (
            patch.object(os, "_exit") as leave,
            patch.object(sys, "stdout", Broken()),
            patch.object(sys, "stderr", Broken()),
        ):
            watchdog.hard_exit()
        leave.assert_called_once_with(0)


class ParentDeathIsBoundedTests(unittest.TestCase):
    """The killed-window path: nobody is left to ask twice, so the clock starts here."""

    def test_the_watchdog_arms_the_deadline_before_it_asks_itself_to_stop(self) -> None:
        order: list[tuple[str, ...]] = []
        with (
            patch.object(
                watchdog, "arm_hard_deadline", side_effect=lambda *a, **k: order.append(("arm",))
            ),
            patch.object(
                os, "kill", side_effect=lambda pid, sig: order.append(("kill", str(pid), str(sig)))
            ),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            watchdog.terminate(4321)
        self.assertEqual(
            [("arm",), ("kill", str(os.getpid()), str(signal.SIGTERM))],
            order,
            "the grace starts first, and the graceful path is still SIGTERM",
        )
        self.assertIn("4321", err.getvalue(), "the line says which process died")


class ShutdownCheckpointTests(unittest.TestCase):
    """The teardown tidy-up, which was one more thing that could hold the exit.

    `wal_checkpoint(TRUNCATE)` waits for every reader to release the log, and a
    cancelled turn leaves a reader behind: the live case spent the whole deadline
    inside it, on the shutdown's own path to the exit.
    """

    def test_a_reader_in_the_way_cannot_hold_the_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wal.db"
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            # The timeout the engine's own connection carries (`engine/db.py`): the
            # point of the test is that shutdown does not inherit it.
            conn.execute("PRAGMA busy_timeout = 5000")
            reader = sqlite3.connect(path)
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM t").fetchall()
            try:
                started = time.monotonic()
                _truncate_wal(conn)
                elapsed = time.monotonic() - started
                self.assertGreater(
                    Path(f"{path}-wal").stat().st_size,
                    0,
                    "the reader has to be genuinely in the way, or this proves nothing",
                )
            finally:
                reader.rollback()
                reader.close()
                conn.close()
        self.assertLess(
            elapsed,
            1.0,
            "a tidy-up that waits on a reader outlives the deadline hiding behind it",
        )


class LauncherWiringTests(unittest.TestCase):
    """What `python3 -m engine` builds: a server that cannot wait forever.

    `main()` is driven for real, with `Server.run` stubbed and no socket bound, so
    the object asserted on is the server the launcher actually builds rather than a
    shape this test re-declares. What it pins is the pair that made the orphan: the
    graceful timeout (uvicorn's default, `None`, is "wait for in-flight work
    forever") and the handler that starts the deadline before that wait begins.
    """

    def test_the_server_is_bounded_and_its_signal_path_starts_the_clock(self) -> None:
        servers: list[uvicorn.Server] = []
        with (
            patch("socket.socket"),
            patch.object(uvicorn.Server, "run", lambda self, sockets=None: servers.append(self)),
            patch.object(watchdog, "start_parent_watchdog"),
            patch.object(watchdog, "arm_hard_deadline") as arm,
            patch.object(watchdog, "hard_exit") as leave,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            main()
            # Everything below is asserted with the patches still in place: the
            # handler under test resolves `arm_hard_deadline` through the module, so
            # leaving the block first would arm a *real* deadline in the test runner.
            self.assertEqual(1, len(servers), "one server, and it is the one main built")
            server = servers[0]
            self.assertEqual(
                watchdog.GRACEFUL_SHUTDOWN_S,
                server.config.timeout_graceful_shutdown,
                "the in-flight wait has to be bounded, or a stop request is only a hope",
            )
            arm.assert_not_called()
            server.handle_exit(signal.SIGTERM, None)
            arm.assert_called_once()
            self.assertTrue(
                server.should_exit,
                "the graceful path still runs — the deadline is a bound behind it, not a "
                "replacement for it",
            )
            leave.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
