"""The review tier (docs/21 P1): the tasks can be passed and failed, and the harness measures what it claims.

Three things, none of which needs a model:

* **A planted defect is really in the diff**, and a control's diff really lacks it. A task whose defect is not there
  measures a reviewer's imagination, and one that cannot be missed measures nothing.
* **The scoring is what the docstring says**: a report that names the defect passes, one that does not fails, a
  severity floor is honoured, and a clean change that draws a loud finding is a false alarm.
* **The canned run proves the wiring and the cost, and claims no recall**: deep mode records its lenses, standard
  mode none, neither exceeds its call cap, and every quality check is `skipped`, never `passed`.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from benchmarks.review_tier import (
    CALL_CAPS,
    MODES,
    expand_review_modes,
    run_review_check,
    run_review_task,
    summarise_review,
)
from benchmarks.runner import DEFAULT_MANIFEST, load_manifest


def review_tasks() -> list[dict[str, Any]]:
    return [t for t in load_manifest(DEFAULT_MANIFEST)["tasks"] if t.get("kind") == "review"]


def added_lines(task: dict[str, Any]) -> str:
    return "\n".join(
        line[1:] for d in task["diff"] for line in (d.get("unified_diff") or "").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


class TestTheTasks(unittest.TestCase):
    def test_the_tier_has_defects_and_controls_in_the_proportion_it_claims(self) -> None:
        tasks = review_tasks()
        defects = [t for t in tasks if t.get("defect")]
        self.assertGreaterEqual(len(defects), 6)
        self.assertGreaterEqual(len(tasks) - len(defects), 2, "without controls a false-alarm count means nothing")
        self.assertEqual("configured", load_manifest(DEFAULT_MANIFEST)["tiers"]["review"]["provider"])
        self.assertEqual(len(tasks), len({t["id"] for t in tasks}))

    def test_every_planted_defect_is_in_the_diff_and_no_control_carries_one(self) -> None:
        for task in review_tasks():
            with self.subTest(task=task["id"]):
                if task.get("defect"):
                    self.assertIn(task["defect"]["planted"], added_lines(task))
                    self.assertTrue(task["defect"]["any_of"])
                else:
                    self.assertFalse(
                        any(c["type"] == "review_report_includes" for c in task["checks"]),
                        "a control has no defect for a report to include",
                    )

    def test_every_task_names_a_diff_against_files_the_fixture_has(self) -> None:
        fixture = Path(DEFAULT_MANIFEST).parent / "fixtures" / "synthetic-repo"
        for task in review_tasks():
            for entry in task["diff"]:
                with self.subTest(task=task["id"], path=entry["path"]):
                    self.assertTrue((fixture / entry["path"]).is_file())

    def test_a_task_runs_once_per_mode(self) -> None:
        expanded = expand_review_modes(review_tasks())
        self.assertEqual(len(review_tasks()) * len(MODES), len(expanded))
        self.assertEqual({"standard", "deep"}, {t["mode"] for t in expanded})
        other = {"id": "smoke-x", "tier": "smoke"}
        self.assertEqual([other], expand_review_modes([other]), "a task that is not a review is untouched")


def report(*findings: dict[str, str]) -> list[dict[str, Any]]:
    return [{"type": "review_report", "payload": {"findings": list(findings), "lenses": [], "status": "3 of 3"}}]


def finding(severity: str, text: str, scope: str = "src/app.py:1") -> dict[str, str]:
    return {"severity": severity, "scope": scope, "category": "x", "evidence": text, "recommendation": "fix"}


class TestTheScoring(unittest.TestCase):
    def check(self, check: dict[str, Any], events: list[dict[str, Any]], mode: str = "deep", **kw: Any) -> str:
        args: dict[str, Any] = {"decision": "approve", "model_calls": 3}
        args.update(kw)
        return run_review_check(check, mode=mode, events=events, **args)[0]

    def test_a_report_that_names_the_defect_passes_and_one_that_does_not_fails(self) -> None:
        wanted = {"type": "review_report_includes", "any_of": ["off-by-one"]}
        self.assertEqual("passed", self.check(wanted, report(finding("major", "an Off-By-One in the slice"))))
        self.assertEqual("failed", self.check(wanted, report(finding("major", "the naming is odd"))))
        self.assertEqual("failed", self.check(wanted, report()))

    def test_the_severity_floor_is_honoured(self) -> None:
        wanted = {"type": "review_report_includes", "any_of": ["slice"]}
        self.assertEqual("failed", self.check(wanted, report(finding("nit", "slice"))), "a nit is below the default floor")
        self.assertEqual("passed", self.check({**wanted, "min_severity": "nit"}, report(finding("nit", "slice"))))

    def test_a_standard_review_has_no_report_and_the_check_says_so_rather_than_failing(self) -> None:
        wanted = {"type": "review_report_includes", "any_of": ["x"]}
        self.assertEqual("skipped", self.check(wanted, [], mode="standard"))

    def test_a_loud_finding_on_a_clean_change_is_a_false_alarm(self) -> None:
        quiet = {"type": "no_blocking_findings"}
        self.assertEqual("passed", self.check(quiet, report(finding("nit", "meh"))))
        self.assertEqual("failed", self.check(quiet, report(finding("major", "wrong"))))
        self.assertEqual("failed", self.check(quiet, report(finding("blocker", "wrong"))))

    def test_the_critics_decision_is_scored_against_what_the_task_expects(self) -> None:
        self.assertEqual("passed", self.check({"type": "critic_requests_changes"}, [], decision="request-changes"))
        self.assertEqual("failed", self.check({"type": "critic_requests_changes"}, [], decision="approve"))
        self.assertEqual("passed", self.check({"type": "critic_approves"}, [], decision="approve"))
        self.assertEqual("failed", self.check({"type": "critic_approves"}, [], decision="request-changes"))
        self.assertEqual("failed", self.check({"type": "critic_approves"}, [], decision="invalid"))

    def test_the_call_cap_is_per_mode(self) -> None:
        cap = {"type": "max_calls", "max": {"standard": 3, "deep": 5}}
        self.assertEqual("passed", self.check(cap, [], mode="deep", model_calls=5))
        self.assertEqual("failed", self.check(cap, [], mode="deep", model_calls=6))
        self.assertEqual("failed", self.check(cap, [], mode="standard", model_calls=4))
        self.assertEqual("passed", self.check({"type": "max_calls"}, [], mode="standard", model_calls=CALL_CAPS["standard"]))

    def test_an_unknown_check_fails_loudly(self) -> None:
        self.assertEqual("failed", self.check({"type": "vibes"}, []))

    def test_the_summary_counts_and_does_not_invent_a_rate(self) -> None:
        def row(mode: str, defect: bool, caught: str, loud: int = 0, decision: str = "approve") -> dict[str, Any]:
            return {
                "review": {"mode": mode, "defect": defect, "decision": decision, "model_calls": 3,
                           "blocking_findings": loud, "quality_measured": True},
                "quality_checks": [{"type": "critic_requests_changes", "status": caught},
                                   {"type": "review_report_includes", "status": caught}],
            }

        summary = summarise_review([
            row("deep", True, "passed"), row("deep", True, "failed"), row("deep", False, "skipped", loud=1),
            row("standard", True, "failed"), row("standard", False, "skipped"),
        ])
        assert summary is not None
        self.assertEqual(1, summary["deep"]["critic_caught"])
        self.assertEqual(1, summary["deep"]["false_alarms"])
        self.assertEqual(0, summary["standard"]["critic_caught"])
        self.assertEqual(0, summary["standard"]["false_alarms"])
        self.assertIsNone(summarise_review([]))

    def test_a_canned_summary_reports_no_recall_at_all(self) -> None:
        canned = {"review": {"mode": "deep", "defect": True, "decision": "approve", "model_calls": 3,
                             "blocking_findings": 0, "quality_measured": False}, "quality_checks": []}
        summary = summarise_review([canned])
        assert summary is not None
        self.assertIsNone(summary["deep"]["critic_caught"], "nothing was measured; zero would say something was")
        self.assertIsNone(summary["deep"]["false_alarms"])


class TestTheCannedRun(unittest.TestCase):
    def run_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            for task in expand_review_modes(review_tasks()):
                results.append(asyncio.run(run_review_task(task, Path(tmp), canned=True)))
        return results

    def test_the_wiring_holds_in_both_modes_and_no_quality_is_claimed(self) -> None:
        results = self.run_all()
        self.assertEqual(len(review_tasks()) * 2, len(results))
        for result in results:
            with self.subTest(task=result["id"]):
                mode = result["review"]["mode"]
                self.assertTrue(result["passed"], [c for c in result["checks"] if c["status"] != "passed"])
                self.assertLessEqual(result["review"]["model_calls"], CALL_CAPS[mode])
                self.assertTrue(result["tokens_are_synthetic"])
                for quality in result["quality_checks"]:
                    self.assertEqual("skipped", quality["status"], "a canned reviewer cannot claim to have found anything")
                lens_checks = [c for c in result["checks"] if c["type"] == "lens_events"]
                self.assertEqual("passed", lens_checks[0]["status"])
        deep = [r for r in results if r["review"]["mode"] == "deep"]
        standard = [r for r in results if r["review"]["mode"] == "standard"]
        self.assertTrue(all(r["review"]["model_calls"] == 3 for r in deep), "two model lenses and the critic")
        self.assertTrue(all(r["review"]["model_calls"] == 1 for r in standard), "the critic alone")
        summary = summarise_review(results)
        assert summary is not None
        self.assertIsNone(summary["deep"]["critic_caught"])


class TestTheCommandLine(unittest.TestCase):
    def test_the_runner_expands_modes_runs_them_and_reports_the_review_summary(self) -> None:
        import json

        from benchmarks.runner import main

        manifest = load_manifest(DEFAULT_MANIFEST)
        manifest["tiers"]["review"]["provider"] = "canned"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            out = Path(tmp) / "report.json"
            code = main(["--tier", "review", "--manifest", str(path), "--report", str(out)])
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(0, code)
        self.assertEqual("canned", report["provider"])
        self.assertEqual(len(review_tasks()) * 2, report["summary"]["tasks"])
        self.assertEqual({"standard", "deep"}, set(report["summary"]["review"]))
        self.assertIsNone(report["summary"]["review"]["deep"]["critic_caught"])
        self.assertTrue(report["summary"]["review"]["deep"]["max_calls_in_a_run"] <= CALL_CAPS["deep"])


if __name__ == "__main__":
    unittest.main()
