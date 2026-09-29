"""A provider's timestamp is read on every supported Python, not only the newest (review of 2026-09-29).

`_iso_day` used `datetime.fromisoformat`, which on Python 3.10 (the declared floor, and what the desktop shell
boots) accepts only what `isoformat()` itself produces: fractional seconds of exactly three or six digits,
and an offset written `+HH:MM`. Ollama's `modified_at` is Go's RFC 3339 with nanoseconds —
`2025-01-02T03:04:05.123456789-07:00` — so on 3.10 every model's date failed to parse, and ordering "newest
first" quietly became alphabetical. The gate did not see it because nothing tested those timestamps on 3.10.

The expectations are epoch seconds worked out independently with `calendar.timegm`, so the parser is not
being checked against itself.
"""

from __future__ import annotations

import calendar
import unittest
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.model_catalog import _iso_day

BASE = calendar.timegm((2025, 1, 2, 3, 4, 5, 0, 0, 0))  # 2025-01-02T03:04:05Z


class TestProviderTimestamps(unittest.TestCase):
    def test_go_rfc3339_nanoseconds_with_an_offset(self) -> None:
        # Ollama's own spelling. -07:00 means the instant is seven hours *later* in UTC.
        got = _iso_day("2025-01-02T03:04:05.123456789-07:00")

        self.assertIsNotNone(got)
        assert got is not None
        self.assertAlmostEqual(BASE + 7 * 3600 + 0.123456, got, places=3)

    def test_every_fraction_length(self) -> None:
        for text, fraction in (
            ("2025-01-02T03:04:05.1Z", 0.1), ("2025-01-02T03:04:05.12Z", 0.12),
            ("2025-01-02T03:04:05.1234Z", 0.1234), ("2025-01-02T03:04:05.12345Z", 0.12345),
            ("2025-01-02T03:04:05.123456Z", 0.123456), ("2025-01-02T03:04:05.1234567Z", 0.123456),
            ("2025-01-02T03:04:05.123456789Z", 0.123456), ("2025-01-02T03:04:05Z", 0.0),
        ):
            with self.subTest(text=text):
                got = _iso_day(text)
                assert got is not None
                self.assertAlmostEqual(BASE + fraction, got, places=3)

    def test_an_offset_without_a_colon(self) -> None:
        got = _iso_day("2025-01-02T03:04:05+0530")

        assert got is not None
        self.assertEqual(BASE - (5 * 3600 + 30 * 60), got)

    def test_a_zulu_suffix_and_an_explicit_zero_offset_agree(self) -> None:
        self.assertEqual(_iso_day("2025-01-02T03:04:05Z"), _iso_day("2025-01-02T03:04:05+00:00"))

    def test_a_lowercase_z_and_a_space_separator(self) -> None:
        got = _iso_day("2025-01-02 03:04:05z")

        self.assertEqual(BASE, got)

    def test_numbers_are_epoch_seconds_as_before(self) -> None:
        self.assertEqual(1700000000.0, _iso_day(1700000000))
        self.assertEqual(1.5, _iso_day(1.5))

    def test_a_date_alone_is_midnight(self) -> None:
        got = _iso_day("2025-01-02")

        assert got is not None
        # A date with no offset is read as local time, as `fromisoformat(...).timestamp()` always did.
        from datetime import datetime

        self.assertEqual(datetime(2025, 1, 2).timestamp(), got)

    def test_nonsense_is_none_never_an_exception(self) -> None:
        cases: list[Any] = ["", "yesterday", "2025-13-45T99:99:99Z", "2025-01-02T03:04:05.Z", None, [], {}, True]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(_iso_day(text))


if __name__ == "__main__":
    unittest.main()
