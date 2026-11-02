"""Read-only tools in one reply run together, and nothing else does (docs/09 §10).

A model that asks for three reads in one reply used to wait for them one after another. The tools that only read
now overlap, up to `MAX_CONCURRENT_READS`, and everything the serial loop promised is still true: results come
back in the order the model asked, a call that is not a read is a barrier, a cancel stops the rest, and a question
to the person ends the run with nothing after it executed.

These assert on what ran, when, and in what order, not on what the model was shown.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.conductor import (
    CONCURRENT_READ_TOOLS,
    MAX_CONCURRENT_READS,
    STAGE_MOVES,
    TOOLS,
    Conductor,
    EndTurn,
)
from engine.toolcall import ToolCall, ToolReply
from tests.test_conductor import ConductorTestCase, _ToolProvider

SLOW = 0.3


def _calls(*pairs: tuple[str, dict[str, Any]]) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name=name, arguments=args) for i, (name, args) in enumerate(pairs)]


class _Probe:
    """A handler that sleeps, and remembers when it started and how many were in flight with it."""

    def __init__(self, label: str, log: list[str], delay: float = SLOW) -> None:
        self.label, self.log, self.delay = label, log, delay
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, args: dict[str, Any]) -> str:
        self.log.append(f"start {args.get('path') or args.get('query') or args.get('argv')}")
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        self.log.append(f"end {args.get('path') or args.get('query') or args.get('argv')}")
        return f"{self.label}:{args.get('path') or args.get('query') or args.get('argv')}"


class TestReadsRunTogether(ConductorTestCase):
    def _conductor(self, replies: list[ToolReply], dispatch: dict[str, Any], **kw: Any) -> Conductor:
        return Conductor(
            _ToolProvider(replies), "m", str(self.repo), list(TOOLS), dispatch,
            system_prompt="s", max_turns=4, **kw,
        )

    def test_the_set_is_read_only_tools_and_none_of_the_moves(self) -> None:
        self.assertEqual({"read_file", "search_code", "git_history", "recall"}, set(CONCURRENT_READ_TOOLS))
        self.assertFalse(CONCURRENT_READ_TOOLS & STAGE_MOVES)
        for writer in ("run_command", "ask_user", "edit_editor", "run_in_machine"):
            self.assertNotIn(writer, CONCURRENT_READ_TOOLS)

    async def test_three_slow_reads_take_about_one_read_not_three(self) -> None:
        log: list[str] = []
        read = _Probe("r", log)
        conductor = self._conductor(
            [
                ToolReply(tool_calls=_calls(
                    ("read_file", {"path": "a"}), ("read_file", {"path": "b"}), ("read_file", {"path": "c"}),
                )),
                ToolReply(text="done"),
            ],
            {"read_file": read},
        )

        began = time.monotonic()
        await conductor.run("go")
        took = time.monotonic() - began

        self.assertEqual(3, read.peak, "all three were in flight together")
        self.assertLess(took, SLOW * 2, f"3 reads of {SLOW}s took {took:.2f}s: they ran one after another")

    async def test_results_come_back_in_the_order_the_model_asked_even_when_the_last_finishes_first(self) -> None:
        async def slow_then_fast(args: dict[str, Any]) -> str:
            await asyncio.sleep(0.3 if args["path"] == "a" else 0.01)
            return f"got {args['path']}"

        provider = _ToolProvider([
            ToolReply(tool_calls=_calls(("read_file", {"path": "a"}), ("read_file", {"path": "b"}))),
            ToolReply(text="done"),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {"read_file": slow_then_fast}, system_prompt="s", max_turns=4,
        )

        await conductor.run("go")

        tool_messages = [m for m in provider.seen_messages[-1] if m.get("role") == "tool"]
        self.assertEqual(["got a", "got b"], [m["content"] for m in tool_messages])
        self.assertEqual(["c0", "c1"], [m["tool_call_id"] for m in tool_messages])
        self.assertEqual(["read_file", "read_file"], conductor.tools_used)

    async def test_no_more_than_the_cap_run_at_once(self) -> None:
        log: list[str] = []
        read = _Probe("r", log, delay=0.05)
        count = MAX_CONCURRENT_READS * 2 + 1
        conductor = self._conductor(
            [
                ToolReply(tool_calls=_calls(*[("read_file", {"path": f"f{i}"}) for i in range(count)])),
                ToolReply(text="done"),
            ],
            {"read_file": read},
        )

        await conductor.run("go")

        self.assertEqual(4, MAX_CONCURRENT_READS)
        self.assertEqual(MAX_CONCURRENT_READS, read.peak)
        self.assertEqual(count, len([e for e in log if e.startswith("end")]), "every one still ran")

    async def test_a_command_between_two_reads_is_a_barrier(self) -> None:
        log: list[str] = []
        read, command = _Probe("r", log, 0.05), _Probe("c", log, 0.05)
        conductor = self._conductor(
            [
                ToolReply(tool_calls=_calls(
                    ("read_file", {"path": "before"}),
                    ("run_command", {"argv": ["true"]}),
                    ("read_file", {"path": "after"}),
                )),
                ToolReply(text="done"),
            ],
            {"read_file": read, "run_command": command},
        )

        await conductor.run("go")

        self.assertEqual(
            ["start before", "end before", "start ['true']", "end ['true']", "start after", "end after"], log,
            "the read after the command did not start until the command ended",
        )

    async def test_a_question_to_the_person_waits_for_the_reads_before_it_and_drops_the_ones_after(self) -> None:
        log: list[str] = []
        read = _Probe("r", log, 0.05)

        async def ask(args: dict[str, Any]) -> str:
            log.append("ask")
            raise EndTurn("Which one?")

        conductor = self._conductor(
            [
                ToolReply(tool_calls=_calls(
                    ("read_file", {"path": "a"}), ("read_file", {"path": "b"}),
                    ("ask_user", {"question": "Which one?"}),
                    ("read_file", {"path": "never"}),
                )),
            ],
            {"read_file": read, "ask_user": ask},
        )

        answer = await conductor.run("go")

        self.assertEqual("Which one?", answer)
        self.assertEqual({"end a", "end b"}, set(log[:4]) - {"start a", "start b"}, "both reads finished first")
        self.assertEqual("ask", log[4])
        self.assertNotIn("start never", log, "nothing after the question ran")

    async def test_a_cancel_between_batches_stops_the_rest(self) -> None:
        log: list[str] = []
        read, command = _Probe("r", log, 0.01), _Probe("c", log, 0.01)
        state = {"cancel": False}

        async def read_then_cancel(args: dict[str, Any]) -> str:
            state["cancel"] = True
            return await read(args)

        conductor = self._conductor(
            [
                ToolReply(tool_calls=_calls(
                    ("read_file", {"path": "a"}), ("read_file", {"path": "b"}),
                    ("run_command", {"argv": ["never"]}),
                )),
            ],
            {"read_file": read_then_cancel, "run_command": command},
            cancelled=lambda: state["cancel"],
        )

        answer = await conductor.run("go")

        self.assertEqual("", answer)
        self.assertNotIn("start ['never']", log, "the command after the cancelled batch did not run")

    async def test_a_cancel_during_a_batch_stops_the_reads_that_had_not_started(self) -> None:
        ran: list[str] = []
        state = {"cancel": False}

        async def first_cancels(args: dict[str, Any]) -> str:
            ran.append(args["path"])
            state["cancel"] = True
            return "ok"

        conductor = self._conductor(
            [ToolReply(tool_calls=_calls(("read_file", {"path": "a"}), ("read_file", {"path": "b"})))],
            {"read_file": first_cancels},
            cancelled=lambda: state["cancel"],
        )

        answer = await conductor.run("go")

        self.assertEqual("", answer)
        self.assertEqual(["a"], ran, "the second read had not started when the cancel came, so it never did")
        self.assertEqual(["read_file"], conductor.tools_used)

    async def test_the_same_call_twice_in_a_row_is_still_run_once(self) -> None:
        log: list[str] = []
        read = _Probe("r", log, 0.01)
        conductor = self._conductor(
            [
                ToolReply(tool_calls=_calls(("read_file", {"path": "a"}), ("read_file", {"path": "a"}))),
                ToolReply(text="done"),
            ],
            {"read_file": read},
        )

        await conductor.run("go")

        self.assertEqual(1, log.count("start a"), "the repeat was refused, as it is in a serial reply")

    async def test_a_read_that_raises_is_text_for_that_call_only(self) -> None:
        async def boom(args: dict[str, Any]) -> str:
            if args["path"] == "bad":
                raise RuntimeError("disk gone")
            return "fine"

        provider = _ToolProvider([
            ToolReply(tool_calls=_calls(("read_file", {"path": "bad"}), ("read_file", {"path": "good"}))),
            ToolReply(text="done"),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {"read_file": boom}, system_prompt="s", max_turns=4,
        )

        await conductor.run("go")

        contents = [m["content"] for m in provider.seen_messages[-1] if m.get("role") == "tool"]
        self.assertIn("That call failed: RuntimeError: disk gone", contents[0])
        self.assertEqual("fine", contents[1])
