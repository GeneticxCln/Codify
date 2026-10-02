"""The planning layer: from a goal to a stored plan.

The fourth layer of `ExecutorService` (see `engine/executor.py`); it calls `executor_design`, `executor_evidence`
and `executor_core`. The design stage it calls is a draft reconstruction (see `executor_design`).
"""

from __future__ import annotations

from typing import Any

from engine.executor_support import AgentOutputInvalid
from engine.laya import LayaDecision, build_state
from engine.library import LibraryService
from engine.models import Goal
from engine.providers import ProviderError
from engine.executor_design import _Design

# Follow-up reconnaissance calls the planner may make while planning (see
# run_planning). The evidence pack is frozen once the librarian finishes; this
# lets the planner reopen it when the pack provably misses what a step needs,
# instead of planning a guess. Same serving machinery, own bound.
MAX_PLANNER_CONSULTS = 1


class _Plan(_Design):
    """Planning: turn a goal into an ordered list of steps.

    Runs the pre-flight gate's verdict, the librarian, the design stage and the planner, and stores the
    steps. Calls the design, evidence and core layers."""

    async def run_planning(self, goal_id: str) -> None:
        """Plan a goal — as its one driver, for as long as it takes.

        The claim is what keeps a second writer off the goal (review of 2026-09-29, finding 2): Start,
        Delete and a retry all consult it, and a goal being planned is not one they may act on.
        """
        if not self.claim_driver(goal_id):
            self._log(goal_id, None, "warn", "another driver is already working on this goal — it is not planned twice")
            return
        try:
            await self._plan_goal(goal_id)
        finally:
            self.release_driver(goal_id)

    async def _plan_goal(self, goal_id: str) -> None:
        """The planning pipeline itself. Callers hold the goal's driver claim (`run_planning`, `run_chat`)."""
        goal = self.goals.get(goal_id)
        ws = self.workspaces.get(goal.workspace_id)

        if self.goals.steps(goal_id):
            # A goal has one plan. A second one collides with the first on `(goal_id, ordinal)` — a raw
            # IntegrityError the goal used to die of after the first plan was already written — and would
            # otherwise run every step twice.
            self._log(goal_id, None, "info", "this goal already has a plan — it is not planned a second time")
            return

        self._preflight_roles(goal_id)

        # ── Laya: System-1 pre-flight gate ──────────────────────────────
        # Cheap typed decisions (intent / risk / injection probability) before
        # any LLM call. High-confidence injection stops the goal here.
        async with self._stage(goal_id, "laya", "laya") as laya_stage:
            try:
                decision = await self.laya.decide(
                    build_state(goal, ws.root_path),
                    on_call=self.orchestrator.gate_call(goal_id),
                )
                if decision.blocked:
                    laya_stage.record("block", decision.block_reason or None)
                elif decision.engine == "skipped":
                    # A gate that was never set up and a gate whose call just
                    # failed both report `engine="skipped"`; only the second is
                    # the role not doing its job, and `unavailable` is what
                    # keeps a broken gate out of `STAGE_SUCCESS_OUTCOMES`.
                    laya_stage.record(
                        "unavailable" if decision.unavailable else "skipped",
                        decision.skipped_reason or None,
                    )
                else:
                    laya_stage.record("allow")
            except Exception as exc:  # pragma: no cover - decide() already guards
                decision = LayaDecision(engine="skipped", skipped_reason=f"gate error: {exc}")
                # The block reached the end without declaring an outcome, and a
                # gate that raised is the one outcome that is not a clean skip.
                laya_stage.record("unavailable", decision.skipped_reason)
        if decision.engine != "skipped":
            self.goals.publish(self._event(
                goal_id, None, "agent_assigned",
                {"role": "laya", "provider": decision.provider or decision.engine, "model": decision.model},
            ))
            self.goals.publish(self._event(
                goal_id, None, "laya_decision", decision.to_payload(),
            ))
        for warning in decision.warnings:
            self._log(goal_id, None, "warn", warning)
        if decision.blocked:
            self._fail(
                goal_id, None, "laya_blocked",
                decision.block_reason or "blocked by Laya",
                role="laya",
            )
            return
        if decision.engine == "skipped":
            self._log(goal_id, None, "info", f"System-1 gate skipped: {decision.skipped_reason}")

        # ── Librarian: reconnaissance before anything is decided ────────
        # The planner used to receive a title and a description and nothing else,
        # so its steps were guesses; the fixer then read only the paths it had
        # guessed. One bounded reconnaissance pass fixes that for the whole goal.
        try:
            async with self._stage(goal_id, "librarian", "librarian") as lib_stage:
                evidence = await self._librarian(goal_id, goal, ws)
                lib_stage.record("incomplete" if evidence.get("capped") else "pack")
        except (AgentOutputInvalid, ProviderError, ValueError) as exc:
            # A librarian that cannot run must not kill a goal that might still
            # work: the planner is told there is no evidence and proceeds.
            self._log(
                goal_id, None, "warn",
                f"librarian unavailable ({getattr(exc, 'code', 'error')}: {exc}) — "
                "planning without evidence",
            )
            evidence = {}

        # ── Design: the direction, locked before anything is planned ───
        # DRAFT RECONSTRUCTION (docs/04 §4.0a, §4.0a.2). After the librarian
        # — it cannot lock a direction from a blank page — and before the
        # planner, which plans against whatever it is handed. It is an aid, so
        # it gets the librarian's rule: a failure here is a warning and
        # planning continues without a contract, because an aid that can kill a
        # goal is a liability rather than an aid.
        try:
            async with self._stage(goal_id, "design", "design") as design_stage:
                if goal.mode == "design":
                    design = await self._design_deliverable(goal_id, goal, ws, evidence)
                    design_stage.record("contract")
                elif goal.mode == "knowledge":
                    design = await self._knowledge_deliverable(goal_id, goal, ws, evidence)
                    design_stage.record("contract")
                else:
                    design = await self._design(goal_id, goal, ws, evidence)
                    design_stage.record("contract" if design else "declined")
        except (AgentOutputInvalid, ProviderError, ValueError) as exc:
            self._log(
                goal_id, None, "warn",
                f"design unavailable ({getattr(exc, 'code', 'error')}: {exc}) — "
                "planning without a design contract",
            )
            design = {}

        await self._plan_steps(goal_id, goal, ws, evidence, design)

    async def _plan_steps(
        self,
        goal_id: str,
        goal: Goal,
        ws: Any,
        evidence: dict[str, Any],
        design: dict[str, Any],
        task: str | None = None,
        fail_goal: bool = True,
    ) -> None:
        """Ask the planner for steps, with its bounded consult loop, and store them.

        Split out of `run_planning` so the conductor's `plan` move can reach the
        planner *without* reaching the rest of the recipe: the gate has already
        run for a turn, and the librarian and the designer are moves the
        conductor may or may not have chosen. What is left here is the part that
        is not a decision — one planning call, at most `MAX_PLANNER_CONSULTS`
        follow-ups to the librarian when the evidence has a hole in it, and the
        steps stored with the goal moved to PENDING.

        `task` is the conductor's own framing of what to plan. The step
        descriptions still come from the planner, because the plan is what the
        user reviews and approves and a model should not be able to relabel it
        on the way past.

        `fail_goal` is the recipe's behaviour: a planner that cannot produce a plan ends the goal, because
        nothing else in the recipe can run without one. The conductor's `plan` move passes False and gets the
        error re-raised instead: it is one move among several, the conductor decides what a failed plan means,
        and a goal marked FAILED under it could not be planned again, nor completed, nor resumed (the turn
        then asked for COMPLETED from FAILED, which is not a legal move).
        """
        prompt = (
            f"Title: {goal.title}\nDescription:\n{task or goal.description}\n\n"
            f"Librarian evidence:\n{self._evidence_text(evidence)}\n"
            f"Design direction:\n{self._design_text(design)}"
        )
        try:
            # A plan built on a blind spot is worse than a late question: the
            # planner may ask the librarian for one bounded follow-up (same
            # request shapes, same read-only serving) when the evidence pack
            # misses what a step needs. The reply merges into the prompt and
            # planning continues; a second ask is refused as a contract error.
            consults_left = MAX_PLANNER_CONSULTS
            round_no = 0
            while True:
                round_no += 1
                cancelled = False
                async with self._stage(goal_id, "planner", "planner", ordinal=round_no) as plan_stage:
                    out = await self.orchestrator.run_agent(
                        "planner", goal_id, None, prompt, accept=self._accept_plan_reply,
                    )
                    # A cancel that landed while the planner was thinking must
                    # win: a goal the user cancelled must not reappear as PENDING
                    # with a plan they explicitly stopped. (PLANNING is a legal
                    # cancel state.) Declared here so the discarded plan is not
                    # measured as a produced one.
                    if self.goals.get(goal_id).status == "CANCELLED":
                        cancelled = True
                        plan_stage.record("cancelled")
                    else:
                        # Declared from the reply rather than after the decision
                        # block below, so the measurement covers the call that
                        # produced the outcome without re-indenting the
                        # bookkeeping that follows it. A consult is a real
                        # answer, not a failure: the planner asked for what it
                        # was missing and got it.
                        consult = out.get("consult") if isinstance(out, dict) else None
                        plan_stage.record(
                            "consult"
                            if not out.get("steps") and isinstance(consult, dict)
                            and (consult.get("reads") or consult.get("searches")
                                 or consult.get("git") or consult.get("run"))
                            else "plan"
                        )
                if cancelled:
                    self._log(goal_id, None, "info", "cancelled during planning — discarding the plan")
                    return
                consult = out.get("consult") if isinstance(out, dict) else None
                if (
                    not out.get("steps")
                    and isinstance(consult, dict)
                    and (consult.get("reads") or consult.get("searches")
                         or consult.get("git") or consult.get("run"))
                ):
                    if consults_left <= 0:
                        raise AgentOutputInvalid(
                            "planner consulted the librarian after its last allowed follow-up",
                            role="planner",
                        )
                    consults_left -= 1
                    ws_root = ws.root_path
                    served, _opened, _matched, refused = await self._serve_library_requests(
                        goal_id, LibraryService(ws_root),
                        self._library_requests(consult),
                    )
                    self._log(
                        goal_id, None, "info",
                        "planner consulted the librarian"
                        + (f" ({refused} request(s) refused)" if refused else ""),
                    )
                    self.goals.publish(self._event(
                        goal_id, None, "plan_consult",
                        {"refused": refused, "material_chars": len(served)},
                    ))
                    # The invitation must match the budget: on the last allowed
                    # follow-up this previously still offered "one more", and a
                    # planner that took the offer failed its own contract on the
                    # next round. Say how many are actually left.
                    if consults_left > 0:
                        tail = (
                            f"You may ask {consults_left} more follow-up"
                            f"{'s' if consults_left != 1 else ''} after this one."
                            if consults_left > 1
                            else "This was your last follow-up — produce the plan now."
                        )
                    else:
                        tail = "You have no follow-ups left — produce the plan now."
                    prompt = (
                        f"{prompt}\n\n--- The librarian answered your follow-up ---\n{served}\n\n"
                        f"Now produce the plan. {tail}"
                    )
                    continue
                steps = self._parse_steps(out)
                self._insert_steps(goal_id, steps)
                self._log(goal_id, None, "info", f"planner produced {len(steps)} steps")
                break
        except (AgentOutputInvalid, ProviderError, ValueError) as exc:
            if not fail_goal:
                raise
            # Planning is the planner's phase; anything raised here is its.
            self._fail(
                goal_id, None, getattr(exc, "code", "agent_output_invalid"), str(exc),
                role=getattr(exc, "role", None) or "planner",
            )
            return
        # The planner-call check above is where a cancel is normally caught, but
        # it is not the last word: `_set_status` reads the goal's *current*
        # version, so nothing downstream would stop a PENDING write from landing
        # on top of a CANCELLED one and reviving a goal the user had stopped.
        if self.goals.get(goal_id).status == "CANCELLED":
            self._log(goal_id, None, "info", "cancelled before the plan was accepted — leaving it cancelled")
            return
        self._set_status(goal_id, "PENDING", None)
