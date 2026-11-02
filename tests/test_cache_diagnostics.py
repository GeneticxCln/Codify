"""`engine/cache_diagnostics.py`: attribute a cache miss without keeping any content.

Two promises. The attribution picks the right reason, in the documented order, from digests alone; and nothing that
the module hands back can hold prompt text, however long the prompt that went in.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import re
import unittest

from engine import cache_diagnostics as cd
from engine.cache_diagnostics import CallFacts, explain, fingerprint, prompt_parts

SYSTEM = "You are the conductor. SECRET-PROMPT-TEXT-9917 " * 50
TOOLS = ["read_file", "search_code", "write"]


def facts(system: str = SYSTEM, tools: list[str] | None = None, model: str = "m1", protocol: str = "anthropic",
          at: float = 1000.0) -> CallFacts:
    return CallFacts(parts=prompt_parts(system, TOOLS if tools is None else tools, model, protocol), timestamp=at)


class TestAttribution(unittest.TestCase):
    def test_the_first_call_has_nothing_before_it(self) -> None:
        self.assertEqual("first_request", explain(None, facts()))

    def test_each_change_is_named(self) -> None:
        base = facts()
        self.assertEqual("model_switch", explain(base, facts(model="m2")))
        self.assertEqual("model_switch", explain(base, facts(protocol="openai")))
        self.assertEqual("system_changed", explain(base, facts(system=SYSTEM + "x")))
        self.assertEqual("tools_changed", explain(base, facts(tools=TOOLS + ["verify"])))
        self.assertEqual("compaction", explain(base, facts(), compacted_between=True))
        self.assertEqual("idle_ttl", explain(base, facts(at=1000.0 + cd.IDLE_TTL_S + 1)))
        self.assertEqual("none", explain(base, facts(at=1010.0)))

    def test_the_order_is_the_documented_one(self) -> None:
        base = facts()
        everything = facts(model="m2", system=SYSTEM + "x", tools=["a"], at=99999.0)
        self.assertEqual("model_switch", explain(base, everything, compacted_between=True))
        self.assertEqual("system_changed", explain(base, facts(system=SYSTEM + "x", tools=["a"], at=99999.0)))
        self.assertEqual("tools_changed", explain(base, facts(tools=["a"], at=99999.0), compacted_between=True))
        self.assertEqual(
            ("first_request", "model_switch", "system_changed", "tools_changed", "compaction", "idle_ttl", "none"),
            cd.REASONS,
        )

    def test_tool_order_is_not_a_change_but_a_different_set_is(self) -> None:
        self.assertEqual("none", explain(facts(tools=["a", "b"]), facts(tools=["b", "a"])))
        self.assertEqual("tools_changed", explain(facts(tools=["a", "b"]), facts(tools=["a"])))


class TestFingerprint(unittest.TestCase):
    def test_it_changes_when_exactly_one_input_changes_and_not_otherwise(self) -> None:
        base = fingerprint(facts().parts)
        self.assertEqual(base, fingerprint(facts().parts), "deterministic")
        self.assertEqual(base, fingerprint(facts(at=5.0).parts), "time is not an input")
        for changed in (facts(model="m2"), facts(protocol="p"), facts(system=SYSTEM + "."), facts(tools=["a"])):
            self.assertNotEqual(base, fingerprint(changed.parts))


class TestNoContent(unittest.TestCase):
    def test_nothing_returned_holds_the_prompt(self) -> None:
        parts = prompt_parts(SYSTEM, TOOLS, "m1", "anthropic")
        shown = repr(parts) + fingerprint(parts)
        self.assertNotIn("SECRET-PROMPT-TEXT", shown)
        self.assertNotIn("read_file", shown, "not even a tool name is kept, only a digest of the set")
        self.assertEqual({"system", "tools", "model", "protocol"}, set(parts))
        for key in ("system", "tools"):
            self.assertRegex(parts[key], rf"^[0-9a-f]{{{cd.DIGEST_CHARS}}}$")
        self.assertRegex(fingerprint(parts), rf"^[0-9a-f]{{{cd.DIGEST_CHARS}}}$")

    def test_a_very_long_name_is_bounded(self) -> None:
        parts = prompt_parts("s", [], "m" * 5000, "p" * 5000)
        self.assertLessEqual(len(parts["model"]), cd.MAX_NAME_CHARS)
        self.assertLessEqual(len(parts["protocol"]), cd.MAX_NAME_CHARS)

    def test_a_lone_surrogate_does_not_crash_the_digest(self) -> None:
        self.assertRegex(cd.digest("bad \ud800 text"), r"^[0-9a-f]{16}$")

    def test_the_module_imports_no_model_clock_or_database(self) -> None:
        import inspect

        source = inspect.getsource(cd)
        for forbidden in ("import time", "import sqlite3", "import httpx", "datetime"):
            self.assertNotIn(forbidden, source)
        self.assertTrue(re.search(r"hashlib", source))


if __name__ == "__main__":
    unittest.main()
