"""The parallel-steps spike runs, and what it measures is true (docs/08 "The parallel-steps spike").

The spike exists so that nobody builds conductor-driven parallel steps on a guess. These keep it honest: it drives
the engine's own step loop, a plan the batching gate refuses really does run as slowly as a sequential one, and
running steps together never changes the outcome (every step completed, one commit each, the same files).

Timings use a latency large enough to dwarf the fixed cost of a commit, so the ratios hold on a loaded machine. The
gate's refusal is also *counted*: `max_concurrency` is the most model calls in flight at once, so a shared runner's
clock cannot make a serialized run look parallel (A is measured first, and pays a cold disk's one-time cost).
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from benchmarks.parallel_spike import render, run_scenario

STEPS = 3
LATENCY = 0.1


class TestParallelSpike(unittest.TestCase):
    """The three scenarios run once for the class: each is seconds of real waiting, and the tests only read them."""

    a: dict[str, Any]
    b: dict[str, Any]
    c: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        async def run_all() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)

                async def one(label: str, *, parallel: bool, overlapping: bool) -> dict[str, Any]:
                    return await run_scenario(
                        label, root, steps=STEPS, latency=LATENCY, driver="recipe",
                        parallel=parallel, overlapping=overlapping,
                    )

                cls.a = await one("A sequential", parallel=False, overlapping=False)
                cls.b = await one("B parallel disjoint", parallel=True, overlapping=False)
                cls.c = await one("C parallel overlapping", parallel=True, overlapping=True)

        asyncio.run(run_all())

    def test_every_configuration_finishes_the_whole_plan_with_one_commit_per_step(self) -> None:
        for row in (self.a, self.b, self.c):
            self.assertEqual("COMPLETED", row["goal_status"], row["scenario"])
            self.assertEqual(STEPS, row["steps_completed"], row["scenario"])
            self.assertEqual(STEPS, row["commits_by_run"], f"{row['scenario']}: no step committed twice or not at all")

    def test_running_steps_together_changes_how_long_not_what_was_written(self) -> None:
        self.assertEqual(self.a["files_written"], self.b["files_written"])
        self.assertEqual(self.a["files_written"], self.c["files_written"])
        self.assertEqual(self.a["model_calls"], self.b["model_calls"], "the same work, not less of it")

    def test_disjoint_steps_run_in_parallel(self) -> None:
        self.assertGreater(
            self.b["max_concurrency"], 1,
            "no two steps were ever in flight together: the disjoint batch never ran in parallel",
        )
        a, b = float(str(self.a["wall_s"])), float(str(self.b["wall_s"]))
        self.assertLess(b, a * 0.8, f"3 disjoint steps took {b:.2f}s against {a:.2f}s sequentially")

    def test_overlapping_steps_are_refused_by_the_gate_and_cost_what_sequential_costs(self) -> None:
        # The proof is the overlap itself, not the clock. A wall-clock ratio could not tell a real overlap from A
        # running cold first: both make C look faster than A. One call in flight at the peak is the gate refusing.
        self.assertEqual(
            1, self.a["max_concurrency"],
            "the sequential baseline had two model calls in flight at once: the counter measures nothing",
        )
        self.assertEqual(
            1, self.c["max_concurrency"],
            f"two steps' model calls were in flight at once ({self.c['max_concurrency']} at the peak): "
            "the gate let overlapping paths overlap",
        )
        a, c = float(str(self.a["wall_s"])), float(str(self.c["wall_s"]))
        self.assertLess(c, a * 2, f"overlapping steps took {c:.2f}s against {a:.2f}s: the refusal must degrade to "
                                  "sequential, not to something slower")

    def test_the_conductor_driver_is_what_runs_when_asked_for(self) -> None:
        async def go() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                return await run_scenario(
                    "D conductor", Path(tmp), steps=2, latency=0.02, driver="conductor",
                    parallel=False, overlapping=False,
                )

        row = asyncio.run(go())
        self.assertEqual("COMPLETED", row["goal_status"])
        self.assertEqual(2, row["steps_completed"])
        self.assertGreater(row["model_calls"], 2 * 4, "a conductor makes its moves one model call at a time")

    def test_the_report_names_every_scenario(self) -> None:
        text = render({
            "steps": STEPS, "latency_s": LATENCY, "scenarios": [self.a, self.b, self.c],
            "speedup_B_over_A": 1.0, "speedup_C_over_A": 1.0, "ceiling_conductor_parallel_s": 0.0,
        })
        for label in ("A sequential", "B parallel disjoint", "C parallel overlapping"):
            self.assertIn(label, text)
