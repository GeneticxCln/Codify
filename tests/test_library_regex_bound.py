"""One regular expression cannot freeze the engine.

The finding this exists for (audit of 2026-09-29, H2): `search_code(regex=true)` ran the model's
pattern with `re` on a worker thread and "bounded" it with a deadline checked between *files*.
CPython's `re` cannot be interrupted and holds the GIL for the whole match, so `(a+)+$` against
one 28-character line took 13.9 s against an advertised 2.0 s, and the event loop stalled for
14.0 s even though the search was not on the loop's thread: HTTP, WebSockets, `/health` and
Cancel all waited. Each extra character doubles it, and a long minified line in a repository
is enough without any hostile pattern.

A thread cannot be made to stop, so the match runs in a process the engine can kill. The
assertions are about what the engine experiences — the loop keeps ticking, the call comes back
refused within a bound, nothing is left running — not about how the worker is arranged.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine import library
from engine.library import LibraryService

# 28 characters is the audit's own measurement: 13.9 s in-process on the audit machine.
CATASTROPHIC_LINE = "a" * 28 + "!\n"
CATASTROPHIC_PATTERN = r"(a+)+$"


def regex_workers() -> list[int]:
    """Pids of any regex worker still running (Linux `/proc`, which is the only target)."""
    found: list[int] = []
    worker = library.REGEX_WORKER.encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        # An argv element equal to the worker's path, not a substring of the command line: a
        # shell whose `-c` text merely mentions the file (or an editor with it open) is not a
        # worker.
        if worker in argv:
            found.append(int(entry.name))
    return found


class TestARegexCannotFreezeTheEngine(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "minified.js").write_text(CATASTROPHIC_LINE, encoding="utf-8")
        (self.root / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
        self.lib = LibraryService(str(self.root))

    async def test_a_catastrophic_pattern_is_refused_and_the_event_loop_never_stalls(self) -> None:
        gaps: list[float] = []
        running = True

        async def ticker() -> None:
            last = time.monotonic()
            while running:
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        watcher = asyncio.create_task(ticker())
        await asyncio.sleep(0.1)
        started = time.monotonic()
        try:
            with mock.patch.object(library, "REGEX_HARD_LIMIT_S", 1.5, create=True), \
                    mock.patch.object(library, "REGEX_BUDGET_S", 0.5, create=True):
                with self.assertRaises(ValueError) as caught:
                    await asyncio.to_thread(self.lib.search, CATASTROPHIC_PATTERN, regex=True)
        finally:
            elapsed = time.monotonic() - started
            running = False
            await watcher

        self.assertIn("too expensive", str(caught.exception))
        self.assertLess(elapsed, 3, "the search was not bounded")
        self.assertLess(
            max(gaps), 0.5,
            f"the event loop stalled for {max(gaps):.1f}s while a regex ran on a worker thread",
        )
        deadline = time.monotonic() + 3
        while regex_workers() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.assertEqual([], regex_workers(), "the worker that ran the pattern is still alive")

    async def test_the_production_bounds_answer_a_catastrophic_pattern_inside_three_seconds(self) -> None:
        # The other test shrinks the limits so the suite stays quick. This one does not touch
        # them: the numbers that ship are the numbers that are held to the bound.
        started = time.monotonic()
        with self.assertRaises(ValueError):
            await asyncio.to_thread(self.lib.search, CATASTROPHIC_PATTERN, regex=True)
        self.assertLess(time.monotonic() - started, 3)
        self.assertLessEqual(library.REGEX_HARD_LIMIT_S, 3)

    async def test_an_ordinary_pattern_still_answers(self) -> None:
        result = await asyncio.to_thread(self.lib.search, r"def\s+greet", regex=True)

        self.assertTrue(result["regex"])
        self.assertEqual(["app.py"], [m["path"] for m in result["matches"]])
        self.assertEqual(1, result["matches"][0]["line"])
        self.assertEqual([], regex_workers())

    async def test_a_worker_that_dies_is_a_refusal_naming_the_failure_not_a_crash(self) -> None:
        broken = self.root.parent / "broken_worker.py"
        broken.write_text("import sys\nsys.stderr.write('boom from the worker')\nsys.exit(3)\n", encoding="utf-8")
        self.addCleanup(broken.unlink)

        with mock.patch.object(library, "REGEX_WORKER", str(broken), create=True):
            with self.assertRaises(ValueError) as caught:
                await asyncio.to_thread(self.lib.search, "greet", regex=True)

        self.assertIn("boom from the worker", str(caught.exception))

    async def test_a_pattern_the_parser_rejects_never_starts_a_process(self) -> None:
        with mock.patch("engine.library.subprocess.Popen", side_effect=AssertionError("spawned")):
            with self.assertRaises(ValueError) as caught:
                await asyncio.to_thread(self.lib.search, "([unclosed", regex=True)
        self.assertIn("invalid regex", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
