"""The conductor bounds what it replays, without losing the conversation.

`Conductor.run` used to append every tool result for the whole turn, each clipped at 12,000 characters, and
re-send all of them on every call. Fourteen calls of file reads is a hundred thousand characters in a local
model's 32k-token window: the evidence nobody can fit is the evidence the answer is supposed to rest on.

`bound_replay` keeps the newest results whole and reduces the older ones to a line that says what they were and
that they can be read again. These tests are about the two halves of that promise: the transcript fits, and the
parts that are not evidence — the person's words and the model's own replies — are never touched.
"""

from __future__ import annotations

import unittest
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.conductor import (
    KEEP_WHOLE_RESULTS,
    MAX_CONTEXT_CHARS,
    MAX_TOOL_RESULT_CHARS,
    Conductor,
    TOOLS,
    bound_replay,
)
from engine.toolcall import ToolReply
from tests.test_conductor import ConductorTestCase, _call, _ToolProvider


def _tool(index: int, content: str, name: str = "read_file") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": f"c{index}", "name": name, "content": content}


def _total(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


class TestTheLoopBoundsWhatItReplays(ConductorTestCase):
    async def test_a_turn_of_twelve_kilobyte_reads_fits_the_budget_by_its_last_call(self) -> None:
        # Ten reads of a whole file, then an answer: without a bound the final request carries 120,000 characters
        # of evidence, and every earlier call carried more than the one before it.
        page = "x" * MAX_TOOL_RESULT_CHARS

        async def read_file(_args: dict[str, Any]) -> str:
            return page

        provider = _ToolProvider(
            [ToolReply(tool_calls=[_call("read_file", path=f"f{i}.py")]) for i in range(10)]
            + [ToolReply(text="done")]
        )
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {"read_file": read_file},
            system_prompt="s", max_turns=11,
        )

        await conductor.run("read everything")

        final = provider.seen_messages[-1]
        tools = [m for m in final if m.get("role") == "tool"]
        self.assertEqual(10, len(tools), "a result was dropped rather than stubbed")
        self.assertLessEqual(
            _total(final), MAX_CONTEXT_CHARS,
            "the last request still carried more than the loop allows",
        )
        stubbed = [m for m in tools if str(m["content"]).startswith("[dropped: ")]
        whole = [m for m in tools if m["content"] == page]
        self.assertEqual(KEEP_WHOLE_RESULTS, len(whole), "the newest results were not kept whole")
        self.assertEqual(10 - KEEP_WHOLE_RESULTS, len(stubbed))
        # Every stub says what it was and what to do about it, and the protocol's pairing is untouched.
        for message in stubbed:
            self.assertIn("read_file returned 12,000 characters", message["content"])
            self.assertIn("call it again", message["content"])
            self.assertTrue(message["tool_call_id"].startswith("c"))
            self.assertEqual("read_file", message["name"])
        # The person's request is conversation, not evidence, and is still the first thing the model reads.
        self.assertEqual("read everything", final[0]["content"])
        self.assertEqual("user", final[0]["role"])


class TestTheBoundItself(unittest.TestCase):
    def test_the_oldest_results_are_stubbed_first_and_the_newest_are_never_touched(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "look at all of this"},
            {"role": "assistant", "content": "reading"},
            *[_tool(i, "y" * 100) for i in range(6)],
        ]
        before = [dict(m) for m in messages]

        stubbed = bound_replay(messages, budget=250, keep=2)

        self.assertEqual(4, stubbed, "it did not stub every result it was allowed to")
        for index in range(2, 6):
            self.assertTrue(messages[index]["content"].startswith("[dropped: "), f"tool {index} was kept")
            self.assertEqual(f"c{index - 2}", messages[index]["tool_call_id"])
        for index in (6, 7):
            self.assertEqual(before[index]["content"], messages[index]["content"], "a newest result was touched")
        self.assertEqual(before[0], messages[0], "the person's words were replaced")
        self.assertEqual(before[1], messages[1], "the model's own reply was replaced")
        # Two results over a budget this small is the documented answer: the newest are kept even then.
        self.assertGreater(_total(messages), 250)

    def test_stubbing_twice_costs_nothing_and_a_stub_never_grows_a_result(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "q"},
            *[_tool(i, "z" * 100) for i in range(4)],
        ]
        self.assertEqual(4, bound_replay(messages, budget=0, keep=0))
        once = [dict(m) for m in messages]
        self.assertEqual(0, bound_replay(messages, budget=0, keep=0), "a second pass stubbed again")
        self.assertEqual(once, messages)

        tiny = [{"role": "user", "content": "q"}, _tool(0, "ok")]
        self.assertEqual(0, bound_replay(tiny, budget=0, keep=0), "a two-letter result was replaced by a longer stub")
        self.assertEqual("ok", tiny[1]["content"])
