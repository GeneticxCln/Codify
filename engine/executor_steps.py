"""The pipeline layer: write, verify, judge and record each step of an approved plan.

The fourth layer of `ExecutorService` (see `engine/executor.py`); it calls `executor_design`, `executor_evidence`
and `executor_core`. The fixer is still the only writer and the verifier still the only role that runs a
command (docs/00 §6.9). The deliverable paths in `_fixer`, `_verifier` and `_critic` are a draft
reconstruction (see `executor_design`).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any

from engine.executor_design import DELIVERABLE_ROLE, DELIVERABLE_SUBJECT, MAX_DESIGN_MD_CHARS
from engine.executor_support import (
    AgentOutputInvalid,
    CriticRejection,
    PathRefused,
    TestsFailed,
    WriteWithdrawn,
    _verifier_outcome,
)
from engine.fs import FileSystemService, PathEscapeError, ProtectedRootError
from engine.library import READ_ONLY_TIMEOUT_S
from engine.models import Goal, PlanStep
from engine.providers import ProviderError
from engine.sandbox import CommandNotAllowed
from engine.services import ApiError
from engine.executor_design import _Design


# How many commands the sandbox may refuse in one step before the verifier is made
# to answer with a verdict. A refusal executes nothing, so it does not consume
# the single-run budget — but it must stay bounded, since each retry is another
# model call.
MAX_REFUSED_TEST_COMMANDS = 2

# How many times a failing test run may be fed back to the fixer before the step
# is declared failed. This is the loop that turns "a tool that proposes" into
# "an agent that finishes": the verifier's output is exactly the evidence the
# fixer was missing when it wrote the broken code. One attempt, deliberately:
# each round costs a fixer + verifier call pair, and a second failure of the
# same step usually means the approach (not the code) is wrong — that needs a
# human, or an edited plan, not a third blind attempt.
MAX_FIX_ATTEMPTS = 1
# How many times the fixer may ask for another pass after applying changes
# ("I set up the config file, now give me the test run"). Separate from the
# test-failure retry: this is the fixer declaring it is not finished, not the
# verifier telling it that it failed. Bounded the same way — an uncapped model
# would loop forever, and each pass costs a model call.
MAX_FIXER_PASSES = 2
# Read-only inspection rounds the critic may request during one review (see
# _critic). Reuses the librarian's read-only allowlist via the sandbox.
MAX_CRITIC_COMMANDS = 2


def _norm_path(p: str) -> str:
    """Normalize a suggested path for disjointness checks.

    Raw strings diverge for the same file (`a/b` vs `./a/b` vs `a//b`);
    normpath + casefold closes the cheap aliases. Absolute spellings that
    stay inside the workspace are reduced to their relative form. Anything
    unparseable falls back to the stripped raw string — a conservative
    mismatch (refuse the batch) beats a torn write.
    """
    s = (p or "").strip()
    if not s:
        return ""
    if s.startswith("/"):
        s = s.lstrip("/")
    norm = os.path.normpath(s)
    if norm == ".":
        return ""
    return norm.casefold()


class _Steps(_Design):
    """Executing a plan: one step at a time, or a wave of independent ones.

    The fixer (the only writer), the verifier (the only role that runs a command), the critic and the
    scribe, the retry path and the apply path. Calls the design, evidence and core layers."""

    async def run_step(self, goal_id: str, step_id: str, stored_files: list[dict[str, Any]] | None = None) -> None:
        """Run one step. stored_files=None asks the fixer for changes; a list
        (possibly empty) replays those exact file operations without a fixer
        call — used by apply_goal to write reviewed dry-run changes."""
        goal = self.goals.get(goal_id)
        step = self._step(goal_id, step_id)
        ws = self.workspaces.get(goal.workspace_id)
        fs = FileSystemService(ws.root_path)
        self._set_step(goal_id, step, "IN_PROGRESS")
        # The same evidence the planner planned from, so a step is written against
        # what the repository actually says rather than against a fresh guess.
        evidence = self._evidence_for(goal_id)
        try:
            if stored_files is None:
                # The fix → verify loop. A failing test run is not the end of a
                # step: the failing output is exactly the evidence the fixer was
                # missing when it wrote the code, so it goes back once, bounded
                # by MAX_FIX_ATTEMPTS. Attempt 1 runs with no feedback; a failed
                # verification feeds the failure back and tries again; the last
                # attempt's failure propagates and fails the step as before.
                outcome: dict[str, Any] | None = None
                summaries: list[dict[str, Any]] | None = None
                prior_failure: dict[str, Any] | None = None
                passes_left = MAX_FIXER_PASSES
                # One counter across attempts and the fixer's own extra passes,
                # so every fixer call this step makes is a distinct measured
                # stage rather than two rows claiming to be attempt 1.
                fixer_calls = 0
                for attempt in range(1, MAX_FIX_ATTEMPTS + 2):  # attempts, plus the final one
                    final = attempt > MAX_FIX_ATTEMPTS
                    try:
                        fixer_calls += 1
                        async with self._stage(
                            goal_id, "fixer", "fixer", step.id, ordinal=fixer_calls,
                        ) as fix_stage:
                            summaries, wants_pass = await self._fixer(
                                goal_id, step, fs, goal.dry_run, evidence,
                                failure_feedback=prior_failure,
                            )
                            fix_stage.record(
                                "wrote" if any(s.get("changed", True) for s in summaries)
                                else "no_change"
                            )
                        # The fixer declared the change multi-stage. Passes are
                        # the fixer's own budget, granted BEFORE verifying (a
                        # test run against admittedly half-written work is a
                        # burn) and WITHOUT touching the retry attempt counter
                        # — a `continue` here would spend a retry slot, and
                        # two passes would exhaust the whole loop.
                        while wants_pass and passes_left > 0:
                            passes_left -= 1
                            self.goals.publish(self._event(
                                goal_id, step.id, "fixer_pass",
                                {
                                    "attempt": attempt,
                                    "max_passes": MAX_FIXER_PASSES,
                                    "passes_left": passes_left,
                                },
                            ))
                            if self._cancelled(goal_id):
                                self._log(goal_id, step.id, "info", "cancelled — not running the fixer's next pass")
                                return
                            fixer_calls += 1
                            async with self._stage(
                                goal_id, "fixer", "fixer", step.id, ordinal=fixer_calls,
                            ) as pass_stage:
                                summaries, wants_pass = await self._fixer(
                                    goal_id, step, fs, goal.dry_run, evidence,
                                    failure_feedback=prior_failure,
                                )
                                pass_stage.record(
                                    "wrote" if any(s.get("changed", True) for s in summaries)
                                    else "no_change"
                                )
                        # The verifier publishes its verdict and raises it as
                        # control flow when the tests failed, so its outcome is
                        # recorded from the exception on that path
                        # (see `_stage_failure_outcome`) and from the verdict it
                        # returns on the others.
                        async with self._stage(
                            goal_id, "verifier", "verifier", step.id, ordinal=attempt,
                        ) as verify_stage:
                            outcome = await self._verifier(
                                goal_id, step, ws, evidence, prior_failure=prior_failure,
                                diffs=summaries,
                            )
                            verify_stage.record(_verifier_outcome(outcome))
                    except TestsFailed as exc:
                        if final:
                            raise
                        prior_failure = {
                            "explanation": str(exc),
                            # The verifier's own published record of what it ran
                            # and saw — the model gets the real record, not a
                            # paraphrase.
                            "outcome": self._last_test_result(goal_id, step.id),
                        }
                        self.goals.publish(self._event(
                            goal_id, step.id, "fix_retry",
                            {
                                "attempt": attempt,
                                "max_attempts": MAX_FIX_ATTEMPTS,
                                "reason": str(exc),
                            },
                        ))
                        # A cancel that lands while tests fail must still win —
                        # the retry is work, and work stops when the user says so.
                        if self._cancelled(goal_id):
                            self._log(goal_id, step.id, "info", "cancelled — not retrying the failed step")
                            return
                        continue
                    break
            else:
                # A replay is a reviewed decision, not a fresh attempt: there is
                # nothing for a retry loop to fix, so it never runs on this path.
                summaries = self._replay_files(goal_id, step, fs, stored_files, dry_run=goal.dry_run)
                # A replay wrote what a reviewed fixer pass already wrote, so the
                # fixer stage is recorded as the replay it was — not as a second
                # attempt to write, which would double the fixer's measured cost.
                self.goals.publish(self._event(
                    goal_id, step.id, "stage_result",
                    {
                        "stage": "fixer", "role": "fixer", "ordinal": 1,
                        "outcome": "replayed", "detail": None,
                        "duration_ms": 0, "tokens": 0, "calls": 0,
                    },
                ))
                async with self._stage(goal_id, "verifier", "verifier", step.id) as replay_verify:
                    outcome = await self._verifier(goal_id, step, ws, evidence, diffs=summaries)
                    replay_verify.record(_verifier_outcome(outcome))
            # The fixer has already written by the time the verifier runs, so
            # those stages are not cancel-safe and cancelling mid-flight leaves
            # their work on disk (documented behavior of a mid-run cancel).
            # But verification, judgment and *the commit* are separable: a
            # cancel that arrives while tests run must stop the goal before the
            # scribe records changes the user asked not to make.
            if self._cancelled(goal_id):
                self._log(goal_id, step.id, "info", "cancelled — skipping review and commit for this step")
                return
            # Every path that reaches the review phase assigned them: the fixer
            # loop sets `summaries`, the replay branch sets it, and a failure
            # returns instead of falling through.
            assert summaries is not None
            # The critic approves by returning and rejects by raising, so its two
            # outcomes come from the two ways out of this block rather than from
            # a value it hands back.
            async with self._stage(goal_id, "critic", "critic", step.id) as critic_stage:
                await self._critic(goal_id, step, fs, summaries, evidence, outcome, ws_root=ws.root_path)
                critic_stage.record("approve")
            if self._cancelled(goal_id):
                self._log(goal_id, step.id, "info", "cancelled — skipping the summary for this step")
                return
            async with self._stage(goal_id, "scribe", "scribe", step.id) as scribe_stage:
                scribe_stage.record(
                    await self._scribe(goal_id, step, summaries, ws.root_path, goal.dry_run, outcome)
                )
        except CriticRejection:
            # Step remains IN_PROGRESS with review_notes, goal is PAUSED; human retry required
            return
        except WriteWithdrawn as exc:
            # The person stopped the goal while the fixer was answering. Nothing was written, and this is
            # not a failure to report: the step stays as it was, and a resume runs it again.
            self._log(goal_id, step_id, "info", f"{exc} The fixer's reply was discarded.")
            return
        except (AgentOutputInvalid, ProviderError) as exc:
            self._fail(
                goal_id, step_id, getattr(exc, "code", "agent_output_invalid"), str(exc),
                role=getattr(exc, "role", None),
            )
            return
        except CommandNotAllowed as exc:
            # Unreachable in practice: the verifier handles refusals itself and
            # retries with a permitted command. Kept as a backstop.
            self._fail(goal_id, step_id, exc.code, str(exc), role="verifier")
            return
        except PathEscapeError as exc:
            self._fail(goal_id, step_id, "path_escape", str(exc), role="fixer")
            return
        except ApiError as exc:
            self._fail(goal_id, step_id, exc.code, exc.message, role=getattr(exc, "role", None))
            return
        except (ValueError, OSError) as exc:
            # Replay path (stored_files) surfaces fs.apply errors as
            # ValueError/OSError; a corrupt stored proposal must fail the step
            # loudly, not escape as internal_error.
            self._fail(goal_id, step_id, "replay_failed", str(exc), role="fixer")
            return
        self._set_step(goal_id, step, "COMPLETED")

    async def apply_goal(self, goal_id: str) -> Goal:
        """Replay a completed dry-run's proposed changes for real.

        Writes the exact file contents the fixer proposed (no new LLM fixer
        calls), then re-runs verifier/critic/scribe per step as a normal
        execution, committing as it goes.
        """
        goal = self.goals.get(goal_id)
        if goal.status not in ("COMPLETED", "FAILED"):
            raise ApiError(409, "illegal_status", f"cannot apply from {goal.status}")
        if not self.claim_driver(goal_id):
            raise ApiError(409, "driver_busy", "another driver is already running this goal")
        try:
            return await self._apply_goal_locked(goal_id)
        finally:
            self.release_driver(goal_id)

    async def _apply_goal_locked(self, goal_id: str) -> Goal:
        rows = self.goals._db.execute(
            "SELECT step_id, path, action, content FROM proposed_files WHERE goal_id = ? ORDER BY rowid",
            (goal_id,),
        ).fetchall()
        if not rows:
            raise ApiError(409, "nothing_to_apply", "dry-run produced no proposed changes")

        by_step: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_step.setdefault(r["step_id"], []).append(
                {"path": r["path"], "action": r["action"], "content": r["content"]}
            )

        # The apply run is a real execution: clear the dry_run flag and reset
        # every step so statuses/events tell the true story.
        self.goals.set_dry_run(goal_id, False)
        for step in self.goals.steps(goal_id):
            self._reset_step(goal_id, step)
        self._set_status(goal_id, "RUNNING", None)

        # Batch on what will actually be WRITTEN, not on the plan's guesses.
        # apply replays stored proposals byte-identically, so a step's real
        # filesystem footprint is its proposed_files rows — which diverge from
        # suggested_paths whenever the plan was edited after the dry run (the
        # edit API allows it) or the fixer wrote somewhere the plan never
        # named. Batching on the guess here would prove disjointness for paths
        # nothing writes while two replays race on the same real file.
        def effective_paths(step_id: str) -> set[str]:
            """A step's real apply footprint: its stored proposals, falling back
            to suggested_paths for a step that proposed nothing (it still runs
            its verifier/critic/scribe tail, so it needs *some* footprint to be
            provable). Steps are looked up from the store each call — a step
            deleted mid-run must yield an empty set, not an AttributeError."""
            paths = {_norm_path(f["path"]) for f in by_step.get(step_id, []) if f.get("path") and _norm_path(f["path"])}
            if paths:
                return paths
            step = next((s for s in self.goals.steps(goal_id) if s.id == step_id), None)
            if step is None:
                return set()
            return {_norm_path(p) for p in (step.suggested_paths or []) if _norm_path(p)}

        remaining = list(self.goals.steps(goal_id))
        while remaining:
            refreshed = self.goals.get(goal_id)
            if refreshed.status != "RUNNING":
                return refreshed
            if refreshed.parallel:
                batch = self._independent_batch(remaining, paths_for=effective_paths)
                batch_ids = {s.id for s in batch}
                remaining = [s for s in remaining if s.id not in batch_ids]
                if len(batch) > 1:
                    try:
                        await self._run_parallel(
                            goal_id, batch, by_step, paths_for=effective_paths,
                        )
                    except ApiError as exc:
                        # Dispatch re-check found the disjointness proof stale
                        # (the plan changed under us). Not a failure: re-read
                        # the plan and let the loop re-batch from reality.
                        self._log(goal_id, None, "warn", f"batch refused, re-batching: {exc.message}")
                        remaining = [s for s in self.goals.steps(goal_id) if s.status != "COMPLETED"]
                        continue
                elif batch:
                    await self.run_step(goal_id, batch[0].id, stored_files=by_step.get(batch[0].id, []))
                else:
                    # The head step's footprint is unprovable (no proposals, no
                    # suggested paths): it runs alone, same contract as the
                    # normal driver.
                    head = remaining.pop(0)
                    await self.run_step(goal_id, head.id, stored_files=by_step.get(head.id, []))
            else:
                step = remaining.pop(0)
                await self.run_step(goal_id, step.id, stored_files=by_step.get(step.id, []))
            if self.goals.get(goal_id).status != "RUNNING":
                return self.goals.get(goal_id)

        try:
            self._set_status(goal_id, "COMPLETED", None)
        except ApiError:
            pass
        return self.goals.get(goal_id)

    def _independent_batch(
        self, steps: list[PlanStep], paths_for: Any = None,
    ) -> list[PlanStep]:
        """The longest prefix of steps that provably cannot observe each other.

        Two steps are independent when neither's target paths appear in the
        other's — disjoint writes AND disjoint reads, because a step reading a
        file another step is rewriting sees torn state. A step with no paths
        ("improve the README prose") touches nothing we can prove, so it
        never batches: it runs alone, exactly as today. Suggested paths are a
        plan, not a straitjacket — which is precisely why they gate parallelism
        rather than being trusted after the fact.

        `paths_for` lets a caller state a step's real footprint when the plan's
        guess would be a lie — apply_goal passes its proposed-file paths, since
        a replay writes exactly what was stored, edited plan or not. The
        dispatch re-check in _run_parallel is the second half of this: both
        halves must agree the proof holds at execution time.

        The batch is also capped at the configured width (Settings → Agents,
        or the CODIFY_PARALLEL_WIDTH env override; default 4): each in-flight
        step is a streaming model session plus its verification tail, so a
        twenty-step plan must open them in waves of 4, not all at once. The
        caller loops until the plan is exhausted, so the cap throttles
        concurrency without serializing anything.
        """
        width = self._parallel_width()

        def paths(s: PlanStep) -> set[str]:
            if paths_for is not None:
                return set(paths_for(s.id))
            return {_norm_path(p) for p in (s.suggested_paths or []) if _norm_path(p)}

        batch: list[PlanStep] = []
        taken: set[str] = set()
        for step in steps:
            if len(batch) >= width:
                break
            mine = paths(step)
            if not mine or mine & taken:
                break
            batch.append(step)
            taken |= mine
        return batch

    async def _run_parallel(
        self, goal_id: str, steps: list[PlanStep], stored_files: dict[str, list[dict[str, Any]]] | None,
        paths_for: Any = None,
    ) -> None:
        """Run a path-disjoint batch concurrently; failures and cancels join.

        Filesystem work is disjoint by construction (the batch gates on
        suggested_paths). The two genuinely shared resources are serialized:
        sandbox commands (a test run sees the whole tree) and git commits
        (the index is global). Cancel fails the whole batch promptly; an
        exception in one step fails the goal after every sibling settles —
        matching the sequential semantics where the next step is simply
        never started.

        The steps are re-read from the store before the gather: the batch was
        proven disjoint against what the planner *wrote*, but run_step must
        execute what is in the DB *now*. A plan edited between batching and
        dispatch (or any drift between the snapshot and the store) would
        otherwise run under a disjointness proof that no longer holds. A
        re-read that breaks disjointness refuses the batch rather than racing
        — refusing is the conservative failure, and matches how the batcher
        treats any step it cannot prove.

        `paths_for` must carry the SAME footprint the batcher proved against:
        a caller that batches on one definition (apply_goal batches on stored
        proposals) but re-checks on another (suggested_paths) would have the
        re-check refuse every batch the batcher legitimately proved — a
        refuse/re-batch loop that never runs a step. One proof, one source.
        """
        current = {s.id: s for s in self.goals.steps(goal_id)}
        resolved: list[PlanStep] = []
        taken: set[str] = set()
        for snap in steps:
            step = current.get(snap.id)
            if step is None:
                raise ApiError(
                    409, "step_vanished",
                    f"step {snap.id} disappeared between batching and dispatch",
                )
            if paths_for is not None:
                mine = {_norm_path(p) for p in paths_for(step.id) if p and _norm_path(p)}
            else:
                mine = {_norm_path(p) for p in (step.suggested_paths or []) if _norm_path(p)}
            if not mine or mine & taken:
                # The proof is stale: this step now shares a path with a batch
                # sibling (or has no provable paths). Refuse the whole batch —
                # running some of it would be exactly the torn write the gate
                # exists to prevent. The caller's next loop pass re-batches
                # from scratch with the current plan.
                raise ApiError(
                    409, "batch_no_longer_disjoint",
                    f"step {step.title!r} no longer provably disjoint from its batch — "
                    "the plan changed after batching; re-batching",
                )
            taken |= mine
            resolved.append(step)

        async def run_one(step: PlanStep) -> None:
            stored = (stored_files or {}).get(step.id)
            await self.run_step(goal_id, step.id, stored_files=stored)

        results = await asyncio.gather(*(run_one(s) for s in resolved), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            if len(errors) > 1:
                self._log(
                    goal_id, None, "warn",
                    f"parallel batch had {len(errors)} failures — reporting the first: "
                    + "; ".join(f"{type(e).__name__}: {e}" for e in errors[1:4]),
                )
            raise errors[0]

    def _replay_files(self, goal_id: str, step: PlanStep, fs: FileSystemService, files: list[dict[str, Any]], dry_run: bool) -> list[dict[str, Any]]:
        """Apply stored file operations and emit the same diff events as _fixer."""
        summaries = fs.apply(files, dry_run=dry_run)
        self._publish_changes(goal_id, step.id, summaries, dry_run)
        self._set_step(goal_id, step, "IN_PROGRESS", last_agent_role="fixer")
        return summaries

    async def retry_step(self, goal_id: str, step_id: str, expected_version: int) -> PlanStep:
        """Retry a step and wait for it. The retry route does not use this — see `begin_retry`."""
        self._prepare_retry(goal_id, step_id, expected_version)
        await self.run_step(goal_id, step_id)
        return self._step(goal_id, step_id)

    def begin_retry(self, goal_id: str, step_id: str, expected_version: int) -> PlanStep:
        """Validate a retry, claim the goal's driver, re-open the step, and return at once.

        The retry route used to await the whole step inside the request — minutes of fixer,
        verifier and critic with the connection held open — and never claimed the driver, so
        `is_driving()` was false for exactly the time the goal was busiest. Everything that can be
        refused is decided here, synchronously, so a request that cannot succeed still answers
        409; the run itself is the caller's to start in the background, and the driver claimed
        here is theirs to release when it ends.
        """
        step = self._prepare_retry(goal_id, step_id, expected_version, claim=True)
        return step

    def _prepare_retry(
        self, goal_id: str, step_id: str, expected_version: int, *, claim: bool = False,
    ) -> PlanStep:
        step = self._step(goal_id, step_id)
        if not (step.status == "FAILED" or (step.status == "IN_PROGRESS" and bool(step.review_notes))):
            raise ApiError(409, "step_not_retryable", f"step {step_id} is not in a retryable state (status={step.status})")
        # A retry re-runs this step's paths while the driver loop may be mid-wave
        # on siblings batched against the OLD suggested_paths. Re-prove
        # disjointness against every still-unfinished step: a plan edit that made
        # this step collide with a running sibling must not turn the retry into
        # the torn write parallelism exists to prevent.
        if goal_id in self._drivers:
            raise ApiError(409, "driver_busy", "another driver is already running this goal")
        goal = self.goals.get(goal_id)
        if goal.parallel:
            others = {
                _norm_path(p)
                for s in self.goals.steps(goal_id)
                if s.id != step_id and s.status != "COMPLETED"
                for p in (s.suggested_paths or [])
                if _norm_path(p)
            }
            mine = {_norm_path(p) for p in (step.suggested_paths or []) if _norm_path(p)}
            if mine & others:
                raise ApiError(
                    409, "retry_collides_with_running",
                    f"step {step.title!r} now shares paths with a step that has not finished "
                    "— edit the plan (or finish the other step) before retrying",
                )
        if claim and not self.claim_driver(goal_id):
            raise ApiError(409, "driver_busy", "another driver is already running this goal")
        try:
            self.goals.update_status(goal_id, expected_version, "RUNNING")
            self._reset_step(goal_id, step)
        except BaseException:
            # A refused or failed retry must not leave the goal claimed by nobody.
            if claim:
                self.release_driver(goal_id)
            raise
        return self._step(goal_id, step_id)

    # --- stages -------------------------------------------------------

    def _last_test_result(self, goal_id: str, step_id: str) -> dict[str, Any]:
        """The verifier's most recent published record for this step.

        Read from the event log, not memory, like every other piece of goal
        state — a resumed goal replays the same evidence.
        """
        latest: dict[str, Any] = {}
        for ev in self.goals.events_after(goal_id, 0):
            if ev.type == "test_result" and ev.step_id == step_id:
                latest = ev.payload or {}
        return latest

    async def _fixer(
        self,
        goal_id: str,
        step: PlanStep,
        fs: FileSystemService,
        dry_run: bool,
        evidence: dict[str, Any] | None = None,
        failure_feedback: dict[str, Any] | None = None,
        guidance: str = "",
    ) -> tuple[list[dict[str, Any]], bool]:
        """Write one step's files.

        `guidance` is what the conductor said when it asked for this write. It
        is placed *beside* the step rather than in place of it: the plan is the
        contract the user approved, and a conductor that could overwrite the
        step's description with its own would be able to change what was agreed
        without the user seeing a new plan. Guidance narrows; it does not
        replace.
        """
        ctx, unreadable = self._suggested_paths_context(fs, step.suggested_paths)

        # On a retry, the failed run's evidence is the most important part of
        # the prompt: what the model wrote did not work, and here is exactly
        # how. Without it the second attempt would be a coin flip.
        feedback_text = ""
        if failure_feedback:
            outcome = failure_feedback.get("outcome") or {}
            lines = [
                "IMPORTANT — your previous attempt FAILED verification:",
                str(failure_feedback.get("explanation", "")),
            ]
            if outcome.get("argv"):
                argv_out = outcome["argv"]
                argv_str = " ".join(argv_out) if isinstance(argv_out, list) else str(argv_out)
                lines.append(
                    f"Command that was run: {argv_str} (exit {outcome.get('exit_code')})"
                )
            lines.append(
                "Fix what the failure describes. Do not start over from scratch — "
                "edit your previous approach."
            )
            feedback_text = "\n".join(lines) + "\n\n"
        # The locked direction, so this step's files match the other steps'
        # rather than each inventing its own palette. A design-deliverable
        # goal's write step is a special case: it is handed the draft itself,
        # because "write a DESIGN.md matching this contract" invites exactly the
        # paraphrase that would make the deliverable drift from its own brief.
        design = self._design_for(goal_id)
        design_section = ""
        if design:
            design_section = f"\n\n{self._design_text(design)}"
            write_path = self._deliverable_write_path(step, design)
            body = str(design.get("design_md") or "")
            if write_path and body:
                mode = str(design.get("mode") or "")
                role = DELIVERABLE_ROLE.get(mode, "the deliverable")
                design_section += (
                    f"\n\nPlan note: this step writes the {mode or 'goal'} deliverable "
                    f"itself — {role}. Add, drop or reword nothing: write it to "
                    f"{write_path} verbatim.\n"
                    f"--- {write_path} (write exactly this) ---\n{body}\n"
                    f"--- end {write_path} ---"
                )
        guidance_section = ""
        if guidance.strip():
            guidance_section = (
                "The conductor asked for this specifically, in addition to the "
                f"step above:\n{guidance.strip()}\n\n"
            )
        applied: dict[str, Any] = {}

        def accept(reply: Any) -> None:
            """Turn a parsed reply into the files it describes, or say why it cannot be.

            Run inside `run_agent`, so a reply the engine cannot apply gets the same one re-ask a reply
            that would not parse gets, with this reason quoted to the model. `fs.apply` resolves every
            edit before it writes anything, so a refused reply has written nothing and asking again is safe.
            """
            notes: list[str] = []
            files = self._parse_files(reply, notes)
            for note in notes:
                self._log(goal_id, step.id, "info", note)
            # The fixer may declare itself unfinished: multi-stage changes (a config
            # file in one pass, the code that reads it in the next) do not fit one
            # reply. It asks by returning needs_another_pass=true WITH a plan note;
            # an empty files list is still just a no-op, so the two can't be confused.
            wants_pass = bool(isinstance(reply, dict) and reply.get("needs_another_pass"))
            if not files and wants_pass:
                raise AgentOutputInvalid(
                    "needs_another_pass requires files in the same reply — ask for "
                    "another pass alongside the changes you just made",
                    role="fixer",
                )
            if not dry_run:
                # Approval is asked about again *here*, at the write. `write` and `run_step` asked before the
                # model call, and a local model takes minutes to answer — which is when a person presses
                # Cancel or Pause. A dry run writes nothing, so it is not held to this.
                withdrawn = self._approval_withdrawn(goal_id)
                if withdrawn:
                    raise WriteWithdrawn(withdrawn)
            try:
                # Apply runs BEFORE storage now: an `edit` op is only a description
                # ("replace this exact text") until the engine resolves it against
                # the file as it exists, and only the resolved full content is a
                # proposal "Apply" can replay deterministically later.
                summaries = fs.apply(files, dry_run=dry_run)
            except ProtectedRootError:
                # About where the workspace *is*, not about anything the model wrote: asking again
                # cannot change it, so it is not put to the model.
                raise
            except PathEscapeError as exc:
                # An absolute path, one that climbs out, one inside `.git`: the model's slip, and the
                # refusal names exactly what to change. Nothing was written (apply resolves every path
                # before it writes), and the code stays `path_escape` if it is still wrong the second time.
                raise PathRefused(
                    f"{exc}. Every path must be relative to the workspace root, like src/app.py: never "
                    "absolute, never containing '..', never inside .git.",
                    role="fixer",
                ) from exc
            except ValueError as exc:
                # An edit whose old_text does not match the file is the fixer's
                # mistake — a contract failure, not an internal error.
                message = str(exc)
                if message.startswith("edit failed for "):
                    message += (
                        ". For action \"edit\", old_text must match the file's current text exactly and appear "
                        "exactly `count` times: include enough surrounding lines to make it unique, set count "
                        "to 0 to replace every occurrence, or use action \"update\" with the file's complete "
                        "new content."
                    )
                raise AgentOutputInvalid(message, role="fixer") from exc
            applied.update(files=files, summaries=summaries, wants_pass=wants_pass)

        await self.orchestrator.run_agent(
            "fixer", goal_id, step.id,
            f"Step: {step.title}\n{step.description}\n\n"
            f"{feedback_text}"
            f"{guidance_section}"
            f"What the librarian found:\n{self._evidence_text(evidence or {})}\n"
            f"Suggested paths (current contents):\n{ctx}{unreadable}"
            f"{design_section}",
            accept=accept,
        )
        files = applied["files"]
        summaries = applied["summaries"]
        wants_pass = bool(applied["wants_pass"])
        if not files:
            self._log(goal_id, step.id, "warn", "fixer returned an empty files list — no changes will be written")
        if dry_run:
            # Persist the proposal so "Apply" can write these exact contents
            # later. An empty list clears the step's previous proposal — a retry
            # that now proposes nothing must not leave stale files applyable.
            self._store_proposed_files(goal_id, step.id, files, summaries)
        self._publish_changes(goal_id, step.id, summaries, dry_run)
        self._set_step(goal_id, step, "IN_PROGRESS", last_agent_role="fixer")
        return summaries, wants_pass

    def _publish_changes(self, goal_id: str, step_id: str, summaries: list[dict[str, Any]], dry_run: bool) -> None:
        """Emit one diff per file that actually changed, and say so when none did.

        A fixer can propose the contents a file already has (a no-op it does not
        know is one). Reporting that as a changed file would put a path in the
        chat's summary and hand the scribe a commit message for a commit that
        cannot happen — so unchanged entries are stated instead.
        """
        changed = [s for s in summaries if s.get("changed", True)]
        for s in changed:
            payload = {"path": s["path"], "unified_diff": s["unified_diff"]}
            if s.get("diff_note"):
                payload["note"] = s["diff_note"]
            self.goals.publish(self._event(goal_id, step_id, "diff", payload))
        unchanged = [s["path"] for s in summaries if not s.get("changed", True)]
        if unchanged:
            self._log(
                goal_id, step_id, "info",
                f"no change: {', '.join(unchanged)} — the proposal matches what is already there",
            )
        self.goals.publish(self._event(
            goal_id, step_id, "file_change_summary",
            {"paths": [s["path"] for s in changed], "dry_run": dry_run,
             "unchanged": unchanged},
        ))

    async def _verifier(
        self, goal_id: str, step: PlanStep, ws: Any, evidence: dict[str, Any] | None = None,
        prior_failure: dict[str, Any] | None = None, diffs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Get a test verdict, treating a refused command as information.

        Returns the step's test outcome — the same fields the `test_result`
        event carries — because the critic is asked to judge these changes
        *after* the tests ran, and a critic that does not know the verdict can
        approve code whose tests just failed.

        A test command that hangs must not wedge the goal: it is reported to the
        verifier as exit 124 so it can return the verdict it owes. A command the
        sandbox rejects is not a failing test — nothing ran. The
        verifier is the agent that can choose an allowed runner instead, so the
        refusal is fed back to it exactly like command output is, and the goal
        survives. Failing the whole goal over `touch` (a real case: the model
        proposed it to create a file, and the step died with
        `command_not_allowed`) threw away work that had already been written for
        a reason the model could have fixed itself.

        Bounds hold: a command may be *executed* at most once per step, a refusal
        executes nothing so it does not consume that budget, and refusals are
        capped — after `MAX_REFUSED_TEST_COMMANDS` the verifier must answer with a
        verdict.
        """
        # DRAFT RECONSTRUCTION (docs/04 §4.0a.1, §4.0a.2). The mechanical brand
        # check is the engine's own finding about the bytes on disk, not the
        # model's, so it runs whatever this step is about to verify and rides on
        # the same `test_result` record as the verdict.
        design = self._design_for(goal_id)
        brand_drifts = self._brand_drifts(diffs or [], design, ws.root_path)
        for drift in brand_drifts:
            self._log(goal_id, step.id, "warn", f"brand contract drift: {drift}")
        write_path = self._deliverable_write_path(step, design)
        if write_path:
            return await self._verifier_deliverable(
                goal_id, step, ws.root_path, write_path, brand_drifts,
            )

        prompt = f"Step: {step.title}\n{step.description}"
        found_test_command = (evidence or {}).get("test_command")
        if found_test_command:
            # Offered, not trusted: the librarian read it out of a manifest and
            # the verifier is the role that finds out whether it is real.
            prompt += (
                "\nThe librarian reports this repository's test command as: "
                f"{' '.join(str(t) for t in found_test_command)} (unverified — it may be wrong "
                "or not allowlisted)."
            )
        if prior_failure:
            prompt += (
                f"\n\nNote: a previous attempt at this step failed ({prior_failure.get('explanation', '')}). "
                "The code has just been changed in response. Verify it afresh."
            )
        # Every call is stateless (no conversation memory between calls), so the
        # refusal/run feedback below must ride on this context rather than
        # replace it — a prompt that is only the refusal leaves the verifier
        # ruling on a step it can no longer see.
        base_prompt = prompt
        argv: list[str] | None = None
        result: dict[str, Any] | None = None
        refusals: list[str] = []
        proposals_left = 1 + MAX_REFUSED_TEST_COMMANDS
        timeout_s = 120

        while True:
            out = await self.orchestrator.run_agent("verifier", goal_id, step.id, prompt)
            proposed = out.get("argv")
            # The contract spells "nothing to run" as null. An empty list or an empty string can only mean
            # the same thing — Qwen2.5-1.5B answered `{"argv": [], "verdict": "skip"}` on three of eleven
            # tasks — and running nothing is the one reading that cannot do harm.
            if proposed == [] or proposed == "":
                proposed = None

            # Verdict-only answer: either it never needed a command, or it has
            # just been told what happened to the one it asked for.
            if proposed is None:
                outcome = {
                    "argv": argv,
                    "verdict": out.get("verdict"),
                    "explanation": out.get("explanation"),
                    "exit_code": result["exit_code"] if result else None,
                    "refused": refusals or [],
                    "ran": argv is not None,
                    "brand_drifts": brand_drifts,
                }
                # Explicit fields rather than **-splatting: `outcome` also
                # carries `ran`/`refused` (event-payload keys the critic's
                # rendering uses) that the publisher has no parameters for.
                self._test_result(
                    goal_id, step, argv=outcome["argv"],
                    verdict=outcome["verdict"], explanation=outcome["explanation"],
                    exit_code=outcome["exit_code"], refusals=outcome["refused"],
                    brand_drifts=brand_drifts,
                )
                self._set_step(goal_id, step, "IN_PROGRESS", last_agent_role="verifier")
                return outcome

            if result is not None:
                raise AgentOutputInvalid("verifier second call must have argv null", role="verifier")
            if proposals_left <= 0:
                raise AgentOutputInvalid(
                    "verifier kept proposing commands the sandbox refused: " + "; ".join(refusals),
                    role="verifier",
                )
            proposals_left -= 1

            if not isinstance(proposed, list) or not proposed or not all(isinstance(a, str) for a in proposed):
                raise AgentOutputInvalid(f"verifier argv must be a non-empty string list, got {proposed!r}", role="verifier")
            try:
                # The sandbox is shared: under a parallel goal another step's
                # test must not run in the tree this command is measuring.
                async with self._sandbox_lock:
                    result = await asyncio.to_thread(
                        self.sandbox.run_command, ws.root_path, proposed, timeout_s=timeout_s,
                    )
            except subprocess.TimeoutExpired:
                # A hang is not a refusal (nothing was proposed wrongly, so it is
                # not the verifier's fault) and not a pass — the command may still
                # be spinning. Treat it exactly like a failing run: report the
                # exit as 124 (timeout(1)'s code), hand the output back, and let
                # the verifier answer the verdict it already owes.
                argv = proposed
                result = {
                    "argv": proposed,
                    "exit_code": 124,
                    "stdout": "",
                    "stderr": f"timed out after {timeout_s}s (killed)\n"
                    + "\n".join(refusals),
                }
            except CommandNotAllowed as exc:
                # CommandNotAllowed carries a code and a message string, not a
                # `.message` attribute.
                refused_because = str(exc)
                refusals.append(f"{' '.join(proposed)} — {refused_because}")
                self._log(
                    goal_id, step.id, "warn",
                    f"test command refused by the sandbox, nothing was run: "
                    f"{' '.join(proposed)} ({refused_because})",
                )
                refusal_note = (
                    f"The command {proposed!r} was NOT run: the sandbox refused it "
                    f"({refused_because}). Propose a different command from the allowed set, or "
                    "return the verdict with argv null and say that no permitted test "
                    "command exists. Allowed: pytest, python -m pytest, npm test, pnpm test, "
                    "cargo test, go test ./..., ruff check, mypy, tsc --noEmit, cargo check, "
                    "cargo clippy, go vet ./..., make lint, make typecheck, read-only git "
                    "status/diff/log -1."
                )
                if proposals_left <= 0:
                    refusal_note = (
                        f"The command was NOT run — the sandbox refused it ({refused_because}). "
                        "It was your last attempt. Return the verdict with argv null."
                    )
                prompt = f"{base_prompt}\n\n{refusal_note}"
                continue

            argv = proposed
            # Same statelessness rule as the refusal path: the run feedback
            # rides on the step context, so the verdict call can still name
            # what it judged.
            prompt = (
                f"{base_prompt}\n\n--- Command output ---\n"
                f"Command ran. exit_code={result['exit_code']}\nstdout:\n{result['stdout']}\n"
                f"stderr:\n{result['stderr']}\nNow return the verdict with argv null."
            )

    async def _verifier_deliverable(
        self,
        goal_id: str,
        step: PlanStep,
        ws_root: str,
        path: str,
        brand_drifts: list[str],
    ) -> dict[str, Any]:
        """Review a document instead of running something (docs/04 §4.0a.2).

        There is no code here to falsify: the artifact is prose, and a command
        would be a guess that costs a round to discover it cannot judge. The
        content is the file on disk when it is there and the step's stored
        proposal when it is not — the same bytes Apply replays, read through the
        same helper the critic uses, so the two judges cannot disagree about
        what they reviewed.

        `skip` is reserved for the genuine absence. A step that proposed
        nothing *and* wrote nothing has delivered no file, and saying so is the
        honest verdict; an empty draft is a proposal, and an empty contract is a
        failure a reviewer must be able to state.
        """
        artifact, where = self._deliverable_artifact(goal_id, step, ws_root, path)
        lines = [
            f"Step: {step.title}\n{step.description}",
            "",
            "This step delivers a document, not code. There is nothing here to falsify "
            "with a command: do not run a command, and do not answer 'skip' because it "
            "is not on disk. Review the artifact below as prose and return your verdict "
            "directly, with argv null.",
        ]
        if artifact is None:
            lines += [
                "",
                f"No {path} was found on disk, and this step proposed none either, so "
                f"there is nothing to review. Answer with verdict 'skip' and name what is "
                f"missing — do not pass a step that delivered no {path}.",
            ]
        else:
            lines += [
                "",
                f"--- {path} {where} ---",
                artifact[:MAX_DESIGN_MD_CHARS],
                f"--- end {path} ---",
                "",
                "Answer 'pass' when it is a complete, faithful realization of the "
                "contract, and 'fail' naming the specific gaps when it is not.",
            ]
        out = await self.orchestrator.run_agent(
            "verifier", goal_id, step.id, "\n".join(lines),
        )
        outcome = {
            "argv": None,
            "verdict": out.get("verdict"),
            "explanation": out.get("explanation"),
            "exit_code": None,
            "refused": [],
            "ran": False,
            "brand_drifts": brand_drifts,
        }
        self._test_result(
            goal_id, step, argv=None, verdict=outcome["verdict"],
            explanation=outcome["explanation"], exit_code=None, refusals=[],
            brand_drifts=brand_drifts,
        )
        self._set_step(goal_id, step, "IN_PROGRESS", last_agent_role="verifier")
        return outcome

    async def _critic(
        self,
        goal_id: str,
        step: PlanStep,
        fs: FileSystemService,
        diffs: list[dict[str, Any]],
        evidence: dict[str, Any] | None = None,
        test_outcome: dict[str, Any] | None = None,
        ws_root: str = "",
    ) -> None:
        diff_lines = []
        for d in diffs:
            diff_lines.append(f"File: {d['path']} ({d['action']})")
            if d.get("unified_diff"):
                diff_lines.append(d["unified_diff"])
        diff_text = "\n".join(diff_lines) if diff_lines else "No changes proposed."

        # The verifier ran before the critic for exactly this: an approve that
        # ignores a failing test is a false claim about the changes' quality.
        # A verdict is always present (the verifier publishes one before this
        # call), so a missing block means wiring broke — say so rather than
        # letting the critic believe there were no tests.
        outcome = test_outcome or {}
        if outcome:
            ran = "ran `" + " ".join(outcome["argv"]) + "`" if outcome.get("ran") else "ran nothing"
            refused_note = (
                f" (refused: {'; '.join(outcome['refused'])})" if outcome.get("refused") else ""
            )
            verdict_text = (
                f"Test verdict: {outcome.get('verdict')} — {ran}, "
                f"exit {outcome.get('exit_code')}{refused_note}. {outcome.get('explanation') or ''}"
            )
        else:
            verdict_text = "Test verdict: NONE REPORTED — the verification stage produced no verdict."

        # The critic may need evidence the diffs cannot carry ("is this symbol
        # actually used?"). It asks for ONE read-only command per round, the
        # engine runs it through the same allowlist the librarian uses, and the
        # output goes back into the next review round. Bounded at 2: a critic
        # that still cannot decide after two commands is indecisive, and each
        # round is a model call.
        base_prompt = (
            f"Step: {step.title}\n{step.description}\n\n"
            f"{verdict_text}\n\n"
            f"What the librarian found:\n{self._evidence_text(evidence or {})}\n\nDiffs:\n{diff_text}"
            # The contract, so the judgment is against acceptance rather than
            # taste — and the mechanical findings the verifier already proved,
            # so this review does not re-litigate what text comparison settled.
            + self._critic_design_section(goal_id, step, ws_root, test_outcome)
        )
        commands_left = MAX_CRITIC_COMMANDS
        prompt = base_prompt
        while True:
            out = await self.orchestrator.run_agent("critic", goal_id, step.id, prompt)
            decision = out.get("decision")
            reasons = out.get("reasons") or []
            requested = out.get("run_command")
            if decision is None and isinstance(requested, list) and requested:
                if commands_left <= 0:
                    self._log(
                        goal_id, step.id, "warn",
                        "critic asked for another command after its last one — deciding from what it has",
                    )
                    raise AgentOutputInvalid(
                        "critic kept requesting commands without deciding", role="critic",
                    )
                commands_left -= 1
                argv = [str(a) for a in requested]
                try:
                    # Same shared-sandbox rule as the verifier: serialized so a
                    # concurrent step's writes cannot move under a read-only probe.
                    async with self._sandbox_lock:
                        result = await asyncio.to_thread(
                            self.sandbox.run_command,
                            ws_root, argv, timeout_s=READ_ONLY_TIMEOUT_S, mode="read_only",
                        )
                    output = (
                        f"Command output (exit {result['exit_code']}):\n"
                        f"stdout:\n{(result['stdout'] or '')[:4000]}\n"
                        f"stderr:\n{(result['stderr'] or '')[:2000]}"
                    )
                except CommandNotAllowed as exc:
                    output = f"The command was NOT run — the sandbox refused it: {exc}. Decide from what you have."
                except OSError as exc:
                    output = f"The command could not be run: {exc}. Decide from what you have."
                self._log(goal_id, step.id, "info", f"critic inspection: {' '.join(argv)}")
                # Same honesty rule as the planner's consult loop: the closing
                # invitation must reflect the real remaining budget. Offering
                # "one more command" on the final round made obeying the prompt
                # a contract violation.
                tail = (
                    "You may request one more read-only command."
                    if commands_left > 0
                    else "That was your last allowed command — give your decision now."
                )
                prompt = f"{base_prompt}\n\n--- Your requested inspection round ---\n{output}\n\nNow give your decision. {tail}"
                continue
            if decision == "approve":
                self._set_step(goal_id, step, "IN_PROGRESS", last_agent_role="critic")
                return
            if decision == "request-changes":
                if not reasons:
                    raise AgentOutputInvalid("critic request-changes requires >=1 reason", role="critic")
                self._set_step(goal_id, step, "IN_PROGRESS", review_notes="\n".join(reasons), last_agent_role="critic")
                self._log(goal_id, step.id, "warn", f"critic requested changes: {reasons}")
                self._set_status(goal_id, "PAUSED", step.id)
                raise CriticRejection("critic requested changes; human retry required", reasons)
            raise AgentOutputInvalid(f"critic decision invalid: {decision!r}", role="critic")

    def _critic_design_section(
        self, goal_id: str, step: PlanStep, ws_root: str, test_outcome: dict[str, Any] | None,
    ) -> str:
        """What the critic is told about the design contract, and the artifact.

        Three things ride on the review. The contract, so the decision is made
        against its acceptance lines. The findings the engine's own text
        comparison already produced, marked as settled — they are facts about
        the bytes, and the critic's judgment is better spent elsewhere. And, for
        a design deliverable, the document itself: a `+`-prefixed unified diff
        is a poor thing to approve a prose contract from, so the approver reads
        the same bytes the verifier reviewed.
        """
        design = self._design_for(goal_id)
        section = f"\n\n{self._design_text(design)}" if design else ""
        drifts = (test_outcome or {}).get("brand_drifts") or []
        if drifts:
            section += (
                "\n\nMechanical brand-contract findings — the engine already compared the "
                "written text against the binding contract. These are facts about the "
                "bytes, not opinions, so do not re-litigate them; spend your judgment on "
                "what text comparison cannot prove:\n"
                + "\n".join(f"- {d}" for d in drifts)
            )
        write_path = self._deliverable_write_path(step, design)
        if not write_path:
            return section
        artifact, where = self._deliverable_artifact(goal_id, step, ws_root, write_path)
        if artifact is None:
            return section
        subject = DELIVERABLE_SUBJECT.get(str(design.get("mode") or ""), "the deliverable")
        section += (
            f"\n\nThis step delivers {subject}. Read it below, not only the diff above: a "
            f"`+`-prefixed unified diff is a poor thing to approve a document from, and "
            f"only your approval puts it in front of them. Request-changes sends it back to "
            f"be revised instead; the user pins the file from here either way.\n\n"
            f"--- {write_path} {where} ---\n{artifact[:MAX_DESIGN_MD_CHARS]}\n"
            f"--- end {write_path} ---"
        )
        return section

    async def _scribe(
        self, goal_id: str, step: PlanStep, diffs: list[dict[str, Any]], root_path: str = "",
        dry_run: bool = False, test_outcome: dict[str, Any] | None = None,
    ) -> str:
        # The diffs themselves, not just their file names: the prompt tells the
        # scribe to describe "what changed and why, from the diff you are given",
        # and a bare path list made that a promise the executor never kept —
        # commit subjects were invented from file names alone. Same cap as the
        # critic's diff budget so one huge change cannot flood the prompt.
        diff_lines = []
        for d in diffs:
            diff_lines.append(f"File: {d['path']} ({d['action']})")
            if d.get("unified_diff"):
                diff_lines.append(d["unified_diff"])
        diff_text = "\n".join(diff_lines) if diff_lines else "No changes proposed."

        outcome = test_outcome or {}
        if outcome:
            verdict_text = (
                f"Test verdict: {outcome.get('verdict')}"
                + (f" — {outcome.get('explanation')}" if outcome.get("explanation") else "")
            )
        else:
            verdict_text = "Test verdict: NONE REPORTED."

        out = await self.orchestrator.run_agent(
            "scribe", goal_id, step.id,
            f"Step: {step.title}\n{step.description}\n\n"
            f"{verdict_text}\n\nDiffs:\n{diff_text}",
        )
        summary = out.get("summary")
        commit_message = out.get("commit_message")
        if not summary or not commit_message:
            raise AgentOutputInvalid("scribe requires summary and commit_message", role="scribe")
        # The critic judges with the verdict; the scribe describes with the diff.
        # Both used to be asked for a judgment the executor never gave them the
        # evidence for: a critic could approve failing changes, and a scribe told
        # to describe "the diff you are given" was given nothing but file names.
        # The message is stored on the step only once a commit has landed (below): the timeline shows a
        # stored message as "Commit: ...", so writing it here — before anything knew whether a commit would
        # follow — showed a commit in a plain folder, a dry run and a cancelled step. Until then the message
        # rides in the log, which loses nothing the model wrote and claims nothing that did not happen.
        self._set_step(goal_id, step, "IN_PROGRESS", last_agent_role="scribe")
        self._log(goal_id, step.id, "info", summary)

        if dry_run or not root_path or not self.git.is_git_repo(root_path):
            # A dry run commits nothing by design, and a workspace that is not a
            # repository has nothing to commit into. Both are the scribe doing its
            # job, so neither is reported as a failure — a per-role rate that
            # counted "not a repo" as a broken scribe would punish every user whose
            # workspace is a plain directory.
            if dry_run:
                self._log(goal_id, step.id, "info", f"dry run — nothing committed; the message would be: {commit_message}")
            else:
                self._log(
                    goal_id, step.id, "info",
                    "not a git repository — nothing committed, the change is on disk. "
                    f"The message would have been: {commit_message}",
                )
            return "skipped" if dry_run else "not_a_repo"
        # The last guard before the one irreversible act. A cancel that
        # landed during the critic's call must not end in a commit the user
        # asked to stop; the summary above still records what was done.
        if self._cancelled(goal_id):
            self._log(
                goal_id, step.id, "info",
                "cancelled — skipping the commit for this step",
            )
            return "cancelled"
        # Only what this step wrote. A bare `git add -A` would commit the
        # user's own half-finished work under this step's message. The git
        # lock serializes the index: two parallel steps committing at once
        # would otherwise interleave their staged paths.
        paths = [d["path"] for d in diffs]
        async with self._git_lock:
            commit_hash = await asyncio.to_thread(
                self.git.commit, root_path, commit_message, paths,
            )
        if commit_hash:
            self._set_step(goal_id, step, "IN_PROGRESS", commit_message=commit_message)
            self._log(goal_id, step.id, "info", f"git committed {commit_hash[:7]}: {commit_message}")
            return "committed"
        if paths:
            # Non-empty paths and no commit: either there was nothing left to
            # record (the files matched what is already committed) or git
            # refused. Silence here reads as "committed" to anyone watching.
            self._log(
                goal_id, step.id, "warn",
                "git commit produced nothing — the step's files match the last commit, "
                "or git refused (check its user.name/user.email config)",
            )
            return "nothing_to_commit"
        return "nothing_to_commit"
