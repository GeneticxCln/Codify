"""`scripts/ci-report.sh` only ever says something true about a commit.

The local gate's verdict is published on the commit as a status, beside the check the
GitHub Actions workflow reports and as the only mark there is when Actions cannot run.
A status is a claim about *a commit*, which
is why most of this file is about the cases where the script must say nothing:
a working tree that is not the commit, a `gh` that is not there, a gate that
rewrote the tree it was testing. A CI mark that can go green on unsaved work is
worse than no mark, because people believe it.

These drive the real script — bash, not a reimplementation — inside a throwaway
git repository, with a stub `gh` that records exactly what it was asked to send.
Nothing here reaches GitHub, which is also what keeps them hermetic; the marker
file each test plants is what proves whether the gate command ran at all.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci-report.sh"

# Records one argument per line, so a test can read back the exact request, and can
# be told to fail. It authenticates nothing, because the script must not either.
GH_STUB = """\
#!/bin/sh
for arg do printf '%s\\n' "$arg"; done >> "$GH_STUB_LOG"
printf -- '--\\n' >> "$GH_STUB_LOG"
exit "${GH_STUB_EXIT:-0}"
"""

# `make` is stubbed for the default-command test only; it must not run a real gate.
MAKE_STUB = """\
#!/bin/sh
printf '%s\\n' "$*" > "$MAKE_STUB_LOG"
exit 0
"""


class CiReportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.bin = base / "bin"
        self.repo.mkdir()
        self.bin.mkdir()
        self.gh_log = base / "gh.log"
        self.make_log = base / "make.log"
        self.marker = base / "gate-ran"
        self.install(self.bin / "gh", GH_STUB)
        self.git("init", "-q", ".")
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-q", "-m", "first")
        self.sha = self.git("rev-parse", "HEAD").strip()

    @staticmethod
    def install(path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            **os.environ,
            # The stubs first, then the real PATH: the script needs git and bash.
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "GH_STUB_LOG": str(self.gh_log),
            "MAKE_STUB_LOG": str(self.make_log),
            # A repository identity that never comes from the developer's machine.
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
        }
        env.update(extra)
        return env

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env(),
            capture_output=True, text=True, check=True,
        )
        return done.stdout

    def report(self, *command: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        # Run from inside the repository, the way `make ci-report` does; the
        # script is invoked directly so a lost executable bit fails here too.
        return subprocess.run(
            [str(SCRIPT), *command], cwd=self.repo, env=self.env(**extra_env),
            capture_output=True, text=True, timeout=60,
        )

    def gh_calls(self) -> list[list[str]]:
        """Every `gh` invocation, as its list of arguments."""
        if not self.gh_log.exists():
            return []
        calls: list[list[str]] = [[]]
        for line in self.gh_log.read_text(encoding="utf-8").splitlines():
            if line == "--":
                calls.append([])
            else:
                calls[-1].append(line)
        return [call for call in calls if call]

    def gate(self, exit_code: int = 0) -> tuple[str, ...]:
        """A gate command that plants a marker, so 'ran' is a fact and not a guess."""
        return ("sh", "-c", f"touch '{self.marker}'; exit {exit_code}")


class TestVerdictIsPublished(CiReportCase):
    def test_a_passing_gate_publishes_success_for_the_commit_it_ran_on(self) -> None:
        done = self.report(*self.gate(0))

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertTrue(self.marker.exists(), "the gate command did not run")
        (call,) = self.gh_calls()
        self.assertEqual(["api", "--method", "POST"], call[:3])
        # The placeholders are gh's to fill from the repository; the SHA is ours.
        self.assertEqual(f"repos/{{owner}}/{{repo}}/statuses/{self.sha}", call[3])
        self.assertIn("state=success", call)
        self.assertIn("context=local/make-ci", call)

    def test_a_failing_gate_publishes_failure_and_keeps_its_exit_code(self) -> None:
        done = self.report(*self.gate(7))

        self.assertEqual(7, done.returncode, "the gate's own exit code was lost")
        (call,) = self.gh_calls()
        self.assertIn("state=failure", call)
        self.assertNotIn("state=success", call)

    def test_the_description_says_what_ran_and_fits_the_api_limit(self) -> None:
        self.report(*self.gate(0))

        (call,) = self.gh_calls()
        (description,) = [a for a in call if a.startswith("description=")]
        text = description.removeprefix("description=")
        self.assertIn("passed", text)
        self.assertLessEqual(len(text), 140, "GitHub rejects a longer status description")

    def test_the_context_can_be_renamed(self) -> None:
        self.report(*self.gate(0), CI_REPORT_CONTEXT="local/other")

        (call,) = self.gh_calls()
        self.assertIn("context=local/other", call)

    def test_with_no_command_it_runs_make_ci(self) -> None:
        self.install(self.bin / "make", MAKE_STUB)

        done = self.report()

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("ci", self.make_log.read_text(encoding="utf-8").strip())
        self.assertEqual(1, len(self.gh_calls()))

    def test_a_failing_gate_is_still_reported_when_it_also_leaves_files_behind(self) -> None:
        # Hiding a red because the run also littered the tree would be the worse
        # mistake, so a failure is published whatever the tree looks like.
        litter = ("sh", "-c", "touch stray.txt; exit 1")

        done = self.report(*litter)

        self.assertEqual(1, done.returncode)
        (call,) = self.gh_calls()
        self.assertIn("state=failure", call)


class TestNothingIsSaidWhenItWouldNotBeTrue(CiReportCase):
    def assertRefusedWithoutRunning(self, done: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(2, done.returncode, done.stderr)
        self.assertFalse(self.marker.exists(), "the gate ran although it was refused")
        self.assertEqual([], self.gh_calls(), "a status was published for a refusal")

    def test_an_edited_tracked_file_is_refused(self) -> None:
        (self.repo / "tracked.txt").write_text("two\n", encoding="utf-8")

        self.assertRefusedWithoutRunning(self.report(*self.gate(0)))

    def test_an_untracked_file_is_refused(self) -> None:
        (self.repo / "new.txt").write_text("x\n", encoding="utf-8")

        self.assertRefusedWithoutRunning(self.report(*self.gate(0)))

    def test_a_missing_gh_is_refused_before_the_gate_is_paid_for(self) -> None:
        done = self.report(*self.gate(0), CI_REPORT_GH="no-such-gh-binary")

        self.assertRefusedWithoutRunning(done)
        self.assertIn("not installed", done.stderr)

    def test_outside_a_repository_is_refused(self) -> None:
        elsewhere = Path(self.tmp.name) / "not-a-repo"
        elsewhere.mkdir()

        done = subprocess.run(
            [str(SCRIPT), *self.gate(0)], cwd=elsewhere, env=self.env(),
            capture_output=True, text=True, timeout=60,
        )

        self.assertEqual(2, done.returncode, done.stderr)
        self.assertFalse(self.marker.exists())

    def test_a_passing_gate_that_rewrote_the_tree_publishes_nothing(self) -> None:
        # The files it tested are no longer the commit's, so a success would be a
        # claim about something else.
        rewrites = ("sh", "-c", "echo changed > tracked.txt; exit 0")

        done = self.report(*rewrites)

        self.assertEqual(2, done.returncode, done.stderr)
        self.assertEqual([], self.gh_calls())
        self.assertIn("changed the working tree", done.stderr)


class TestPublishingCanFail(CiReportCase):
    def test_a_status_that_cannot_be_published_is_not_reported_as_success(self) -> None:
        done = self.report(*self.gate(0), GH_STUB_EXIT="1")

        self.assertEqual(3, done.returncode, "the gate passed; only publishing failed")
        self.assertIn("could not be published", done.stderr)

    def test_a_failing_gate_keeps_its_own_code_even_when_publishing_also_fails(self) -> None:
        done = self.report(*self.gate(9), GH_STUB_EXIT="1")

        self.assertEqual(9, done.returncode)


class TestItIsWired(unittest.TestCase):
    def test_the_script_is_executable(self) -> None:
        # The Makefile runs it by path, so a checkout that lost the bit breaks
        # `make ci-report` with an error that names nothing.
        self.assertTrue(os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable")

    def test_the_makefile_has_the_target(self) -> None:
        makefile = (SCRIPT.parent.parent / "Makefile").read_text(encoding="utf-8")
        self.assertRegex(makefile, r"(?m)^ci-report:")
        self.assertRegex(makefile, r"(?m)^\tscripts/ci-report\.sh$")


if __name__ == "__main__":
    unittest.main()
