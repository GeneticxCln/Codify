"""The cache section of `audit_usage` (docs/21 P3): reads against writes, and why a cache missed.

Hand-built timelines, because the attribution is arithmetic over fingerprints: a miss is a call that read nothing right
after a call that used the cache, and its reason is the first thing that differs between the two.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import unittest
from typing import Any

from engine.cache_diagnostics import prompt_parts
from engine.usage_audit import REPORT_CHARS, audit, format_audit

SYSTEM = "conductor system prompt"


class Timeline:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._seq = 0

    def add(self, kind: str, payload: dict[str, Any], at: float | None = None) -> None:
        self._seq += 1
        self.events.append({
            "type": kind, "payload": payload, "goal_id": "g1", "step_id": None, "sequence": self._seq,
            "timestamp": 1_000.0 + self._seq if at is None else at,
        })

    def call(
        self, read: int = 0, write: int = 0, system: str = SYSTEM, tools: tuple[str, ...] = ("a", "b"),
        model: str = "m1", at: float | None = None, fingerprint: bool = True, role: str = "conductor",
    ) -> None:
        payload: dict[str, Any] = {
            "role": role, "provider": "p", "model": model, "input_tokens": 1000, "output_tokens": 50,
            "cache_read_tokens": read, "cache_write_tokens": write,
        }
        if fingerprint:
            payload["prompt_parts"] = prompt_parts(system, tools, model, "p")
        self.add("usage", payload, at)


class TestCacheReport(unittest.TestCase):
    def cache(self, t: Timeline) -> dict[str, Any]:
        result: dict[str, Any] = audit(t.events)["cache"]
        return result

    def test_reads_writes_and_share_of_input(self) -> None:
        t = Timeline()
        t.call(write=800)
        t.call(read=800)
        report = self.cache(t)
        self.assertEqual((800, 800, 2), (report["read_tokens"], report["write_tokens"], report["calls"]))
        self.assertEqual(0.4, report["read_share_of_input"])
        self.assertEqual(0, report["misses"])

    def test_a_miss_after_a_change_is_attributed_to_that_change(self) -> None:
        for change, kwargs in (
            ("system_changed", {"system": SYSTEM + " (edited)"}),
            ("tools_changed", {"tools": ("a",)}),
            ("model_switch", {"model": "m2"}),
        ):
            with self.subTest(change=change):
                t = Timeline()
                t.call(write=800)
                t.call(**kwargs)
                self.assertEqual([(change, 1)], self.cache(t)["miss_reasons"])

    def test_a_miss_after_a_long_gap_is_idle_ttl_and_one_with_nothing_changed_is_none(self) -> None:
        t = Timeline()
        t.call(write=800, at=1000.0)
        t.call(at=1000.0 + 301)
        t.call(write=800, at=1400.0)
        t.call(at=1401.0)
        self.assertEqual([("idle_ttl", 1), ("none", 1)], self.cache(t)["miss_reasons"])

    def test_a_compaction_between_two_calls_is_named(self) -> None:
        t = Timeline()
        t.call(write=800)
        t.add("session_compacted", {"summary": "s", "turns": 3, "automatic": True, "covers_through": 3})
        t.call()
        self.assertEqual([("compaction", 1)], self.cache(t)["miss_reasons"])

    def test_a_call_after_one_that_never_used_the_cache_is_not_a_miss(self) -> None:
        t = Timeline()
        t.call()
        t.call()
        self.assertEqual(0, self.cache(t)["misses"])

    def test_roles_are_followed_separately(self) -> None:
        t = Timeline()
        t.call(write=800, role="conductor")
        t.call(role="critic", system="a different role's prompt")
        t.call(read=800, role="conductor")
        self.assertEqual(0, self.cache(t)["misses"], "the critic's call is not the conductor's next call")

    def test_calls_without_a_fingerprint_are_counted_but_never_explained(self) -> None:
        t = Timeline()
        t.call(write=800, fingerprint=False)
        t.call(fingerprint=False)
        report = self.cache(t)
        self.assertEqual((2, 0, 0), (report["calls"], report["calls_with_fingerprint"], report["misses"]))
        self.assertIn("0 of 2 calls carry a prompt fingerprint", format_audit(audit(t.events)))

    def test_the_text_says_tokens_and_the_reasons_and_stays_within_the_report_cap(self) -> None:
        t = Timeline()
        t.call(write=800)
        t.call(system=SYSTEM + "!")
        text = format_audit(audit(t.events))
        self.assertIn("system_changed 1", text)
        self.assertIn("not a saving, not a price", text)
        self.assertNotIn("$", text)
        self.assertLessEqual(len(text), REPORT_CHARS)

    def test_no_cache_activity_prints_no_cache_section(self) -> None:
        t = Timeline()
        t.call()
        self.assertNotIn("Cache (the provider's own token counts", format_audit(audit(t.events)))


if __name__ == "__main__":
    unittest.main()
