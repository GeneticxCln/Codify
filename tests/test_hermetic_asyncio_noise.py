"""The suite's output is not buried under asyncio's slow-callback warnings.

`IsolatedAsyncioTestCase` runs each test's loop in debug mode, which logs "Executing <Task …> took 0.126
seconds" for every callback over 100 ms: about three hundred lines a run in front of the summary. `tests/hermetic.py`
drops that one message. These pin both halves of that promise: the noise is gone, and nothing else asyncio says is.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import logging
import time
import unittest


class TestSlowCallbacksAreNotNews(unittest.IsolatedAsyncioTestCase):
    async def test_a_callback_that_holds_the_loop_is_not_logged(self) -> None:
        self.assertTrue(asyncio.get_running_loop().get_debug(), "the loop is not in debug mode, so this proves nothing")

        with self.assertNoLogs("asyncio", level="WARNING"):
            await asyncio.sleep(0)
            time.sleep(0.25)  # holds the loop for this task's whole step, well over the 100 ms threshold
            await asyncio.sleep(0)

    async def test_everything_else_asyncio_logs_still_gets_through(self) -> None:
        with self.assertLogs("asyncio", level="WARNING") as seen:
            logging.getLogger("asyncio").warning("Task exception was never retrieved")
            logging.getLogger("asyncio").error("socket.send() raised exception.")
            logging.getLogger("asyncio").warning("Executing <Handle> is not what it looks like")

        self.assertEqual(3, len(seen.records))

    async def test_a_message_that_only_resembles_one_is_kept(self) -> None:
        with self.assertLogs("asyncio", level="WARNING") as seen:
            logging.getLogger("asyncio").warning("Executing the plan took a while")

        self.assertEqual(1, len(seen.records))


if __name__ == "__main__":
    unittest.main()
