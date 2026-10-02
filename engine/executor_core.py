"""The executor's core: its state, the stage measurement, the status/step writes and the failure path.

The lowest layer of `ExecutorService` (see `engine/executor.py`). It calls no other layer; every other layer
calls it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from engine.agent_orchestrator import AgentOrchestrator
from engine.executor_support import AgentOutputInvalid, CriticRejection, TestsFailed, WriteWithdrawn
from engine.git import GitService
from engine.laya import LayaService
from engine.models import PAUSE_REASONS, Event, EventType, PlanStep, ROLES
from engine.recall import distill_observations
from engine.role_repair import config_problems
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, ApiError, GoalService, WorkspaceService
from engine.webview_bridge import WebviewBridge

if TYPE_CHECKING:
    from engine.services import SettingsService
    from engine.trace import TraceService

# What a stage that never declared publishes when it raised instead, mapped
# below `_stage_failure_outcome` (which needs `CriticRejection`, defined
# further down).


class _Stage:
    """The handle a stage uses to say what it achieved.

    First declaration wins, so a stage with several exit paths (the planner
    consults, the critic's inspection rounds) can declare from whichever branch
    it leaves by without the later ones overwriting the truth.
    """

    __slots__ = ("name", "role", "step_id", "ordinal", "outcome", "detail", "_declared")

    def __init__(self, name: str, role: str, step_id: str | None, ordinal: int) -> None:
        self.name = name
        self.role = role
        self.step_id = step_id
        self.ordinal = ordinal
        # The default a stage that never declared is published under: it
        # reached the end of the block without saying what it achieved.
        self.outcome = "unavailable"
        self.detail: str | None = None
        self._declared = False

    def record(self, outcome: str, detail: str | None = None) -> None:
        """Declare this stage's outcome (docs/04 §4.7)."""
        if self._declared:
            return
        self.outcome = outcome
        self.detail = detail
        self._declared = True

# How many steps of a parallel goal may run at once. Each running step is a
# streaming model session plus its verifier/critic/scribe tail, so an unbounded
# batch against a plan with many independent steps would open every session
# simultaneously — rate limits, memory, and a burst the user cannot read anyway.
# The batch machinery already loops until the plan is exhausted, so capping the
# batch size turns parallelism into waves instead of removing it.
#
# Where the width comes from, in order: the CODIFY_PARALLEL_WIDTH env var (an
# operator's explicit override, clamped so a nonsense value cannot disable the
# bound), then the persisted `parallel_width` setting (Settings → Agents), then
# this default.
DEFAULT_PARALLEL_WIDTH = 4

def _env_parallel_width() -> int | None:
    raw = (os.environ.get("CODIFY_PARALLEL_WIDTH") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return max(1, min(value, 16))


# What a stage that never declared publishes when it raised instead. Two of
# these exceptions are not failures at all: `TestsFailed` and `CriticRejection`
# are how a verifier says "fail" and a critic says "request-changes" — a stage
# that did its job and is raising on the way out has succeeded at being that
# role, and a rate that counted those as invalid replies would say the verifier
# is broken precisely when it is working. Ordered, because both subclass
# AgentOutputInvalid.
_STAGE_EXCEPTIONS: tuple[tuple[type[BaseException], str], ...] = (
    (TestsFailed, "fail"),
    (CriticRejection, "request_changes"),
    (WriteWithdrawn, "cancelled"),
    (AgentOutputInvalid, "invalid"),
)


def _stage_failure_outcome(exc: Exception) -> str:
    """The outcome a stage that raised without declaring is published under."""
    for exc_type, outcome in _STAGE_EXCEPTIONS:
        if isinstance(exc, exc_type):
            return outcome
    return "unavailable"


def _shape(value: Any) -> str:
    """A value's kind, as a person would name it in an error."""
    if value is None:
        return "null"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, str):
        return "a string"
    return type(value).__name__


class _ExecutorCore:
    """The state every layer of the executor shares, and the primitives they all use.

    Constructed once; holds the services, the locks and the one-driver-per-goal guard, and owns the stage
    measurement, the status and step writes, the failure path, and the parsers for a planner's and a fixer's
    replies. Every other layer (`executor_evidence`, `executor_design`, `executor_plan`, `executor_steps`,
    `executor_conduct`) inherits this, so an attribute any of them reads is declared once, here."""
    def __init__(
        self,
        goals: GoalService,
        workspaces: WorkspaceService,
        registry: AgentRegistryService,
        sandbox: SandboxService,
        git: GitService | None = None,
        laya: LayaService | None = None,
        tracer: TraceService | None = None,
        bridge: WebviewBridge | None = None,
    ):
        self.goals = goals
        self.workspaces = workspaces
        self.orchestrator = AgentOrchestrator(registry, goals, tracer=tracer)
        self.sandbox = sandbox
        self.git = git or GitService()
        # System-1 pre-flight gate (see engine/laya.py). Optional by design: a
        # gate that cannot run is a skipped gate, never a broken pipeline.
        self.laya = laya or LayaService(registry=registry)
        # The bridge to the shell's browser webviews (engine/webview_bridge.py).
        # Optional for the same reason the gate is: an engine with no desktop
        # shell behind it has no page, and `read_page` says so in a sentence
        # rather than failing the turn.
        self.bridge: WebviewBridge | None = bridge
        # Shared-resource locks for parallel goals. asyncio.Lock() is loop-lazy
        # (binds on first acquire), so constructing here — before any loop
        # exists — is safe.
        self._sandbox_lock = asyncio.Lock()
        self._git_lock = asyncio.Lock()
        # Optional settings store (SettingsService). Attached by app lifespan
        # when present; tests without one just get the default width.
        # Annotated at the field so the None default is the *documented* empty
        # state, not a type the checker reads out of one constructor.
        self.settings: SettingsService | None = None
        # One driver per goal: start, retry, and apply each spawn a driver
        # loop, and two loops on one goal re-run the same steps concurrently.
        self._drivers: set[str] = set()

    def _event(self, goal_id: str, step_id: str | None, type_: EventType, payload: dict[str, Any]) -> Event:
        return Event(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            step_id=step_id,
            type=type_,
            payload=payload,
            timestamp=time.time(),
            sequence=self.goals.next_sequence(goal_id),
        )

    # ── stage measurement (docs/04 §4.7) ───────────────────────────────
    #
    # One `stage_result` event per role stage: what the stage was for, whether
    # it achieved it, what it cost, and how long it took. Everything downstream
    # — the per-role success rate, the per-stage cost table, the failure
    # breakdown — is arithmetic over these events, so the measurement is taken
    # where the stage actually runs rather than reconstructed from whatever
    # side effects it happened to leave behind.
    #
    # `duration_ms` is wall clock across the whole stage, not the sum of its
    # model calls: a stage's cost is the calls *and* the engine's own work
    # between them (diff rendering, `fs.apply`, a sandboxed command). Tokens are
    # read back from the `usage` events the orchestrator published during the
    # stage, so spend is attributed from the one record that already exists
    # rather than counted twice.

    @contextlib.asynccontextmanager
    async def _stage(
        self, goal_id: str, stage: str, role: str, step_id: str | None = None,
        ordinal: int = 0,
    ) -> AsyncIterator[_Stage]:
        """Time and account one role stage, publishing what it measured.

        Transparent to control flow by design: it publishes in a `finally`, so
        a stage that raised is still measured and the outcome says which kind
        of failure it was, and it neither raises nor swallows anything of its
        own. A measurement that could fail a goal would quietly make the thing
        it measures worth avoiding.

        An exception does not overwrite an outcome the stage already declared:
        `TestsFailed` and `CriticRejection` are how a verifier says "fail" and a
        critic says "request-changes" — a stage that did its job and is raising
        on the way out is a success at being that role, not an invalid reply.
        """
        handle = _Stage(stage, role, step_id, ordinal)
        try:
            # A goal deleted from another process has nowhere to publish to.
            # That is not this stage's failure, so it is measured and dropped.
            before: int | None = self.goals.current_sequence(goal_id)
        except ApiError:
            before = None
        started = time.monotonic()
        try:
            yield handle
        except asyncio.CancelledError:
            handle.outcome = "cancelled"
            raise
        except Exception as exc:
            if not handle._declared:
                handle.outcome = _stage_failure_outcome(exc)
            raise
        finally:
            if before is not None:
                self._publish_stage_result(goal_id, handle, before, started)

    def _publish_stage_result(
        self, goal_id: str, handle: _Stage, before: int, started: float,
    ) -> None:
        """Publish one stage's outcome, cost and wall clock.

        Spend is summed from this stage's own `usage` events, matched on role
        *and* step: under a parallel goal another step's calls land in the same
        goal's log between the same two sequence numbers, and attributing them
        here would move cost between steps that ran at the same time.
        """
        tokens = 0
        calls = 0
        for ev in self.goals.events_after(goal_id, before):
            if ev.type != "usage" or ev.step_id != handle.step_id:
                continue
            payload = ev.payload or {}
            if payload.get("role") != handle.role:
                continue
            calls += 1
            for key in ("input_tokens", "output_tokens"):
                value = payload.get(key)
                if isinstance(value, (int, float)):
                    tokens += int(value)
        self.goals.publish(self._event(
            goal_id, handle.step_id, "stage_result",
            {
                "stage": handle.name,
                "role": handle.role,
                "ordinal": handle.ordinal,
                "outcome": handle.outcome,
                "detail": handle.detail,
                "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                "tokens": tokens,
                "calls": calls,
            },
        ))

    def _parallel_width(self) -> int:
        """The configured parallel width: env override, then persisted setting,
        then the built-in default. Read per batch, so a settings change lands
        on the next wave without a restart."""
        env = _env_parallel_width()
        if env is not None:
            return env
        if self.settings is not None:
            try:
                return self.settings.get_int("parallel_width")
            except Exception:
                pass
        return DEFAULT_PARALLEL_WIDTH

    # --- public -------------------------------------------------------

    def is_driving(self, goal_id: str) -> bool:
        """Is a coroutine currently driving this goal's steps?

        The authoritative "something is still running" signal, as opposed to the
        status column, which a goal can leave while a driver is still between
        steps. Delete uses it to close the window between reading the status
        and removing the row.
        """
        return goal_id in self._drivers

    def claim_driver(self, goal_id: str) -> bool:
        """Take exclusive right to drive this goal's steps.

        start, retry, and apply each spawn a driver loop. Without this guard a
        retry landing mid-run spawned a SECOND driver that re-read the same
        unfinished steps — two fixers on the same files, the exact torn write
        the batching gate exists to prevent. Single-threaded event loop makes
        the check-then-set atomic between awaits.
        """
        if goal_id in self._drivers:
            return False
        self._drivers.add(goal_id)
        return True

    def release_driver(self, goal_id: str) -> None:
        self._drivers.discard(goal_id)

    def _preflight_roles(self, goal_id: str) -> None:
        """Name, once, every role that cannot be called — before the first call.

        The pipeline already diagnoses this properly, one role at a time:
        `AgentNotConfigured` says "no model is configured for the librarian role"
        and points at the right screen. The problem is that you learn it from a
        goal that died, one role per run, and nothing ever mentions the *other*
        six. A run that cannot possibly finish is the case where one line is
        worth more than the run.

        A warning, not a block, for the same reason laya, the librarian and the
        designer are warnings: the roles that do work should still do their work,
        and a run killed by a setting is a run nobody can inspect. The judgement
        is `config_problems` — the same rule the Settings screen's repair uses,
        minus discovery, because a provider that has not been asked proves
        nothing and this line has to be cheap enough to always print.
        """
        registry = getattr(self.orchestrator, "registry", None)
        if registry is None:  # pragma: no cover - the orchestrator always has one
            return
        try:
            broken = config_problems(
                registry.configs_with_key_state(),
                registry.provider_key_status(),
                ROLES,
            )
        except Exception as exc:  # pragma: no cover - a preflight is never fatal
            self._log(goal_id, None, "warn", f"role preflight could not run: {exc}")
            return
        if not broken:
            return
        detail = "; ".join(f"{role}: {reason}" for role, reason in broken)
        self._log(
            goal_id, None, "warn",
            f"{len(broken)} of {len(ROLES)} agent roles cannot be called, so this goal "
            f"will fail when it reaches them — {detail}. Open Settings → Agent Roles, "
            "or press Repair to point them at a model that is reachable.",
        )

    def _approval_withdrawn(self, goal_id: str) -> str | None:
        """Why a write that was approved when it began may no longer happen — or None if it may.

        Asked again at the write itself (`_fixer`), after a model call that can take minutes. It is narrower
        than `_write_allowed` on purpose: that asks whether approval *exists*, this asks whether a person has
        *taken it back*. A goal that is `FAILED` because a parallel sibling failed has not had its approval
        taken back — the batch is meant to let its healthy steps finish (`docs/04` §3.0) — so only the
        person's own Cancel and Pause, a deleted goal, and a goal switched back to plan-only stop the write.
        """
        try:
            goal = self.goals.get(goal_id)
        except ApiError:
            return "Nothing was written: this goal no longer exists."
        if goal.plan_only:
            return "Nothing was written: this goal was switched back to plan-only while the fixer was answering."
        if goal.status in ("CANCELLED", "PAUSED"):
            return (
                f"Nothing was written: the goal was {goal.status.lower()} while the fixer was answering, "
                "so its reply was not applied."
            )
        return None

    def _cancelled(self, goal_id: str) -> bool:
        """True when the goal was cancelled (or otherwise left RUNNING) mid-step.

        `_run_steps` only checks status *between* steps, so without this a cancel
        landing mid-step changed nothing: the step ran to the end — including the
        scribe's `git commit` of changes the user had just asked to stop. Writing
        to the user's repository is the one act a cancel must be able to prevent,
        so the stages that lead to it re-check before doing irreversible work.
        """
        return self.goals.get(goal_id).status != "RUNNING"

    def _test_result(
        self,
        goal_id: str,
        step: PlanStep,
        argv: list[str] | None,
        verdict: str | None,
        explanation: str | None,
        exit_code: int | None = None,
        refusals: list[str] | None = None,
        brand_drifts: list[str] | None = None,
        output_tail: str = "",
    ) -> None:
        if verdict not in ("pass", "fail", "skip"):
            raise AgentOutputInvalid(f"verifier verdict invalid: {verdict!r}", role="verifier")
        self.goals.publish(self._event(
            goal_id, step.id, "test_result",
            {
                "argv": argv,
                "verdict": verdict,
                "explanation": explanation,
                "exit_code": exit_code,
                # Commands the sandbox refused, so a "pass" with no command is
                # distinguishable from a pass that actually ran a suite.
                "refused": refusals or [],
                "ran": argv is not None,
                # The engine's mechanical findings against a binding brand
                # contract. Advisory: they are on the record the critic reads
                # and they never change the verdict, which stays the tests'.
                "brand_drifts": brand_drifts or [],
                # The end of what the command printed (`library.command_tail`), so the fixer's retry and the
                # conductor's `verify` can say *why* it failed and not only that it did. Third-party text,
                # produced by the repository's own code: never recallable (`RECALLABLE["test_result"]` is
                # the verdict alone) and only ever shown to the role that has to fix the failure.
                "output_tail": output_tail,
            },
        ))
        if verdict == "fail":
            raise TestsFailed(f"tests failed: {explanation}")

    # --- parsing ------------------------------------------------------

    def _accept_plan_reply(self, reply: Any) -> None:
        """The planner's test of its own reply, run inside `run_agent` so a bad plan is asked for again.

        Two replies are good: a plan, and a *consult* — no steps and a request for the librarian, which the
        planning loop serves and then asks again. Anything else that cannot be turned into steps is refused
        with the parser's own reason, which the model is shown.
        """
        consult = reply.get("consult") if isinstance(reply, dict) else None
        if (
            isinstance(reply, dict) and not reply.get("steps") and isinstance(consult, dict)
            and (consult.get("reads") or consult.get("searches") or consult.get("git") or consult.get("run"))
        ):
            return
        self._parse_steps(reply)

    def _parse_steps(self, out: Any) -> list[dict[str, Any]]:
        steps = (out or {}).get("steps")
        if not isinstance(steps, list) or not (1 <= len(steps) <= 20):
            raise AgentOutputInvalid("planner must return 1..20 steps", role="planner")
        parsed = []
        for s in steps:
            if not isinstance(s, dict):
                raise AgentOutputInvalid(
                    f"each planner step must be an object with a title and a description, not {_shape(s)}",
                    role="planner",
                )
            title = s.get("title")
            desc = s.get("description")
            paths = s.get("suggested_paths") or []
            if not title or not desc:
                raise AgentOutputInvalid("planner step missing title/description", role="planner")
            if not isinstance(paths, list):
                raise AgentOutputInvalid("planner suggested_paths must be a list", role="planner")
            parsed.append({"title": title, "description": desc, "suggested_paths": [str(p) for p in paths]})
        return parsed

    def _parse_files(self, out: Any, notes: list[str] | None = None) -> list[dict[str, Any]]:
        files = (out or {}).get("files")
        if not isinstance(files, list):
            raise AgentOutputInvalid("fixer must return files list", role="fixer")
        parsed = []
        for f in files:
            if not isinstance(f, dict):
                raise AgentOutputInvalid(
                    f"each fixer file entry must be an object with a path and an action, not {_shape(f)}",
                    role="fixer",
                )
            path = f.get("path")
            action = f.get("action")
            content = f.get("content")
            if not path or action not in ("create", "update", "delete", "edit"):
                raise AgentOutputInvalid("fixer file entry invalid", role="fixer")
            if action == "delete" and content is not None:
                raise AgentOutputInvalid("fixer delete must have null content", role="fixer")
            if (
                action == "edit" and not f.get("edits") and isinstance(content, str) and content.strip()
            ):
                # `edit` with a complete file in `content` and no edits: the model wrote the file it wants
                # and used the wrong name for it. It is the commonest fixer reply a 1.5B model produced,
                # and asking again did not help — it does not know what `edit` is for. Only with content to
                # write (an empty one would blank the file), and never when edits are present: those keep
                # their contract meaning and `content` is ignored, as the prompt says.
                if notes is not None:
                    notes.append(
                        f"fixer sent action=edit with content and no edits for {path}; "
                        "treated as a whole-file update"
                    )
                parsed.append({"path": path, "action": "update", "content": content})
                continue
            if action == "edit":
                edits = f.get("edits")
                if not isinstance(edits, list) or not edits:
                    raise AgentOutputInvalid(
                        f"fixer edit for {path} requires a non-empty edits list of {{old_text, new_text}}; "
                        'to replace the whole file use action "update" with its complete content',
                        role="fixer",
                    )
                for e in edits:
                    if not isinstance(e, dict) or not isinstance(e.get("old_text"), str) or not isinstance(e.get("new_text"), str):
                        raise AgentOutputInvalid(
                            "each fixer edit needs string old_text and new_text", role="fixer"
                        )
                # `content` plays no part in an edit; it is resolved from the file.
                parsed.append({"path": path, "action": action, "content": None, "edits": edits})
            else:
                parsed.append({"path": path, "action": action, "content": content})
        return parsed

    # --- db helpers ---------------------------------------------------

    def _step(self, goal_id: str, step_id: str) -> PlanStep:
        """One step by id, or a 404-class ApiError.

        This used to raise `AgentOutputInvalid("step not found")` — a
        model-reply defect — for a lookup the *client* got wrong. Mislabeling a
        client mistake as an agent-output defect misroutes it: the fallback
        machinery would retry the goal on the role's second target, and the
        retry would fail identically. A bad id is a 404, not a bad model.
        """
        for s in self.goals.steps(goal_id):
            if s.id == step_id:
                return s
        raise ApiError(404, "unknown_step", f"step {step_id} not found in goal {goal_id}")

    def _insert_steps(self, goal_id: str, steps: list[dict[str, Any]]) -> None:
        for ordinal, s in enumerate(steps):
            self.goals._db.execute(
                "INSERT INTO plan_steps (id, goal_id, ordinal, title, description, suggested_paths, status, review_notes, commit_message, last_agent_role) "
                "VALUES (?, ?, ?, ?, ?, ?, 'PENDING', NULL, NULL, NULL)",
                (str(uuid.uuid4()), goal_id, ordinal, s["title"], s["description"], json.dumps(s["suggested_paths"])),
            )
        self.goals._db.commit()

    def _store_proposed_files(
        self, goal_id: str, step_id: str, files: list[dict[str, Any]], summaries: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist a dry-run proposal so Apply can replay it byte-identically.

        `edit` ops are stored as the resolved full-content update (from the
        apply summaries), never as the raw search/replace description: a file
        may move between the dry run and Apply, and re-running a match against
        moved text would silently do something else. Resolved content makes the
        stored proposal exactly what the user reviewed in the diff.
        """
        db = self.goals._db
        by_path = {
            s["path"]: s
            for s in (summaries or [])
            if s.get("resolved_content") is not None and s.get("changed", True)
        }
        db.execute("DELETE FROM proposed_files WHERE goal_id = ? AND step_id = ?", (goal_id, step_id))
        now = time.time()
        for f in files:
            path, action, content = f["path"], f["action"], f.get("content")
            if action == "edit":
                resolved = by_path.get(path)
                if resolved is None:
                    # An edit that resolved to nothing (no change) has no
                    # proposal worth storing — Apply would be a no-op anyway.
                    continue
                path, action, content = path, "update", resolved["resolved_content"]
            db.execute(
                "INSERT INTO proposed_files (id, goal_id, step_id, path, action, content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), goal_id, step_id, path, action, content, now),
            )
        db.commit()

    def _reset_step(self, goal_id: str, step: PlanStep, *, keep_notes: bool = False) -> None:
        if keep_notes:
            # A retry that a conductor will drive: the critic's notes are what its next `write` acts on.
            self.goals._db.execute(
                "UPDATE plan_steps SET status='PENDING', commit_message=NULL, last_agent_role=NULL WHERE id = ?",
                (step.id,),
            )
        else:
            self.goals._db.execute(
                "UPDATE plan_steps SET status='PENDING', review_notes=NULL, commit_message=NULL, "
                "last_agent_role=NULL WHERE id = ?",
                (step.id,),
            )
        self.goals._db.commit()

    def _set_step(self, goal_id: str, step: PlanStep, status: str, **fields: Any) -> None:
        cols = ["status = ?"]
        vals: list[Any] = [status]
        if "last_agent_role" in fields and fields["last_agent_role"] is not None:
            cols.append("last_agent_role = ?")
            vals.append(fields["last_agent_role"])
        for key in ("review_notes", "commit_message"):
            if key in fields:
                cols.append(f"{key} = ?")
                vals.append(fields[key])
        vals.append(step.id)
        self.goals._db.execute(f"UPDATE plan_steps SET {', '.join(cols)} WHERE id = ?", vals)
        self.goals._db.commit()
        self.goals.publish(self._event(
            goal_id, step.id, "step_status",
            {"status": status, **{k: v for k, v in fields.items() if k in ("review_notes", "commit_message")}},
        ))

    def _is_cancelled(self, goal_id: str) -> bool:
        """Has a person cancelled this goal? Read from the stored status, every time."""
        try:
            return self.goals.get(goal_id).status == "CANCELLED"
        except ApiError:
            # A goal that no longer exists is not one anything should keep running for.
            return True

    def _set_status(
        self, goal_id: str, status: str, step_id: str | None,
        *, reason_code: str | None = None, reason: str | None = None,
    ) -> None:
        # update_status publishes the goal_status event itself.
        current = self.goals.get(goal_id)
        if current.status == "CANCELLED" and status != "CANCELLED":
            # Cancel is a person's decision and nothing a runner finishes afterwards may undo
            # it. The service refuses the move too; this is what keeps a runner that lost the
            # race from surfacing that refusal as a crash in a background task.
            return
        try:
            self.goals.update_status(
                goal_id, current.version, status, step_id, reason_code=reason_code, reason=reason,
            )
        except ApiError as exc:
            if exc.code in ("illegal_status", "version_conflict") and self._is_cancelled(goal_id):
                # The cancel landed between the read above and the write.
                return
            raise
        if status in ("COMPLETED", "FAILED", "CANCELLED"):
            # Terminal, whichever way it went: this run just taught the
            # workspace something, and the store is refined now rather than
            # discovered stale by the next turn's brief. Best-effort — a
            # consolidation failure must never turn a finished run into a
            # failed one, and the log line is the only trace it leaves.
            try:
                self._consolidate(goal_id)
            except Exception as exc:  # noqa: BLE001 — memory is never load-bearing
                self._log(
                    goal_id, None, "warn",
                    f"observation consolidation failed: {exc}",
                )

    def _pause(self, goal_id: str, step_id: str | None, code: str, detail: str = "") -> None:
        """Pause a goal for an engine reason, and say why in the engine's own words.

        `code` is one of `models.PAUSE_CODES`; the sentence is `PAUSE_REASONS[code]` and nothing a model wrote.
        A pause can be caused by text a model produced about a repository (a critic's reasons, a provider's
        error message), which is third-party, and the status event and the log line this writes are read by
        other tools (recall surfaces warn logs). `detail` is for words the engine itself composed, such as a
        provider's error *code*, never its message.

        The write gate is untouched: `PAUSED` refuses `write` (`_write_allowed`), and only the person's Start
        moves a paused goal back to RUNNING (invariant 9).
        """
        reason = PAUSE_REASONS[code] + (f" ({detail})" if detail else "")
        self._set_status(goal_id, "PAUSED", step_id, reason_code=code, reason=reason)
        # Not logged when the pause did not land: a Cancel that won the race leaves the goal CANCELLED.
        if self.goals.get(goal_id).status == "PAUSED":
            self._log(goal_id, step_id, "warn", f"paused: {reason}")

    def _consolidate(self, goal_id: str) -> int:
        """Distill this run's outcomes into the durable observation store.

        The write half of docs/10 §6's reflect: the *engine* refines the store
        after each run — never a model mid-turn — because memory is what a
        future turn trusts, and a future turn's trust is not something a
        conductor reply gets to write. The rows come from `recall_events` (the
        same allow-listed scan the tools read) with ids, so every observation
        carries its supporting event ids.
        """
        goal = self.goals.get(goal_id)
        rows = self.goals.recall_events(goal.workspace_id, with_ids=True)
        observations = distill_observations(rows)
        return self.goals.record_observations(goal.workspace_id, observations)

    def _fail(
        self,
        goal_id: str,
        step_id: str | None,
        code: str,
        message: str,
        role: str | None = None,
    ) -> None:
        """Publish a failure, naming the role responsible when it is known.

        `role` is what makes "why did this fail?" answerable: the UI can show that
        role's provider, model, credential, and live catalog instead of asking the
        user to infer from prose which agent to go and inspect.

        A terminal status is never overwritten: a cancel that lands mid-step
        followed by the held call surfacing an error must leave the goal
        CANCELLED, not relabel the user's decision as a failure.
        """
        current = self.goals.get(goal_id)
        if current.status in ("CANCELLED", "COMPLETED"):
            return
        self.goals.publish(self._event(
            goal_id, step_id, "error",
            {"code": code, "message": message, "role": role},
        ))
        if step_id:
            try:
                step = self._step(goal_id, step_id)
                self._set_step(goal_id, step, "FAILED")
            except Exception:
                pass
        try:
            self._set_status(goal_id, "FAILED", step_id)
        except Exception:
            pass

    def _log(self, goal_id: str, step_id: str | None, level: str, message: str) -> None:
        self.goals.publish(self._event(goal_id, step_id, "log", {"level": level, "message": message}))
