"""The conductor's model calls are booked and traced like every other role's.

Every model call the engine makes is meant to be a `usage` event, a failure is an `agent_call_failed`, and a
goal that asked to be recorded holds the call in its trace. The conductor's loop calls
`provider.complete_with_tools` directly and so bypassed all three: a conductor-driven goal that spent a
dozen calls showed no conductor spend in `/goals/{id}/usage`, the Stats rollup or the trace, and the audit's
"silent role" check flagged `scribe` (whose configuration the conductor borrows) on every turn, because the
conductor's `agent_assigned` was never followed by a `usage` event under that name.

The test double reports usage through the provider's own `_report_usage`, the path a real provider takes, so
what is asserted is that the sink is attached around the call and not merely that a method exists.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest import mock

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import _silent_roles, _usage_from_events
from engine.conductor import TOOLS, Conductor
from engine.providers import ProviderError
from engine.toolcall import ToolReply, ToolSpec
from engine.trace import TraceService
from tests.test_conductor import ConductorTestCase, _call, _DyingProvider, _ToolProvider


def _usage_block() -> dict[str, Any]:
    return {"usage": {"prompt_tokens": 100, "completion_tokens": 20}}


class _Metered(_ToolProvider):
    """A scripted model that reports usage on every call, as a real provider does."""

    async def complete_with_tools(
        self, system_prompt: str, messages: list[dict[str, Any]],
        tools: list[ToolSpec], model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> ToolReply:
        reply = await super().complete_with_tools(
            system_prompt, messages, tools, model, temperature, max_tokens,
            num_ctx=num_ctx, keep_alive=keep_alive,
        )
        self._report_usage("openai_compat", _usage_block())
        return reply


class _MeteredDying(_DyingProvider):
    async def complete_with_tools(
        self, system_prompt: str, messages: list[dict[str, Any]],
        tools: list[ToolSpec], model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> ToolReply:
        reply = await super().complete_with_tools(
            system_prompt, messages, tools, model, temperature, max_tokens,
            num_ctx=num_ctx, keep_alive=keep_alive,
        )
        self._report_usage("openai_compat", _usage_block())
        return reply


class TestEveryConductorCallIsBooked(ConductorTestCase):
    def _events(self, kind: str) -> list[Any]:
        return [e for e in self.goals.events_after(self.goal.id, 0) if e.type == kind]

    async def _run_two_calls(self) -> _Metered:
        provider = _Metered([
            ToolReply(text="", tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(text="It parses."),
        ])
        await self._executor(provider).run_chat(self.goal.id)
        return provider

    async def test_n_calls_are_n_usage_events_under_the_conductor(self) -> None:
        await self._run_two_calls()

        booked = [e.payload for e in self._events("usage") if e.payload.get("role") == "conductor"]

        self.assertEqual(2, len(booked), booked)
        for payload in booked:
            self.assertEqual("stub-model", payload["model"])
            self.assertTrue(payload["provider"], "a usage event naming no provider is the same dishonesty")
            self.assertEqual(100, payload["input_tokens"])
            self.assertEqual(20, payload["output_tokens"])
            self.assertIsInstance(payload["duration_ms"], int)

    async def test_the_usage_document_has_a_conductor_bucket(self) -> None:
        await self._run_two_calls()

        document = _usage_from_events(list(self.goals.events_after(self.goal.id, 0)))

        self.assertEqual(2, document["by_role"]["conductor"]["calls"])
        self.assertEqual(240, document["by_role"]["conductor"]["total_tokens"])
        self.assertEqual(240, document["totals"]["total_tokens"])

    async def test_the_sink_does_not_outlive_the_loop(self) -> None:
        # The provider object is shared; a sink left behind would book a later role's call as the
        # conductor's.
        provider = await self._run_two_calls()

        self.assertIsNone(provider.usage_sink)

    async def test_a_conductor_turn_does_not_make_scribe_a_silent_role(self) -> None:
        await self._run_two_calls()
        events = list(self.goals.events_after(self.goal.id, 0))
        assigned = [e for e in events if e.type == "agent_assigned"]
        self.assertTrue(assigned, "the loop announces itself")

        silent = _silent_roles("COMPLETED", events, _usage_from_events(events))

        self.assertEqual([], silent)

    def test_a_real_silent_role_is_still_reported(self) -> None:
        # The control for the one above: the exemption is for the conductor's own announcement and nothing else.
        class _Event:
            def __init__(self, type_: str, payload: dict[str, Any]) -> None:
                self.type, self.payload = type_, payload

        events = [_Event("agent_assigned", {"role": "scribe", "provider": "p", "model": "m"})]

        silent = _silent_roles("COMPLETED", events, {"by_role": {}})

        self.assertEqual(["scribe"], [s["role"] for s in silent])


class TestAFailedCallIsRecorded(ConductorTestCase):
    def _loop(
        self, primary: _ToolProvider, fallback: _ToolProvider | None, goal_id: str,
    ) -> Conductor:
        executor = self._executor(primary)
        cfg = executor.orchestrator.registry.get_config("scribe")
        primary_cfg = cfg.model_copy(update={"provider": "primary-slug"})
        fallback_cfg = cfg.model_copy(update={"provider": "fallback-slug"})
        targets: list[tuple[Any, str, Any]] = [(primary, "primary-model", primary_cfg)]
        if fallback is not None:
            targets.append((fallback, "fallback-model", fallback_cfg))
        return Conductor(
            primary, "primary-model", str(self.repo),
            [t for t in TOOLS if t.name == "read_file"],
            {"read_file": self._read},
            system_prompt="s",
            fallback=(fallback, "fallback-model") if fallback else None,
            ledger=executor.orchestrator.tool_call_ledger(goal_id, None, targets),
        )

    @staticmethod
    async def _read(args: dict[str, Any]) -> str:
        return "def parse(text): ..."

    async def test_a_dead_primary_is_an_agent_call_failed_and_the_fallback_is_booked_as_itself(self) -> None:
        primary = _MeteredDying(fail_on={1: "provider_unreachable"})
        fallback = _Metered([ToolReply(text="answered by the fallback")])

        answer = await self._loop(primary, fallback, self.goal.id).run("go")

        self.assertEqual("answered by the fallback", answer)
        events = list(self.goals.events_after(self.goal.id, 0))
        failed = [e.payload for e in events if e.type == "agent_call_failed"]
        self.assertEqual(1, len(failed), failed)
        self.assertEqual("conductor", failed[0]["role"])
        self.assertEqual("primary-slug", failed[0]["provider"])
        self.assertEqual("primary-model", failed[0]["model"])
        self.assertEqual("primary", failed[0]["target"])
        self.assertEqual("provider_unreachable", failed[0]["code"])
        booked = [e.payload for e in events if e.type == "usage"]
        self.assertEqual(1, len(booked), "the failed call spent nothing the provider reported")
        self.assertEqual("fallback-slug", booked[0]["provider"], "spend belongs to the target that served it")
        self.assertEqual("fallback-model", booked[0]["model"])
        self.assertEqual("conductor", booked[0]["role"])

    async def test_the_failure_is_booked_even_when_it_ends_the_loop(self) -> None:
        primary = _MeteredDying(fail_on={1: "provider_unreachable"})

        with self.assertRaises(ProviderError):
            await self._loop(primary, None, self.goal.id).run("go")

        failed = [e.payload for e in self.goals.events_after(self.goal.id, 0)
                  if e.type == "agent_call_failed"]
        self.assertEqual(["provider_unreachable"], [f["code"] for f in failed])
        self.assertIsNone(primary.usage_sink, "a failed call must still release the sink")

    async def test_a_loop_with_no_ledger_still_runs(self) -> None:
        # Every caller before this existed built a Conductor without one.
        provider = _Metered([ToolReply(text="fine")])

        answer = await Conductor(
            provider, "m", str(self.repo), [], {}, system_prompt="s",
        ).run("go")

        self.assertEqual("fine", answer)
        self.assertEqual([], [e for e in self.goals.events_after(self.goal.id, 0) if e.type == "usage"])


class TestATracedGoalRecordsTheConductor(ConductorTestCase):
    async def _run(self, trace: bool) -> TraceService:
        traces = TraceService(self.conn)
        if trace:
            self.goals.set_trace(self.goal.id, True)
        provider = _Metered([
            ToolReply(text="", tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(text="It parses."),
        ])
        executor = self._executor(provider)
        executor.orchestrator.tracer = traces
        with mock.patch.dict(os.environ, {"CODIFY_TRACE_PROMPTS": "1"}):
            await executor.run_chat(self.goal.id)
        return traces

    async def test_a_traced_goal_holds_one_row_per_conductor_call(self) -> None:
        traces = await self._run(trace=True)

        rows = [c for c in traces.calls(self.goal.id) if c["role"] == "conductor"]

        self.assertEqual(2, len(rows), [c["role"] for c in traces.calls(self.goal.id)])
        self.assertEqual("stub-model", rows[0]["model"])
        self.assertEqual(100, rows[0]["input_tokens"])
        self.assertIn("read_file", rows[0]["response"], "the call the model made is the response")
        self.assertIn("It parses.", rows[1]["response"])

    async def test_a_row_holds_what_the_model_was_newly_shown_not_the_whole_history_again(self) -> None:
        # Resending every earlier message in every row is quadratic, and the rows in order already hold the
        # whole conversation: the first the prompt, the second the tool result.
        traces = await self._run(trace=True)

        rows = [c for c in traces.calls(self.goal.id) if c["role"] == "conductor"]

        # Parsed, not searched: the prompt is JSON, so its text arrives escaped and a substring test
        # against the raw question would pass whether or not the question was repeated.
        first = json.loads(rows[0]["user_prompt"])
        second = json.loads(rows[1]["user_prompt"])
        # The first row is the conversation so far and the new prompt; nothing has been called yet.
        self.assertEqual("user", first[-1]["role"])
        self.assertNotIn("tool", [m["role"] for m in first])
        self.assertEqual(["tool"], [m["role"] for m in second], "only what is new since the last call")
        self.assertIn("def parse", second[0]["content"], "the tool result is what the second call was shown")

    async def test_an_untraced_goal_records_nothing(self) -> None:
        traces = await self._run(trace=False)

        self.assertEqual(0, traces.count(self.goal.id))
