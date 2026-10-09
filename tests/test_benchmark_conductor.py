"""The benchmark can drive the conductor, which is what a person's goal gets.

Until this existed every number the harness produced was about the fixed recipe, and the default execution path of the
product (`conductor_can_drive` is true on any install whose scribe can call tools) had never been run by a benchmark at
all. The smoke tier runs it with a scripted conductor: that proves the path's plumbing, an approved plan taken through
`write`, `verify`, `review` and `summarize` to COMPLETED with bytes on disk, and says nothing about whether a model
could do the same.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests import hermetic  # noqa: F401

from benchmarks.provider import APPROVED_STEP, STEP_MOVES, CannedProvider
from benchmarks.runner import BenchmarkError, load_manifest, run_task
from engine.executor import ExecutorService
from engine.toolcall import ToolCall, ToolSpec

SMOKE = [t for t in load_manifest()["tasks"] if t.get("tier") == "smoke"]


def _tools(*names: str) -> list[ToolSpec]:
    return [ToolSpec(name=n, description=n, parameters={"type": "object", "properties": {}}) for n in names]


def _step_prompt() -> list[dict[str, Any]]:
    return [{"role": "user", "content": f"{APPROVED_STEP}Add a banner\n\nYou are working on one step only: step s-1, 'Add'.\n"}]


def _ask(provider: CannedProvider, messages: list[dict[str, Any]], offered: tuple[str, ...] = STEP_MOVES) -> Any:
    return asyncio.run(provider.complete_with_tools("", messages, _tools(*offered), "m", 0.0, 100))


class TestTheScriptedConductor(unittest.TestCase):
    def test_it_asks_for_the_moves_in_order_and_then_says_the_step_is_done(self) -> None:
        provider = CannedProvider(conductor=True)
        messages = _step_prompt()
        seen = []
        for _ in range(len(STEP_MOVES)):
            reply = _ask(provider, messages)
            self.assertEqual(1, len(reply.tool_calls))
            call = reply.tool_calls[0]
            seen.append(call.name)
            self.assertEqual("s-1", call.arguments["step_id"])
            messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
        self.assertEqual(list(STEP_MOVES), seen)
        done = _ask(provider, messages)
        self.assertEqual([], done.tool_calls)
        self.assertIn("done", done.text)

    def test_a_move_it_was_not_offered_ends_the_run_instead_of_being_called(self) -> None:
        """The property that keeps a scripted run honest: it never calls what the engine did not offer."""
        reply = _ask(CannedProvider(conductor=True), _step_prompt(), offered=("verify", "review", "summarize"))
        self.assertEqual([], reply.tool_calls)
        self.assertIn("write", reply.text)

    def test_it_has_no_script_for_anything_but_an_approved_step(self) -> None:
        reply = _ask(CannedProvider(conductor=True), [{"role": "user", "content": "The user says: hello"}])
        self.assertEqual([], reply.tool_calls)

    def test_only_a_conductor_provider_claims_to_call_tools(self) -> None:
        """Otherwise every canned run, recipe included, would be handed to the conductor by the engine."""
        self.assertTrue(CannedProvider(conductor=True).supports_tools)
        self.assertFalse(CannedProvider().supports_tools)

    def test_it_reads_the_calls_it_already_made_from_either_shape_of_transcript(self) -> None:
        messages = _step_prompt() + [
            {"role": "assistant", "content": "", "tool_calls": [ToolCall(id="1", name="write", arguments={})]},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "verify"}]},
        ]
        self.assertEqual("review", _ask(CannedProvider(conductor=True), messages).tool_calls[0].name)


class TestTheConductorDriver(unittest.TestCase):
    def _run(self, task: dict[str, Any], driver: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            return asyncio.run(run_task(task, Path(tmp), canned=True, driver=driver))

    def test_every_smoke_task_is_taken_to_the_end_by_the_conductor(self) -> None:
        for task in SMOKE:
            with self.subTest(task=task["id"]):
                result = self._run(task, "conductor")
                self.assertEqual("COMPLETED", result["status"], result["failure"])
                self.assertTrue(result["passed"], json.dumps(result["checks"]))
                self.assertEqual("conductor", result["driver"])
                # One call per move, and one more to hear that the step is done: the proof that the conductor,
                # and not the recipe behind it, is what moved the step.
                self.assertEqual(len(STEP_MOVES) + 1, result["conductor_calls"])

    def test_the_recipe_driver_never_asks_the_conductor_for_anything(self) -> None:
        result = self._run(SMOKE[0], "recipe")
        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual(0, result["conductor_calls"])

    def test_the_conductor_writes_through_the_write_gate_and_the_files_reach_the_disk(self) -> None:
        task = SMOKE[0]
        result = self._run(task, "conductor")
        self.assertEqual("passed", next(c["status"] for c in result["checks"] if c["type"] == "files_written"))

    def test_a_setup_that_cannot_run_the_conductor_is_refused_not_quietly_run_as_the_recipe(self) -> None:
        """The engine falls back to the recipe when it has no conductor; a benchmark that did so would mislabel itself."""
        with mock.patch.object(ExecutorService, "conductor_can_drive", return_value=False):
            with self.assertRaises(BenchmarkError) as caught:
                self._run(SMOKE[0], "conductor")
        self.assertIn("--driver recipe", str(caught.exception))

    def test_an_unknown_driver_is_named(self) -> None:
        with self.assertRaises(BenchmarkError) as caught:
            self._run(SMOKE[0], "magic")
        self.assertIn("magic", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
