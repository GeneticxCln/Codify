"""One model reply may carry only so many tool calls, and the rest are never run.

`max_turns` bounds the conductor's *model calls*, and the move budget bounds its stage moves, but nothing bounded how
many tool calls a single reply could ask for. A reply of 20,000 `read_file` calls under `max_turns=3` ran all 20,000: a
trivial tool took a second and 28 MB, a tool that returns a clipped 60,000-character result took 2.6 s and 353 MB at
peak (every result is appended to the transcript before `bound_replay` can trim it on the next call), and a real read or
search costs far more per call. Any reply can ask for it, including one steered by hostile page, repository or MCP text.
The effect is a local engine that is slow or out of memory, not data leaving it (the calls only read, or are refused).

The cap is on what is run, not on what the model may say: the calls past it are dropped before they enter the transcript
or the trace, and the model is told once, in the last result it receives, so it can ask again for what it still needs.
"""

from __future__ import annotations

import tempfile
import unittest
from typing import Any

from engine.conductor import MAX_CALLS_PER_REPLY, Conductor
from engine.providers import BaseProvider
from engine.toolcall import ToolCall, ToolReply, ToolSpec

READ_FILE = ToolSpec(
    name="read_file", description="d", parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)


class _OneBigReply(BaseProvider):
    """Answers its first call with `n` read_file calls and its second with text, keeping what it was shown."""

    supports_tools = True

    def __init__(self, n: int) -> None:
        self.n = n
        self.calls = 0
        self.seen_on_second_call: list[dict[str, Any]] = []

    async def complete(self, *args: Any, **kwargs: Any) -> str:
        return ""

    async def complete_with_tools(
        self, system_prompt: str, messages: list[dict[str, Any]], tools: list[ToolSpec], model: str,
        temperature: float, max_tokens: int, *, num_ctx: int | None = None, keep_alive: str | None = None,
        cache_prefix: bool = False,
    ) -> ToolReply:
        self.calls += 1
        if self.calls == 1:
            return ToolReply(
                text="", tool_calls=[ToolCall(id=f"c{i}", name="read_file", arguments={"path": f"f{i}.txt"}) for i in range(self.n)],
            )
        self.seen_on_second_call = [dict(m) for m in messages]
        return ToolReply(text="done")


class _Ledger:
    """Records the calls each reply carried, which is what the trace row is made from."""

    def __init__(self) -> None:
        self.replies: list[ToolReply] = []

    def open(self, request: Any) -> Any:
        ledger = self

        class Books:
            def succeeded(self, reply: ToolReply) -> None:
                ledger.replies.append(reply)

            def failed(self, exc: Any) -> None:
                pass

            def close(self) -> None:
                pass

        return Books()


class TestAReplyMayAskForOnlySoManyCalls(unittest.IsolatedAsyncioTestCase):
    async def _run(self, n: int, ledger: _Ledger | None = None) -> tuple[_OneBigReply, list[str], str]:
        executed: list[str] = []

        async def read_file(args: dict[str, Any]) -> str:
            executed.append(str(args["path"]))
            return f"contents of {args['path']}"

        provider = _OneBigReply(n)
        with tempfile.TemporaryDirectory() as root:
            conductor = Conductor(
                provider, "m", root, tools=[READ_FILE], dispatch={"read_file": read_file},
                system_prompt="s", max_turns=3, max_moves=2, ledger=ledger,
            )
            answer = await conductor.run("go")
        return provider, executed, answer

    async def test_a_reply_of_thousands_of_calls_runs_only_the_first_few(self) -> None:
        provider, executed, answer = await self._run(5000)

        self.assertEqual("done", answer)
        self.assertEqual([f"f{i}.txt" for i in range(MAX_CALLS_PER_REPLY)], executed, "the calls past the cap ran")

    async def test_the_transcript_keeps_only_the_calls_that_ran_and_says_so_once(self) -> None:
        provider, executed, _ = await self._run(MAX_CALLS_PER_REPLY + 40)

        assistant = [m for m in provider.seen_on_second_call if m["role"] == "assistant"][0]
        results = [m for m in provider.seen_on_second_call if m["role"] == "tool"]
        self.assertEqual(MAX_CALLS_PER_REPLY, len(assistant["tool_calls"]), "the dropped calls are in the history")
        self.assertEqual(
            [c.id for c in assistant["tool_calls"]], [m["tool_call_id"] for m in results],
            "every call kept has exactly one result, which the providers require",
        )
        notes = [m["content"] for m in results if "were dropped without running" in m["content"]]
        self.assertEqual(1, len(notes), "the model is told once")
        self.assertIs(results[-1]["content"], notes[0], "and in the last result it receives")
        self.assertIn(f"first {MAX_CALLS_PER_REPLY} of the {MAX_CALLS_PER_REPLY + 40} calls", notes[0])

    async def test_the_trace_is_not_handed_the_dropped_calls(self) -> None:
        ledger = _Ledger()

        await self._run(MAX_CALLS_PER_REPLY + 40, ledger)

        self.assertEqual(MAX_CALLS_PER_REPLY, len(ledger.replies[0].tool_calls))

    async def test_a_reply_at_the_cap_is_untouched(self) -> None:
        # The control: the cap drops nothing the model was allowed, and says nothing.
        provider, executed, _ = await self._run(MAX_CALLS_PER_REPLY)

        self.assertEqual(MAX_CALLS_PER_REPLY, len(executed))
        results = [m for m in provider.seen_on_second_call if m["role"] == "tool"]
        self.assertEqual(MAX_CALLS_PER_REPLY, len(results))
        self.assertFalse([m for m in results if "dropped" in m["content"]], "a note was added to a reply within the cap")


if __name__ == "__main__":
    unittest.main()
