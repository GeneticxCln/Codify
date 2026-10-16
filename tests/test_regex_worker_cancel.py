"""A Cancel stops the file-scanning worker where it runs, not when its clock ends.

Before this, `library._run_regex_worker` waited with `communicate(timeout=...)` and took no signal: a command stopped on a
Cancel within `CANCEL_POLL_S`, a regex search or scan ran to its hard limit (2.5 s, 15 s). The audit in `docs/14` §3 named
it as the cost that arrives with any longer child. These tests use the same catastrophic pattern as
`test_library_regex_bound.py`, which holds the worker for its whole budget, so a worker that stops early was stopped.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from engine import library
from engine.library import LibraryService, RegexWorkerCancelled
from engine.scan import PROFILES_DIRNAME, run_scan
from tests.test_library_regex_bound import CATASTROPHIC_LINE, CATASTROPHIC_PATTERN, regex_workers


def cancel_soon(delay_s: float) -> threading.Event:
    cancel = threading.Event()
    timer = threading.Timer(delay_s, cancel.set)
    timer.daemon = True
    timer.start()
    return cancel


class TestCancelReachesTheWorker(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "minified.js").write_text(CATASTROPHIC_LINE, encoding="utf-8")

    def assertGone(self) -> None:
        deadline = time.monotonic() + 3
        while regex_workers() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([], regex_workers(), "the worker is still alive after the cancel")

    def test_a_search_stops_at_the_cancel_not_at_its_clock(self) -> None:
        cancel = cancel_soon(0.4)
        started = time.monotonic()
        with self.assertRaises(RegexWorkerCancelled) as caught:
            LibraryService(str(self.root)).search(CATASTROPHIC_PATTERN, regex=True, cancel=cancel)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, library.REGEX_HARD_LIMIT_S - 0.5, f"waited {elapsed:.1f}s: it ran to its clock")
        self.assertIn("cancelled", str(caught.exception))
        self.assertGone()

    def test_a_scan_stops_at_the_cancel(self) -> None:
        profiles = self.root / PROFILES_DIRNAME
        profiles.mkdir(parents=True)
        (profiles / "boom.json").write_text(
            json.dumps({
                "name": "boom", "description": "a slow rule",
                "rules": [{
                    "id": "boom", "title": "Slow", "severity": "info", "pattern": CATASTROPHIC_PATTERN,
                    "languages": [], "where": "raw", "why": "it is slow", "fix": "none",
                }],
            }),
            encoding="utf-8",
        )
        cancel = cancel_soon(0.4)
        started = time.monotonic()
        with self.assertRaises(ValueError) as caught:
            run_scan(str(self.root), profile="boom", cancel=cancel)
        self.assertIsInstance(caught.exception, RegexWorkerCancelled)
        self.assertLess(time.monotonic() - started, 5)
        self.assertGone()

    def test_a_signal_already_set_never_starts_the_wait(self) -> None:
        cancel = threading.Event()
        cancel.set()
        started = time.monotonic()
        with self.assertRaises(RegexWorkerCancelled):
            LibraryService(str(self.root)).search(CATASTROPHIC_PATTERN, regex=True, cancel=cancel)
        self.assertLess(time.monotonic() - started, 2)
        self.assertGone()

    def test_an_unset_signal_changes_nothing_about_an_ordinary_search(self) -> None:
        (self.root / "a.txt").write_text("needle here\n", encoding="utf-8")
        found = LibraryService(str(self.root)).search("needle", regex=True, cancel=threading.Event())
        self.assertEqual(["a.txt"], [m["path"] for m in found["matches"]])

    def test_without_a_signal_the_clock_still_ends_it(self) -> None:
        with self.assertRaises(library.RegexWorkerTimeout):
            LibraryService(str(self.root)).search(CATASTROPHIC_PATTERN, regex=True)
        self.assertGone()


if __name__ == "__main__":
    unittest.main()
