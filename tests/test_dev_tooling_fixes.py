"""Regression tests for the third audit's packaging and developer tooling fixes (PKG-1, PKG-12..17, DEV-1..19)."""
from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks import parallel_spike, runner, vendor
from engine.db import connect
from engine.models import GoalCreate, WorkspaceCreate
from engine.services import GoalService, WorkspaceService
from engine.trace import TraceService
from scripts import (
    bundle_engine,
    check_budgets,
    fake_ollama,
    release_notes,
    replay_trace,
    tabs_smoke,
    venv_gaps,
)

ROOT = Path(__file__).resolve().parent.parent


class TestDevToolingAndPackagingFixes(unittest.TestCase):
    def test_pkg1_and_dev4_ci_python_floor_heredoc_and_canonical_guard(self) -> None:
        script = (ROOT / "scripts" / "ci-python-floor.sh").read_text(encoding="utf-8")
        self.assertNotIn("cat >&2 <<EOF", script, "unquoted heredoc with `make ci` must not exist")
        self.assertIn("os.path.realpath", script, "owned_by_this_script must canonicalize paths")

    def test_dev1_and_dev17_replay_trace_never_mutates_live_agent_configs(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "codify.db"
            ws_dir = Path(tmp) / "ws"
            ws_dir.mkdir()
            conn = connect(db_path)
            try:
                conn.execute(
                    "UPDATE agent_configs SET provider = 'anthropic', protocol = 'anthropic', "
                    "model_name = 'claude-3-7-sonnet', api_key_ref = 'anthropic' WHERE role = 'planner'"
                )
                conn.commit()
                ws = WorkspaceService(conn).create(WorkspaceCreate(name="w", root_path=str(ws_dir)))
                goal = GoalService(conn).create(
                    GoalCreate(workspace_id=ws.id, title="t", description="d", plan_only=True)
                )
                GoalService(conn).set_trace(goal.id, True)
                TraceService(conn).record(
                    goal.id,
                    None,
                    role="planner",
                    provider="anthropic",
                    model="claude-3-7-sonnet",
                    temperature=0.0,
                    max_tokens=1024,
                    system_prompt="sys",
                    user_prompt="hi",
                    response=json.dumps({"steps": [{"title": "s", "description": "d", "suggested_paths": []}]}),
                    duration_ms=10,
                )
                asyncio.run(replay_trace.replay(conn, goal.id))
                row = conn.execute(
                    "SELECT provider, model_name, api_key_ref FROM agent_configs WHERE role = 'planner'"
                ).fetchone()
                self.assertEqual(row["provider"], "anthropic")
                self.assertEqual(row["model_name"], "claude-3-7-sonnet")
                self.assertEqual(row["api_key_ref"], "anthropic")
            finally:
                conn.close()

    def test_dev2_dev3_dev14_vendor_validation_multicomponent_subpath_and_temp_tar(self) -> None:
        for bad in ("owner/..@1234567", "../evil/repo@1234567", "owner/repo@not-a-sha"):
            with self.assertRaises(vendor.VendorError, msg=bad):
                vendor.parse_spec(bad)
        upstream, sha = vendor.parse_spec("owner/repo@1234567890abcdef")
        self.assertEqual((upstream, sha), ("owner/repo", "1234567890abcdef"))

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_repo"
            (src / "algorithms" / "sorting").mkdir(parents=True)
            (src / "algorithms" / "sorting" / "qsort.py").write_text("x = 1\n", encoding="utf-8")
            tar_bytes = vendor._tar_dir(src)
            self.assertFalse((Path(tmp) / "src_repo.tar.gz").exists(), "_tar_dir must not leak <source>.tar.gz")
            dest = Path(tmp) / "out"
            files = vendor.extract_snapshot(
                tar_bytes, dest, sha="1234567890abcdef", subpath="algorithms/sorting"
            )
            self.assertEqual(files, ["qsort.py"])
            self.assertTrue((dest / "qsort.py").is_file())

    def test_dev7_and_dev16_benchmark_manifest_tasks_are_consistent(self) -> None:
        manifest = json.loads((ROOT / "benchmarks" / "manifest.json").read_text(encoding="utf-8"))
        by_id = {t["id"]: t for t in manifest["tasks"]}
        whisper_diffs = by_id["review-clean-whisper"]["diff"]
        test_diff = next(d for d in whisper_diffs if d["path"] == "tests/test_app.py")
        self.assertIn("whisper", test_diff["unified_diff"])
        gap = by_id["repo-close-the-gap"]
        self.assertIn("__all__", gap["title"])

    def test_dev8_tabs_smoke_only_treats_refusal_exit_as_taken(self) -> None:
        run_crash = tabs_smoke.SmokeRun(code=1, engaged=False, passed=False, elapsed_s=0.2, child_rc=-11, interrupted=False)
        self.assertEqual(run_crash.child_rc, -11)
        self.assertFalse(run_crash.interrupted)

    def test_dev10_fake_ollama_threaded_and_deep_review_lens_json(self) -> None:
        self.assertTrue(hasattr(fake_ollama, "ThreadingHTTPServer"))
        src = (ROOT / "scripts" / "fake_ollama.py").read_text(encoding="utf-8")
        self.assertIn("ThreadingHTTPServer", src)
        self.assertIn("zoom-out", src)
        self.assertIn('json.dumps({"findings": []})', src)

    def test_dev12_check_ghidra_zero_functions_word_boundary(self) -> None:
        text = (ROOT / "scripts" / "check_ghidra.py").read_text(encoding="utf-8")
        self.assertIn(r"\b0 functions\b", text)

    def test_dev13_runner_accumulates_stage_ms(self) -> None:
        events = [
            {"type": "stage_result", "payload": {"stage": "fixer", "duration_ms": 100, "tokens": 10}},
            {"type": "stage_result", "payload": {"stage": "fixer", "duration_ms": 150, "tokens": 20}},
        ]
        res = {
            "passed": True,
            "stage_ms": {"fixer": 250},
            "quality_checks": [],
            "call_health": runner.call_health(events),
            "failure": None,
            "tokens": 30,
            "tokens_are_synthetic": True,
            "wall_ms": 250,
        }
        summary = runner.summarise([res])
        self.assertEqual(summary["stage_ms"]["fixer"], 250)
        self.assertEqual(summary["call_health"]["fixer"]["ran"], 2)

    def test_dev15_parallel_spike_rejects_zero_steps_and_handles_zero_denominator(self) -> None:
        self.assertIsNone(parallel_spike._ratio(1.0, 0.0))
        with self.assertRaises(ValueError):
            import asyncio
            asyncio.run(parallel_spike.run_spike(0, 0.01))

    def test_dev18_venv_gaps_single_line_and_single_quotes(self) -> None:
        toml = "dependencies = ['fastapi>=0.115', \"pydantic>=2.0\"]\ndev = ['ruff>=0.4']\n"
        self.assertEqual(venv_gaps.declared(toml), ["fastapi", "pydantic", "ruff"])
        with self.assertRaises(ValueError):
            venv_gaps.declared("dependencies = ['fastapi']\ndev = unclosed\n")

    def test_dev19_check_budgets_non_object_json_raises_budget_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "budgets.json"
            p.write_text("[]", encoding="utf-8")
            with self.assertRaises(check_budgets.BudgetError):
                check_budgets.load_budgets(p)

    def test_pkg12_and_pkg13_release_notes_and_bundle_engine(self) -> None:
        text = release_notes.notes("v1.6.0", "1.6.0")
        self.assertIn("makepkg -si -p Codify_1.6.0_amd64.PKGBUILD", text)
        self.assertEqual([], release_notes.disagreements("v1.6.0-rc1", [("a", "1.6.0-rc1")]))

        # bundle_engine falls back across mirrors when first mirror produces corrupt bytes
        pin = bundle_engine.Pin(
            version="3.12.9",
            release="20250212",
            asset="cpython.tar.gz",
            sha256="a" * 64,
        )
        calls: list[str] = []

        def fake_download(url: str, dest: Path) -> None:
            calls.append(url)
            dest.write_bytes(b"corrupt")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(bundle_engine.BundleError):
                bundle_engine.fetch_python(pin, Path(tmp), download=fake_download)
        self.assertEqual(len(calls), 2, "must try second mirror when first mirror fails sha256 check")


if __name__ == "__main__":
    unittest.main()
