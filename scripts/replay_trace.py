#!/usr/bin/env python3
"""Replay a goal's recorded model calls, with no provider in the loop.

A recording is only a recording until something can run it. This is that
something: it rebuilds the conditions the run happened under, hands the engine
a `ReplayProvider` instead of a factory, and reports whether the run landed
where it did the first time.

Three rules it follows, and each one is the difference between a replay and a
story:

* **Nothing is written to your workspace.** The tree is copied into a scratch
  directory first and the replay goal is pointed at the copy, so the fixer
  writing files inside a replay cannot touch the code that recording came
  from. The scratch directory is kept and printed, so a divergence can be
  inspected rather than guessed at.
* **The gate is replayed, not re-run.** The Laya verdict the run got is read
  back from its `laya_decision` event. Re-running it would make a live
  decision the recording never saw, and would change what the run did before
  the first model call even happens.
* **A mismatch stops and is reported.** `ReplayProvider` refuses a prompt its
  recording does not hold rather than answer a different question. The exit
  code says which happened: 0 the recording replayed, 1 it diverged, 2 the
  recording could not be read at all.

Usage:
    python3 scripts/replay_trace.py --goal <goal-id> [--db PATH] \
            [--from TREE] [--into DIR]

`--from` is the one that matters for a run that changed anything: it names the
tree as it was when the run started, which is the tree the prompts ask about.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from engine.db import connect, default_db_path
from engine.executor import ExecutorService
from engine.laya import LayaDecision, LayaService
from engine.models import (
    ROLES,
    AgentConfig,
    AgentConfigUpdate,
    GoalCreate,
    WorkspaceCreate,
)
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, ApiError, GoalService, WorkspaceService
from engine.trace import ReplayProvider, TraceMismatch, TraceService

EXIT_OK = 0
EXIT_DIVERGED = 1
EXIT_UNUSABLE = 2


class _ReplayedGate(LayaService):
    """Answers with the verdict the recording got, rather than a fresh one.

    The gate runs before any model call, so a live decision here would change
    the run's shape before the replay had served anything — a blocked recording
    would sail past the block, and an unblocked one could stop short of it.
    """

    def __init__(self, verdict: LayaDecision) -> None:
        super().__init__(registry=None)
        self._verdict = verdict

    async def decide(self, state: dict[str, Any]) -> LayaDecision:  # noqa: D102
        return self._verdict


class _ReplayFactory(ProviderFactory):
    """Hands every role the one provider that answers from the recording.

    `current_role` is set from the `AgentConfig` the engine asked for — the
    same signal the real providers get — because a keyword scan of the prompt
    would misread one role's script for another's.
    """

    def __init__(self, provider: ReplayProvider) -> None:
        super().__init__(Keychain())
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        self.provider.current_role = config.role
        return self.provider


def _gate_verdict(events: list[Any]) -> LayaDecision:
    """The recorded gate's decision, or "skipped" when it never ran.

    Read from the event log rather than recomputed: this is a replay, and the
    gate's answer is part of what happened rather than something to decide
    again.
    """
    for event in reversed(events):
        if event.type != "laya_decision":
            continue
        payload = dict(event.payload)
        return LayaDecision(
            engine=str(payload.get("engine") or "skipped"),
            blocked=bool(payload.get("blocked")),
            block_reason=payload.get("block_reason"),
            skipped_reason=payload.get("skipped_reason"),
        )
    return LayaDecision(engine="skipped", skipped_reason="replay: gate did not run")


def _stage_sequence(events: list[Any]) -> list[tuple[str, str]]:
    """`(stage, outcome)` for every stage the run took, in order."""
    return [
        (str(e.payload.get("stage")), str(e.payload.get("outcome")))
        for e in events
        if e.type == "stage_result"
    ]


async def replay(
    conn: Any,
    goal_id: str,
    source: Path | None = None,
    into: Path | None = None,
) -> dict[str, Any]:
    """Replay one recorded goal into a scratch copy of a workspace tree.

    `source` is the tree the recording's prompts were built from — **the tree
    as it was when the run started**. It defaults to the recorded goal's own
    workspace, which is only still that tree if the run wrote nothing. A run
    that fixed a file left the workspace one line ahead of its own prompts, so
    replaying it wants a pristine copy here (`--from`), and refuses rather than
    answering a question the recording never held the answer to.

    Returns a report; raises `ApiError` when the goal or its recording cannot
    be read, and `TraceMismatch` when the run diverges partway through. The
    caller turns those into exit codes — the report itself is the product.
    """
    goals = GoalService(conn)
    workspaces = WorkspaceService(conn)
    traces = TraceService(conn)

    recorded_goal = goals.get(goal_id)  # 404 if unknown
    calls = traces.calls(goal_id)
    if not calls:
        raise ApiError(
            409,
            "nothing_recorded",
            f"goal {goal_id} has no recording — arm it with PUT /goals/{id}/trace "
            "before the run starts",
        )
    recorded_ws = workspaces.get(recorded_goal.workspace_id)
    source = source or Path(recorded_ws.root_path)

    # A byte-identical copy of the tree the recording saw. Not optional: the
    # prompts embed the workspace, and replaying against the live tree would
    # both mutate the user's files and refuse on the first changed line.
    into = into or Path(tempfile.mkdtemp(prefix="codify-replay-"))
    into.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, into, dirs_exist_ok=True)
    else:
        raise ApiError(
            409,
            "source_missing",
            f"the tree to replay against is not a directory: {source}",
        )

    replay_ws = workspaces.create(
        WorkspaceCreate(name=f"replay-{goal_id[:8]}", root_path=str(into.resolve()))
    )
    replay_goal = goals.create(
        GoalCreate(
            workspace_id=replay_ws.id,
            title=recorded_goal.title,
            description=recorded_goal.description,
            dry_run=recorded_goal.dry_run,
            plan_only=recorded_goal.plan_only,
            mode=recorded_goal.mode,
            trace=False,
        )
    )

    provider = ReplayProvider(calls)
    registry = AgentRegistryService(conn, _ReplayFactory(provider), Keychain())
    for role in ROLES:
        registry.set_config(role, AgentConfigUpdate(provider="ollama", model_name="replay"))

    recorded_events = goals.events_after(goal_id, 0)
    executor = ExecutorService(
        goals,
        workspaces,
        registry,
        SandboxService(),
        laya=_ReplayedGate(_gate_verdict(recorded_events)),
        tracer=None,
    )

    diverged: str | None = None
    try:
        await executor.run_planning(replay_goal.id)
        recorded_plan_only = recorded_goal.plan_only
        if not recorded_plan_only:
            steps = goals.steps(replay_goal.id)
            if steps:
                goals.update_status(
                    replay_goal.id, goals.get(replay_goal.id).version, "RUNNING"
                )
            for step in steps:
                await executor.run_step(replay_goal.id, step.id)
    except TraceMismatch as exc:
        diverged = str(exc)

    replay_events = goals.events_after(replay_goal.id, 0)
    final = goals.get(replay_goal.id)
    if diverged is None and final.status == "FAILED":
        # The usual shape of a divergence. `TraceMismatch` is a ProviderError,
        # so the orchestrator absorbs it into `agent_call_failed`, the retries
        # give up, and only then does the goal fail — the refusal arrives as a
        # failed run rather than as an exception out here.
        #
        # The *first* refusal, in order, is the useful one: a run that drifted
        # usually refuses at the librarian and only fails later, at the first
        # stage the goal cannot continue without. Reporting the fatal stage
        # would name the planner for a tree the librarian was the first to
        # see, which sends the reader to the wrong call.
        for event in replay_events:
            if event.type != "agent_call_failed":
                continue
            payload = dict(event.payload)
            if payload.get("code") == "trace_mismatch":
                diverged = str(payload.get("message") or "trace mismatch")
                break
        if diverged is None:
            for event in reversed(replay_events):
                if event.type == "error":
                    payload = dict(event.payload)
                    diverged = str(
                        payload.get("message") or payload.get("code")
                        or "the replay failed"
                    )
                    break
            else:
                diverged = "the replay failed before every recorded call was served"
    stages = _stage_sequence(replay_events)
    return {
        "goal_id": goal_id,
        "replay_goal_id": replay_goal.id,
        "scratch": str(into),
        "source": str(source),
        "recorded_calls": len(calls),
        "served_calls": len(provider.served),
        "stages": stages,
        "status": final.status,
        "diverged": diverged,
        # Every recorded call consumed, and no call the recording lacks was
        # ever asked. Either half failing means the run was not this run.
        "matched": diverged is None and len(provider.served) == len(calls),
    }


def _print(report: dict[str, Any]) -> None:
    print(f"recorded : {report['recorded_calls']} calls")
    print(f"served   : {report['served_calls']} calls")
    print(f"status   : {report['status']}")
    print(f"scratch  : {report['scratch']}")
    if report["stages"]:
        print("stages   :")
        for stage, outcome in report["stages"]:
            print(f"  {stage:<10} {outcome}")
    print(f"source   : {report['source']}")
    if report["diverged"]:
        print(f"\nDIVERGED: {report['diverged']}", file=sys.stderr)
        print(
            "If this run wrote files, re-run with --from pointing at the tree as "
            "it was before it: the prompts were built from that tree.",
            file=sys.stderr,
        )
    elif report["matched"]:
        print("\nMATCH: every recorded call was served, in order.")
    else:
        print(
            f"\nDIVERGED: {report['recorded_calls']} calls recorded but only "
            f"{report['served_calls']} served — the run stopped short of the "
            "recording.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay a goal's recorded model calls without a provider.",
    )
    parser.add_argument("--goal", required=True, help="the goal id to replay")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="engine database (default: the engine's own)",
    )
    parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=None,
        help=(
            "tree to replay against — the workspace as it was when the run "
            "started (default: the recorded goal's own workspace)"
        ),
    )
    parser.add_argument(
        "--into",
        type=Path,
        default=None,
        help="directory to write the replay's copy into (default: a fresh temp dir)",
    )
    args = parser.parse_args(argv)

    db = args.db or default_db_path()
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return EXIT_UNUSABLE

    conn = connect(db)
    try:
        report = asyncio.run(replay(conn, args.goal, args.source, args.into))
    except ApiError as exc:
        print(exc.message, file=sys.stderr)
        return EXIT_UNUSABLE
    except TraceMismatch as exc:
        print(f"DIVERGED: {exc}", file=sys.stderr)
        return EXIT_DIVERGED
    finally:
        conn.close()

    _print(report)
    return EXIT_OK if report["matched"] else EXIT_DIVERGED


if __name__ == "__main__":
    raise SystemExit(main())
