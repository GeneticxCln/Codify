"""`engine/review.py`: the goldfish packet, the finding parser, the deterministic merge, the report.

Pure module, so these tests need no provider, no database and no workspace. The properties worth pinning are the
ones a refactor would quietly break: the packet carries nothing the author knew (that is what makes the reader a
goldfish), a malformed finding is refused rather than repaired, two lenses that found the same thing are one
finding, and a lens that did not run is never reported as a lens that found nothing.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import unittest
from typing import Any

from engine import review
from engine.review import (
    Finding,
    LensRun,
    ReviewPacket,
    ReviewReplyInvalid,
    build_packet,
    parse_findings,
    readiness_findings,
    render_for_critic,
    render_report,
    synthesize,
)

DIFFS: list[dict[str, Any]] = [
    {"path": "src/app.py", "action": "modify", "unified_diff": "@@ -1 +1 @@\n-def greet():\n+def welcome():\n"},
    {"path": "README.md", "action": "modify", "unified_diff": None},
]


def _finding(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "severity": "major",
        "category": "call-site",
        "scope": "src/app.py:3",
        "evidence": "greet() is still called in tests/test_app.py",
        "recommendation": "update the call site",
    }
    base.update(over)
    return base


class TestThePacket(unittest.TestCase):
    def _packet(self) -> ReviewPacket:
        return build_packet(
            request="Rename greet to welcome",
            step_title="Rename the function",
            step_description="Change src/app.py",
            acceptance=["tests pass", "no stale name"],
            diffs=DIFFS,
        )

    def test_it_carries_the_request_the_step_the_acceptance_the_diff_and_the_paths(self) -> None:
        text = self._packet().text()
        for needle in (
            "Rename greet to welcome", "Rename the function", "Change src/app.py",
            "tests pass", "no stale name", "+def welcome():", "src/app.py", "README.md",
        ):
            self.assertIn(needle, text)

    def test_the_packet_has_no_slot_for_what_the_author_knew(self) -> None:
        # The builder's parameters are the whole surface: evidence, summaries and plan rationale are not among them,
        # so no caller can hand one over by mistake. A new parameter is a decision this test makes loud.
        import inspect

        params = set(inspect.signature(build_packet).parameters)
        self.assertEqual(
            {"request", "step_title", "step_description", "acceptance", "diffs", "max_diff_chars"}, params,
        )

    def test_a_string_the_author_knew_does_not_appear_unless_it_was_handed_over(self) -> None:
        evidence = "LIBRARIAN-EVIDENCE-7731"
        self.assertNotIn(evidence, self._packet().text())

    def test_a_long_diff_is_cut_and_says_so(self) -> None:
        big = [{"path": "a.py", "action": "modify", "unified_diff": "+x\n" * 5000}]
        packet = build_packet(
            request="r", step_title="t", step_description="d", acceptance=[], diffs=big, max_diff_chars=200,
        )
        self.assertLess(len(packet.diff), 400)
        self.assertIn("cut", packet.diff)

    def test_no_diff_is_said_not_silent(self) -> None:
        packet = build_packet(request="r", step_title="t", step_description="d", acceptance=[], diffs=[])
        self.assertIn("No changes", packet.text())


class TestParsingFindings(unittest.TestCase):
    def test_a_good_reply_parses_with_its_lens(self) -> None:
        found = parse_findings({"findings": [_finding()]}, "zoom-out")
        self.assertEqual(1, len(found))
        self.assertEqual("zoom-out", found[0].lens)
        self.assertEqual("major", found[0].severity)

    def test_no_findings_is_a_valid_answer_only_when_it_is_stated(self) -> None:
        self.assertEqual([], parse_findings({"findings": []}, "comprehension"))
        replies: list[object] = [{}, {"findings": None}, {"findings": "none"}, [], "all good", None]
        for reply in replies:
            with self.subTest(reply=reply), self.assertRaises(ReviewReplyInvalid):
                parse_findings(reply, "comprehension")

    def test_a_malformed_finding_refuses_the_whole_reply(self) -> None:
        bad = (
            _finding(severity="critical"),
            _finding(severity=None),
            _finding(scope=""),
            _finding(scope="   "),
            _finding(recommendation=""),
            _finding(category=""),
            _finding(evidence=7),
            "not an object",
        )
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ReviewReplyInvalid):
                parse_findings({"findings": [_finding(), item]}, "zoom-out")

    def test_text_is_bounded(self) -> None:
        found = parse_findings({"findings": [_finding(evidence="e" * 5000)]}, "zoom-out")
        self.assertLessEqual(len(found[0].evidence), review.MAX_TEXT_CHARS)

    def test_a_flood_of_findings_is_capped(self) -> None:
        flood = [_finding(scope=f"f{i}.py:1") for i in range(review.MAX_FINDINGS_PER_LENS + 9)]
        self.assertEqual(review.MAX_FINDINGS_PER_LENS, len(parse_findings({"findings": flood}, "zoom-out")))

    def test_an_unknown_lens_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse_findings({"findings": []}, "vibes")


class TestSynthesis(unittest.TestCase):
    def _f(self, lens: str, **over: object) -> Finding:
        return parse_findings({"findings": [_finding(**over)]}, lens)[0]

    def test_the_same_scope_and_category_collapses_with_the_highest_severity_and_both_lenses(self) -> None:
        merged = synthesize([
            self._f("comprehension", severity="nit"),
            self._f("zoom-out", severity="blocker", category="Call-Site", scope="SRC/app.py:3"),
        ])
        self.assertEqual(1, len(merged))
        self.assertEqual("blocker", merged[0].severity)
        self.assertEqual(("zoom-out", "comprehension"), merged[0].lenses, "lens order is the lens table's order")

    def test_different_scopes_or_categories_stay_apart_and_sort_by_severity(self) -> None:
        merged = synthesize([
            self._f("zoom-out", severity="nit", scope="a.py:1"),
            self._f("zoom-out", severity="blocker", scope="b.py:1"),
            self._f("zoom-out", severity="major", scope="b.py:1", category="other"),
        ])
        self.assertEqual(["blocker", "major", "nit"], [m.severity for m in merged])

    def test_the_merge_is_deterministic_in_any_input_order(self) -> None:
        items = [
            self._f("zoom-out", severity="major"),
            self._f("comprehension", severity="major", evidence="different words"),
            self._f("readiness", severity="nit", scope="x.py:1"),
        ]
        self.assertEqual(synthesize(items), synthesize(list(reversed(items))))

    def test_nothing_in_nothing_out(self) -> None:
        self.assertEqual([], synthesize([]))


class TestReadiness(unittest.TestCase):
    def test_a_cited_path_that_does_not_exist_is_a_finding(self) -> None:
        found = readiness_findings(
            suggested_paths=["src/app.py", "src/gone.py"], changed=["src/app.py"],
            exists=lambda p: p == "src/app.py",
        )
        self.assertEqual(["src/gone.py"], [f.scope for f in found if f.category == "missing-path"])
        self.assertEqual("readiness", found[0].lens)
        self.assertEqual("major", found[0].severity)

    def test_a_path_the_step_wrote_counts_as_existing(self) -> None:
        self.assertEqual([], readiness_findings(["notes.md"], ["notes.md"], exists=lambda p: False))

    def test_source_changed_with_no_test_changed_is_a_nit_not_a_blocker(self) -> None:
        found = readiness_findings([], ["engine/x.py"], exists=lambda p: True)
        self.assertEqual(["nit"], [f.severity for f in found])
        self.assertEqual([], readiness_findings([], ["engine/x.py", "tests/test_x.py"], exists=lambda p: True))
        self.assertEqual([], readiness_findings([], ["README.md"], exists=lambda p: True))


class TestTheReport(unittest.TestCase):
    def _runs(self, *statuses: str) -> tuple[LensRun, ...]:
        return tuple(
            LensRun(lens=lens, status=status, findings=0, calls=1 if lens != "readiness" else 0, duration_ms=5)
            for lens, status in zip(review.LENSES, statuses, strict=False)
        )

    def test_a_lens_that_did_not_run_is_named_and_never_counted_as_clean(self) -> None:
        report = review.ReviewReport(runs=self._runs("ran", "not_run", "ran"), findings=())
        self.assertEqual("2 of 3 lenses ran — comprehension not run", report.status_line())
        text = render_report(report)
        self.assertIn("comprehension not run", text)
        self.assertIn("No findings from the lenses that ran.", text, "silence from the lenses that ran, said as that")

    def test_all_ran_is_said_plainly(self) -> None:
        report = review.ReviewReport(runs=self._runs("ran", "ran", "ran"), findings=())
        self.assertEqual("3 of 3 lenses ran", report.status_line())

    def test_the_payload_holds_text_the_ui_can_draw(self) -> None:
        finding = synthesize([parse_findings({"findings": [_finding()]}, "zoom-out")[0]])
        payload = review.ReviewReport(runs=self._runs("ran", "ran", "ran"), findings=tuple(finding)).payload()
        self.assertEqual("3 of 3 lenses ran", payload["status"])
        self.assertEqual("major", payload["findings"][0]["severity"])
        self.assertEqual(["zoom-out"], payload["findings"][0]["lenses"])
        self.assertEqual(3, len(payload["lenses"]))

    def test_the_critic_is_told_these_are_evidence_not_a_verdict(self) -> None:
        finding = synthesize([parse_findings({"findings": [_finding()]}, "zoom-out")[0]])
        text = render_for_critic(review.ReviewReport(runs=self._runs("ran", "ran", "ran"), findings=tuple(finding)))
        self.assertIn("not a verdict", text)
        self.assertIn("src/app.py:3", text)
        self.assertIn("you decide", text.lower())


if __name__ == "__main__":
    unittest.main()
