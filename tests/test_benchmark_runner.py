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
import errno
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests import hermetic  # noqa: F401

from benchmarks import runner
from benchmarks.runner import (
    BenchmarkError,
    call_health,
    configured_models,
    failure_of,
    load_manifest,
    main,
    materialize,
    resolve_repo,
    seed_agent_configs,
    summarise,
)
from engine.db import connect
from engine.git import GitService

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

    def test_a_report_says_which_driver_ran_the_tasks(self) -> None:
        """A benchmark number is a number about whoever took the plan to the end, and the report must say who.

        The conductor is what a person's goal gets on an install with a tool-calling model; the fixed recipe is
        what remains for `parallel` goals and for installs without one. A rate quoted without this field reads
        as a claim about the product when it may be a claim about the floor under it (docs/08 §6).
        """
        for driver in ("conductor", "recipe"):
            with self.subTest(driver=driver), tempfile.TemporaryDirectory() as tmp:
                report = Path(tmp) / "report.json"
                code, _ = _run_main(["--tier", "smoke", "--driver", driver, "--report", str(report)])
                self.assertEqual(0, code)
                data = json.loads(report.read_text(encoding="utf-8"))
                self.assertEqual(driver, data["driver"])
                self.assertEqual({driver}, {t["driver"] for t in data["tasks"]})

    def test_the_default_driver_is_the_one_a_persons_goal_gets(self) -> None:
        """No flag measures the default execution path, not the fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            _run_main(["--tier", "smoke", "--report", str(report)])
            self.assertEqual("conductor", json.loads(report.read_text(encoding="utf-8"))["driver"])

    def test_the_console_says_which_driver_ran_the_tasks_too(self) -> None:
        for driver in ("conductor", "recipe"):
            with self.subTest(driver=driver):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    main(["--tier", "smoke", "--driver", driver])
                self.assertRegex(out.getvalue(), rf"driver\s+{driver}")

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

            # The scratch store is created by the engine's own schema, the way
            # `run_task` creates it — not by a hand-written CREATE TABLE. A copy
            # of the schema in a test is a second thing to forget: `num_ctx` was
            # added to `agent_configs` and this fixture alone had never heard of
            # it, so the seeded run died on a column the production path already
            # had. The test is about the copy leaving the engine store alone, not
            # about the shape of the table.
            scratch = connect(Path(tmp) / "scratch.db")
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


class CallHealthTests(unittest.TestCase):
    """How often a role needed its reply asked for again (audit of 2026-09-29, 3.4).

    A pass/fail per task hides the thing a small model's reliability is made of: how many of its
    replies were usable the first time. The executor already records every failed call and whether a
    same-model re-ask followed (`agent_call_failed`, `retrying`), so the harness only has to count.
    """

    @staticmethod
    def _stage(role: str) -> dict[str, object]:
        return {"type": "stage_result", "payload": {"stage": role, "role": role}}

    @staticmethod
    def _failed(role: str, *, retrying: bool = False, code: str = "agent_output_invalid") -> dict[str, object]:
        payload: dict[str, object] = {"role": role, "code": code}
        if retrying:
            payload["retrying"] = True
        return {"type": "agent_call_failed", "payload": payload}

    def test_a_reask_is_counted_against_the_role_that_needed_it(self) -> None:
        health = call_health([
            self._stage("planner"), self._stage("fixer"),
            self._failed("fixer", retrying=True),
        ])

        self.assertEqual({"planner": 1, "fixer": 1}, health["ran"])
        self.assertEqual({"fixer": 1}, health["reasks"])
        self.assertEqual({"fixer": 1}, health["failed_calls"])

    def test_a_failure_that_was_not_a_reask_is_a_failed_call_and_nothing_more(self) -> None:
        health = call_health([self._failed("critic", code="provider_http")])

        self.assertEqual({}, health["reasks"])
        self.assertEqual({"critic": 1}, health["failed_calls"])

    def test_a_run_with_no_failures_has_empty_counts_not_missing_keys(self) -> None:
        self.assertEqual({"ran": {}, "reasks": {}, "failed_calls": {}}, call_health([]))

    def test_the_summary_rates_re_asks_per_role_over_every_task(self) -> None:
        def result(reasks: dict[str, int]) -> dict[str, object]:
            row = SummaryTests._result(True)
            row["call_health"] = {"ran": {"fixer": 1, "planner": 1}, "reasks": reasks, "failed_calls": reasks}
            return row

        summary = summarise([result({"fixer": 1}), result({}), result({"fixer": 1, "planner": 1}), result({})])

        self.assertEqual(
            {"fixer": {"ran": 4, "reasks": 2, "failed_calls": 2}, "planner": {"ran": 4, "reasks": 1, "failed_calls": 1}},
            summary["call_health"],
        )

    def test_results_from_before_this_field_existed_still_summarise(self) -> None:
        self.assertEqual({}, summarise([SummaryTests._result(True)])["call_health"])


class ACrashedTaskIsAResultTests(unittest.TestCase):
    """A task that blows up is a failed task, not the end of the run (audit of 2026-09-29, 3.3).

    The first real-model baseline died eleven tasks in on an `AttributeError` from one model reply, and the
    report is written at the end, so every finished task's result went with it: minutes of a slow local
    model, gone, and no record of *which* task crashed. A benchmark's whole job is to say what happened, so
    a crash is recorded as one — with its cause — and the rest of the run goes on.
    """

    @staticmethod
    def _manifest(tmp: str) -> Path:
        path = Path(tmp) / "manifest.json"
        base = {
            "tier": "smoke", "repo": SYNTHETIC, "title": "t", "description": "d",
            "canned_write": [{"path": "banner.txt", "content": "X\n"}],
            "checks": [{"type": "goal_completed"}, {"type": "files_written", "min": 1}],
        }
        path.write_text(json.dumps({
            "version": 1, "tiers": {"smoke": {"description": "d", "provider": "canned"}}, "repos": [],
            "tasks": [{**base, "id": "first"}, {**base, "id": "second"}],
        }), encoding="utf-8")
        return path

    def _run_with_first_task_crashing(self) -> tuple[int, str, dict[str, object]]:
        real = runner.run_task
        calls: list[str] = []

        async def flaky(task: dict[str, object], *args: object, **kwargs: object) -> dict[str, object]:
            calls.append(str(task["id"]))
            if task["id"] == "first":
                raise RuntimeError("boom")
            return await real(task, *args, **kwargs)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            out = io.StringIO()
            with mock.patch.object(runner, "run_task", flaky), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = main(["--tier", "smoke", "--manifest", str(self._manifest(tmp)), "--report", str(report)])
            data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(["first", "second"], calls, "the run stopped at the crash")
        return code, out.getvalue(), data

    def test_the_crash_is_recorded_with_its_cause_and_the_run_goes_on(self) -> None:
        code, _, data = self._run_with_first_task_crashing()

        tasks = {t["id"]: t for t in data["tasks"]}  # type: ignore[attr-defined]
        self.assertEqual("ERRORED", tasks["first"]["status"])
        self.assertFalse(tasks["first"]["passed"])
        self.assertIn("RuntimeError: boom", tasks["first"]["error"])
        self.assertTrue(tasks["second"]["passed"], "the task after the crash did not run to completion")
        self.assertEqual(1, code)

    def test_a_crash_counts_against_the_pass_rate(self) -> None:
        _, _, data = self._run_with_first_task_crashing()

        summary = data["summary"]
        self.assertEqual(2, summary["tasks"])  # type: ignore[index]
        self.assertEqual(1, summary["passed"])  # type: ignore[index]
        self.assertEqual(50, summary["pass_rate"])  # type: ignore[index]

    def test_each_task_reports_as_it_finishes_not_only_at_the_end(self) -> None:
        _, out, _ = self._run_with_first_task_crashing()

        lines = out.splitlines()
        first = next(i for i, line in enumerate(lines) if line.startswith("finished first"))
        second = next(i for i, line in enumerate(lines) if line.startswith("finished second"))
        summary = next(i for i, line in enumerate(lines) if line.startswith("tier "))
        self.assertLess(first, second)
        self.assertLess(second, summary)
        self.assertIn("ERRORED", lines[first])


class FailureOfTests(unittest.TestCase):
    """Why a task failed, from the run's own events — a pass rate with no causes cannot be acted on."""

    def test_the_last_error_event_is_the_cause(self) -> None:
        events = [
            {"type": "error", "payload": {"code": "provider_http", "message": "first", "role": "critic"}},
            {"type": "error", "payload": {"code": "agent_output_invalid", "message": "last", "role": "fixer"}},
        ]

        self.assertEqual({"code": "agent_output_invalid", "role": "fixer", "message": "last"}, failure_of(events))

    def test_a_run_with_no_error_has_no_failure(self) -> None:
        self.assertIsNone(failure_of([{"type": "log", "payload": {"message": "hi"}}]))

    def test_a_long_message_is_cut_so_a_report_stays_readable(self) -> None:
        events = [{"type": "error", "payload": {"code": "x", "message": "y" * 5000, "role": None}}]

        failure = failure_of(events)

        assert failure is not None
        self.assertLessEqual(len(failure["message"]), 400)

    def test_the_summary_counts_failures_by_code(self) -> None:
        def result(code: str | None) -> dict[str, object]:
            row = SummaryTests._result(code is None)
            if code:
                row["failure"] = {"code": code, "role": "fixer", "message": "m"}
            return row

        summary = summarise([result("agent_output_invalid"), result(None), result("agent_output_invalid"), result("provider_http")])

        self.assertEqual({"agent_output_invalid": 2, "provider_http": 1}, summary["failure_codes"])


class RecordTests(unittest.TestCase):
    """`--record DIR` keeps what a run did, so a real model's failures can be read and committed as fixtures.

    The two failure classes that mattered most in the first real-model baseline were found only because a
    debugging wrapper turned tracing on and kept the scratch stores; the runner deletes both. Recording is
    that wrapper, as a flag: every goal's model calls are traced (`docs/04` §8) and the per-task stores
    and workspaces are moved to DIR instead of removed.
    """

    def test_the_stores_survive_and_hold_every_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            keep = Path(tmp) / "kept"

            code, _ = _run_main(["--tier", "smoke", "--only", "smoke-add-banner", "--record", str(keep)])

            self.assertEqual(0, code)
            stores = sorted(keep.glob("*.db"))
            self.assertEqual(["smoke-add-banner.db"], [p.name for p in stores])
            conn = sqlite3.connect(stores[0])
            try:
                roles = {r[0] for r in conn.execute("SELECT role FROM trace_calls")}
            finally:
                conn.close()
            self.assertTrue({"planner", "fixer"} <= roles, roles)
            self.assertTrue((keep / "smoke-add-banner" / "banner.txt").is_file(), "the workspace was not kept")

    def test_each_task_is_kept_as_it_finishes_not_only_at_the_end(self) -> None:
        # The copy used to happen once, in a `finally` after the last task, so a run that was killed —
        # a container restart, an OOM, a closed terminal — kept nothing, and a baseline that takes an
        # hour on a CPU was lost with it. Looked at from the second task's start: the first is already there.
        with tempfile.TemporaryDirectory() as tmp:
            keep = Path(tmp) / "kept"
            manifest = Path(tmp) / "manifest.json"
            manifest.write_text(json.dumps({
                "version": 1,
                "tiers": {"smoke": {"description": "d", "provider": "canned"}},
                "repos": [],
                "tasks": [{
                    "id": name, "tier": "smoke", "repo": SYNTHETIC, "title": name, "description": "d",
                    "canned_write": [{"path": "banner.txt", "content": "X\n"}],
                    "checks": [{"type": "goal_completed"}],
                } for name in ("first", "second")],
            }), encoding="utf-8")
            kept_when_each_started: dict[str, list[str] | None] = {}
            real = runner._run_one

            def spy(task: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
                kept_when_each_started[str(task["id"])] = (
                    sorted(p.name for p in keep.glob("*.db")) if keep.exists() else None
                )
                return real(task, *args, **kwargs)

            with mock.patch("benchmarks.runner._run_one", spy):
                code, _ = _run_main(["--tier", "smoke", "--manifest", str(manifest), "--record", str(keep)])

            self.assertEqual(0, code)
            self.assertEqual(["first.db"], kept_when_each_started["second"],
                             "the first task's store was not kept until the whole run ended")
            self.assertEqual(["first.db", "second.db"], sorted(p.name for p in keep.glob("*.db")))

    def test_without_the_flag_nothing_is_kept_and_nothing_is_traced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch("benchmarks.runner.tempfile.mkdtemp", return_value=str(Path(tmp) / "w")) as made:
            (Path(tmp) / "w").mkdir()

            code, _ = _run_main(["--tier", "smoke", "--only", "smoke-add-banner"])

            self.assertEqual(0, code)
            self.assertTrue(made.called)
            self.assertFalse((Path(tmp) / "w").exists(), "the scratch directory was left behind")


class TheRecordCanBeKeptMoreThanOnceTests(unittest.TestCase):
    """`--record` copies the scratch root after every task and again at the end, over what it already copied.

    Git writes its objects read-only (0444) and `shutil.copy2` keeps the mode, so the first copy of a task's
    workspace leaves read-only files in the record directory, and the second copy has to open one of them for
    writing. For anyone but root that is `EACCES`: `--record` failed after the second task on every machine
    that is not a root container, and the three tests below it in this file failed with it (found by a
    `make ci-report` on a developer's machine, 2026-09-29). Root ignores the mode bits, which is why the run
    that wrote it, and every run since, was green.

    So these tests do not depend on who runs them: `_as_an_unprivileged_user` is what the kernel does to a
    user who is not root, applied to the one call that writes the destination.
    """

    @staticmethod
    def _as_an_unprivileged_user() -> Any:
        real = shutil.copyfile

        def copyfile(src: Any, dst: Any, *, follow_symlinks: bool = True) -> Any:
            if os.path.lexists(dst) and not os.stat(dst).st_mode & stat.S_IWUSR:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(dst))
            return real(src, dst, follow_symlinks=follow_symlinks)

        return mock.patch("shutil.copyfile", copyfile)

    @staticmethod
    def _a_workspace_with_a_git_object(root: Path, text: str) -> Path:
        obj = root / "task" / ".git" / "objects" / "ab" / "cdef"
        obj.parent.mkdir(parents=True, exist_ok=True)
        if obj.exists():
            obj.chmod(0o644)
        obj.write_text(text, encoding="utf-8")
        obj.chmod(0o444)
        return obj

    def test_the_emulation_fails_the_plain_copy_it_stands_in_for(self) -> None:
        # Guard the guard: if this passed, the tests below would prove nothing about the fix.
        with tempfile.TemporaryDirectory() as tmp:
            work, keep = Path(tmp) / "work", Path(tmp) / "kept"
            self._a_workspace_with_a_git_object(work, "one")
            with self._as_an_unprivileged_user():
                shutil.copytree(work, keep, dirs_exist_ok=True)
                with self.assertRaises(shutil.Error):
                    shutil.copytree(work, keep, dirs_exist_ok=True)

    def test_a_second_copy_over_read_only_objects_replaces_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work, keep = Path(tmp) / "work", Path(tmp) / "kept"
            self._a_workspace_with_a_git_object(work, "one")
            with self._as_an_unprivileged_user():
                runner._keep_record(work, keep)
                self._a_workspace_with_a_git_object(work, "two")

                runner._keep_record(work, keep)

            kept = keep / "task" / ".git" / "objects" / "ab" / "cdef"
            self.assertEqual("two", kept.read_text(encoding="utf-8"))

    def test_the_copy_is_still_a_copy_not_a_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work, keep = Path(tmp) / "work", Path(tmp) / "kept"
            source = self._a_workspace_with_a_git_object(work, "one")

            runner._keep_record(work, keep)

            self.assertEqual("one", source.read_text(encoding="utf-8"))
            self.assertEqual(0o444, stat.S_IMODE(source.stat().st_mode), "the source's mode was changed")

    def test_a_task_run_twice_through_main_keeps_a_record_a_user_can_read(self) -> None:
        # The path a person takes, with the emulation on: two tasks, so the second copy lands on the first's files.
        with tempfile.TemporaryDirectory() as tmp:
            keep = Path(tmp) / "kept"
            manifest = Path(tmp) / "manifest.json"
            manifest.write_text(json.dumps({
                "version": 1,
                "tiers": {"smoke": {"description": "d", "provider": "canned"}},
                "repos": [],
                "tasks": [{
                    "id": name, "tier": "smoke", "repo": SYNTHETIC, "title": name, "description": "d",
                    "canned_write": [{"path": "banner.txt", "content": "X\n"}],
                    "checks": [{"type": "goal_completed"}],
                } for name in ("first", "second")],
            }), encoding="utf-8")

            with self._as_an_unprivileged_user():
                code, _ = _run_main(["--tier", "smoke", "--manifest", str(manifest), "--record", str(keep)])

            self.assertEqual(0, code)
            for name in ("first", "second"):
                self.assertTrue((keep / f"{name}.db").is_file(), name)
                self.assertTrue((keep / name / ".git").is_dir(), f"{name} lost its repository")


class TheWorkspaceIsARealRepositoryTests(unittest.TestCase):
    """A task's scratch workspace is a git repository with the fixture committed, like a user's checkout.

    It was a plain directory. In one, the scribe's commit is skipped (`not_a_repo`) and a verifier that
    proposes `git diff` — small models reach for it constantly — is told "not a git repository", which reads
    as the change failing. A real-model baseline should measure what a person would get, and a person's
    workspace is nearly always a repository (second audit pass, 2026-09-29).
    """

    def test_the_fixture_is_committed_and_the_work_is_committed_on_top(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            keep = Path(tmp) / "kept"

            code, _ = _run_main(["--tier", "smoke", "--only", "smoke-add-banner", "--record", str(keep)])

            self.assertEqual(0, code)
            work = keep / "smoke-add-banner"
            self.assertTrue((work / ".git").is_dir(), "the workspace is not a git repository")
            subjects = subprocess.run(
                ["git", "log", "--format=%s"], cwd=work, capture_output=True, text=True, check=True,
            ).stdout.splitlines()
            self.assertEqual("chore: benchmark fixture", subjects[-1], "the fixture was not the first commit")
            self.assertGreater(len(subjects), 1, "the run's own work was not committed on top of it")
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=work, capture_output=True, text=True, check=True,
            ).stdout
            self.assertEqual("", status, "the tree was left dirty")

    def test_a_repository_that_could_not_be_started_is_said_out_loud(self) -> None:
        # A run that quietly fell back to a plain directory would be measuring something other than what
        # its report claims; it still runs, and the message names the consequence.
        err = io.StringIO()
        with mock.patch.object(GitService, "init_repo", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(["--tier", "smoke", "--only", "smoke-add-banner"])

        self.assertEqual(0, code)
        self.assertIn("could not start a git repository", err.getvalue())
        self.assertIn("plain directory", err.getvalue())

    def test_a_working_repository_start_says_nothing(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            main(["--tier", "smoke", "--only", "smoke-add-banner"])

        self.assertNotIn("git repository", err.getvalue())

    def test_git_internals_are_not_files_the_run_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git" / "objects" / "aa").mkdir(parents=True)
            (root / ".git" / "objects" / "aa" / "bb").write_text("x", encoding="utf-8")
            (root / "a.txt").write_text("x", encoding="utf-8")
            (root / "sub").mkdir()
            (root / "sub" / "b.txt").write_text("x", encoding="utf-8")

            self.assertEqual({"a.txt", str(Path("sub") / "b.txt")}, runner._files(root))


class TheReasonATaskFailedIsPrintedTests(unittest.TestCase):
    """A task that completes and then fails its *quality* check says why, in the console summary.

    The summary listed only the harness checks under a failed task, and a task that completes but does the
    wrong thing fails no harness check: `repo-remove-shout` and `repo-word-count` printed `FAIL` and nothing
    beneath it, and the reason (the module had been deleted; the repo's own tests errored) was in the JSON
    report alone (second audit pass, 2026-09-29). For a benchmark, why a task failed is the line that matters.
    """

    def _summary(self, quality: list[dict[str, str]]) -> str:
        real = runner._run_one

        def wrapper(task: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
            result = real(task, *args, **kwargs)
            result["quality_checks"] = quality
            result["passed"] = all(q["status"] == "passed" for q in quality)
            return result

        out = io.StringIO()
        with mock.patch.object(runner, "_run_one", wrapper), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            main(["--tier", "smoke", "--only", "smoke-add-banner"])
        return out.getvalue()

    def test_a_failed_quality_check_is_named_with_its_detail(self) -> None:
        text = self._summary([
            {"type": "test_command", "kind": "quality", "status": "failed",
             "detail": "exit 1: ModuleNotFoundError: No module named 'app'"},
        ])
        self.assertIn("FAIL smoke-add-banner", text)
        self.assertIn("test_command: exit 1: ModuleNotFoundError: No module named 'app'", text)

    def test_a_skipped_quality_check_is_not_listed_under_a_passing_task(self) -> None:
        # A canned run skips every quality check, and the summary already counts them on one line.
        text = self._summary([
            {"type": "file_exists", "kind": "quality", "status": "skipped",
             "detail": "needs a real provider — a canned run cannot claim task quality"},
        ])
        self.assertNotIn("needs a real provider", text.split("smoke-add-banner", 2)[-1])

    def test_a_passing_quality_check_is_not_listed(self) -> None:
        text = self._summary([
            {"type": "file_exists", "kind": "quality", "status": "passed", "detail": "src/util.py exists"},
        ])
        self.assertNotIn("src/util.py exists", text)


class RepeatAndThresholdTests(unittest.TestCase):
    """One run of a model proves little; a number is only worth having with its spread."""

    @staticmethod
    def _manifest(tmp: str, *, passing: bool) -> Path:
        """A one-task canned manifest whose harness checks pass, or cannot."""
        path = Path(tmp) / "manifest.json"
        path.write_text(json.dumps({
            "version": 1,
            "tiers": {"smoke": {"description": "d", "provider": "canned"}},
            "repos": [],
            "tasks": [{
                "id": "t", "tier": "smoke", "repo": SYNTHETIC, "title": "t", "description": "d",
                "canned_write": [{"path": "banner.txt", "content": "X\n"}],
                "checks": [
                    {"type": "goal_completed"},
                    # A harness check, so a canned run can really fail it: one file is written, and
                    # asking for five is a task the run cannot pass.
                    {"type": "files_written", "min": 1 if passing else 5},
                ],
            }],
        }), encoding="utf-8")
        return path

    def _report(self, tmp: str, argv: list[str]) -> tuple[int, str, dict[str, object]]:
        report = Path(tmp) / "report.json"
        code, err = _run_main([*argv, "--report", str(report)])
        return code, err, json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}

    def test_repeat_runs_every_task_that_many_times_with_its_own_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, data = self._report(tmp, ["--tier", "smoke", "--manifest", str(self._manifest(tmp, passing=True)), "--repeat", "3"])

        self.assertEqual(0, code)
        tasks = data["tasks"]
        assert isinstance(tasks, list)
        self.assertEqual([1, 2, 3], [t["attempt"] for t in tasks])
        self.assertEqual({"t"}, {t["id"] for t in tasks})
        self.assertEqual(3, data["summary"]["tasks"])  # type: ignore[index]

    def test_a_pass_rate_below_the_floor_fails_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._manifest(tmp, passing=False)
            code, err, _ = self._report(tmp, ["--tier", "smoke", "--manifest", str(manifest), "--min-pass-rate", "50"])

        self.assertEqual(1, code)

    def test_a_pass_rate_at_or_above_the_floor_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _ = self._report(tmp, ["--tier", "smoke", "--manifest", str(self._manifest(tmp, passing=True)), "--min-pass-rate", "100"])

        self.assertEqual(0, code)

    def test_the_floor_is_reported_in_words_when_it_is_breached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            manifest = self._manifest(tmp, passing=False)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                main(["--tier", "smoke", "--manifest", str(manifest), "--min-pass-rate", "50"])

        self.assertIn("below the floor", out.getvalue())

    def test_a_floor_outside_zero_to_one_hundred_is_a_usage_error(self) -> None:
        for value in ("-1", "101", "lots"):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
                    main(["--tier", "smoke", "--min-pass-rate", value])
                self.assertEqual(2, caught.exception.code)

    def test_repeat_must_be_a_positive_number(self) -> None:
        for value in ("0", "-2", "many"):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
                    main(["--tier", "smoke", "--repeat", value])
                self.assertEqual(2, caught.exception.code)


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
