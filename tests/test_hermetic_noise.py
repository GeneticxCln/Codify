"""The suite's output is not buried under asyncio's slow-callback notes.

`IsolatedAsyncioTestCase` runs each test on a loop in debug mode, which logs any step over 100 ms as
"Executing <Task ...> took 0.130 seconds". On a loaded machine that was about three hundred lines a run, each one a
page-wide task repr, between the reader and the one line a failure prints. `tests/hermetic.py` drops those notes and
only those: the rest of debug mode is how a never-awaited coroutine gets noticed.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import logging
import unittest


class TestSlowCallbackNotes(unittest.TestCase):
    def test_a_slow_callback_note_is_dropped(self) -> None:
        with self.assertNoLogs("asyncio", level="DEBUG"):
            logging.getLogger("asyncio").warning(
                "Executing <Task finished name='Task-8800' coro=<ServerCase.asyncSetUp() done> result=None> took 0.128 seconds"
            )

    def test_every_other_asyncio_message_still_gets_through(self) -> None:
        with self.assertLogs("asyncio", level="WARNING") as seen:
            logging.getLogger("asyncio").warning("coroutine 'x' was never awaited")
            logging.getLogger("asyncio").error("Task exception was never retrieved")
            # It has to be the whole shape: a message that merely begins "Executing" is somebody else's.
            logging.getLogger("asyncio").warning("Executing the plan took a while")
        self.assertEqual(len(seen.records), 3)

    def test_importing_the_module_again_does_not_stack_filters(self) -> None:
        before = len(logging.getLogger("asyncio").filters)
        hermetic.quiet_slow_callback_notes()
        hermetic.quiet_slow_callback_notes()
        self.assertEqual(len(logging.getLogger("asyncio").filters), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
