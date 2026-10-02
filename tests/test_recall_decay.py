"""Old lessons fade: what a workspace's memory says about a failure it has not seen in months.

The brief puts a handful of observations in front of the conductor on every turn ("this failure has happened
here, with no retry recorded past it"). Nothing in an observation ever aged: a one-off failure from a year ago,
long since fixed, carried the same weight and the same words as one that happened yesterday, and it sat in the
brief until five newer ones pushed it out. A stale lesson is worse than none, because the model is told it is
what this workspace's history has *taught*.

So the brief reads age. Strength is the proof count halved every thirty days since the failure was last seen, an
observation under half a point of strength is left out of the brief, and the ones that stay say how long ago.
All of it is at read time, from rows and events that already exist: no column, no migration, and `recall` (the
tool over the raw events) still finds the old failure when someone asks for it.

"Last seen" is the age of the newest event behind the observation when the scan can see one, and the row's own
`refined_at` only when it cannot. The consolidation pass runs after every run and refines every subject still in
its scan window, so `refined_at` of a quiet workspace's year-old failure is *yesterday*, and trusting it would make
the decay a no-op exactly where it is needed.
"""

from __future__ import annotations

import json
import time
import unittest
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.recall import (
    BRIEF_FLOOR,
    MAX_BRIEF_OBSERVATIONS,
    OBSERVATION_HALF_LIFE_DAYS,
    build_brief,
    observation_age_days,
    observation_strength,
    seen_ago,
)

DAY = 86_400.0
NOW = 1_800_000_000.0


def stored(subject: str, proof: int, days_ago: float | None, lesson: str = "This failure has happened here, with no retry recorded past it.") -> dict[str, Any]:
    return {
        "subject": subject, "lesson": lesson, "example": "", "proof": proof, "evidence": [],
        "refined_at": 0.0 if days_ago is None else NOW - days_ago * DAY,
    }


def failure(code: str, days_ago: float, n: int = 1) -> dict[str, Any]:
    return {
        "goal_id": f"g{n}", "step_id": "", "type": "error", "sequence": n, "goal_title": "A goal",
        "payload": json.dumps({"code": code, "message": "boom"}),
        "timestamp": NOW - days_ago * DAY,
    }


def brief(observations: list[dict[str, Any]] | None = None, events: list[dict[str, Any]] | None = None, **kw: Any) -> str:
    return build_brief([], events or [], observation_rows=observations or [], now=NOW, **kw)


class TestTheArithmetic(unittest.TestCase):
    def test_the_half_life_is_thirty_days_and_the_floor_is_half_a_point(self) -> None:
        self.assertEqual(30.0, OBSERVATION_HALF_LIFE_DAYS)
        self.assertEqual(0.5, BRIEF_FLOOR)

    def test_strength_is_the_proof_halved_every_half_life(self) -> None:
        self.assertAlmostEqual(8.0, observation_strength(8, 0.0))
        self.assertAlmostEqual(4.0, observation_strength(8, 30.0))
        self.assertAlmostEqual(2.0, observation_strength(8, 60.0))
        self.assertAlmostEqual(1.0, observation_strength(8, 90.0))

    def test_an_age_that_is_not_known_costs_nothing(self) -> None:
        self.assertEqual(8.0, observation_strength(8, None))

    def test_the_age_is_in_days_from_the_clock_it_is_given(self) -> None:
        self.assertAlmostEqual(12.0, observation_age_days(NOW - 12 * DAY, NOW) or 0.0)

    def test_a_time_in_the_future_is_age_zero_not_a_boost(self) -> None:
        self.assertEqual(0.0, observation_age_days(NOW + 5 * DAY, NOW))
        self.assertEqual(8.0, observation_strength(8, observation_age_days(NOW + 5 * DAY, NOW)))

    def test_a_time_that_was_never_recorded_is_an_unknown_age(self) -> None:
        for never in (0.0, -1.0, None):
            self.assertIsNone(observation_age_days(never, NOW))


class TestHowItIsSaid(unittest.TestCase):
    def test_today_one_day_and_many(self) -> None:
        self.assertEqual("last seen today", seen_ago(0.0))
        self.assertEqual("last seen today", seen_ago(0.9))
        self.assertEqual("last seen 1 day ago", seen_ago(1.0))
        self.assertEqual("last seen 1 day ago", seen_ago(1.9))
        self.assertEqual("last seen 12 days ago", seen_ago(12.4))
        self.assertEqual("last seen 200 days ago", seen_ago(200.0))


class TestTheBriefReadsAge(unittest.TestCase):
    def test_a_single_failure_a_little_over_a_half_life_ago_is_left_out_and_one_just_under_stays(self) -> None:
        text = brief([stored("code:old", 1, 31), stored("code:recent", 1, 29)])
        self.assertIn("code:recent", text)
        self.assertNotIn("code:old", text)

    def test_the_floor_itself_stays(self) -> None:
        # One proof, exactly one half-life: strength is exactly the floor, and the floor is "under", not "at".
        self.assertIn("code:edge", brief([stored("code:edge", 1, 30)]))

    def test_what_stays_says_how_long_ago(self) -> None:
        text = brief([stored("code:recent", 1, 12), stored("code:today", 1, 0)])
        self.assertIn("last seen 12 days ago", text)
        self.assertIn("last seen today", text)

    def test_a_lesson_proven_many_times_lasts_longer_and_still_goes_in_the_end(self) -> None:
        self.assertIn("code:heavy", brief([stored("code:heavy", 8, 100)]))  # 8 * 2**(-100/30) = 0.79
        self.assertNotIn("code:heavy", brief([stored("code:heavy", 8, 150)]))  # = 0.25

    def test_a_row_with_no_recorded_time_is_kept_and_not_dated(self) -> None:
        text = brief([stored("code:undated", 1, None)])
        self.assertIn("code:undated", text)
        self.assertNotIn("last seen", text)

    def test_when_everything_has_faded_the_brief_has_no_section_at_all(self) -> None:
        self.assertEqual("", brief([stored("code:old", 1, 400)]))

    def test_the_other_sections_are_not_touched(self) -> None:
        threads = [{
            "id": "t1", "title": "Speed up login", "asks": ["fix slow login"], "runs": 1,
            "completed": 1, "failed": 0, "cancelled": 0, "last_touched": NOW - 400 * DAY,
        }]
        text = build_brief(threads, [], observation_rows=[stored("code:old", 1, 400)], now=NOW)
        self.assertIn("Speed up login", text)
        self.assertNotIn("code:old", text)

    def test_the_cap_still_holds(self) -> None:
        rows = [stored(f"code:n{n}", 3, 1) for n in range(MAX_BRIEF_OBSERVATIONS + 4)]
        self.assertLessEqual(brief(rows).count("- code:"), MAX_BRIEF_OBSERVATIONS)

    def test_the_cap_holds_when_the_scan_adds_lessons_the_store_has_not_seen(self) -> None:
        rows = [stored(f"code:s{n}", 3, 1) for n in range(3)]
        events = [failure(f"d{n}", 1, n=n + 1) for n in range(6)]
        text = brief(rows, events)
        self.assertEqual(MAX_BRIEF_OBSERVATIONS, text.count("- code:"))
        self.assertIn("code:s0", text, "the store's lessons come first")


class TestTheEventsDecideWhenTheyCanBeSeen(unittest.TestCase):
    def test_a_stored_lesson_the_scan_saw_recently_stays_whatever_its_row_says(self) -> None:
        text = brief([stored("code:again", 1, 300)], [failure("again", 2)])
        self.assertIn("code:again", text)
        self.assertIn("last seen 2 days ago", text, "the label is the newest event's age, not the row's 300")

    def test_a_row_the_pass_refreshed_yesterday_still_fades_if_its_events_are_old(self) -> None:
        # The consolidation pass bumps `refined_at` of every subject still in its scan window, so a quiet
        # workspace's year-old failure has a row that says "yesterday". Its events say otherwise.
        text = brief([stored("code:quiet", 1, 1)], [failure("quiet", 120)])
        self.assertNotIn("code:quiet", text)

    def test_a_subject_only_the_scan_knows_is_aged_by_its_events_too(self) -> None:
        self.assertIn("code:fresh", brief(events=[failure("fresh", 3)]))
        self.assertNotIn("code:stale", brief(events=[failure("stale", 120)]))

    def test_the_newest_event_of_a_subject_is_the_one_that_counts(self) -> None:
        text = brief(events=[failure("mixed", 200, n=1), failure("mixed", 4, n=2), failure("mixed", 90, n=3)])
        self.assertIn("code:mixed", text)
        self.assertIn("last seen 4 days ago", text)

    def test_a_faded_stored_lesson_is_not_brought_back_as_a_new_one(self) -> None:
        text = brief([stored("code:quiet", 1, 1)], [failure("quiet", 120)])
        self.assertEqual("", text, "the scan re-derived a lesson the decay had just dropped")


class TestWhatDecayDoesNotTouch(unittest.TestCase):
    def test_the_default_clock_is_the_real_one(self) -> None:
        now = time.time()
        text = build_brief([], [], observation_rows=[{**stored("code:real", 1, 0), "refined_at": now - 2 * DAY}])
        self.assertIn("code:real", text)
        self.assertIn("last seen 2 days ago", text)

    def test_the_raw_events_are_still_there_for_the_tool(self) -> None:
        # Decay is in the brief. `recall` over the same rows still finds the old failure when asked.
        from engine.recall import search

        rows = [failure("ancient", 500)]
        self.assertEqual(1, len(search(rows, "ancient")["matches"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
