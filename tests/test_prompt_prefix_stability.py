"""The prefix a prompt cache keys on is byte-identical from one call to the next (docs/21 P3).

A cache read needs the same leading bytes as the write it repays. The conductor's system prompt is re-sent on every
call of a run, so anything in it that changes between calls (the time, a counter, an id, a random order) turns every
call into a miss that costs a write premium for nothing. Nothing here is a model or a network: the properties are about
the strings the engine builds, held by building them twice under different clocks, ids and random seeds.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import random
import re
import tempfile
import time
import unittest
import uuid
from unittest import mock

from engine.chat_prompts import CHAT_SYSTEM_PROMPT, CONDUCTOR_SYSTEM_PROMPT
from engine.conductor_tools import ConductorTools
from engine.default_prompts import DEFAULT_PROMPTS
from engine.executor_conduct import conductor_system_prompt
from engine.skills import load_skills

# The shapes of text that change from call to call: an ISO date or time, a UUID, an epoch time (nine digits or more),
# and a clock reading. A static prompt has none of them.
VOLATILE = (
    ("an ISO timestamp", re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")),
    ("a calendar date", re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")),
    ("a UUID", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("an epoch time", re.compile(r"\b\d{9,}\b")),
    ("a clock time", re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b")),
)


def volatile_in(text: str) -> list[str]:
    return [name for name, pattern in VOLATILE if pattern.search(text)]


class TestTheDetector(unittest.TestCase):
    def test_it_flags_the_things_that_change_between_calls(self) -> None:
        for injected in (
            "Generated at 2026-11-02T10:11:12Z",
            "Today is 2026-11-02.",
            f"Run {uuid.uuid4()}",
            "At 1793632824763 the run began",
            "It is 10:11:12 now",
        ):
            with self.subTest(injected=injected):
                self.assertTrue(volatile_in(f"You are the conductor. {injected}"), "a volatile string slipped past")

    def test_it_passes_ordinary_prompt_text(self) -> None:
        self.assertEqual([], volatile_in("You are Codify. Read docs/00 section 6. Retry at most 2 times, in 3 steps."))


class TestThePrefixIsStable(unittest.TestCase):
    def skills_menu(self) -> str:
        with tempfile.TemporaryDirectory() as root:
            return load_skills(root, ConductorTools.NAMES).menu()

    def test_the_conductor_system_prompt_does_not_depend_on_the_clock_an_id_or_a_seed(self) -> None:
        first = conductor_system_prompt(self.skills_menu())
        with mock.patch.object(time, "time", return_value=9_999_999_999.0), \
                mock.patch.object(uuid, "uuid4", return_value=uuid.UUID(int=1)):
            random.seed(12345)
            second = conductor_system_prompt(self.skills_menu())
        self.assertEqual(first, second)

    def test_the_conductor_system_prompt_holds_nothing_volatile(self) -> None:
        self.assertEqual([], volatile_in(conductor_system_prompt(self.skills_menu())))

    def test_every_role_and_chat_prompt_is_static_text(self) -> None:
        prompts: dict[str, str] = {str(k): v for k, v in DEFAULT_PROMPTS.items()}
        prompts.update({"chat": CHAT_SYSTEM_PROMPT, "conductor": CONDUCTOR_SYSTEM_PROMPT})
        for name, text in prompts.items():
            with self.subTest(prompt=name):
                self.assertEqual([], volatile_in(text))

    def test_the_skills_menu_has_one_stable_order(self) -> None:
        menu = self.skills_menu()
        self.assertEqual(menu, self.skills_menu())
        names = [line.split(":", 1)[0].removeprefix("- ").split(" ")[0] for line in menu.splitlines() if line.startswith("- ")]
        self.assertEqual(sorted(names), names, "an unordered menu would reshuffle the prefix between runs")

    def test_the_tool_list_is_in_a_fixed_order_with_no_repeats(self) -> None:
        self.assertEqual(len(ConductorTools.NAMES), len(set(ConductorTools.NAMES)))
        self.assertEqual(list(ConductorTools.NAMES), list(ConductorTools.NAMES))
        # Reordering tools invalidates a prefix as surely as editing the system prompt, so the order is pinned
        # to the table's own, not to a set's iteration order.
        self.assertIsInstance(ConductorTools.NAMES, tuple)

    def test_a_volatile_string_would_change_the_prefix_and_be_caught(self) -> None:
        injected = conductor_system_prompt(self.skills_menu() + "\nGenerated at 2026-11-02T10:11:12Z")
        self.assertNotEqual(conductor_system_prompt(self.skills_menu()), injected)
        self.assertEqual(["an ISO timestamp"], volatile_in(injected))


if __name__ == "__main__":
    unittest.main()
