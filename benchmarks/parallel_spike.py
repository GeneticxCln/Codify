"""Measure before building: what would conductor-driven parallel steps buy? (docs/08 §parallel spike)

Today a plan's steps run one at a time under the conductor. Steps can run together only on the recipe driver, for a
`parallel` goal, and only when the planner named paths that provably do not overlap (`_independent_batch`). A
conductor-driven version would have to be built on the same footing, so before building it this measures what the
footing is worth:

* **A — recipe, sequential**: the baseline for the recipe.
* **B — recipe, parallel, disjoint paths**: the best case the batching gate admits.
* **C — recipe, parallel, overlapping paths**: the gate's refusal; it must degrade to A, not to something slower.
* **D — conductor, sequential**: what a person's approved plan gets today.

Everything here is hermetic: no network, no model, no spend. Each model call sleeps `--latency` seconds, which is the
only thing that makes the numbers mean anything, so **the output is about scheduling, never about quality**: a
scripted provider cannot say whether a model would split a task into disjoint steps, and that, not the scheduler,
is the open question. What it does settle is the ceiling: if B is not clearly faster than A with latency that is
all model time, a conductor-driven version cannot be either, and nothing should be built.

It drives the engine's own `_run_steps_locked` (the loop `POST /goals/{id}/start` runs), not a copy of it.

    python3 -m benchmarks.parallel_spike --steps 4 --latency 0.05
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from benchmarks.provider import CannedProvider
from benchmarks.runner import SkippedGate, _files, _start_repository, materialize
from engine.app import _run_steps_locked
from engine.db import connect
from engine.executor import ExecutorService
from engine.git import GitService
from engine.models import ROLES, AgentConfig, AgentConfigUpdate, GoalCreate, WorkspaceCreate
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, SettingsService, WorkspaceService
from engine.toolcall import ToolReply, ToolSpec

_STEP_LINE = re.compile(r"^Step: Write (\S+)", re.MULTILINE)


class _Script:
    """What the scripted model says, and how long every call takes. Shared by every role's provider."""

    def __init__(self, steps: int, latency: float, *, overlapping: bool, conductor: bool) -> None:
        self.steps = steps
        self.latency = latency
        self.overlapping = overlapping
        self.conductor = conductor
        self.calls = 0
        # The conductor script is stateless (it rebuilds its next move from the transcript), so one instance serves.
        self.conductor_script = CannedProvider([], conductor=True)
        self.other = CannedProvider([])

    def path(self, i: int) -> str:
        return f"f{i}.txt"

    def footprint(self, i: int) -> str:
        """The path the plan *declares* for step i. The gate batches on this, so overlapping plans all name one file
        while each step still writes a file of its own: the run stays checkable and the gate is what is measured."""
        return "shared.txt" if self.overlapping else self.path(i)

    def reply(self, role: str, user_prompt: str) -> dict[str, Any]:
        if role == "planner":
            return {"steps": [
                {"title": f"Write {self.path(i)}", "description": "Create the file.", "suggested_paths": [self.footprint(i)]}
                for i in range(self.steps)
            ]}
        if role == "fixer":
            found = _STEP_LINE.search(user_prompt)
            path = found.group(1) if found else self.path(0)
            return {"files": [{"path": path, "action": "create", "content": f"{path}\n"}]}
        return self.other._reply(role)


class _RoleProvider(BaseProvider):
    """One role's provider, made fresh per `build`: its role and usage sink are its own, so concurrent steps cannot
    overwrite each other's (the shared canned provider keeps `current_role` on itself, which is one-at-a-time)."""

    def __init__(self, script: _Script, role: str) -> None:
        self.script = script
        self.role = role

    @property
    def supports_tools(self) -> bool:
        return self.script.conductor

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
        json_schema: dict[str, Any] | None = None, thinking_effort: str | None = None,
    ) -> str:
        await asyncio.sleep(self.script.latency)
        self.script.calls += 1
        if self.usage_sink is not None:
            self.usage_sink({"input_tokens": 100, "output_tokens": 40, "total_tokens": 140})
        return json.dumps(self.script.reply(self.role, user_prompt))

    async def complete_with_tools(
        self, system_prompt: str, messages: list[dict[str, Any]], tools: list[ToolSpec], model: str,
        temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None, cache_prefix: bool = False,
    ) -> ToolReply:
        await asyncio.sleep(self.script.latency)
        self.script.calls += 1
        inner = self.script.conductor_script
        inner.usage_sink = self.usage_sink
        return await inner.complete_with_tools(
            system_prompt, messages, tools, model, temperature, max_tokens, num_ctx=num_ctx, keep_alive=keep_alive,
        )


class _Factory(ProviderFactory):
    def __init__(self, script: _Script) -> None:
        super().__init__(Keychain())
        self.script = script

    def build(self, config: AgentConfig) -> BaseProvider:
        return _RoleProvider(self.script, config.role)


async def run_scenario(
    label: str, work_root: Path, *, steps: int, latency: float, driver: str, parallel: bool, overlapping: bool,
) -> dict[str, Any]:
    """One configuration, planned then executed; the wall clock is the execution only (planning is the same in all)."""
    slug = re.sub(r"\W+", "-", label.lower()).strip("-")
    workspace = work_root / slug
    materialize("synthetic-repo", workspace)
    _start_repository(workspace)
    before = _files(workspace)
    conn = connect(work_root / f"{slug}.db")
    try:
        script = _Script(steps, latency, overlapping=overlapping, conductor=driver == "conductor")
        registry = AgentRegistryService(conn, _Factory(script), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(provider="ollama", model_name="spike"))
        goals, workspaces = GoalService(conn), WorkspaceService(conn)
        ws = workspaces.create(WorkspaceCreate(name=slug, root_path=str(workspace)))
        goal = goals.create(GoalCreate(
            workspace_id=ws.id, title="spike", description="write the files", parallel=parallel,
        ))
        executor = ExecutorService(goals, workspaces, registry, SandboxService(), laya=SkippedGate())
        executor.settings = SettingsService(conn)
        if (driver == "conductor") != executor.conductor_can_drive(goal.id):
            raise RuntimeError(f"{label}: the engine would drive this with the other driver than {driver!r}")

        await executor.run_planning(goal.id)
        if goals.get(goal.id).status != "RUNNING":
            goals.update_status(goal.id, goals.get(goal.id).version, "RUNNING")
        planned = len(goals.steps(goal.id))
        calls_before = script.calls

        app = SimpleNamespace(state=SimpleNamespace(executor=executor, goals=goals))
        began = time.monotonic()
        await _run_steps_locked(app, goal.id)  # type: ignore[arg-type]  # the two attributes it reads, nothing else
        wall = time.monotonic() - began

        done = [s for s in goals.steps(goal.id) if s.status == "COMPLETED"]
        # `read_only` frames its answer with a `$ git ... (exit N)` header; the rest is one line per commit.
        log = GitService().read_only(str(workspace), ["log", "--oneline"]).splitlines()
        commits = len([line for line in log if not line.startswith("$ ")])
        written = sorted(_files(workspace) - before)
        return {
            "scenario": label, "driver": driver, "parallel": parallel, "overlapping": overlapping,
            "wall_s": round(wall, 3), "steps_planned": planned, "steps_completed": len(done),
            "goal_status": goals.get(goal.id).status, "model_calls": script.calls - calls_before,
            # +1 for the fixture commit `_start_repository` makes.
            "commits_by_run": commits - 1, "files_written": written,
        }
    finally:
        conn.close()


async def run_spike(steps: int, latency: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codify-parallel-spike-") as tmp:
        root = Path(tmp)
        rows = [
            await run_scenario("A recipe sequential", root, steps=steps, latency=latency,
                               driver="recipe", parallel=False, overlapping=False),
            await run_scenario("B recipe parallel, disjoint", root, steps=steps, latency=latency,
                               driver="recipe", parallel=True, overlapping=False),
            await run_scenario("C recipe parallel, overlapping", root, steps=steps, latency=latency,
                               driver="recipe", parallel=True, overlapping=True),
            await run_scenario("D conductor sequential", root, steps=steps, latency=latency,
                               driver="conductor", parallel=False, overlapping=False),
        ]
    by = {r["scenario"][0]: r for r in rows}
    return {
        "steps": steps, "latency_s": latency,
        "scenarios": rows,
        "speedup_B_over_A": round(by["A"]["wall_s"] / by["B"]["wall_s"], 2),
        "speedup_C_over_A": round(by["A"]["wall_s"] / by["C"]["wall_s"], 2),
        # What conductor-driven parallel steps could at most do for a plan the gate admits: the conductor's
        # sequential time divided by the speedup the recipe already gets from the same gate.
        "ceiling_conductor_parallel_s": round(by["D"]["wall_s"] / (by["A"]["wall_s"] / by["B"]["wall_s"]), 3),
    }


def render(report: dict[str, Any]) -> str:
    lines = [f"parallel-steps spike: {report['steps']} steps, {report['latency_s']}s per model call", ""]
    lines.append(f"{'scenario':34} {'wall':>8} {'steps':>7} {'calls':>6} {'commits':>8}  status")
    for r in report["scenarios"]:
        lines.append(
            f"{r['scenario']:34} {r['wall_s']:>7.2f}s {r['steps_completed']:>3}/{r['steps_planned']:<3} "
            f"{r['model_calls']:>6} {r['commits_by_run']:>8}  {r['goal_status']}"
        )
    lines += [
        "",
        f"B over A (best case the gate admits): {report['speedup_B_over_A']}x",
        f"C over A (the gate refusing):         {report['speedup_C_over_A']}x  (1.0 = degraded to sequential, as it should)",
        f"D, if it got B's speedup:             {report['ceiling_conductor_parallel_s']}s",
        "",
        "This is scheduling only. It cannot say whether a model plans disjoint steps; see docs/08.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--latency", type=float, default=0.05, help="seconds each scripted model call takes")
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report here")
    args = parser.parse_args(argv)
    report = asyncio.run(run_spike(args.steps, args.latency))
    print(render(report))
    if args.report is not None:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
