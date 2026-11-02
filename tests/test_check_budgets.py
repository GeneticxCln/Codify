"""`scripts/check_budgets.py`: the size budget fails on growth, passes a normal build, and says when it cannot tell.

Synthetic builds in a temp directory, so no network, no display and no real `ui/dist` is needed; the one test that reads
the committed `ui/budgets.json` only checks that it parses, so the gate cannot go red because a build is missing.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts import check_budgets as cb

INDEX = '<html><head><script type="module" crossorigin src="/assets/index-ABC.js"></script></head></html>'


def build(root: Path, entry_bytes: int, other_bytes: int) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX, encoding="utf-8")
    (dist / "assets" / "index-ABC.js").write_bytes(b"x" * entry_bytes)
    (dist / "assets" / "editor-DEF.js").write_bytes(b"y" * other_bytes)
    return dist


def budgets_file(root: Path, total: int, entry: int, tolerance: object = 10, **override: object) -> Path:
    entry_doc = {"baseline": entry, "reason": "r", "measured_at": "t"}
    total_doc = {"baseline": total, "reason": "r", "measured_at": "t"}
    doc = {"tolerance_pct": tolerance, "budgets": {"dist_total_bytes": total_doc, "entry_chunk_bytes": entry_doc}}
    doc.update(override)
    path = root / "budgets.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class TestMeasure(unittest.TestCase):
    def test_it_measures_the_total_and_the_entry_chunk_named_by_index_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = build(Path(tmp), 1000, 5000)
            sizes = cb.measure(dist)
        self.assertEqual(1000, sizes["entry_chunk_bytes"])
        self.assertEqual(len(INDEX.encode()) + 1000 + 5000, sizes["dist_total_bytes"])

    def test_a_missing_build_is_a_cannot_tell_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(cb.BudgetError, "make build-ui"):
            cb.measure(Path(tmp) / "dist")

    def test_an_index_with_no_module_script_or_a_script_that_escapes_the_build_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = build(Path(tmp), 10, 10)
            (dist / "index.html").write_text("<html></html>", encoding="utf-8")
            with self.assertRaisesRegex(cb.BudgetError, "no module script"):
                cb.measure(dist)
            (dist / "index.html").write_text(
                '<script type="module" src="/../../etc/hostname"></script>', encoding="utf-8")
            with self.assertRaisesRegex(cb.BudgetError, "not a file inside"):
                cb.measure(dist)


class TestCompare(unittest.TestCase):
    BASE = {"dist_total_bytes": 2_000_000, "entry_chunk_bytes": 450_000}

    def test_a_normal_build_passes(self) -> None:
        failures, notes = cb.compare({"dist_total_bytes": 2_050_000, "entry_chunk_bytes": 460_000}, self.BASE, 0.10)
        self.assertEqual(([], []), (failures, notes))

    def test_growth_of_fifteen_percent_fails_and_names_the_metric(self) -> None:
        grown = {"dist_total_bytes": 2_300_000, "entry_chunk_bytes": 517_500}
        failures, _ = cb.compare(grown, self.BASE, 0.10)
        self.assertEqual(2, len(failures))
        self.assertIn("entry_chunk_bytes", failures[1])
        self.assertIn("+15.0%", failures[1])

    def test_exactly_at_the_tolerance_passes_and_one_byte_over_fails(self) -> None:
        at = {"dist_total_bytes": 2_200_000, "entry_chunk_bytes": 495_000}
        self.assertEqual([], cb.compare(at, self.BASE, 0.10)[0])
        over = {"dist_total_bytes": 2_200_001, "entry_chunk_bytes": 495_000}
        self.assertEqual(1, len(cb.compare(over, self.BASE, 0.10)[0]))

    def test_a_much_smaller_build_passes_with_a_note_to_lower_the_baseline(self) -> None:
        failures, notes = cb.compare({"dist_total_bytes": 1_000_000, "entry_chunk_bytes": 450_000}, self.BASE, 0.10)
        self.assertEqual([], failures)
        self.assertEqual(1, len(notes))
        self.assertIn("lowering", notes[0])


class TestLoadBudgets(unittest.TestCase):
    def load(self, **kwargs: object) -> tuple[float, dict[str, int]]:
        with tempfile.TemporaryDirectory() as tmp:
            return cb.load_budgets(budgets_file(Path(tmp), 2_000_000, 450_000, **kwargs))

    def test_a_good_file_loads(self) -> None:
        tolerance, baselines = self.load()
        self.assertEqual(0.10, tolerance)
        self.assertEqual({"dist_total_bytes": 2_000_000, "entry_chunk_bytes": 450_000}, baselines)

    def test_a_baseline_must_say_why(self) -> None:
        for field in ("reason", "measured_at"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                path = budgets_file(Path(tmp), 1, 1)
                doc = json.loads(path.read_text())
                doc["budgets"]["entry_chunk_bytes"][field] = " "
                path.write_text(json.dumps(doc))
                with self.assertRaisesRegex(cb.BudgetError, field):
                    cb.load_budgets(path)

    def test_bad_values_are_refused(self) -> None:
        for tolerance in (True, "10", -1, 101, None):
            with self.subTest(tolerance=tolerance), self.assertRaises(cb.BudgetError):
                self.load(tolerance=tolerance)
        with tempfile.TemporaryDirectory() as tmp:
            path = budgets_file(Path(tmp), 0, 450_000)
            with self.assertRaisesRegex(cb.BudgetError, "positive"):
                cb.load_budgets(path)

    def test_a_budget_nothing_checks_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = budgets_file(Path(tmp), 1, 1)
            doc = json.loads(path.read_text())
            doc["budgets"]["startup_ms"] = {"baseline": 1, "reason": "r", "measured_at": "t"}
            path.write_text(json.dumps(doc))
            with self.assertRaisesRegex(cb.BudgetError, "startup_ms"):
                cb.load_budgets(path)

    def test_the_committed_baselines_parse(self) -> None:
        tolerance, baselines = cb.load_budgets(cb.DEFAULT_BUDGETS)
        self.assertGreater(tolerance, 0)
        self.assertEqual(set(cb.METRICS), set(baselines))


class TestMain(unittest.TestCase):
    def run_main(self, dist: Path, budgets: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cb.main(["--dist", str(dist), "--budgets", str(budgets)])
        return code, out.getvalue(), err.getvalue()

    def test_within_budget_exits_zero_and_over_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = build(root, 1000, 5000)
            total = len(INDEX.encode()) + 6000
            self.assertEqual(0, self.run_main(dist, budgets_file(root, total, 1000))[0])
            code, _, err = self.run_main(dist, budgets_file(root, total, 800))
            self.assertEqual(1, code)
            self.assertIn("entry_chunk_bytes", err)

    def test_simulated_fifteen_percent_growth_fails_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_total = len(INDEX.encode()) + 6000
            budgets = budgets_file(root, baseline_total, 1000)
            grown = build(root, 1150, 5000 + int(baseline_total * 0.15))
            self.assertEqual(1, self.run_main(grown, budgets)[0])

    def test_cannot_tell_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(2, self.run_main(root / "nope", budgets_file(root, 1, 1))[0])
            self.assertEqual(2, self.run_main(build(root, 1, 1), root / "missing.json")[0])


if __name__ == "__main__":
    unittest.main()
