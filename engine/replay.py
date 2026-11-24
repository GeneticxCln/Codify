"""Replay a goal's recorded model calls in a scratch copy of its workspace, with no provider.

Shared by `POST /goals/{id}/trace/replay` (in-app replay) and `scripts/replay_trace.py` (CLI).

Three rules:
- **Nothing is written to the user's workspace.** The tree is copied into a scratch directory first.
- **The gate is replayed, not re-run.** The recorded `laya_decision` event supplies the verdict.
- **A mismatch stops and is reported.** `ReplayProvider` refuses any prompt whose digest differs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import GateCall, LayaDecision, LayaService
from engine.models import (
    ROLES,
    AgentConfig,
    AgentConfigUpdate,
    ConversationCreate,
    GoalCreate,
    TurnCreate,
    WorkspaceCreate,
)
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import (
    AgentRegistryService,
    ApiError,
    ConversationService,
    GoalService,
    WorkspaceService,
)
from engine.trace import ReplayProvider, TraceMismatch, TraceService


class _ReplayedGate(LayaService):
    """Answers with the verdict the recording got, rather than a fresh one."""

    def __init__(self, verdict: LayaDecision) -> None:
        super().__init__(registry=None)
        self._verdict = verdict

    async def decide(  # noqa: D102
        self, state: dict[str, Any], on_call: GateCall | None = None,
        primary: Any = None,
    ) -> LayaDecision:
        return self._verdict


class _ReplayFactory(ProviderFactory):
    """Hands every role the one provider that answers from the recording."""

    def __init__(self, provider: ReplayProvider, keychain: Keychain | None = None) -> None:
        super().__init__(keychain if keychain is not None else Keychain())
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        self.provider.current_role = config.role
        return self.provider


def _ignore_special_files(directory: str, names: list[str]) -> set[str]:
    """Skip sockets, FIFOs, and device nodes when copying a workspace tree into scratch."""
    ignored: set[str] = set()
    for name in names:
        try:
            mode = os.lstat(os.path.join(directory, name)).st_mode
        except OSError:
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            ignored.add(name)
    return ignored


def _copy_workspace_tree(source: Path, into: Path) -> None:
    """Copy `source` into `into` without dereferencing symlinks or crashing on dangling links."""
    try:
        shutil.copytree(
            source,
            into,
            symlinks=True,
            ignore_dangling_symlinks=True,
            ignore=_ignore_special_files,
            dirs_exist_ok=True,
        )
    except (shutil.Error, OSError) as exc:
        raise ApiError(
            422,
            "replay_copy_failed",
            f"could not copy workspace tree for replay: {exc}",
        ) from exc


def _gate_verdict(events: list[Any]) -> LayaDecision:
    """The recorded gate's decision, or 'skipped' when it never ran."""
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
    *,
    cleanup_scratch: bool = False,
) -> dict[str, Any]:
    """Replay one recorded goal into a scratch copy of a workspace tree.

    When `cleanup_scratch` is True (the HTTP route), a freshly created temp tree is removed once
    the replay finishes so in-app replays leave no temp folders behind.
    """
    src_goals = GoalService(conn)
    src_workspaces = WorkspaceService(conn)
    src_traces = TraceService(conn)

    recorded_goal = src_goals.get(goal_id)  # 404 unknown_goal
    calls = src_traces.calls(goal_id)
    if not calls:
        raise ApiError(
            409,
            "nothing_recorded",
            f"goal {goal_id} has no recording — arm it with PUT /goals/{goal_id}/trace "
            "before the run starts",
        )
    recorded_ws = src_workspaces.get(recorded_goal.workspace_id)
    recorded_events = src_goals.events_after(goal_id, 0)
    source = source or Path(recorded_ws.root_path)

    owns_scratch = into is None
    scratch_parent: Path | None = None
    if into is None:
        # Nest the copied workspace inside a private 0700 temp container rather than directly under
        # `/tmp` so `shutil.copytree` preserves the directory's own metadata and `..` is a clean
        # private parent (not world-writable `/tmp` with unrelated sibling entries).
        scratch_parent = Path(tempfile.mkdtemp(prefix="codify-replay-"))
        into = scratch_parent / (source.name or "workspace")
    try:
        if source.is_dir():
            await asyncio.to_thread(_copy_workspace_tree, source, into)
        else:
            raise ApiError(
                409,
                "source_missing",
                f"the tree to replay against is not a directory: {source}",
            )

        with tempfile.TemporaryDirectory(prefix="codify-replay-db-") as scratch_db_dir:
            replay_conn = connect(Path(scratch_db_dir) / "replay.db")
            try:
                goals = GoalService(replay_conn)
                workspaces = WorkspaceService(replay_conn)
                replay_ws = workspaces.create(
                    WorkspaceCreate(name=f"replay-{goal_id[:8]}", root_path=str(into.resolve()))
                )
                if recorded_goal.mode == "chat":
                    convo = ConversationService(replay_conn).create(
                        ConversationCreate(workspace_id=replay_ws.id, title=recorded_goal.title[:80])
                    )
                    replay_goal = goals.create_turn(
                        convo.id,
                        TurnCreate(
                            prompt=(recorded_goal.description or recorded_goal.title).strip() or "replay",
                            trace=False,
                        ),
                    )
                else:
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
                scratch_keychain = Keychain(secrets_path=Path(scratch_db_dir) / "secrets.json")
                registry = AgentRegistryService(
                    replay_conn, _ReplayFactory(provider, scratch_keychain), scratch_keychain
                )
                for role in ROLES:
                    registry.set_config(role, AgentConfigUpdate(provider="ollama", model_name="replay"))

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
                    if recorded_goal.mode == "chat":
                        await executor.run_chat(replay_goal.id)
                    else:
                        await executor.run_planning(replay_goal.id)
                        if not recorded_goal.plan_only:
                            steps = goals.steps(replay_goal.id)
                            if steps:
                                goals.update_status(
                                    replay_goal.id, goals.get(replay_goal.id).version, "RUNNING"
                                )
                            for step in steps:
                                await executor.run_step(replay_goal.id, step.id)
                                if goals.get(replay_goal.id).status != "RUNNING":
                                    break
                            if steps and goals.get(replay_goal.id).status == "RUNNING":
                                goals.update_status(
                                    replay_goal.id, goals.get(replay_goal.id).version, "COMPLETED"
                                )
                except TraceMismatch as exc:
                    diverged = str(exc)

                replay_events = goals.events_after(replay_goal.id, 0)
                final = goals.get(replay_goal.id)
            finally:
                replay_conn.close()

        if diverged is None and final.status == "FAILED":
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
                            payload.get("message") or payload.get("code") or "the replay failed"
                        )
                        break
                else:
                    diverged = "the replay failed before every recorded call was served"

        stages = _stage_sequence(replay_events)
        gate_calls = [c for c in calls if c.get("role") == "laya"]
        conductor_calls = [c for c in calls if c.get("role") == "conductor"]
        return {
            "goal_id": goal_id,
            "replay_goal_id": replay_goal.id,
            "scratch": None if (cleanup_scratch and owns_scratch) else str(into),
            "source": str(source),
            "recorded_calls": len(calls),
            "served_calls": len(provider.served),
            "gate_calls_replayed": len(gate_calls),
            "conductor_calls_skipped": len(conductor_calls),
            "stages": stages,
            "status": final.status,
            "diverged": diverged,
            "matched": diverged is None
            and len(provider.served) + len(gate_calls) + len(conductor_calls) == len(calls),
        }
    finally:
        if cleanup_scratch and owns_scratch:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(shutil.rmtree, scratch_parent or into, True)
