"""`scripts/check_ghidra.py` (`make check-ghidra`) reports honestly, in both directions.

The script is the named real-Ghidra verification and is outside the gate, so the gate must at least prove the script
itself: it passes against an install that behaves, fails (rather than skipping) where Ghidra is missing or broken, and
never touches the caller's own `CODIFY_HOME`. It runs here against the stand-in `analyzeHeadless`.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import fake_ghidra

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_ghidra.py"


class TestTheRealGhidraCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.binary = self.base / "small.bin"
        self.binary.write_bytes(b"\x7fELF" + bytes(range(256)))
        self.mine = self.base / "my-codify-home"
        self.mine.mkdir()

    def run_script(self, install: Path | None, *flags: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "GHIDRA_INSTALL_DIR"}
        env["CODIFY_HOME"] = str(self.mine)
        if install is not None:
            env["GHIDRA_INSTALL_DIR"] = str(install)
        return subprocess.run(  # noqa: S603 — this interpreter and the script under test
            [sys.executable, str(SCRIPT), "--binary", str(self.binary), *flags],
            env=env, capture_output=True, text=True, timeout=120, check=False, cwd=REPO,
        )

    def test_it_passes_against_an_install_that_behaves_and_says_which_checks_it_made(self) -> None:
        done = self.run_script(fake_ghidra.make_install(self.base))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("Ghidra 12.0.2 at ", done.stdout)
        for check in ("summary lists functions", "functions view has rows", "imports view answers",
                      "sections view has rows", "served from the cache", "no run directory is left behind"):
            self.assertIn(check, done.stdout)
        self.assertTrue(done.stdout.rstrip().endswith("PASS"))

    def test_without_ghidra_it_fails_and_names_why_it_does_not_skip(self) -> None:
        done = self.run_script(None)
        self.assertEqual(1, done.returncode)
        self.assertIn("FAIL  Ghidra is not usable (not_configured)", done.stdout)

    def test_an_install_that_cannot_analyse_is_a_failure_with_the_reason(self) -> None:
        done = self.run_script(fake_ghidra.make_install(self.base, mode="exit"))
        self.assertEqual(1, done.returncode)
        self.assertIn("the analysis did not run (failed)", done.stdout)

    def test_an_export_with_nothing_in_it_does_not_pass(self) -> None:
        empty = {**fake_ghidra.GOOD_EXPORT, "functions": [], "blocks": [], "totals": {"functions": 0, "imports": 0, "strings": 0, "blocks": 0}}
        done = self.run_script(fake_ghidra.make_install(self.base, export=empty))
        self.assertEqual(1, done.returncode, done.stdout)
        self.assertIn("FAIL", done.stdout)

    def test_it_never_writes_to_the_callers_own_state_directory(self) -> None:
        self.run_script(fake_ghidra.make_install(self.base))
        self.assertEqual([], list(self.mine.iterdir()))

    def test_a_missing_binary_is_a_usage_error_not_a_pass(self) -> None:
        self.binary.unlink()
        self.assertEqual(2, self.run_script(fake_ghidra.make_install(self.base)).returncode)


if __name__ == "__main__":
    unittest.main()
