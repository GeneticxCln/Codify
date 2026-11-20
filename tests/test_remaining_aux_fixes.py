"""Regression tests for the third audit's engine auxiliary modules, scan profiles, and built-in skills (AUX-1 .. AUX-14)."""
from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from engine import ask, home, metrics, scan, stats, surface_editor, surfaces
from engine.conductor_tools import ConductorTools
from engine.db import connect
from engine.models import TurnCreate, WorkspaceCreate
from engine.services import SettingsService, WorkspaceService
from engine.skills import builtin_skills
from engine.suggestions import SuggestionWatch


class TestAuxFixes(unittest.TestCase):
    def test_aux1_and_aux12_suggestion_watch_concurrency_and_pruning(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "codify.db")
            try:
                ws_svc = WorkspaceService(conn)
                repo = Path(tmp) / "repo"
                repo.mkdir()
                ws_svc.create(WorkspaceCreate(name="w", root_path=str(repo)))
                settings = SettingsService(conn)
                watch = SuggestionWatch(conn, settings, ws_svc)
                watch._ci_checked["stale-deleted-ws"] = 1.0
                asyncio.run(watch.sweep())
                self.assertNotIn("stale-deleted-ws", watch._ci_checked)
            finally:
                conn.close()

    def test_aux2_and_aux3_surfaces_and_editor_scrub_or_reject_surrogates(self) -> None:
        with self.assertRaises(ValidationError):
            surfaces.SurfaceWindow(machines=["\ud800"])
        ans = surfaces.SurfaceAnswer(id="req-1", error="bad\ud800err")
        self.assertNotIn("\ud800", ans.error or "")
        sel = surface_editor.Selection(from_line=1, to_line=1, text="hello\ud800")
        self.assertNotIn("\ud800", sel.text)

    def test_aux4_and_aux10_builtin_scan_profiles_redos_and_accuracy(self) -> None:
        loaded = scan.load_profiles(None)
        self.assertEqual((), tuple(loaded.problems))
        by_name = {p.name: p for p in loaded.profiles}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # ReDoS payload: 50 KB of repeated unclosed parens/brackets across lines
            (root / "redos.py").write_text(("eval(foo(\n" * 4000) + "x = 1\n", encoding="utf-8")
            all_rules = [rule for p in loaded.profiles for rule in p.rules]
            result = scan.scan_tree(str(root), [r.to_worker() for r in all_rules], None, True, False, 5.0)
            self.assertIsInstance(result["findings"], list)

            # AUX-10: yaml.load(..., Loader=yaml.SafeLoader) is not flagged, while unsafe yaml.load(data) is
            (root / "safe_yaml.py").write_text(
                "import yaml\nyaml.load(stream, Loader=yaml.SafeLoader)\n",
                encoding="utf-8",
            )
            deser_rules = [r.to_worker() for r in by_name["deserialization"].rules]
            res_safe = scan.scan_tree(str(root), deser_rules, "safe_yaml.py", True, False, 5.0)
            self.assertEqual([], [f for f in res_safe["findings"] if f["rule"] == "python-yaml-unsafe-load"])

            # AUX-10: Jinja comments {# ... | safe #} are masked when include_comments=False
            (root / "tpl.jinja").write_text("{# {{ user | safe }} #}\n<p>clean</p>\n", encoding="utf-8")
            web_rules = [r.to_worker() for r in by_name["web"].rules]
            res_tpl = scan.scan_tree(str(root), web_rules, "tpl.jinja", True, False, 5.0)
            self.assertEqual([], [f for f in res_tpl["findings"] if f["rule"] == "template-safe-filter"])

    def test_aux5_aux6_aux7_home_isolation_and_symlink_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.txt"
            target.write_text("secret", encoding="utf-8")
            target.chmod(0o644)
            link = root / "link.txt"
            link.symlink_to(target)
            home.tighten_to_owner(link)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644, "tighten_to_owner must not chmod symlink target")
            home.ensure_private_file(link)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644, "ensure_private_file must not chmod symlink target")
            self.assertEqual(target.read_text(encoding="utf-8"), "secret")

            tok1 = home.boot_token()
            tok2 = home.boot_token()
            self.assertEqual(tok1, tok2)
            self.assertEqual(len(tok1), 64)

    def test_aux9_metrics_and_stats_non_finite_resilience(self) -> None:
        self.assertIsNone(metrics._finite_float(float("nan")))
        self.assertIsNone(metrics._finite_int(float("inf")))
        ov = stats.build_overview(
            [{"id": "g1", "status": "COMPLETED", "created_at": 1000.0}],
            [
                {
                    "goal_id": "g1",
                    "type": "usage",
                    "timestamp": 1001.0,
                    "payload": {"role": "planner", "total_tokens": float("inf"), "duration_ms": float("nan")},
                }
            ],
            window_days=30,
            now=2000.0,
        )
        self.assertEqual(ov["usage"]["total_tokens"], 0)

    def test_aux13_ask_cr_normalization_turn_create_blank_and_regex_worker(self) -> None:
        q = ask.parse_question({"question": "Line1\rLine2"})
        self.assertEqual(q.text, "Line1\nLine2")
        with self.assertRaises(ValidationError):
            TurnCreate(prompt="   \t\n  ")
        proc = subprocess.run(
            [sys.executable, "engine/regex_worker.py"],
            input="not valid json",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        reply = json.loads(proc.stdout)
        self.assertFalse(reply["ok"])

    def test_aux14_choose_a_skill_routes_all_expected_skills(self) -> None:
        skills, problems = builtin_skills(ConductorTools.NAMES)
        self.assertEqual([], problems)
        by_name = {s.name: s for s in skills}
        router = by_name["choose-a-skill"].body
        for expected in ("ship-a-change", "verify-change", "review-replies", "health-review"):
            self.assertIn(f"`{expected}`", router)
        self.assertIn("read_file", by_name["plan-loop"].moves)
        self.assertIn("git_history", by_name["encode-a-lesson"].moves)
        self.assertIn("ask_user", by_name["working-style"].moves)


if __name__ == "__main__":
    unittest.main()
