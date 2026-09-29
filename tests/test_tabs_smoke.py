"""The tabs-smoke harness, without a display.

`scripts/tabs_smoke.py` needs a real window and a real engine to do its job,
and those are exactly the things it must not need to have its *judgement*
tested. The verdict logic is separable from the launch — `run_smoke` reads the
child's stdout lines and decides — so these tests feed it a child that replays
a recorded run and assert the decision, the diagnosis and the contract strings.
The end-to-end claim itself stays with `make smoke-tabs`, which is where a
display and a display's failures belong.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from io import StringIO
from pathlib import Path

# `scripts` is a package (`scripts/__init__.py`), imported the way
# `tests/test_embed_probe.py` imports `scripts.embed_smoke` — a plain package
# import, no sys.path surgery and no `# type: ignore[import-not-found]`, which
# this repo treats as a config problem (pyproject.toml says why).
from scripts import tabs_smoke

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class VerdictTestCase(unittest.TestCase):
    """`run_smoke` against a fake child: the decision and the diagnosis."""

    def run_script(self, body: str, engage_timeout: float = 2.0) -> tuple[int, str]:
        """Run the harness's `main` with a shell binary that is this script."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "codify-desktop"
            # lstrip, not dedent alone: the shebang must be byte 0 or exec
            # reads the file as a script with no interpreter line.
            fake.write_text(textwrap.dedent(body).lstrip("\n"))
            fake.chmod(0o755)
            original = tabs_smoke.SHELL_BINARY
            tabs_smoke.SHELL_BINARY = fake
            # The engagement bound is production's 20s; a recorded replay needs
            # two, or the suite pays for a deadline aimed at a real desktop.
            # The retry policy is production's 4 x 3s for the same reason.
            original_engage = tabs_smoke.ENGAGE_TIMEOUT_S
            original_retries = tabs_smoke.GUARD_RETRIES
            original_backoff = tabs_smoke.GUARD_BACKOFF_S
            tabs_smoke.ENGAGE_TIMEOUT_S = engage_timeout
            tabs_smoke.GUARD_BACKOFF_S = 0.1
            # Captured, not inherited: the harness relays the child's streams,
            # and the test reads them back from the buffer.
            stdout, stderr = StringIO(), StringIO()
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = stdout, stderr
            try:
                code = tabs_smoke.main(["--timeout", "90"])
                return code, stdout.getvalue() + stderr.getvalue()
            finally:
                sys.stdout, sys.stderr = old_out, old_err
                tabs_smoke.SHELL_BINARY = original
                tabs_smoke.ENGAGE_TIMEOUT_S = original_engage
                tabs_smoke.GUARD_RETRIES = original_retries
                tabs_smoke.GUARD_BACKOFF_S = original_backoff

    def test_a_pass_is_engaged_passed_seeded_and_exited_zero(self) -> None:
        code, output = self.run_script(
            """
            #!/usr/bin/env python3
            print("tabs-smoke: mode engaged")
            print("tabs-smoke: engine is up on 7430")
            print("tabs-smoke: seeded k_tabs_smoke_seed")
            print("tabs-smoke: restored browser k_tabs_smoke_seed")
            print("tabs-smoke: PASS")
            raise SystemExit(0)
            """
        )
        self.assertEqual(code, 0, output)
        self.assertIn("PASS in", output)

    def test_a_seed_that_never_came_back_fails_and_names_the_gap(self) -> None:
        code, output = self.run_script(
            """
            #!/usr/bin/env python3
            print("tabs-smoke: mode engaged")
            print("tabs-smoke: engine is up on 7430")
            print("tabs-smoke: seeded k_tabs_smoke_seed")
            print("tabs-smoke: FAILED the seeded tab was never seated within 75s")
            raise SystemExit(1)
            """
        )
        self.assertEqual(code, 1, output)
        self.assertIn("FAIL after", output)
        self.assertIn("the seeded tab was never seated", output)

    def test_a_pass_without_the_seed_in_the_strip_is_a_fail(self) -> None:
        # The child said PASS, but the restored lines are the strip as the
        # engine held it, and the seed is not in them. The harness reads the
        # strip for itself precisely so a verdict cannot outrun its evidence.
        code, output = self.run_script(
            """
            #!/usr/bin/env python3
            print("tabs-smoke: mode engaged")
            print("tabs-smoke: seeded k_tabs_smoke_seed")
            print("tabs-smoke: restored browser k_other")
            print("tabs-smoke: PASS")
            raise SystemExit(0)
            """
        )
        self.assertEqual(code, 1, output)
        self.assertIn("not the seed", output)

    def test_silence_is_diagnosed_as_stale_binary_or_guard(self) -> None:
        code, output = self.run_script(
            """
            #!/usr/bin/env python3
            import time
            time.sleep(300)
            """
        )
        self.assertEqual(code, 1, output)
        self.assertIn("never announced the mode", output)
        self.assertIn("single-instance guard", output)
        self.assertIn("--rebuild", output)

    def test_a_guard_refusal_is_retried_and_a_late_engagement_recovers(self) -> None:
        # The refusal shape — silent, exit 0, fast, never engaged — is the
        # previous run's widow holding the bus name. The harness retries it;
        # the second launch here engages for real, which is the recovery the
        # policy exists for.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "seen"
            fake = Path(tmp) / "codify-desktop"
            fake.write_text(
                textwrap.dedent(
                    f"""
                    #!/usr/bin/env python3
                    import os, sys
                    if not os.path.exists({str(marker)!r}):
                        # The refusal: silent, exit 0, immediate — the shape
                        # the single-instance guard leaves behind.
                        open({str(marker)!r}, "w").close()
                        sys.exit(0)
                    print("tabs-smoke: mode engaged")
                    print("tabs-smoke: engine is up on 7430")
                    print("tabs-smoke: seeded k_tabs_smoke_seed")
                    print("tabs-smoke: restored browser k_tabs_smoke_seed")
                    print("tabs-smoke: PASS")
                    raise SystemExit(0)
                    """
                ).lstrip("\n")
            )
            fake.chmod(0o755)
            original = tabs_smoke.SHELL_BINARY
            original_engage = tabs_smoke.ENGAGE_TIMEOUT_S
            original_retries = tabs_smoke.GUARD_RETRIES
            original_backoff = tabs_smoke.GUARD_BACKOFF_S
            tabs_smoke.SHELL_BINARY = fake
            tabs_smoke.ENGAGE_TIMEOUT_S = 2.0
            tabs_smoke.GUARD_BACKOFF_S = 0.1
            stdout, stderr = StringIO(), StringIO()
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = stdout, stderr
            try:
                code = tabs_smoke.main(["--timeout", "90"])
                output = stdout.getvalue() + stderr.getvalue()
            finally:
                sys.stdout, sys.stderr = old_out, old_err
                tabs_smoke.SHELL_BINARY = original
                tabs_smoke.ENGAGE_TIMEOUT_S = original_engage
                tabs_smoke.GUARD_RETRIES = original_retries
                tabs_smoke.GUARD_BACKOFF_S = original_backoff
        self.assertEqual(code, 0, output)
        self.assertIn("retry 1/", output)
        self.assertIn("PASS in", output)

    def test_a_run_that_never_engaged_at_all_is_a_failure_not_an_exit_zero(self) -> None:
        # The single-instance guard exits 0, silently, so by exit status alone a
        # refusal is indistinguishable from a pass. Every attempt refusing means
        # the smoke never happened — no window, no engine, nothing proven — and a
        # harness that returned the refusal's own status would report a green
        # `make smoke-tabs` for a run that opened nothing. The one verdict this
        # path may print is a failure.
        code, output = self.run_script(
            """
            #!/usr/bin/env python3
            raise SystemExit(0)
            """
        )
        self.assertEqual(code, 1, output)
        self.assertIn("every launch was refused", output)
        self.assertIn("single-instance name", output)

    def test_the_timeout_floor_leaves_room_for_the_shell_deadline(self) -> None:
        # The shell fails itself at 75s; a harness bound at or below that
        # would kill the child mid-verdict and turn a named failure into a
        # silence. argparse is the gate.
        with self.assertRaises(SystemExit):
            tabs_smoke.main(["--timeout", "75"])
        with self.assertRaises(SystemExit):
            tabs_smoke.main(["--timeout", "20"])


class ContractTestCase(unittest.TestCase):
    """The strings the two halves of the smoke share, held in one place."""

    def test_the_shell_lines_and_the_harness_agree(self) -> None:
        # The Rust freeze (`the_tabs_smoke_lines_name_the_reader_that_waits_for_
        # them`) reads this file for its constants; this reads the Rust for
        # the same strings, so a rename on either side fails both gates.
        rust = (PROJECT_ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
        for constant in (
            tabs_smoke.TABS_SMOKE_ENV,
            tabs_smoke.LINE,
        ):
            self.assertIn(constant, rust)

    def test_the_env_gate_is_written_once_and_copied_from_the_real_environment(self) -> None:
        # A developer's shell that happens to export the variable would turn
        # every boot into a smoke; exactly one writer exists, and it is the
        # copy `main` hands the child — which itself must come from
        # os.environ.copy(), so the developer's exported variables travel and
        # only the two facts that make the run safe are added.
        source = (PROJECT_ROOT / "scripts" / "tabs_smoke.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("env[TABS_SMOKE_ENV]"), 1)
        self.assertIn("os.environ.copy()", source)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
