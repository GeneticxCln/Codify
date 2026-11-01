"""The conductor bounds what it replays, without losing the conversation.

`Conductor.run` used to append every tool result for the whole turn, each clipped at 12,000 characters, and
re-send all of them on every call. Fourteen calls of file reads is a hundred thousand characters in a local
model's 32k-token window: the evidence nobody can fit is the evidence the answer is supposed to rest on.

`bound_replay` keeps the newest results whole and reduces the older ones to a line that says what they were and
that they can be read again. `keep` is a preference, not a promise: with four results and `keep=4` nothing was
stubbable, so the bound did not bind. The newest results are now the last to go, not exempt.

The conversation needs its own bound, because `bound_replay` never replaces a person's words or the model's own
replies. `bound_history` applies it once, when the thread's earlier turns are turned into messages: newest kept,
each message clipped, the oldest turns dropped.

These tests are about the halves of that promise: the transcript fits, and the parts that are not evidence — the
person's words and the model's own replies — are never quietly rewritten mid-turn.
"""

from __future__ import annotations

import unittest
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.conductor import (
    KEEP_WHOLE_RESULTS,
    MAX_CONTEXT_CHARS,
    MAX_HISTORY_CHARS,
    MAX_TOOL_RESULT_CHARS,
    Conductor,
    TOOLS,
    bound_history,
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
    def test_the_oldest_results_are_stubbed_first_and_the_newest_are_kept_while_they_fit(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "look at all of this"},
            {"role": "assistant", "content": "reading"},
            *[_tool(i, "y" * 1000) for i in range(6)],
        ]
        before = [dict(m) for m in messages]

        stubbed = bound_replay(messages, budget=3000, keep=2)

        self.assertEqual(4, stubbed, "it did not stub every older result it was allowed to")
        for index in range(2, 6):
            self.assertTrue(messages[index]["content"].startswith("[dropped: "), f"tool {index} was kept")
            self.assertEqual(f"c{index - 2}", messages[index]["tool_call_id"])
        for index in (6, 7):
            self.assertEqual(before[index]["content"], messages[index]["content"], "a newest result was touched")
        self.assertEqual(before[0], messages[0], "the person's words were replaced")
        self.assertEqual(before[1], messages[1], "the model's own reply was replaced")
        self.assertLessEqual(_total(messages), 3000, "the transcript did not fit the budget")

    def test_four_results_over_budget_are_still_reduced(self) -> None:
        # The defect: `keep=4` meant a turn with exactly four results had nothing stubbable, so a transcript
        # those four had already overflowed went to the provider whole. The budget has to win.
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "x" * 2000},
            *[_tool(i, "y" * 1000) for i in range(4)],
        ]

        stubbed = bound_replay(messages, budget=4000)

        self.assertEqual(3, stubbed, "the newest results were exempt when they should have been last")
        self.assertLessEqual(_total(messages), 4000, "the transcript still did not fit")
        self.assertEqual("y" * 1000, messages[-1]["content"], "the newest was reduced before it had to be")
        for message in messages[1:4]:
            self.assertTrue(message["content"].startswith("[dropped: "))

    def test_the_budget_outranks_keeping_any_result_when_none_of_them_fits(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "q" * 3500},
            *[_tool(i, "z" * 1000) for i in range(4)],
        ]

        self.assertEqual(4, bound_replay(messages, budget=4000))

        self.assertLessEqual(_total(messages), 4000)
        for message in messages[1:]:
            self.assertTrue(message["content"].startswith("[dropped: "), "a result stayed over budget")

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


class TestTheHistoryItself(unittest.TestCase):
    def test_the_newest_turns_are_kept_and_the_oldest_are_dropped(self) -> None:
        history = [{"prompt": f"p{i}", "reply": f"r{i}"} for i in range(10)]

        messages = bound_history(history, budget=20, per_message=100)

        # Five turns of four characters fit; the newest five are the ones that survive.
        self.assertEqual(10, len(messages))
        self.assertEqual({"role": "user", "content": "p5"}, messages[0])
        self.assertEqual({"role": "assistant", "content": "r9"}, messages[-1])

    def test_a_huge_message_is_clipped_and_keeps_both_ends(self) -> None:
        messages = bound_history(
            [{"prompt": "a" * 50_000, "reply": "b" * 50_000}], per_message=1000,
        )

        self.assertEqual(["user", "assistant"], [m["role"] for m in messages])
        for message, letter in zip(messages, "ab", strict=True):
            content = str(message["content"])
            self.assertTrue(content.startswith(letter * 500), "the opening of the message was lost")
            self.assertTrue(content.endswith(letter * 500), "the end of the message was lost")
            self.assertIn("chars elided", content)
            self.assertLessEqual(len(content), 1100)

    def test_a_turn_whose_reply_was_never_written_still_replays_the_question(self) -> None:
        self.assertEqual(
            [{"role": "user", "content": "did it work?"}],
            bound_history([{"prompt": "did it work?", "reply": ""}]),
        )

    def test_no_history_is_no_messages(self) -> None:
        self.assertEqual([], bound_history(None))
        self.assertEqual([], bound_history([]))


class TestTheLoopReplaysABoundedHistory(ConductorTestCase):
    async def test_a_thread_of_huge_turns_does_not_fill_the_window(self) -> None:
        provider = _ToolProvider([ToolReply(text="done")])
        conductor = Conductor(provider, "m", str(self.repo), [], {}, system_prompt="s")
        history = [
            {"prompt": f"question {i} " + "q" * 50_000, "reply": f"answer {i} " + "a" * 50_000}
            for i in range(12)
        ]

        await conductor.run("now this", history)

        sent = provider.seen_messages[-1]
        self.assertEqual("now this", sent[-1]["content"])
        self.assertEqual("user", sent[-1]["role"])
        earlier = sent[:-1]
        self.assertLessEqual(_total(earlier), MAX_HISTORY_CHARS, "the history filled the window")
        self.assertIn("answer 11", earlier[-1]["content"], "the newest exchange was lost")
        self.assertNotIn("question 0", "".join(str(m["content"]) for m in sent))
