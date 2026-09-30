"""The gate must not be able to pass a run that died.

This exists because it happened. `engine.app.main` ended the process through
`watchdog.hard_exit` — `os._exit(0)` — and two tests called it in-process. The
test runner was gone at that point, silently, with exit status 0, roughly halfway
through the suite. `make test-engine` saw 0 and reported success; 549 of 1054
tests had never run, and every one of them was free to be broken.

`engine.app` is split now (`serve` returns, `main` is the entry point), so the
specific cause is fixed. The guard in `scripts/run_tests.py` is what makes the
*class* of cause impossible to mistake for a pass, and this is the test that says
so: a throwaway module that calls `os._exit(0)` mid-run, run through the real
wrapper, which has to come back non-zero.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts.run_tests import completed, leaked_connections

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "scripts" / "run_tests.py"


class RunnerSummaryTest(unittest.TestCase):
    """`completed()` on the two shapes of output unittest can leave behind."""

    def test_a_finished_run_is_recognised(self) -> None:
        self.assertTrue(completed("...\n----------------------------------------------------------------------\nRan 1056 tests in 79.6s\n\nOK\n"))

    def test_a_finished_failing_run_is_still_a_finished_run(self) -> None:
        # The guard is about death, not about redness. `FAILED` is a verdict, and
        # this wrapper must not turn a red suite green.
        self.assertTrue(completed("FAIL: something\nRan 3 tests in 0.1s\n\nFAILED (failures=1)\n"))

    def test_a_run_cut_off_mid_test_is_not_a_pass(self) -> None:
        # Exactly what the suite left behind: progress dots, no summary.
        self.assertFalse(completed(".................................\n"))

    def test_a_summary_without_a_verdict_is_not_a_pass(self) -> None:
        # Reached `Ran …` and then died in teardown. The count alone is not an
        # opinion about whether the run finished.
        self.assertFalse(completed("Ran 1056 tests in 79.6s\n"))

    def test_empty_output_is_not_a_pass(self) -> None:
        self.assertFalse(completed(""))


class LeakedConnectionsTest(unittest.TestCase):
    """`leaked_connections()` reads the one warning that says a database handle was never closed.

    Python 3.13 made that a `ResourceWarning`, and unittest prints it — into the scrollback of a run that
    still ends `OK`. Two of them scrolled past on a developer's 3.14 host leg for as long as the suite had
    leaked, which is how a leak in the test setup of `test_api` went unseen. Nothing older than 3.13 says
    it, so on the 3.10 floor this finds nothing and costs nothing.
    """

    REAL = (
        "/usr/lib/python3.14/traceback.py:393: ResourceWarning: unclosed database in "
        "<sqlite3.Connection object at 0x7f2b8afb7790>\n  frame = frame.f_back\n"
        "ResourceWarning: Enable tracemalloc to get the object allocation traceback\n"
    )

    def test_the_real_warning_is_found(self) -> None:
        self.assertEqual(1, leaked_connections(self.REAL))

    def test_each_one_is_counted(self) -> None:
        self.assertEqual(2, leaked_connections(self.REAL + "...ok\n" + self.REAL))

    def test_a_different_resource_warning_is_not_this_guards_business(self) -> None:
        other = (
            "x.py:1: ResourceWarning: unclosed file <_io.TextIOWrapper name='a' mode='r'>\n"
            "y.py:2: ResourceWarning: unclosed <socket.socket fd=5>\n"
        )
        self.assertEqual(0, leaked_connections(other))

    def test_a_clean_run_has_none(self) -> None:
        self.assertEqual(0, leaked_connections("...\nRan 3 tests in 0.1s\n\nOK\n"))
        self.assertEqual(0, leaked_connections(""))


class RunnerRefusesADeadRunTest(unittest.TestCase):
    """The wrapper, end to end, against a suite that really does `os._exit(0)`."""

    def _run_wrapper(self, body: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "test_cut_short.py"
            module.write_text(textwrap.dedent(body))
            return subprocess.run(
                [
                    sys.executable,
                    str(WRAPPER),
                    "discover",
                    "-s",
                    tmp,
                    "-p",
                    "test_*.py",
                    "-v",
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO),
                timeout=120,
            )

    def test_a_suite_that_exits_zero_mid_run_is_reported_as_dead(self) -> None:
        # The bug, reproduced in miniature: two tests pass, the third ends the
        # process with status 0, and the wrapper has to notice.
        result = self._run_wrapper(
            """
            import os
            import unittest


            class CutShort(unittest.TestCase):
                def test_a_passes(self):
                    self.assertTrue(True)

                def test_b_passes(self):
                    self.assertTrue(True)

                def test_c_ends_the_process(self):
                    os._exit(0)
            """
        )
        self.assertEqual(
            result.returncode,
            1,
            "the wrapper passed a run that exited 0 without a summary — the "
            "exact shape that let half the engine suite go unrun",
        )
        self.assertIn("FATAL", result.stderr)
        self.assertIn("Ran N tests", result.stderr)

    def test_a_suite_that_actually_finishes_reports_its_own_verdict(self) -> None:
        # The other half: a real, complete, *failing* run must come back
        # non-zero with unittest's own verdict intact. A guard that turned red
        # into green would be worse than no guard.
        result = self._run_wrapper(
            """
            import unittest


            class GenuinelyFailing(unittest.TestCase):
                def test_this_fails(self):
                    self.assertEqual(1, 2)
            """
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED", result.stdout + result.stderr)
        self.assertNotIn("FATAL", result.stderr)

    def test_a_suite_that_passes_reports_success(self) -> None:
        result = self._run_wrapper(
            """
            import unittest


            class Fine(unittest.TestCase):
                def test_this_passes(self):
                    self.assertTrue(True)
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FATAL", result.stderr)


class RunnerRefusesALeakedConnectionTest(unittest.TestCase):
    """The wrapper, end to end: a suite that passes but leaves an open connection behind is not a pass.

    The throwaway suite emits the warning itself rather than leaking a real handle, so this holds on every
    Python the gate runs on, the 3.10 floor included, where a real leak would say nothing at all.
    """

    def _run(self, body: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "test_it.py").write_text(textwrap.dedent(body))
            return subprocess.run(
                [sys.executable, str(WRAPPER), "discover", "-s", tmp, "-p", "test_*.py", "-v"],
                capture_output=True, text=True, cwd=str(REPO), timeout=120,
            )

    def test_a_passing_suite_that_leaked_a_connection_fails_and_says_so(self) -> None:
        result = self._run(
            """
            import unittest
            import warnings


            class Leaks(unittest.TestCase):
                def test_passes_but_leaks(self):
                    warnings.warn(
                        "unclosed database in <sqlite3.Connection object at 0x7f0000000001>",
                        ResourceWarning,
                    )
            """
        )
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("FATAL", result.stderr)
        self.assertIn("connection", result.stderr)
        self.assertIn("OK", result.stdout + result.stderr, "the suite itself passed; only the leak fails the run")

    def test_another_resource_warning_does_not_fail_a_passing_suite(self) -> None:
        result = self._run(
            """
            import unittest
            import warnings


            class Other(unittest.TestCase):
                def test_passes(self):
                    warnings.warn("unclosed file <_io.TextIOWrapper name='a' mode='r'>", ResourceWarning)
            """
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("FATAL", result.stderr)


if __name__ == "__main__":
    unittest.main()
