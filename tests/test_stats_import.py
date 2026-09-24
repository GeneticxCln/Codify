"""The stats-import validator and store, tested directly.

`engine/stats_import.py` is pure by design — no clock, no DB in the validator —
so the rules are exercised as plain dicts. The store tests use a throwaway
SQLite file. The point of the validator tests is that the engine's notion of a
valid export cannot drift from what the panel accepts on the client: a file
that passes `ui/src/statsHistory.ts` must not be rejected here for a different
reason, and a hand-edited one must not slip through because the client checked
it and the engine trusted that.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import sqlite3
import unittest
from typing import Any

from engine.stats_import import (
    MAX_IMPORT_DAYS,
    StatsImportInvalid,
    StatsImportService,
    clean_source,
    validate_import,
)


def day(name: str, tokens: int = 10) -> dict[str, Any]:
    return {
        "day": name,
        "day_stats": {
            "date": name, "created": 1, "succeeded": 1, "failed": 0,
            "cancelled": 0, "total_tokens": tokens, "calls": 1,
        },
        "goals": {
            "goals": 1, "active": 0, "succeeded": 1, "failed": 0,
            "cancelled": 0, "success_rate": 100,
        },
        "usage": {
            "input_tokens": tokens - 1, "output_tokens": 1,
            "total_tokens": tokens, "calls": 1, "avg_duration_ms": 12,
        },
    }


def document(*days: str) -> dict[str, Any]:
    return {"exported_at": "2026-01-01T00:00:00.000Z", "days": [day(d) for d in days]}


class TestValidateImport(unittest.TestCase):
    def test_accepts_a_well_formed_document(self) -> None:
        out = validate_import(document("2026-01-01", "2026-01-02"))
        self.assertEqual([d["day"] for d in out["days"]], ["2026-01-01", "2026-01-02"])
        self.assertEqual(out["exported_at"], "2026-01-01T00:00:00.000Z")

    def test_rejects_structural_nonsense(self) -> None:
        # `payload` is deliberately not always an object: a non-object upload is
        # one of the cases under test, so it stays `Any`.
        cases: list[tuple[Any, str]] = [
            (None, "not_an_object"),
            ([], "not_an_object"),
            ({"days": [day("2026-01-01")]}, "missing_exported_at"),
            ({"exported_at": "  ", "days": [day("2026-01-01")]}, "missing_exported_at"),
            ({"exported_at": "x"}, "missing_days"),
            ({"exported_at": "x", "days": {}}, "missing_days"),
            ({"exported_at": "x", "days": []}, "empty"),
        ]
        for payload, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(StatsImportInvalid) as ctx:
                    validate_import(payload)
                self.assertEqual(ctx.exception.code, code)

    def test_rejects_duplicate_and_unordered_days(self) -> None:
        with self.assertRaises(StatsImportInvalid) as ctx:
            validate_import(document("2026-01-01", "2026-01-01"))
        self.assertEqual(ctx.exception.code, "duplicate_day")

        with self.assertRaises(StatsImportInvalid) as ctx:
            validate_import(document("2026-01-02", "2026-01-01"))
        self.assertEqual(ctx.exception.code, "out_of_order")
        # The message names both dates, so the user can find the problem.
        self.assertIn("2026-01-01", ctx.exception.message)
        self.assertIn("2026-01-02", ctx.exception.message)

    def test_rejects_a_malformed_day_key(self) -> None:
        """The day is the primary key, so a bad one would sort wrong forever."""
        # The last case is a number, not a string: a non-string key must be
        # rejected by the validator rather than formatted into place.
        bad_keys: list[Any] = ["20260101", "2026-1-1", "01-01-2026", "", "yesterday", 20260101]
        for bad in bad_keys:
            with self.subTest(day=bad):
                with self.assertRaises(StatsImportInvalid) as ctx:
                    validate_import({"exported_at": "x", "days": [day(bad)]})
                self.assertEqual(ctx.exception.code, "bad_day")

    def test_requires_day_stats_to_agree_with_the_day(self) -> None:
        doc = document("2026-01-01")
        doc["days"][0]["day_stats"]["date"] = "2026-01-02"
        with self.assertRaises(StatsImportInvalid) as ctx:
            validate_import(doc)
        self.assertEqual(ctx.exception.code, "bad_day")

    def test_rejects_counts_that_are_not_whole_non_negative_numbers(self) -> None:
        for value in (-1, 1.5, "3", True, None, [1]):
            with self.subTest(value=value):
                doc = document("2026-01-01")
                doc["days"][0]["day_stats"]["created"] = value
                with self.assertRaises(StatsImportInvalid) as ctx:
                    validate_import(doc)
                self.assertEqual(ctx.exception.code, "bad_day")

    def test_allows_the_nullable_measurements(self) -> None:
        """An unmeasured duration and an unmeasured rate are honest, not invalid."""
        doc = document("2026-01-01")
        doc["days"][0]["usage"]["avg_duration_ms"] = None
        doc["days"][0]["goals"]["success_rate"] = None
        out = validate_import(doc)
        self.assertIsNone(out["days"][0]["usage"]["avg_duration_ms"])
        self.assertIsNone(out["days"][0]["goals"]["success_rate"])

    def test_strips_unknown_keys_instead_of_storing_them(self) -> None:
        doc = document("2026-01-01")
        doc["days"][0]["injected"] = "x"
        doc["extra_top_level"] = {"anything": True}
        out = validate_import(doc)
        self.assertEqual(
            sorted(out["days"][0]), ["day", "day_stats", "goals", "usage"]
        )
        self.assertNotIn("extra_top_level", out)

    def test_caps_the_number_of_days(self) -> None:
        too_many = {"exported_at": "x", "days": [day("2026-01-01")] * (MAX_IMPORT_DAYS + 1)}
        with self.assertRaises(StatsImportInvalid) as ctx:
            validate_import(too_many)
        self.assertEqual(ctx.exception.code, "too_many_days")


class TestCleanSource(unittest.TestCase):
    def test_keeps_a_plain_name(self) -> None:
        self.assertEqual(clean_source("codify-stats-history.json"), "codify-stats-history.json")

    def test_strips_paths_control_characters_and_bounds_length(self) -> None:
        for raw, absent in [
            ("../../etc/passwd", "/"),
            ("a\\b", "\\"),
            ("name\ninjected", "\n"),
            ("null\x00byte", "\x00"),
        ]:
            with self.subTest(raw=raw):
                out = clean_source(raw)
                self.assertNotIn(absent, out)
        self.assertEqual(len(clean_source("x" * 5000)), 200)

    def test_non_strings_become_empty(self) -> None:
        self.assertEqual(clean_source(None), "")
        self.assertEqual(clean_source(42), "")


class _FailingConn(sqlite3.Connection):
    """A connection whose executemany can be made to fail on demand.

    sqlite3.Connection attributes are read-only, so the failure has to be
    injected through a subclass rather than by assignment.
    """

    fail_insert = False

    def executemany(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail_insert:
            raise sqlite3.Error("disk full")
        return super().executemany(*args, **kwargs)


class TestStatsImportService(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", factory=_FailingConn)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """CREATE TABLE stats_imports (
                 day TEXT PRIMARY KEY,
                 document TEXT NOT NULL,
                 source TEXT NOT NULL DEFAULT '',
                 imported_at REAL NOT NULL
               );"""
        )
        self.service = StatsImportService(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_get_is_none_before_anything_is_imported(self) -> None:
        self.assertIsNone(self.service.get())
        self.assertEqual(self.service.count(), 0)

    def test_replace_then_read_back_in_chronological_order(self) -> None:
        self.service.replace(document("2026-01-01", "2026-01-02"), "f.json")
        stored = self.service.get()
        assert stored is not None, "the replaced document is readable back"
        self.assertEqual(
            [d["day"] for d in stored["days"]], ["2026-01-01", "2026-01-02"],
            "the reader and the validator agree on one order",
        )
        self.assertEqual(stored["source"], "f.json")

    def test_out_of_order_input_is_refused_rather_than_silently_sorted(self) -> None:
        """A good error must not be defeated by a reader that re-sorts quietly."""
        with self.assertRaises(StatsImportInvalid) as ctx:
            self.service.replace(document("2026-01-02", "2026-01-01"))
        self.assertEqual(ctx.exception.code, "out_of_order")

    def test_replace_replaces_the_previous_document(self) -> None:
        self.service.replace(document("2026-01-01", "2026-01-02"))
        self.service.replace(document("2026-02-01"))
        stored = self.service.get()
        assert stored is not None, "the replacement is readable back"
        self.assertEqual([d["day"] for d in stored["days"]], ["2026-02-01"])
        self.assertEqual(self.service.count(), 1)

    def test_a_rejected_document_changes_nothing(self) -> None:
        self.service.replace(document("2026-01-01"))
        with self.assertRaises(StatsImportInvalid):
            self.service.replace({"exported_at": "x", "days": []})
        self.assertEqual(self.service.count(), 1, "a bad upload must not clear the good one")

    def test_a_rollback_leaves_the_previous_import_intact(self) -> None:
        self.service.replace(document("2026-01-01"))
        # Force the insert half to fail after the DELETE has already run.
        self.conn.fail_insert = True
        try:
            with self.assertRaises(StatsImportInvalid) as ctx:
                self.service.replace(document("2026-02-01"))
            self.assertEqual(ctx.exception.code, "storage_failed")
        finally:
            self.conn.fail_insert = False
        stored = self.service.get()
        assert stored is not None, "a failed write must not leave a half-replaced document"
        self.assertEqual([d["day"] for d in stored["days"]], ["2026-01-01"])

    def test_clear_reports_how_many_days_it_removed(self) -> None:
        self.service.replace(document("2026-01-01", "2026-01-02"))
        self.assertEqual(self.service.clear(), 2)
        self.assertEqual(self.service.clear(), 0)
        self.assertIsNone(self.service.get())

    def test_an_unparseable_row_is_skipped_not_fatal(self) -> None:
        self.service.replace(document("2026-01-01", "2026-01-02"))
        self.conn.execute(
            "UPDATE stats_imports SET document = ? WHERE day = ?",
            ("{not json", "2026-01-01"),
        )
        self.conn.commit()
        stored = self.service.get()
        assert stored is not None, "the surviving day is still readable"
        self.assertEqual(
            [d["day"] for d in stored["days"]], ["2026-01-02"],
            "one corrupt day costs that day, not the whole import",
        )

    def test_exported_at_is_kept_alongside_each_stored_day(self) -> None:
        self.service.replace(document("2026-01-01"), "f.json")
        stored = self.service.get()
        assert stored is not None, "the stored document is readable"
        self.assertEqual(stored["exported_at"], "2026-01-01T00:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
