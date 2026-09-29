"""Prove the per-commit build check decides what it claims, and says so out loud.

`scripts/check_history.py` answers a question `make ci` cannot — was every
commit in this range green, or only the tip — and a checker that answers it
badly is worse than none. The two failure modes that matter are both quiet:

* it skips work and exits 0, so a range of red middles passes as green;
* it reports thirty "FAIL"s and no reason, so nobody acts on the report.

So the policy is separated from the half that runs things (`skip_reason`,
`verdict`, `first_error_line`, `format_table`, `build_legs`) and this suite
covers it directly. Nothing here spawns a process: the sweep is minutes per
commit, and a test that ran it would be a test nobody keeps. What is pinned is
the decision each result produces, which is the part a future edit could
quietly invert.

The legs themselves are named here too, because the leg list is a judgement
about what can break a split and it should be a visible one. Adding a leg
without a reason, or dropping one that decided seven commits ago, is the kind
of drift this file makes visible.
"""

from __future__ import annotations

import contextlib
import io
import signal
import unittest
from unittest.mock import patch

from scripts import check_history
from scripts.check_history import (
    CommitResult,
    build_legs,
    first_error_line,
    format_table,
    skip_reason,
    verdict,
)

PROJECT_ROOT_LEGS = ("lint", "typecheck", "python", "tsc", "tsc-tests", "uitest")


class TestSkipRule(unittest.TestCase):
    """A one-commit push has no middle, so it is `make ci`'s problem, not this
    tool's — and the tool says that rather than exiting 0 in silence."""

    def test_an_empty_range_is_skipped_with_a_reason(self) -> None:
        reason = skip_reason(0)
        assert reason is not None, "an empty range must explain itself"
        self.assertIn("no commits", reason)

    def test_a_one_commit_range_is_skipped_because_it_has_no_middle(self) -> None:
        reason = skip_reason(1)
        assert reason is not None, "a one-commit range must explain itself"
        self.assertIn("no middle", reason)
        # It names the gate that already covers it, so a reader can tell the
        # difference between "not run" and "passed trivially".
        self.assertIn("make ci", reason)

    def test_two_commits_is_enough_to_be_worth_checking(self) -> None:
        """The threshold is exactly here: a range grows a middle at two."""
        self.assertIsNone(skip_reason(2))
        self.assertIsNone(skip_reason(7))


class TestVerdict(unittest.TestCase):
    def test_a_fully_green_range_exits_zero(self) -> None:
        results = [CommitResult("aaaaaaa", "one"), CommitResult("bbbbbbb", "two")]
        for result in results:
            for leg in PROJECT_ROOT_LEGS:
                result.legs[leg] = "ok"
        self.assertEqual(0, verdict(results))

    def test_one_broken_commit_makes_the_whole_range_red(self) -> None:
        good = CommitResult("aaaaaaa", "one")
        good.legs = {leg: "ok" for leg in PROJECT_ROOT_LEGS}
        bad = CommitResult("bbbbbbb", "two")
        bad.legs = {leg: "ok" for leg in PROJECT_ROOT_LEGS}
        bad.legs["uitest"] = "FAIL"
        self.assertEqual(1, verdict([good, bad]))

    def test_a_commit_that_could_not_be_checked_is_not_a_pass(self) -> None:
        """A missing toolchain blocks the commit rather than passing it. Reading
        "no legs ran" as "every leg passed" is the exact silence this tool
        exists to stop, and it is why `blocked` is a field instead of a
        sentinel value smuggled into `legs`."""
        result = CommitResult("aaaaaaa", "one")
        result.blocked = "no ui/node_modules to reuse"
        self.assertFalse(result.ok)
        self.assertEqual(1, verdict([result]))
        self.assertIn("not checked", format_table([result], list(PROJECT_ROOT_LEGS)))

    def test_a_commit_with_no_legs_at_all_is_not_a_pass(self) -> None:
        """`--legs` naming nothing leaves a commit with an empty result. An
        empty dict must not read as vacuously green."""
        self.assertEqual(1, verdict([CommitResult("aaaaaaa", "one")]))


class TestFirstErrorLine(unittest.TestCase):
    """A report of thirty FAILs with no reasons is a report nobody acts on, and
    the whole reason the first matching line is extracted per leg."""

    def test_mypy_reports_its_error_line(self) -> None:
        out = "engine/app.py:12: error: Module has no attribute \"X\"  [attr-defined]\nFound 4 errors"
        self.assertIn("engine/app.py:12", first_error_line("typecheck", out))

    def test_unittest_reports_its_failing_test(self) -> None:
        out = "FAIL: test_the_boot_notice (test_home.Suite)\nRan 951 tests\nFAILED (failures=1)"
        self.assertIn("test_the_boot_notice", first_error_line("python", out))

    def test_tsc_reports_its_error_line(self) -> None:
        out = "src/App.tsx(90,5): error TS2345: Argument of type 'x' is not assignable"
        self.assertIn("TS2345", first_error_line("tsc", out))

    def test_node_test_reports_its_failing_test(self) -> None:
        out = "ℹ fail 1\n✖ the shell mounts once (1.2ms)"
        self.assertIn("the shell mounts once", first_error_line("uitest", out))

    def test_noise_is_not_mistaken_for_a_reason(self) -> None:
        """`npm notice` and asyncio's slow-test warning are the two things that
        appear above every real error in this project's output."""
        out = "npm notice run codify-desktop-ui@0.2.0 test\nExecuting <Task pending> took 0.2s\nerror TS1: real"
        self.assertIn("TS1", first_error_line("tsc", out))

    def test_an_unrecognised_failure_still_says_something(self) -> None:
        """A leg that fails in a way the markers do not know must not produce an
        empty cell — an empty reason reads as "no reason given" and gets ignored."""
        self.assertNotEqual("", first_error_line("lint", "Killed: out of memory"))
        self.assertNotEqual("", first_error_line("lint", ""))


class TestTheReport(unittest.TestCase):
    def test_a_failing_row_carries_its_reason_underneath(self) -> None:
        result = CommitResult("abc1234", "ui: eight themes")
        result.legs = {leg: "ok" for leg in PROJECT_ROOT_LEGS}
        result.legs["uitest"] = "FAIL"
        result.reasons["uitest"] = "the shell mounts once"
        table = format_table([result], list(PROJECT_ROOT_LEGS))
        self.assertIn("abc1234", table)
        self.assertIn("ui: eight themes", table)
        self.assertIn("uitest", table)
        self.assertIn("the shell mounts once", table)

    def test_every_leg_is_its_own_column(self) -> None:
        result = CommitResult("abc1234", "s")
        result.legs = {leg: "ok" for leg in PROJECT_ROOT_LEGS}
        table = format_table([result], list(PROJECT_ROOT_LEGS))
        for leg in PROJECT_ROOT_LEGS:
            self.assertIn(leg, table)


class TestTheLegs(unittest.TestCase):
    """The leg list is the judgement about what can break a split, so it is
    pinned rather than left to whatever the default happens to be today."""

    def test_the_default_legs_are_the_decisive_ones(self) -> None:
        legs = build_legs(check_history.ROOT / ".venv", check_history.ROOT / "ui" / "node_modules")
        self.assertEqual(list(PROJECT_ROOT_LEGS), [leg.name for leg in legs])

    def test_the_python_leg_runs_the_suite_and_not_a_smoke_test(self) -> None:
        """A leg that ran a subset would report a broken commit as green, which
        is the failure this whole tool exists to prevent."""
        legs = build_legs(check_history.ROOT / ".venv", check_history.ROOT / "ui" / "node_modules")
        python_leg = next(leg for leg in legs if leg.name == "python")
        self.assertIn("unittest", python_leg.argv)
        self.assertIn("discover", python_leg.argv)
        self.assertIn("test_*.py", python_leg.argv)

    def test_the_typescript_legs_typecheck_the_tests_too(self) -> None:
        """`node --test` erases types rather than checking them, so the suite
        that reads a component's props passes whatever the interface became.
        Without `tsc-tests` a split that only breaks a type would read green."""
        legs = build_legs(check_history.ROOT / ".venv", check_history.ROOT / "ui" / "node_modules")
        test_leg = next(leg for leg in legs if leg.name == "tsc-tests")
        self.assertIn("tsconfig.test.json", test_leg.argv)
        self.assertEqual("ui", test_leg.cwd)

    def test_a_missing_tool_is_named_rather_than_resolved_to_nothing(self) -> None:
        """The fallback keeps the bare name so the leg fails as a missing command
        with an actionable message, never as an argv that silently does nothing."""
        legs = build_legs(check_history.ROOT / ".venv", check_history.ROOT / "ui" / "node_modules")
        for leg in legs:
            self.assertTrue(all(part for part in leg.argv), f"{leg.name} has an empty argv entry")


class TestTheTargetIsWired(unittest.TestCase):
    def test_the_makefile_target_passes_a_range(self) -> None:
        """A target that ran the script with no range would check HEAD against
        nothing and pass — the silent-skip failure one level up."""
        makefile = (check_history.ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("HISTORY_RANGE ?=", makefile)
        self.assertRegex(makefile, r"(?m)^\tpython3 scripts/check_history\.py --range")

    def test_the_target_is_not_part_of_the_gate(self) -> None:
        """It answers a different question of different commits. Folding it into
        `check` would either re-run the whole gate per commit or make `make ci`
        depend on a remote-tracking ref being present — and a gate that needs the
        network to run is a gate that gets skipped."""
        makefile = (check_history.ROOT / "Makefile").read_text(encoding="utf-8")
        for line in makefile.splitlines():
            if line.startswith(("check:", "ci:")):
                self.assertNotIn("check-history", line, f"{line!r} would pull the sweep into the gate")


class TestCleanupIsArmedBeforeTheSweep(unittest.TestCase):
    """The sweep is minutes per commit, so stopping it must not strand it.

    `atexit` and the SIGINT/SIGTERM handlers were registered *after* the commit
    loop — armed only once the sweep had already finished. A Ctrl-C (or any
    exception) in the middle therefore found nothing registered: `check_one`'s
    own `finally` tore down the worktree of the commit in flight, and every
    other worktree — plus the bookkeeping `_worktrees_created` held for exactly
    this — was left on disk until reboot. A checker that litters on the one
    exit path a user actually exercises is a checker people delete rather than
    run, which is the same judgment `check_one`'s docstring already makes.

    The test drives `main()` in-process with `check_one` raising on the first
    commit — the shape of a mid-sweep abort, without a real sweep — and asserts
    that by the moment that abort happens, `atexit` holds `_cleanup_all` and
    both signals have handlers. Registered after the loop, neither holds.
    Everything that could touch the machine is mocked, so the test runs in
    milliseconds and never clones a thing.
    """

    def test_an_abort_mid_sweep_finds_the_handlers_already_armed(self) -> None:
        with (
            patch.object(check_history, "check_one", side_effect=KeyboardInterrupt),
            patch.object(check_history, "_commits_in_range", return_value=(["aaa1111", "bbb2222"], "")),
            patch.object(check_history, "_subject", return_value="a subject"),
            patch.object(check_history, "_deps_moved", return_value=False),
            patch.object(
                check_history,
                "build_legs",
                return_value=[check_history.Leg("probe", ("true",))],
            ),
            patch("atexit.register") as register,
            patch("scripts.check_history.signal.signal") as set_handler,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                check_history.main(["--range", "aaa1111..bbb2222"])

        # The abort happened inside the first `check_one` — that is the mocked
        # side effect — so anything registered by now was registered before the
        # sweep got past its first commit.
        register.assert_called_once_with(check_history._cleanup_all)
        armed = {call.args[0] for call in set_handler.call_args_list}
        self.assertIn(signal.SIGINT, armed, "Ctrl-C mid-sweep would strand the worktrees")
        self.assertIn(signal.SIGTERM, armed, "a killed sweep would strand the worktrees")


if __name__ == "__main__":
    unittest.main()
