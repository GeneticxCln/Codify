"""The benchmark harness, proved without a network or a real model.

Two properties matter more than the arithmetic. First, the smoke tier must run
hermetically — a benchmark nobody can run locally is a benchmark nobody runs.
Second, the configured tier must fail with a *diagnosis* before it spends a
token, because the alternative is paying for a goal to learn a config was
absent.

The synthetic fixture is the reason both are testable here: it is committed to
this repository, so no third-party source is needed to exercise the runner.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import hermetic  # noqa: F401

from benchmarks import runner
from benchmarks.runner import (
    BenchmarkError,
    configured_models,
    load_manifest,
    main,
    materialize,
    resolve_repo,
    seed_agent_configs,
    summarise,
)
from engine.db import connect

SYNTHETIC = "synthetic-repo"


def _run_main(argv: list[str]) -> tuple[int, str]:
    """Run the CLI, returning its exit code and everything it printed.

    Both streams are captured: the runner's progress report is for a human at
    a terminal, and leaving it on would bury the test result that follows.
    """
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = main(argv)
    return code, err.getvalue()


class ManifestTests(unittest.TestCase):
    """The manifest is data, so nothing stops it drifting into nonsense."""

    def test_manifest_loads_and_declares_what_the_runner_needs(self) -> None:
        manifest = load_manifest()
        self.assertEqual(manifest["version"], 1)
        self.assertIn("smoke", manifest["tiers"])
        for tier in manifest["tiers"].values():
            self.assertIn(tier["provider"], ("canned", "configured"))

    def test_every_task_names_a_known_tier_and_an_existing_repo(self) -> None:
        manifest = load_manifest()
        for task in manifest["tasks"]:
            with self.subTest(task=task["id"]):
                self.assertIn(task["tier"], manifest["tiers"])
                # Resolution is the same call the runner makes before spending.
                self.assertTrue(resolve_repo(str(task["repo"])).is_dir())

    def test_every_check_is_a_kind_the_runner_can_actually_run(self) -> None:
        known = {"goal_completed", "stages", "files_written", "file_exists",
                 "file_contains", "test_command"}
        manifest = load_manifest()
        for task in manifest["tasks"]:
            for check in task["checks"]:
                with self.subTest(task=task["id"], check=check["type"]):
                    self.assertIn(check["type"], known)

    def test_smoke_tier_is_canned_so_it_can_never_spend(self) -> None:
        manifest = load_manifest()
        self.assertEqual(manifest["tiers"]["smoke"]["provider"], "canned")

    def test_no_third_party_repo_is_declared_without_a_notice(self) -> None:
        """Vendoring without provenance is the thing docs/08 forbids."""
        manifest = load_manifest()
        for repo in manifest.get("repos", []):
            with self.subTest(repo=repo.get("name")):
                self.assertTrue(repo.get("license"), "vendored repo has no license")
                self.assertTrue(repo.get("upstream"), "vendored repo has no upstream")


class FixtureTests(unittest.TestCase):
    """The synthetic repo must be a copy, or tasks would mutate the source."""

    def test_materialize_gives_each_task_its_own_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "work"
            materialize(SYNTHETIC, dest)
            copy = dest / "src" / "app.py"
            source = resolve_repo(SYNTHETIC) / "src" / "app.py"
            self.assertTrue(copy.is_file())

            original = source.read_text(encoding="utf-8")
            copy.write_text("MUTATED\n", encoding="utf-8")

            # The copy moved; the fixture did not. A fixture that drifts makes
            # every later run quietly measure something else.
            self.assertEqual(
                source.read_text(encoding="utf-8"), original,
                "materialize handed back the fixture itself, not a copy",
            )

    def test_unknown_repo_is_named_rather_than_silently_skipped(self) -> None:
        with self.assertRaises(BenchmarkError) as ctx:
            resolve_repo("not-a-repo")
        self.assertIn("not-a-repo", str(ctx.exception))


class SmokeTierTests(unittest.TestCase):
    """The hermetic tier is the one a developer runs on a whim."""

    def test_smoke_tier_passes_without_a_network(self) -> None:
        code, _ = _run_main(["--tier", "smoke"])
        self.assertEqual(code, 0)

    def test_smoke_report_says_its_tokens_are_synthetic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            code, _ = _run_main(["--tier", "smoke", "--report", str(report)])
            self.assertEqual(code, 0)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(data["summary"]["tokens_are_synthetic"])
            self.assertEqual(data["provider"], "canned")

    def test_a_canned_run_reports_quality_as_skipped_not_passed(self) -> None:
        """The dishonest failure mode: a canned run claiming task quality."""
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            _run_main(["--tier", "smoke", "--report", str(report)])
            data = json.loads(report.read_text(encoding="utf-8"))
            summary = data["summary"]
            self.assertGreater(summary["quality_skipped"], 0)
            self.assertEqual(summary["quality_passed"], 0)

    def test_unknown_tier_lists_the_ones_that_exist(self) -> None:
        code, err = _run_main(["--tier", "definitely-not"])
        self.assertEqual(code, 2)
        self.assertIn("known tiers", err)

    def test_missing_manifest_is_reported_not_raised(self) -> None:
        code, err = _run_main(["--manifest", "/tmp/does-not-exist.json"])
        self.assertEqual(code, 2)
        self.assertIn("no manifest", err)


class ConfiguredTierTests(unittest.TestCase):
    """Preflight must diagnose, never crash and never spend."""

    @staticmethod
    def _engine_db(tmp: str) -> Path:
        """A real engine store holding two configured roles."""
        path = Path(tmp) / "engine.sqlite"
        conn = connect(path)
        try:
            for role, model in (("fixer", "some-model"), ("critic", "")):
                conn.execute(
                    "INSERT OR REPLACE INTO agent_configs (role, display_name, provider,"
                    " protocol, model_name, temperature, max_tokens,"
                    " fallback_model_name, updated_at)"
                    " VALUES (?, ?, 'ollama', 'openai-chat', ?, 0.0, 4096, '', 0.0)",
                    (role, role, model),
                )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_missing_database_suggests_the_tier_that_needs_nothing(self) -> None:
        code, err = _run_main(["--tier", "repo_scale", "--engine-db",
                               "/tmp/definitely-missing.sqlite"])
        self.assertEqual(code, 2)
        self.assertIn("--tier smoke", err)

    def test_a_file_that_is_not_the_engine_store_is_diagnosed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            foreign = Path(tmp) / "foreign.sqlite"
            sqlite3.connect(foreign).close()  # a real file, wrong contents
            code, err = _run_main(["--tier", "repo_scale", "--engine-db", str(foreign)])
            self.assertEqual(code, 2)
            self.assertIn("not a Codify engine database", err)

    def test_roles_without_a_model_are_named_before_anything_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, err = _run_main(["--tier", "repo_scale",
                                   "--engine-db", str(self._engine_db(tmp))])
            self.assertEqual(code, 2)
            self.assertIn("no model configured", err)

    def test_configured_models_reads_roles_without_writing_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._engine_db(tmp)
            self.assertEqual(configured_models(db), ["fixer"])

    def test_seeding_the_scratch_store_leaves_the_engine_store_untouched(self) -> None:
        """The whole point of the copy: runs must not land in user history."""
        with tempfile.TemporaryDirectory() as tmp:
            db = self._engine_db(tmp)
            before = sqlite3.connect(db)
            try:
                source_rows = before.execute(
                    "SELECT COUNT(*) FROM agent_configs"
                ).fetchone()[0]
            finally:
                before.close()

            scratch = sqlite3.connect(":memory:")
            scratch.execute(
                "CREATE TABLE agent_configs (role TEXT PRIMARY KEY,"
                " display_name TEXT NOT NULL, provider TEXT NOT NULL,"
                " protocol TEXT NOT NULL, model_name TEXT NOT NULL,"
                " api_key_ref TEXT, base_url TEXT, system_prompt_override TEXT,"
                " temperature REAL NOT NULL, max_tokens INTEGER NOT NULL,"
                " fallback_provider TEXT, fallback_model_name TEXT NOT NULL DEFAULT '',"
                " fallback_protocol TEXT, fallback_base_url TEXT,"
                " updated_at REAL NOT NULL)"
            )
            ready = seed_agent_configs(db, scratch)
            scratch.close()

            self.assertEqual(ready, ["fixer"])
            after = sqlite3.connect(db)
            try:
                self.assertEqual(
                    after.execute("SELECT COUNT(*) FROM agent_configs").fetchone()[0],
                    source_rows,
                )
            finally:
                after.close()


class SummaryTests(unittest.TestCase):
    """The printed verdict is the only thing a reader acts on."""

    @staticmethod
    def _result(passed: bool, quality: str = "skipped") -> dict[str, object]:
        return {
            "id": "t", "passed": passed, "wall_ms": 10, "tokens": 5,
            "tokens_are_synthetic": True,
            "quality_checks": [{"status": quality}],
            "stage_ms": {"fixer": 4}, "checks": [],
        }

    def test_pass_rate_is_none_when_there_is_nothing_to_rate(self) -> None:
        self.assertIsNone(summarise([])["pass_rate"])

    def test_a_failed_task_is_counted_not_dropped(self) -> None:
        summary = summarise([self._result(True), self._result(False)])
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["pass_rate"], 50)

    def test_quality_skips_and_passes_are_reported_separately(self) -> None:
        summary = summarise([
            self._result(True, "skipped"),
            self._result(True, "passed"),
            self._result(False, "failed"),
        ])
        self.assertEqual(summary["quality_skipped"], 1)
        self.assertEqual(summary["quality_passed"], 1)
        self.assertEqual(summary["quality_failed"], 1)

    def test_stage_totals_rank_the_slowest_stage_first(self) -> None:
        summary = summarise([self._result(True), self._result(True)])
        self.assertEqual(next(iter(summary["stage_ms"])), "fixer")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestCommandTests(unittest.TestCase):
    """The one spawn the harness owns, pinned the way the freeze demands.

    A task's test command is a real process that can fork, background and ignore
    SIGTERM. `tests/test_no_unguarded_spawns.py` allowlists that spawn on the
    strength of these three tests, so they have to be about the spawn and not
    about the summary line.
    """

    @staticmethod
    def _grandchild_script(marker: Path) -> str:
        """A command that backgrounds a sleeper and then hangs forever.

        The sleeper is what makes the test worth running: killing only the direct
        child would leave it alive, and the marker it eventually writes is how
        that shows up.
        """
        late = (
            "import time, pathlib\n"
            "time.sleep(3)\n"
            f"pathlib.Path({str(marker)!r}).write_text('orphan', encoding='utf-8')\n"
        )
        return (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {late!r}])\n"
            "time.sleep(30)\n"
        )

    def test_a_hung_command_kills_its_whole_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "orphan.txt"
            with mock.patch.object(runner, "TEST_TIMEOUT_S", 1):
                returncode, output = runner._run_test_command(
                    ["python3", "-c", self._grandchild_script(marker)], Path(tmp)
                )

            self.assertEqual(returncode, runner.TIMEOUT_EXIT)
            self.assertIn("whole process group was killed", output)

            # The sleeper's marker lands at ~3s. Sleeping past it turns "no
            # marker" into evidence rather than an absence.
            time.sleep(4)
            self.assertFalse(
                marker.exists(),
                "the command's own child outlived the timeout — the group was "
                "signalled but the tree was not killed",
            )

    def test_a_timeout_is_reported_as_a_timeout_not_a_test_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(runner, "TEST_TIMEOUT_S", 1):
                returncode, _ = runner._run_test_command(
                    ["python3", "-c", "import time; time.sleep(30)"], Path(tmp)
                )
            self.assertEqual(returncode, runner.TIMEOUT_EXIT)

    def test_a_failing_command_reports_its_own_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            returncode, output = runner._run_test_command(
                ["python3", "-c", "raise SystemExit(3)"], Path(tmp)
            )
            self.assertEqual(returncode, 3)
            self.assertNotIn("timed out", output)

    def test_a_passing_command_exits_zero_with_no_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            returncode, _ = runner._run_test_command(
                ["python3", "-c", "print('ok')"], Path(tmp)
            )
            self.assertEqual(returncode, 0)

    def test_argv0_with_a_path_is_refused(self) -> None:
        """A basename or nothing: the manifest cannot point outside PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BenchmarkError) as ctx:
                runner._run_test_command(["/bin/sh", "-c", "true"], Path(tmp))
            self.assertIn("basename", str(ctx.exception))
            self.assertFalse((Path(tmp) / "anything").exists())

    def test_a_command_that_cannot_start_is_a_diagnosis_not_a_crash(self) -> None:
        """The guard leads, so the exec failure is the guard's to report.

        127 is the shell's "not found", and it arrives with the binary named —
        a missing tool must read as a missing tool, not as a zero exit the
        harness would score as a passing task.
        """
        with tempfile.TemporaryDirectory() as tmp:
            returncode, output = runner._run_test_command(
                ["definitely-not-a-real-binary"], Path(tmp)
            )
        self.assertEqual(returncode, 127)
        self.assertIn("definitely-not-a-real-binary", output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
