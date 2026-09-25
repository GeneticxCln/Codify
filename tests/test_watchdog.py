"""The engine's half of "close the window, stop the engine".

The desktop shell already kills its child on a clean exit, and on a dropped handle.
The failure this suite exists for is the one a signal leaves behind: the window is
gone and `python3 -m engine` is still holding port 7430 and the SQLite file, so the
next launch has to fight it for both. `CODIFY_PARENT_PID` is the contract — the
shell passes its own pid, the engine watches it — so these tests pin the states that
matter: armed with a live pid, disarmed without one (a standalone run must behave
exactly as it did before this existed), and a polling loop that actually reaches its
handler instead of stopping after one look.

Nothing here kills a process: the loop takes its predicate and its handler as
arguments, and only the predicates themselves are exercised against real pids.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from engine import watchdog


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


if __name__ == "__main__":
    unittest.main()
