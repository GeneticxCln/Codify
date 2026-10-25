"""`analyze_binary`: the door that starts Ghidra, proven against a stand-in `analyzeHeadless` (`tests/fake_ghidra.py`).

Nothing here needs Ghidra, so nothing here can skip. What is proven is the door: how the install is found, what the
child is started with and under, what the engine does with a copy of the file and with the export it gets back, and
that every way a run goes wrong ends in a named refusal with nothing left running or left behind. That the export
script runs inside a real Ghidra is a separate, named verification (`docs/14` §4), not a claim made here.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from engine import binary_analysis as ba
from engine.binary_analysis import BinaryAnalysisError
from tests import fake_ghidra

REPO = Path(__file__).resolve().parent.parent
BINARY = b"\x7fELF" + bytes(range(256)) * 8


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "ws"
        self.root.mkdir()
        (self.root / "app.bin").write_bytes(BINARY)
        self.state = self.base / "state"
        patch = mock.patch.object(ba, "state_dir", lambda: self.state)
        patch.start()
        self.addCleanup(patch.stop)
        self.install = fake_ghidra.make_install(self.base)

    def env(self, **extra: str) -> dict[str, str]:
        return {"GHIDRA_INSTALL_DIR": str(self.install), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), **extra}

    def run_tool(self, path: str = "app.bin", view: str = "summary", query: str = "", limit: int = 50,
                 cancel: threading.Event | None = None, env: dict[str, str] | None = None) -> str:
        return ba.analyze_binary(str(self.root), path, view, query, limit, cancel, env if env is not None else self.env())

    def refusal(self, **kw: Any) -> BinaryAnalysisError:
        with self.assertRaises(BinaryAnalysisError) as caught:
            self.run_tool(**kw)
        return caught.exception

    def runs(self) -> list[Path]:
        try:
            return list((self.state / "runs").iterdir())
        except OSError:
            return []


class TestFindingGhidra(Case):
    def test_an_unset_variable_is_a_named_failure_that_says_how_to_fix_it(self) -> None:
        err = self.refusal(env={"PATH": os.environ["PATH"]})
        self.assertEqual("not_configured", err.code)
        self.assertIn("GHIDRA_INSTALL_DIR", str(err))
        self.assertIn("absolute", str(err))

    def test_a_relative_path_is_never_executed(self) -> None:
        err = self.refusal(env=self.env(GHIDRA_INSTALL_DIR="ghidra"))
        self.assertEqual("not_absolute", err.code)
        self.assertEqual([], fake_ghidra.records(self.install))

    def test_a_configured_path_that_is_wrong_is_an_error_even_when_another_ghidra_is_on_path(self) -> None:
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "analyzeHeadless"
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
        err = self.refusal(env=self.env(GHIDRA_INSTALL_DIR=str(self.base / "nowhere"), PATH=f"{bin_dir}:/usr/bin"))
        self.assertEqual("incomplete_install", err.code)

    def test_a_folder_without_the_release_layout_is_incomplete(self) -> None:
        (self.base / "empty").mkdir()
        self.assertEqual("incomplete_install", self.refusal(env=self.env(GHIDRA_INSTALL_DIR=str(self.base / "empty"))).code)

    def test_a_launcher_that_is_not_executable_is_named(self) -> None:
        launcher = self.install / "support" / "analyzeHeadless"
        launcher.chmod(0o644)
        self.assertEqual("not_executable", self.refusal().code)

    def install_of(self, version: str) -> Path:
        home = self.base / f"v{len(list(self.base.iterdir()))}"
        home.mkdir()
        return fake_ghidra.make_install(home, version=version)

    def test_a_version_this_was_not_written_for_is_refused_by_name(self) -> None:
        for version in ("9.2", "13.0", "", "banana"):
            with self.subTest(version=version):
                other = self.install_of(version)
                err = self.refusal(env=self.env(GHIDRA_INSTALL_DIR=str(other)))
                self.assertEqual("unsupported_version", err.code)
                self.assertEqual([], fake_ghidra.records(other))

    def test_the_supported_lines_are_accepted(self) -> None:
        for version in ("11.4", "12.0.2", "12.1.4"):
            with self.subTest(version=version):
                found = ba.locate_ghidra({"GHIDRA_INSTALL_DIR": str(self.install_of(version))})
                self.assertEqual(version, found.version)

    def test_finding_it_executes_nothing(self) -> None:
        ba.locate_ghidra(self.env())
        self.assertEqual([], fake_ghidra.records(self.install))


class TestWhatTheChildIsStartedWith(Case):
    def one_run(self, **env: str) -> dict[str, Any]:
        self.run_tool(env=self.env(**env))
        records = fake_ghidra.records(self.install)
        self.assertEqual(1, len(records))
        return records[0]

    def test_the_flags_are_fixed_and_the_target_is_the_private_copy(self) -> None:
        rec = self.one_run()
        argv = rec["argv"]
        for flag in ("-readOnly", "-deleteProject", "-import", "-scriptPath", "-postScript", "-analysisTimeoutPerFile"):
            self.assertIn(flag, argv)
        target = argv[argv.index("-import") + 1]
        self.assertTrue(target.startswith(str(self.state / "runs")), "the import is not under the private state directory")
        self.assertNotIn(str(self.root), " ".join(argv), "the workspace path reached the argv")
        self.assertEqual(ba.SCRIPT_NAME, argv[argv.index("-postScript") + 1])

    def test_the_copy_is_the_file_and_is_owner_only_and_the_original_is_untouched(self) -> None:
        before = (self.root / "app.bin").stat()
        rec = self.one_run()
        self.assertEqual(hashlib.sha256(BINARY).hexdigest(), rec["target_sha"])
        self.assertEqual(0o600, rec["target_mode"])
        self.assertEqual(BINARY, (self.root / "app.bin").read_bytes())
        self.assertEqual(before.st_mtime_ns, (self.root / "app.bin").stat().st_mtime_ns)
        self.assertEqual({"app.bin"}, {p.name for p in self.root.iterdir()}, "something was written into the workspace")

    def test_the_export_script_is_the_engines_own_file_copied_in(self) -> None:
        rec = self.one_run()
        self.assertEqual(ba.SCRIPT_SOURCE.read_text(encoding="utf-8"), rec["script_text"])
        self.assertIn("class CodifyExport extends GhidraScript", rec["script_text"])

    def test_the_environment_is_stripped_to_what_a_jvm_needs(self) -> None:
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-secret", "CODIFY_BOOT_TOKEN": "tok", "JAVA_TOOL_OPTIONS": "-javaagent:x"}):
            rec = self.one_run(**{"OPENAI_API_KEY": "sk-secret", "JAVA_TOOL_OPTIONS": "-javaagent:x", "GITHUB_TOKEN": "ghp"})
        keys = set(rec["env"])
        self.assertFalse(keys & {"OPENAI_API_KEY", "CODIFY_BOOT_TOKEN", "JAVA_TOOL_OPTIONS", "GITHUB_TOKEN", "GHIDRA_INSTALL_DIR"}, keys)
        self.assertLessEqual(keys, {"PATH", "LANG", "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TMPDIR", "MAXMEM",
                                    "CODIFY_SANDBOX_PARENT_PID", "CODIFY_SPAWN_LIMITS", "LC_CTYPE", "PWD", "SHLVL", "_"})
        for key in ("HOME", "TMPDIR"):
            self.assertTrue(rec["env"][key].startswith(str(self.state / "runs")), f"{key} is outside the private run directory")

    def test_the_heap_cap_defaults_to_two_gigabytes_and_obeys_a_valid_setting_only(self) -> None:
        self.assertEqual("2G", self.one_run()["env"]["MAXMEM"])
        self.setUp()
        self.assertEqual("512M", self.one_run(GHIDRA_HEADLESS_MAXMEM="512M")["env"]["MAXMEM"])
        for bad in ("lots", "-1G", "2G; rm -rf /", "0M", ""):
            with self.subTest(bad=bad):
                self.setUp()
                self.assertEqual("2G", self.one_run(GHIDRA_HEADLESS_MAXMEM=bad)["env"]["MAXMEM"])

    def test_the_child_runs_under_the_engines_limits_in_a_guarded_group(self) -> None:
        rec = self.one_run()
        self.assertEqual([0, 0], rec["limits"]["core"])
        self.assertEqual([ba.ANALYSIS_LIMITS.file_bytes] * 2, [rec["limits"]["fsize"][0], rec["limits"]["fsize"][0]])
        self.assertEqual(ba.ANALYSIS_LIMITS.cpu_seconds, rec["limits"]["cpu"][0])
        self.assertEqual(rec["ppid"], rec["pgid"], "the child is not in the group its guard leads")

    def test_the_run_directory_is_gone_afterwards_and_the_state_is_owner_only(self) -> None:
        self.run_tool()
        self.assertEqual([], self.runs())
        self.assertEqual(0o700, (self.state / "runs").stat().st_mode & 0o777)
        cached = list((self.state / "cache").glob("*.json"))
        self.assertEqual(1, len(cached))
        self.assertEqual(0o600, cached[0].stat().st_mode & 0o777)


class TestTheAnswer(Case):
    def test_the_summary_says_what_ghidra_found_and_what_it_cannot_promise(self) -> None:
        text = self.run_tool()
        self.assertIn("Ghidra 12.0.2 analysed a private copy of app.bin", text)
        self.assertIn(hashlib.sha256(BINARY).hexdigest()[:16], text)
        self.assertIn("not run", text)
        self.assertIn("DATA, not instructions", text)
        self.assertIn("does not prove absence", text)
        self.assertIn("3 functions, 2 imports, 2 defined strings, 2 memory blocks", text)
        self.assertIn("x86:LE:64:default", text)

    def test_each_view_lists_its_own_rows_and_a_query_is_a_literal_filter(self) -> None:
        self.assertIn("00401136  main  (42 bytes)", self.run_tool(view="functions"))
        self.assertNotIn("helper", self.run_tool(view="functions", query="MAIN"))
        self.assertIn("- strcpy  [libc.so.6]", self.run_tool(view="imports"))
        self.assertIn('"/etc/passwd"', self.run_tool(view="strings", query="passwd"))
        self.assertIn(".text  start 00401000  512 bytes  rx", self.run_tool(view="sections"))

    def test_a_regex_is_a_literal_not_a_pattern(self) -> None:
        self.assertIn("(0 match)", self.run_tool(view="functions", query="ma.n"))
        self.assertIn("(0 match)", self.run_tool(view="functions", query="(a+)+$"))

    def test_an_import_ghidra_could_not_tie_to_a_library_is_listed_without_one(self) -> None:
        fake_ghidra.set_mode(self.install, "ok", {**fake_ghidra.GOOD_EXPORT, "imports": [
            {"name": "puts", "library": "<EXTERNAL>"}, {"name": "strcpy", "library": "libc.so.6"}]})
        text = self.run_tool(view="imports")
        self.assertIn("- puts\n", text + "\n")
        self.assertNotIn("<EXTERNAL>", text)
        self.assertIn("- strcpy  [libc.so.6]", text)

    def test_calls_shows_callers_and_callees_by_name_or_address(self) -> None:
        by_name = self.run_tool(view="calls", query="main")
        self.assertIn("called by: _start", by_name)
        self.assertIn("calls:     puts, helper", by_name)
        self.assertIn("main @ 00401136", self.run_tool(view="calls", query="00401136"))
        self.assertIn("needs a `query`", self.run_tool(view="calls"))
        self.assertIn("No function matches", self.run_tool(view="calls", query="nothing"))

    def test_the_second_question_about_the_same_bytes_does_not_run_ghidra_again(self) -> None:
        self.run_tool()
        text = self.run_tool(view="imports")
        self.assertEqual(1, len(fake_ghidra.records(self.install)))
        self.assertIn("from an earlier run", text)

    def test_a_cancel_reaches_a_call_that_is_waiting_for_another_analysis(self) -> None:
        """One analysis runs at a time. A call queued behind it used to wait out the whole run, up to five
        minutes, whatever the goal's signal said."""
        stop = threading.Event()
        ba._RUN_LOCK.acquire()

        def release_if_held() -> None:
            if ba._RUN_LOCK.locked():
                ba._RUN_LOCK.release()

        self.addCleanup(release_if_held)
        threading.Timer(0.3, stop.set).start()
        started = time.monotonic()
        error = self.refusal(cancel=stop)
        self.assertEqual("cancelled", error.code)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([], fake_ghidra.records(self.install), "Ghidra must not have been started")

    def test_a_cache_that_cannot_be_written_does_not_cost_the_answer(self) -> None:
        with mock.patch.object(ba, "_cache_put", side_effect=OSError("disk full")):
            text = self.run_tool()
        self.assertIn("Ghidra", text)

    def test_a_cache_write_that_fails_halfway_leaves_no_temporary_file(self) -> None:
        target = self.state / "cache" / "x.json"
        with mock.patch.object(json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                ba._cache_put(target, {"a": 1})
        self.assertEqual([], [p.name for p in target.parent.iterdir()])

    def test_a_cache_file_pruned_by_another_engine_mid_listing_is_not_an_error(self) -> None:
        folder = self.state / "cache"
        folder.mkdir(parents=True)
        for n in range(ba.CACHE_KEEP + 3):
            (folder / f"{n:03d}.json").write_text("{}", encoding="utf-8")
        real = Path.stat
        gone = folder / "000.json"

        def vanishing(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == gone:
                raise FileNotFoundError(str(self))
            return real(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", vanishing):
            ba._cache_put(folder / "new.json", {"a": 1})
        self.assertTrue((folder / "new.json").exists())

    def test_other_bytes_are_analysed_again(self) -> None:
        self.run_tool()
        (self.root / "other.bin").write_bytes(BINARY + b"!")
        self.run_tool("other.bin")
        self.assertEqual(2, len(fake_ghidra.records(self.install)))

    def test_a_view_the_tool_does_not_have_is_refused_before_anything_starts(self) -> None:
        err = self.refusal(view="decompile")
        self.assertEqual("bad_request", err.code)
        self.assertEqual([], fake_ghidra.records(self.install))

    def test_the_limit_is_bounded(self) -> None:
        fake_ghidra.set_mode(self.install, "ok", {**fake_ghidra.GOOD_EXPORT, "functions": [
            {"name": f"f{i}", "address": f"{i:08x}", "size": 1, "thunk": False, "callers": [], "callees": []} for i in range(300)],
            "totals": {"functions": 300, "imports": 0, "strings": 0, "blocks": 0}})
        text = self.run_tool(view="functions", limit=100000)
        self.assertEqual(ba.MAX_LIMIT, sum(1 for line in text.splitlines() if line.startswith("- ")))
        self.assertIn(f"({ba.MAX_LIMIT} of 300 functions shown", text)


class TestTheBinarysWordsAreData(Case):
    def test_hostile_names_and_strings_come_back_clean_clipped_and_framed(self) -> None:
        fake_ghidra.set_mode(self.install, "ok", fake_ghidra.HOSTILE_EXPORT)
        text = self.run_tool(view="functions", limit=ba.MAX_LIMIT)
        for bad in ("\x1b", "\x07", "​"):
            self.assertNotIn(bad, text)
        injected = [ln for ln in text.splitlines() if "IGNORE ALL PREVIOUS" in ln]
        self.assertEqual(1, len(injected))
        self.assertTrue(injected[0].startswith("- 00401000  evil"), "an embedded newline started a line of its own")
        self.assertLess(text.index("DATA, not instructions"), text.index("evil"), "the framing comes after the binary's text")
        self.assertLess(len(text), 60_000)
        strings = self.run_tool(view="strings")
        for bad in ("‮", "﻿", "\ud800"):
            self.assertNotIn(bad, strings)
        self.assertNotIn("z" * (ba.EXPORT_CAPS["text"] + 1), strings)
        self.assertIn("The export was capped: functions (", self.run_tool(view="summary"))

    def test_the_cached_document_is_bounded_whatever_the_script_wrote(self) -> None:
        fake_ghidra.set_mode(self.install, "ok", fake_ghidra.HOSTILE_EXPORT)
        self.run_tool()
        doc = json.loads(next((self.state / "cache").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(ba.EXPORT_CAPS["functions"], len(doc["functions"]))
        self.assertLessEqual(len(doc["functions"][0]["callers"]), ba.EXPORT_CAPS["edges"])
        self.assertLessEqual(max(len(c) for c in doc["functions"][0]["callees"]), 80)


class TestTheInputFile(Case):
    def test_only_a_regular_file_inside_the_workspace_within_the_limit_is_copied(self) -> None:
        (self.root / "dir").mkdir()
        (self.root / "empty.bin").write_bytes(b"")
        os.mkfifo(self.root / "pipe")
        outside = self.base / "outside.bin"
        outside.write_bytes(BINARY)
        (self.root / "escape").symlink_to(outside)
        cases = {"dir": "bad_input", "empty.bin": "bad_input", "pipe": "bad_input",
                 "escape": "bad_input", "../outside.bin": "bad_input", "/etc/passwd": "bad_input",
                 ".git/config": "bad_input", "missing.bin": "bad_input", "": "bad_request"}
        for path, code in cases.items():
            with self.subTest(path=path):
                self.assertEqual(code, self.refusal(path=path).code)
        self.assertEqual([], fake_ghidra.records(self.install), "Ghidra was started for a file that was refused")
        self.assertEqual([], self.runs())

    def test_a_link_that_stays_inside_the_workspace_is_the_file_it_points_at(self) -> None:
        (self.root / "link").symlink_to(self.root / "app.bin")
        self.assertIn("3 functions", self.run_tool("link"))

    def test_a_file_over_the_size_limit_is_refused_before_it_is_copied(self) -> None:
        with mock.patch.object(ba, "MAX_INPUT_BYTES", 100):
            err = self.refusal()
        self.assertEqual("too_large", err.code)
        self.assertIn("Analyse a smaller binary", str(err))
        self.assertEqual([], fake_ghidra.records(self.install))

    def test_a_link_swapped_in_after_the_path_was_resolved_is_not_followed(self) -> None:
        outside = self.base / "outside.bin"
        outside.write_bytes(BINARY)
        link = self.base / "swapped"
        link.symlink_to(outside)
        with self.assertRaises(BinaryAnalysisError) as caught:
            ba._snapshot(link, self.base / "copy.bin")
        self.assertEqual("bad_input", caught.exception.code)
        self.assertIn("link", str(caught.exception))
        self.assertFalse((self.base / "copy.bin").exists())

    def test_the_copy_is_checked_against_what_was_read(self) -> None:
        # The check lives inside _snapshot, so exercise it by tampering between the copy and the read-back.
        original_open = open

        def tamper(path: Any, *a: Any, **kw: Any) -> Any:
            if str(path).endswith("target.bin") and a and a[0] == "rb":
                Path(path).write_bytes(b"tampered")
            return original_open(path, *a, **kw)

        with mock.patch("builtins.open", tamper):
            err = self.refusal()
        self.assertEqual("bad_input", err.code)
        self.assertTrue(err.retryable)
        self.assertEqual([], fake_ghidra.records(self.install))


class TestEveryWayARunGoesWrong(Case):
    def failing(self, mode: str, **kw: Any) -> BinaryAnalysisError:
        fake_ghidra.set_mode(self.install, mode)
        err = self.refusal(**kw)
        self.assertEqual([], self.runs(), f"{mode}: a run directory was left behind")
        self.assertEqual([], list((self.state / "cache").glob("*.json")) if (self.state / "cache").exists() else [], f"{mode}: a failure was cached")
        return err

    def test_a_nonzero_exit_names_the_status_and_quotes_the_log_without_control_characters(self) -> None:
        err = self.failing("exit")
        self.assertEqual("failed", err.code)
        self.assertIn("status 3", str(err))
        self.assertIn("java not found", str(err))
        self.assertNotIn("\x1b", str(err))
        self.assertIn("JAVA_HOME", str(err))
        self.assertFalse(err.retryable)

    def test_exit_zero_without_an_export_is_still_a_failure(self) -> None:
        self.assertEqual("no_export", self.failing("silent").code)

    def test_a_script_error_reported_in_the_export_is_a_failure(self) -> None:
        err = self.failing("script_error")
        self.assertEqual("failed", err.code)
        self.assertIn("NullPointerException", str(err))

    def test_garbage_the_wrong_shape_and_the_wrong_version_are_refused(self) -> None:
        for mode in ("garbage", "array", "wrong_version"):
            with self.subTest(mode=mode):
                self.assertEqual("bad_export", self.failing(mode).code)

    def test_an_oversize_export_is_refused_unread(self) -> None:
        self.assertEqual("bad_export", self.failing("flood").code)

    def test_a_run_past_its_deadline_is_killed_and_may_be_retried(self) -> None:
        with mock.patch.object(ba, "ANALYSIS_DEADLINE_S", 1.0):
            started = time.monotonic()
            err = self.failing("hang")
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual("timeout", err.code)
        self.assertTrue(err.retryable)
        self.assertFalse(fake_ghidra.alive(fake_ghidra.records(self.install)[0]["pid"]), "the stand-in outlived its deadline")

    def test_a_child_that_ignores_term_is_still_gone(self) -> None:
        with mock.patch.object(ba, "ANALYSIS_DEADLINE_S", 1.0):
            self.failing("ignore_term")
        self.assertFalse(fake_ghidra.alive(fake_ghidra.records(self.install)[0]["pid"]))

    def test_a_cancel_stops_the_run_where_it_is_and_kills_the_whole_group(self) -> None:
        fake_ghidra.set_mode(self.install, "child")
        cancel = threading.Event()
        threading.Timer(1.5, cancel.set).start()
        started = time.monotonic()
        err = self.refusal(cancel=cancel)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual("cancelled", err.code)
        record = fake_ghidra.records(self.install)[0]
        grandchild = int((self.install / "child.pid").read_text(encoding="utf-8"))
        deadline = time.monotonic() + 3
        while (fake_ghidra.alive(record["pid"]) or fake_ghidra.alive(grandchild)) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(fake_ghidra.alive(record["pid"]))
        self.assertFalse(fake_ghidra.alive(grandchild), "a process the analysis started outlived its cancel")
        self.assertEqual([], self.runs())

    def test_a_run_that_failed_can_be_repeated_and_succeeds(self) -> None:
        self.failing("exit")
        fake_ghidra.set_mode(self.install, "ok")
        self.assertIn("3 functions", self.run_tool())


class TestWhatACrashLeaves(Case):
    def dead_pid(self) -> int:
        child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 — this interpreter, a literal no-op, to get a pid that has exited
        child.wait()
        return child.pid

    def test_the_next_run_sweeps_the_directories_of_dead_pids_and_nothing_else(self) -> None:
        runs = self.state / "runs"
        runs.mkdir(parents=True)
        dead = runs / f"{self.dead_pid()}-aaaa"
        alive_dir = runs / f"{os.getppid()}-bbbb"
        stranger = runs / "not-ours"
        for d in (dead, alive_dir, stranger):
            (d / "home").mkdir(parents=True)
            (d / "home" / "file").write_text("x", encoding="utf-8")
        self.run_tool()
        self.assertFalse(dead.exists(), "a dead engine's run directory survived the sweep")
        self.assertTrue(alive_dir.exists(), "a live pid's directory was swept")
        self.assertTrue(stranger.exists(), "a directory that is not ours was swept")

    def test_the_sweep_counts_what_it_removed_and_survives_a_missing_directory(self) -> None:
        self.assertEqual(0, ba.sweep_stale_runs(self.base / "never-made"))
        runs = self.state / "runs"
        (runs / f"{self.dead_pid()}-x").mkdir(parents=True)
        self.assertEqual(1, ba.sweep_stale_runs(self.state))

    def test_the_cache_keeps_only_the_newest_entries(self) -> None:
        with mock.patch.object(ba, "CACHE_KEEP", 2):
            for i in range(4):
                (self.root / f"b{i}.bin").write_bytes(BINARY + bytes([i]))
                self.run_tool(f"b{i}.bin")
        self.assertEqual(2, len(list((self.state / "cache").glob("*.json"))))


class TestTheChildDiesWithTheEngine(Case):
    def test_killing_the_engine_takes_ghidra_and_what_it_started_with_it(self) -> None:
        fake_ghidra.set_mode(self.install, "child")
        code = (
            "import os, sys\n"
            "from engine import binary_analysis as ba\n"
            f"ba.analyze_binary({str(self.root)!r}, 'app.bin', env={self.env()!r})\n"
        )
        engine = subprocess.Popen(  # noqa: S603 — this interpreter and a test's own code, standing in for the engine
            [sys.executable, "-c", code], cwd=REPO, start_new_session=True,
            env={**os.environ, "CODIFY_HOME": str(self.base / "home")},
        )
        try:
            marker = self.install / "child.pid"
            deadline = time.monotonic() + 20
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "the stand-in never started")
            record = fake_ghidra.records(self.install)[0]
            grandchild = int(marker.read_text(encoding="utf-8"))
            self.assertTrue(fake_ghidra.alive(record["pid"]) and fake_ghidra.alive(grandchild))
            os.kill(engine.pid, signal.SIGKILL)
            engine.wait()
            deadline = time.monotonic() + 8
            while (fake_ghidra.alive(record["pid"]) or fake_ghidra.alive(grandchild)) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(fake_ghidra.alive(record["pid"]), "Ghidra outlived the engine")
            self.assertFalse(fake_ghidra.alive(grandchild), "a process Ghidra started outlived the engine")
        finally:
            if engine.poll() is None:
                engine.kill()
                engine.wait()


class TestTheExportScript(unittest.TestCase):
    """The Java file is the engine's own; these are the claims its header makes, as source."""

    text = ba.SCRIPT_SOURCE.read_text(encoding="utf-8")

    def test_it_is_named_for_its_class_and_extends_ghidras_script(self) -> None:
        self.assertEqual("CodifyExport.java", ba.SCRIPT_NAME)
        self.assertIn("public class CodifyExport extends GhidraScript", self.text)

    def test_it_opens_no_socket_starts_no_process_and_writes_one_file(self) -> None:
        for forbidden in ("java.net", "Socket", "ProcessBuilder", "Runtime.getRuntime", "exec(", "System.exit",
                          "currentProgram.getUndoManager", ".setName(", ".createLabel(", "start(", "delete("):
            self.assertNotIn(forbidden, self.text)
        self.assertEqual(1, self.text.count("Files.write("))

    def test_the_caps_it_is_given_are_the_ones_the_engine_passes(self) -> None:
        argv = ba._argv(ba.GhidraInstall(Path("/g"), Path("/g/support/analyzeHeadless"), "12.0"), Path("/r"), Path("/r/t"), Path("/r/o"))
        passed = argv[argv.index("-postScript") + 1:]
        self.assertEqual(["CodifyExport.java", "/r/o", *(str(ba.EXPORT_CAPS[k]) for k in ("functions", "imports", "strings", "blocks", "edges", "text"))], passed)
        self.assertIn("args.length < 7", self.text)


if __name__ == "__main__":
    unittest.main()
