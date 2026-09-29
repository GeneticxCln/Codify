"""The shape of a guarded command, as the spawn freeze reads it (review of 2026-09-29, finding 4).

`guarded_argv` puts the guard in front of a command. It is now idempotent — a command that is already guarded
is returned as it is — because the freeze checks each `Popen` for `guarded_argv(...)` *at the call*, so a spawn
helper that receives an argv from elsewhere (the folder picker does) wraps it at the site instead of trusting
every caller to have done so, and a caller that did is not wrapped twice: a guard in front of a guard would
lead the process group the inner one is trying to lead.
"""

from __future__ import annotations

import sys
import unittest

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.spawn_guard import ENV_PARENT_PID, SPAWN_GUARD, guarded_argv, guarded_env


class TestGuardedArgv(unittest.TestCase):
    def test_the_guard_leads_and_the_command_follows(self) -> None:
        self.assertEqual([sys.executable, SPAWN_GUARD, "ls", "-l"], guarded_argv(["ls", "-l"]))

    def test_an_already_guarded_command_is_not_guarded_again(self) -> None:
        once = guarded_argv(["ls", "-l"])

        self.assertEqual(once, guarded_argv(once))

    def test_the_result_is_a_new_list_the_caller_may_edit(self) -> None:
        once = guarded_argv(["ls"])
        again = guarded_argv(once)

        again.append("-l")

        self.assertEqual([sys.executable, SPAWN_GUARD, "ls"], once)

    def test_a_command_that_merely_mentions_the_guard_is_still_wrapped(self) -> None:
        # Only a *leading* interpreter and guard path count as already guarded.
        argv = ["cat", SPAWN_GUARD]

        self.assertEqual([sys.executable, SPAWN_GUARD, *argv], guarded_argv(argv))

    def test_the_environment_carries_this_processs_pid(self) -> None:
        import os

        self.assertEqual(str(os.getpid()), guarded_env({"A": "1"})[ENV_PARENT_PID])
        self.assertEqual("1", guarded_env({"A": "1"})["A"])


if __name__ == "__main__":
    unittest.main()
