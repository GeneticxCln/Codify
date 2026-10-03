"""Data the engine ships with must not be matched by a `.gitignore` rule.

`engine/builtin_profiles/secrets.json` was written, tested and merged, and never committed: line `secrets.json` of
`.gitignore` (the guard for the credentials fallback store) also matches a rule file with that name. Every test passed on
the machine that wrote it, because the file was on its disk, and failed on a clean checkout, because it was not in the
repository. `git status` does not list ignored files, so nothing on the author's side showed it.

This asks git the question directly: is any file under a directory of built-in data ignored? `git check-ignore` answers
"no" for a file that is tracked, so a deliberate exception (a `!` line) and a committed file both pass, and the only thing
that fails is the case that bit: a file that exists, matches an ignore rule, and so will not travel.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directories whose every file is read by the engine at run time, so a missing one is a behaviour change nothing else notices.
SHIPPED = ("engine/builtin_profiles", "engine/builtin_skills")


class TestShippedDataTravelsWithTheRepository(unittest.TestCase):
    def shipped_files(self) -> list[str]:
        found = sorted(
            path.relative_to(ROOT).as_posix()
            for directory in SHIPPED
            for path in (ROOT / directory).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
        self.assertTrue(found, f"nothing found under {SHIPPED}: the directories moved and this test did not")
        return found

    def test_no_file_the_engine_ships_with_is_ignored_by_git(self) -> None:
        ignored = []
        for rel in self.shipped_files():
            done = subprocess.run(
                ["git", "check-ignore", "-q", "--", rel], cwd=ROOT, capture_output=True, timeout=30, check=False,
            )
            if done.returncode == 0:
                ignored.append(rel)
            elif done.returncode != 1:
                # 128 is git failing (not a checkout, no git). No skip: a guarantee that cannot be asked is not one.
                self.fail(f"`git check-ignore` could not answer for {rel}: exit {done.returncode}: {done.stderr.decode(errors='replace')}")
        self.assertEqual(
            ignored, [],
            "these files exist but a .gitignore rule matches them, so they will not be committed or cloned: "
            "add a `!path` exception after the rule, or rename the file",
        )

    def test_the_check_can_fail(self) -> None:
        # A negative control, so a `check-ignore` that always answered "no" would not make the test above pass for nothing:
        # a file that is certainly ignored (a bytecode cache) is reported as ignored by the same call.
        done = subprocess.run(
            ["git", "check-ignore", "-q", "--", "engine/__pycache__/x.pyc"], cwd=ROOT, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(done.returncode, 0, "git did not call a .pyc ignored; the question above is not being asked properly")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
