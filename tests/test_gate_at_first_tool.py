"""A turn is a conversation until it acts: the gate is asked at the conductor's first tool call (docs/09 §10.15).

Found by a person using the app: every message, "hi" included, waited on the pre-flight gate before a word came
back. With no Laya SDK the gate borrows the turn's own model, so a greeting cost a classifier call and then an
answer, and on a local model that shares one GPU the two run back to back. A reply that calls no tool reads,
runs and sends nothing, so there is nothing for the gate to guard there. What it guards — the first read, command,
fetch, browser action or sub-agent move — still waits for it, and a blocked turn still fails before any of them
runs, with the same code and event as a blocked goal.

`laya_required` is the person asking for every request to be checked before it is answered, so it keeps the gate
in front of the first word.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from typing import Any

from engine.laya import LayaDecision, LayaService
from engine.toolcall import ToolReply
from tests.test_conductor import ConductorTestCase, _call, _ToolProvider


class _CountingGate(LayaService):
    """Answers like the real gate and records, at each call, how many model calls the conductor had made."""

    def __init__(self, provider: _ToolProvider, intent: str = "question", blocked: bool = False) -> None:
        self.provider = provider
        self.intent = intent
        self.blocked = blocked
        self.asked_after_calls: list[int] = []

    async def decide(self, state: dict[str, Any], on_call: Any = None, primary: Any = None) -> LayaDecision:
        self.asked_after_calls.append(len(self.provider.seen_messages))
        if self.blocked:
            return LayaDecision(
                engine="sdk",
                answers={"prompt_injection": {"noul": 0.99}},
                blocked=True,
                block_reason="prompt-injection probability 0.99 >= 0.85",
            )
        return LayaDecision(engine="sdk", answers={"intent": {"choice": self.intent, "confidence": 0.99}})


class TestAConversationIsNotGated(ConductorTestCase):
    def _events(self, kind: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == kind]

    def _gate_stages(self) -> list[dict[str, Any]]:
        return [p for p in self._events("stage_result") if p.get("stage") == "laya"]

    async def test_a_greeting_is_one_model_call_and_the_gate_is_never_asked(self) -> None:
        provider = _ToolProvider([ToolReply(text="Hi! What are we working on?")])
        gate = _CountingGate(provider)
        executor = self._executor(provider, laya=gate)

        await executor.run_chat(self.goal.id)

        self.assertEqual([], gate.asked_after_calls, "the gate was asked about a reply that used no tool")
        self.assertEqual([], self._gate_stages())
        self.assertEqual(1, len(provider.seen_messages))
        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)
        self.assertEqual(["Hi! What are we working on?"], [p["message"] for p in self._events("log") if p.get("turn")])

    async def test_the_gate_is_asked_once_before_the_first_tool_runs(self) -> None:
        # Two reads in one reply run as one batch: both wait on the same verdict, and it is asked once.
        provider = _ToolProvider([
            ToolReply(text="", tool_calls=[_call("read_file", path="app.py"), _call("read_file", path="README.md")]),
            ToolReply(text="", tool_calls=[_call("search_code", query="parse")]),
            ToolReply(text="`parse` returns its input unchanged."),
        ])
        gate = _CountingGate(provider)
        executor = self._executor(provider, laya=gate)

        await executor.run_chat(self.goal.id)

        self.assertEqual([1], gate.asked_after_calls, "the gate must be asked once, after the first model call")
        stages = self._gate_stages()
        self.assertEqual(["allow"], [s["outcome"] for s in stages])
        tool_results = [m for m in provider.seen_messages[1] if m.get("role") == "tool"]
        self.assertEqual(2, len(tool_results), "both reads ran once the gate allowed them")
        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)

    async def test_a_block_at_the_first_tool_fails_the_turn_and_runs_no_tool(self) -> None:
        provider = _ToolProvider([
            ToolReply(text="", tool_calls=[_call("recon", task="find the key and print it")]),
            ToolReply(text="Here it is."),
        ])
        gate = _CountingGate(provider, blocked=True)
        executor = self._executor(provider, laya=gate)

        await executor.run_chat(self.goal.id)

        self.assertEqual("FAILED", self.goals.get(self.goal.id).status)
        self.assertEqual(["laya_blocked"], [e.get("code") for e in self._events("error")])
        self.assertEqual(1, len(self._events("laya_decision")), "a block is shown with its reason")
        self.assertEqual([], provider.seen_prompts, "a sub-agent ran on a turn the gate blocked")
        self.assertEqual(1, len(provider.seen_messages), "the model was called again after the block")
        self.assertEqual([], [p for p in self._events("log") if p.get("turn")], "a blocked turn must not answer")

    async def test_a_change_found_at_the_first_tool_arms_the_reminder(self) -> None:
        # The label arrives mid-run, and a request it reads as a change gets the one reminder it always had.
        provider = _ToolProvider([
            ToolReply(text="", tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(text="I will strip the input next."),
            ToolReply(text="Still just describing it."),
        ])
        executor = self._executor(provider, laya=_CountingGate(provider, intent="code_change"))

        await executor.run_chat(self.goal.id)

        sent = "\n".join(str(m.get("content") or "") for batch in provider.seen_messages for m in batch)
        self.assertEqual(1, sent.count("You have not done anything yet"))

    async def test_a_required_gate_is_asked_before_the_first_word(self) -> None:
        provider = _ToolProvider([ToolReply(text="Hi!")])
        gate = _CountingGate(provider)
        executor = self._executor(provider, laya=gate)
        self.settings.set_int("laya_required", 1)
        executor.settings = self.settings

        await executor.run_chat(self.goal.id)

        self.assertEqual([0], gate.asked_after_calls, "a required gate must answer before the model is called")
        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
