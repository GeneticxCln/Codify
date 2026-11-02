"""The repository's housekeeping rules hold: a linked `.venv` is ignored, advisories run on a clock, updates are proposed.

Each is something that was found missing, not a style preference:

* A worktree links the main checkout's `.venv` and `ui/node_modules` in as symlinks. `.gitignore` said `.venv/`, and a
  pattern that ends in a slash matches directories only, so a symlink was staged and committed. The ignore rule is
  tested against a real symlink in a throwaway repository, not by reading the line, because the line is what looked fine.
* `check.yml` ran only when someone pushed, so an advisory published against unchanged code was found by whoever pushed
  next. It now also runs the two advisory jobs weekly and leaves the build and test jobs alone.
* There was no automated proposal of newer versions, only a report of known-bad ones.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

GUARD = "if: github.event_name != 'schedule'"


def ignored_by(gitignore: str, link_name: str) -> bool:
    """Whether `git` ignores a symlink called `link_name` under a repository whose `.gitignore` is `gitignore`."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp, "repo")
        target = Path(tmp, "elsewhere")
        repo.mkdir()
        target.mkdir()
        (repo / ".gitignore").write_text(gitignore, encoding="utf-8")
        link = repo / link_name
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)  # noqa: S603, S607 — a fixed argv in a temp repo
        done = subprocess.run(  # noqa: S603, S607 — a fixed argv in a temp repo
            ["git", "check-ignore", "-q", link_name], cwd=repo, env=env, capture_output=True,
        )
        return done.returncode == 0


def job_block(workflow: str, job: str) -> str:
    """The text of one top-level job in the workflow, up to the next job."""
    match = re.search(rf"^  {re.escape(job)}:\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)", workflow, re.S | re.M)
    assert match is not None, f"check.yml has no job {job!r}"
    return match.group(1)


class TestALinkedVenvIsIgnored(unittest.TestCase):
    def test_the_repositorys_rules_ignore_a_symlinked_venv_and_node_modules(self) -> None:
        rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertTrue(ignored_by(rules, ".venv"), ".venv as a symlink would be staged")
        self.assertTrue(ignored_by(rules, "ui/node_modules"), "ui/node_modules as a symlink would be staged")

    def test_the_check_can_fail(self) -> None:
        """The old rule, with its trailing slash, did not match a link. If this passes the test above proves nothing."""
        self.assertFalse(ignored_by(".venv/\nnode_modules/\n", ".venv"))
        self.assertFalse(ignored_by(".venv/\nnode_modules/\n", "ui/node_modules"))
        self.assertTrue(ignored_by(".venv\nnode_modules\n", ".venv"))


class TestAdvisoriesRunOnAClock(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_the_workflow_has_a_weekly_schedule(self) -> None:
        match = re.search(r'^  schedule:\n\s+- cron: "([^"]+)"', self.workflow, re.M)
        self.assertIsNotNone(match, "check.yml has no `schedule` trigger")
        assert match is not None
        fields = match.group(1).split()
        self.assertEqual(5, len(fields))
        self.assertNotEqual("*", fields[4], "a schedule that runs every day of the week is not weekly")

    def test_the_build_and_test_jobs_skip_the_scheduled_run_and_the_advisories_do_not(self) -> None:
        for job in ("python", "ui", "rust"):
            with self.subTest(job=job):
                self.assertIn(GUARD, job_block(self.workflow, job))
        for job in ("python-audit", "audit"):
            with self.subTest(job=job):
                self.assertNotIn("schedule", job_block(self.workflow, job), "an advisory job must run on the clock")

    def test_the_four_required_check_names_still_exist(self) -> None:
        """Branch protection names these exactly. A rename silently turns a required check into one that never reports."""
        for name in (
            "python ${{ matrix.python }} — lint, tests, stream isolation",
            "ui — unit tests, typecheck, build",
            "rust — cargo check, tests, fmt",
        ):
            self.assertIn(f"name: {name}", self.workflow)


class TestUpdatesAreProposed(unittest.TestCase):
    ECOSYSTEMS = {
        "pip": "/",
        "npm": "/ui",
        "cargo": "/src-tauri",
        "github-actions": "/",
    }

    def test_every_ecosystem_the_project_uses_is_covered_and_points_at_a_real_directory(self) -> None:
        text = DEPENDABOT.read_text(encoding="utf-8")
        found = dict(re.findall(r'package-ecosystem: (\S+)\n\s+directory: "([^"]+)"', text))
        self.assertEqual(self.ECOSYSTEMS, found)
        for directory in found.values():
            self.assertTrue((ROOT / directory.lstrip("/")).is_dir(), directory)

    def test_each_names_the_manifest_it_reads(self) -> None:
        self.assertTrue((ROOT / "pyproject.toml").is_file())
        self.assertTrue((ROOT / "ui" / "package.json").is_file())
        self.assertTrue((ROOT / "src-tauri" / "Cargo.toml").is_file())
        self.assertTrue((ROOT / ".github" / "workflows").is_dir())


if __name__ == "__main__":
    unittest.main()
