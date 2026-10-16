"""A scan report reaches the model whole, and what it did not look at comes before what it found.

The conductor cuts any tool result over `MAX_TOOL_RESULT_CHARS` to its first two thirds and drops the rest. `scan_code`
used to write its `Coverage:` line, the "stopped at its limit" line and the next step *last*, so a report with hits in
about twenty rules (14,000 characters, measured) lost exactly the sentences that say what was not scanned, and a clean
looking result could not be checked against them (`docs/14` §3). The report is now built to fit, and says what it held
back. These tests format a worst-case result with the real profiles and send it through the real `_clip`.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import re
import unittest
from typing import Any

from engine.conductor import MAX_TOOL_RESULT_CHARS, _clip
from engine.scan import MAX_FINDINGS, REPORT_CHARS, Profile, ProfileSet, format_scan, load_profiles

NAMES = ("secrets", "injection", "crypto", "deserialization", "unsafe-c", "web")


class TestTheReportFitsTheConductorsCap(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles: ProfileSet = load_profiles(None)
        found = [self.profiles.get(n) for n in NAMES]
        self.assertNotIn(None, found, "a built-in profile is missing")
        self.chosen: list[Profile] = [p for p in found if p is not None]
        self.rules = [r for p in self.chosen for r in p.rules]

    def result(self, rules: int, per: int, width: int, **extra: Any) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for rule in self.rules[:rules]:
            counts[rule.id] = per
            findings += [
                {"rule": rule.id, "path": f"src/some/deeper/module/file_{i}.py", "line": 120 + i, "text": "x" * width}
                for i in range(per)
            ]
        base: dict[str, Any] = {
            "findings": findings[:MAX_FINDINGS], "counts": counts, "files_scanned": 900,
            "skipped": {"tests": 40, "binary": 12}, "partial_files": 2, "stopped": "files",
        }
        return {**base, **extra}

    def report(self, *shape: int) -> str:
        return format_scan(self.result(*shape), self.chosen, self.profiles)

    def test_the_budget_is_under_the_conductors_cap(self) -> None:
        self.assertLess(REPORT_CHARS, MAX_TOOL_RESULT_CHARS)

    def test_every_realistic_and_worst_case_shape_survives_the_clip_untouched(self) -> None:
        for shape in ((3, 8, 100), (8, 8, 100), (20, 3, 100), (40, 2, 200), (56, 1, 200)):
            with self.subTest(shape=shape):
                text = self.report(*shape)
                self.assertLessEqual(len(text), MAX_TOOL_RESULT_CHARS)
                self.assertEqual(text, _clip(text), "the conductor would cut this report")

    def test_the_coverage_stop_and_next_lines_survive_when_the_findings_are_many(self) -> None:
        text = _clip(self.report(56, 1, 200))
        self.assertIn("Coverage: 900 files scanned", text)
        self.assertIn("The scan stopped at its limit of", text)
        self.assertIn("Next: read each hit with `read_file`", text)

    def test_what_was_not_seen_comes_before_what_was_found(self) -> None:
        text = self.report(8, 8, 100)
        self.assertLess(text.index("Coverage:"), text.index("\n["), "coverage should precede the first finding")
        self.assertLess(text.index("The scan stopped at its limit"), text.index("\n["))

    def test_groups_that_did_not_fit_are_named_and_counted_not_silently_dropped(self) -> None:
        text = self.report(56, 1, 200)
        found = re.search(r"\((\d+) more rule groups? matched and (?:is|are) not shown, to keep this result whole: ", text)
        self.assertIsNotNone(found, "the report held groups back without saying so")
        assert found is not None
        shown = text.count("\n[")
        self.assertEqual(len(self.rules[:56]), shown + int(found.group(1)), "every rule with a hit is either shown or counted")

    def test_a_small_report_has_no_held_back_note_and_keeps_every_group(self) -> None:
        text = self.report(3, 2, 60)
        self.assertNotIn("not shown, to keep this result whole", text)
        self.assertEqual(3, text.count("\n["))

    def test_the_other_notes_still_come_through(self) -> None:
        text = format_scan(self.result(2, 2, 60, omitted=7, stopped="time"), self.chosen, self.profiles)
        self.assertIn("7 lower-ranked hits are not shown", text)
        self.assertIn("The scan stopped at its time budget", text)


if __name__ == "__main__":
    unittest.main()
