"""The conductor layer: answering a turn, and driving an approved plan with the conductor's moves.

The top layer of `ExecutorService` (see `engine/executor.py`); it calls `executor_plan` and `executor_core`. The
moves themselves are `conductor_tools.ConductorTools`; this layer owns the turn's prompt, the model choice
and the fallback when no tool-capable model is configured.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from engine.chat_prompts import CHAT_SYSTEM_PROMPT, CONDUCTOR_SYSTEM_PROMPT
from engine.conductor import BASE_TOOLS, Conductor, DEFAULT_MAX_MOVES, DEFAULT_MAX_TURNS, STEP_TOOLS
from engine.conductor_tools import ConductorTools, _Conducted
from engine.executor_support import AgentNotConfigured
from engine.laya import LayaDecision, build_state
from engine.models import AgentConfig, AgentRole, BUILTIN_PROVIDERS, Goal
from engine.providers import ProviderError
from engine.recall import build_brief
from engine.services import ApiError, custom_provider_address
from engine.skills import SkillSet, load_skills
from engine.toolcall import ToolSpec
from engine.executor_plan import _Plan


# How many earlier turns of a thread are put in front of the model answering the
# next one (see `_turn_prompt`). A conversation the model cannot remember is the
# defect docs/09 §10 exists to close, and this is what makes "now do the other
# one" mean anything. Bounded because a thread is unbounded and a prompt is
# not, and because the most recent exchange is what a follow-up refers to — so
# the oldest turns are the ones worth losing.
TURN_HISTORY_TURNS = 12


def _clip_text(text: str, limit: int) -> str:
    """Trim to a limit, marking that something was dropped.

    Both ends are kept, the same rule `laya._clip` uses on a request: a turn
    that opens "here are the 40 files I need you to look at" and closes with the
    actual question loses the question to a middle-ellipsis.
    """
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    half = max(1, limit // 2)
    return f"{clean[:half]}…[{len(clean) - 2 * half} chars elided]…{clean[-half:]}"


# The keys a turn's answer is most likely to have been wrapped in, most useful
# first. Mirrors the UI's `ui/src/replyPreview.ts` deliberately: two readers of
# the same turn must strip the same envelope, or the transcript's collapsed
# preview and its expanded text disagree about what was said.
_TURN_REPLY_KEYS = (
    "answer", "message", "reply", "response", "text", "summary", "detail",
    "explanation", "result", "error",
)

if TYPE_CHECKING:
    from engine.executor import ExecutorService


def _as_prose(raw: str) -> str:
    """Unwrap a structured reply, because the model did it anyway.

    `CHAT_SYSTEM_PROMPT` opens by telling the model not to reply with JSON. A
    code model asked a direct question does it regardless — measured on a live
    qwen2.5-coder:7b, which answered a turn with
    `{"error": "I cannot read or access files…"}` — and the user reads a brace
    and a colon where a sentence was meant to be. That is the same complaint
    that started this work, one layer down.

    So a reply that parses as a JSON object and carries a readable string field
    loses the envelope. A reply that is JSON but has nothing readable in it is
    returned **unchanged**: dropping the envelope to reveal nothing useful would
    turn an odd-looking answer into an empty one, which is worse.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    candidate = text
    if candidate.startswith("```"):
        candidate = candidate.strip("`").strip()
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].strip()
    if not candidate.startswith("{"):
        return text
    try:
        parsed = json.loads(candidate)
    except ValueError:
        # Still prose as far as the reader is concerned: an object that does not
        # parse is a truncated stream or an odd sentence, and stripping it to
        # nothing would replace something readable with nothing.
        return text
    if isinstance(parsed, dict):
        for key in _TURN_REPLY_KEYS:
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # Named `error` with no prose key still beats a raw object: the model
        # already put the sentence somewhere, it just chose the wrong container.
        strings = [v for v in parsed.values() if isinstance(v, str) and v.strip()]
        if len(strings) == 1:
            return strings[0].strip()
    # Nothing readable. The envelope is dropped only when it was hiding
    # something; dropping it to reveal nothing turns an odd answer into an empty
    # one, which is worse.
    return text


# The gate labels that describe a *change*, the only ones that point the conductor at
# `ship-a-change` and arm its reminder. `question`, `other` and an unlabelled request
# are answered directly (see `_intent_brief`). The fallback below, for a turn with no
# conductor or one that could not finish, is a different decision with its own
# rationale — planning is a superset of answering — and is not this set.
CHANGE_INTENTS = ("code_change", "ops_command")


class _Conduct(_Plan):
    """Chat turns and the conductor loop that drives a goal.

    A turn is answered (or escalated into a goal) by the conductor; an approved plan is resumed by it.
    Calls the planning layer and the core."""

    # ── a turn: the gate's other answer ─────────────────────────────────

    async def run_chat(self, goal_id: str) -> None:
        """Answer a turn — as the goal's one driver, for as long as the turn runs.

        The conductor's `plan` move leaves the goal `PENDING` while the turn goes on to write its answer, and
        `PENDING` is what Start accepts. Without the claim, pressing Start then began a second driver on a
        goal whose turn was still running, and Delete was allowed too (review of 2026-09-29, finding 2).
        Start and Delete both consult `is_driving`; Cancel deliberately does not.
        """
        if not self.claim_driver(goal_id):
            self._log(goal_id, None, "warn", "another driver is already working on this goal — this turn was not run")
            return
        try:
            await self._answer_turn(goal_id)
        finally:
            self.release_driver(goal_id)

    async def _answer_turn(self, goal_id: str) -> None:
        """Answer a turn, or hand it to the pipeline.

        The sibling of `run_planning`, spawned by the turns route instead of
        `POST /goals`, and the whole reason a greeting does not start eight
        agents. The gate has already been asked what kind of request this is —
        `laya` scores intent, risk, injection and ambiguity on every goal, and
        until now every answer it produced was used the same way: run the
        pipeline. So the branch is here, on the one signal that was computed and
        then thrown away.

        Three outcomes, and the third is the point:

        * **blocked** — the injection gate fires, exactly as in planning. A
          blocked turn is a blocked goal, same code, same event, because it is
          the same gate guarding the same engine.
        * **question** — the conductor, if this install can run one, and
          otherwise a single model call. Either way: no plan, no steps, no
          verifier. The conductor is an upgrade and never a prerequisite
          (`_conduct` returns None and the turn degrades), because a user must
          be able to ask a question on any install.
        * **anything else** — the full pipeline, by delegating to
          `run_planning` on this same goal. Not a copy of it: the delegation, so
          there is one implementation of planning and a turn that needs a plan
          gets the identical one.

        The history is what makes a second turn mean anything. A thread's earlier
        prompts and replies are read back from the rows that already hold them
        (`GoalService.turn_history`) and put in front of the model, because the
        common follow-up — "now do the other one" — is unintelligible without
        them, and an assistant that forgets what it just said is the thing this
        whole feature exists to stop.

        The gate runs on every turn, and on a turn it does not *report* — see
        the announcement below. A goal's log is a run's audit trail, where the
        gate's verdict belongs; a turn's log is a conversation, and a verdict
        card over the top of "hi" says the person was classified before they
        were answered (docs/09 §10.15).
        """
        goal = self.goals.get(goal_id)
        ws = self.workspaces.get(goal.workspace_id)

        # Measured as the `laya` stage, which is what it is: the same gate, on
        # the same goal, doing the same job as it does in `run_planning`. A
        # "turn" stage would have to be a ninth entry in STAGE_OUTCOMES, and
        # that vocabulary is keyed by role (tests/test_metrics.py) — so naming it
        # after the role would be the truth anyway, and the gate's outcome lands
        # in the same per-stage table as every other goal's.
        async with self._stage(goal_id, "laya", "laya") as gate_stage:
            try:
                decision = await self.laya.decide(
                    build_state(goal, ws.root_path),
                    on_call=self.orchestrator.gate_call(goal_id),
                )
                if decision.blocked:
                    gate_stage.record("block", decision.block_reason or None)
                elif decision.engine == "skipped":
                    # The same distinction `run_planning` draws, for the same
                    # reason: a gate nobody configured and a gate whose call
                    # failed both report `engine == "skipped"`, and only the
                    # second is the role not doing its job. Scoring a broken
                    # gate as a deliberate skip is a measurement lie —
                    # `skipped` is in STAGE_SUCCESS_OUTCOMES and `unavailable`
                    # is not.
                    gate_stage.record(
                        "unavailable" if decision.unavailable else "skipped",
                        decision.skipped_reason or None,
                    )
                else:
                    gate_stage.record("allow")
            except Exception as exc:  # pragma: no cover - decide() already guards
                decision = LayaDecision(
                    engine="skipped", skipped_reason=f"gate error: {exc}", unavailable=True
                )
                gate_stage.record("unavailable", decision.skipped_reason)
        # Announced only when the gate has something a person has to act on.
        #
        # The gate still ran, and it still gated: the stage above recorded the
        # outcome, `tests/test_turns.py` reads that record to prove it, and the
        # block below still refuses the turn. What changed is the audience. A
        # goal's log is an audit trail — a run is eight roles and the reader
        # wants to see who was dispatched — while a turn's log is a
        # conversation, and "Laya gate passed (intent: question, risk 0.00)" over
        # the top of "hi" reads as a pipeline that vetted the user before
        # answering them. That is the behaviour docs/09 §10 exists to remove, and
        # publishing the verdict was the part of it still on screen.
        #
        # Blocked and warned turns keep both events: a refusal with no reason on
        # screen is a bug report, and a warning the gate raised is the gate doing
        # its job loudly rather than a report about nothing. A skipped gate never
        # carries warnings (`laya.decide`), so this condition also subsumes the
        # `engine != "skipped"` it replaces.
        if decision.blocked or decision.warnings:
            self.goals.publish(self._event(
                goal_id, None, "agent_assigned",
                {"role": "laya", "provider": decision.provider or decision.engine,
                 "model": decision.model},
            ))
            self.goals.publish(self._event(
                goal_id, None, "laya_decision", decision.to_payload(),
            ))
        for warning in decision.warnings:
            self._log(goal_id, None, "warn", warning)
        if decision.blocked:
            self._fail(
                goal_id, None, "laya_blocked",
                decision.block_reason or "blocked by Laya", role="laya",
            )
            return

        # The conductor decides everything, when there is one.
        #
        # It may answer, it may recon and plan, it may send the librarian to look
        # at three files and then decide nothing needs changing. All of those are
        # decisions, and honouring them is the point of having a brain. What it
        # is *not* allowed to do is fail silently: `_Conducted.finished` is False
        # only when its model errored or it spent its whole call budget without
        # producing either an answer or a plan, and on that path the engine runs
        # the sequence it would have run before the conductor existed.
        #
        # So the old behaviour is still the floor. It is just no longer the
        # ceiling.
        intent = decision.intent
        root = ws.root_path or ""

        if root:
            conducted = await self._conduct(goal_id, goal, root, intent=intent)
            if conducted.cancelled:
                # The user stopped this turn. `run_planning` guards its writes for exactly this;
                # `run_chat` did not, and ended with an unconditional COMPLETED that overwrote
                # CANCELLED and published the reply anyway.
                self._log(goal_id, None, "info", "cancelled while the conductor was running — nothing more was done")
                return
            if conducted.finished:
                reply = _as_prose(conducted.answer or "") or "(no answer)"
                self.goals.publish(self._event(
                    goal_id, None, "log",
                    {"level": "info", "message": reply, "turn": True},
                ))
                if not self.goals.steps(goal_id):
                    # Nothing was planned, so this turn is a finished answer.
                    #
                    # When the gate read the request as a change, that is worth
                    # saying out loud. A weak model narrates the sequence it
                    # means to run — observed live, where a 7B called `use_skill`
                    # and `recon` and then replied "2. `plan` — Turn the request
                    # into steps" without planning anything. The turn is not a
                    # failure (the model may have decided, with evidence, that no
                    # change was needed) so it is not overridden; but it must not
                    # read as success either, or a workspace that nobody touched
                    # looks like one that was updated.
                    if intent in CHANGE_INTENTS:
                        self._log(
                            goal_id, None, "warn",
                            f"the gate read this as {intent!r} and the conductor "
                            "finished without planning anything: no file was "
                            "changed. If you wanted this done, ask again and say "
                            "so plainly.",
                        )
                    self._complete_turn(goal_id)
                # With steps, planning already left the goal PENDING and that
                # stands. "Here is the plan, approve it" is not a finished
                # goal, and marking it COMPLETED would clear the very state the
                # approval gate reads.
                return
            if conducted.planned:
                # It got as far as a plan before it stopped. That plan is the goal's plan: running the
                # standard sequence now would plan the goal a second time, on top of the first.
                self._log(
                    goal_id, None, "warn",
                    f"the conductor stopped early ({conducted.explanation()}), but the plan it made stands — "
                    "it is waiting for you to approve it, or you can ask again",
                )
                return
            if not conducted.unavailable:
                self._log(
                    goal_id, None, "warn",
                    f"the conductor did not finish this turn ({conducted.explanation()}) "
                    "— running Codify's own sequence instead",
                )
                self.goals.publish(self._event(
                    goal_id, None, "log",
                    {
                        "level": "warn",
                        "message": (
                            "the conductor could not finish this request, so Codify "
                            "ran its standard sequence"
                        ),
                    },
                ))
        else:
            self._log(
                goal_id, None, "info",
                "this workspace has no root path, so there is nothing a conductor "
                "could read — answering without tools",
            )

        # Everything below is what this route did before the conductor could
        # decide: the recipe for anything that is not a plain question, and one
        # streamed call for anything that is.
        if intent != "question" or decision.engine == "skipped":
            if decision.engine == "skipped":
                why = (
                    f"the System-1 gate is not answering ({decision.skipped_reason}), "
                    "so nothing classified this request"
                )
            else:
                why = f"the gate classified this as {intent!r}"
            self._log(
                goal_id, None, "info",
                f"{why} — running the full pipeline. Open Settings → Agent Roles to "
                "point the gate at a model so questions can be answered directly.",
            )
            # Delegated, not reimplemented. `run_planning` sets PENDING when it
            # finishes; returning here without touching the status is what lets
            # one goal be either shape depending on what was asked. The claim is already held.
            await self._plan_goal(goal_id)
            return

        try:
            reply = _as_prose(await self._turn_reply(goal_id, goal)) or "(no answer)"
        except (ProviderError, AgentNotConfigured) as exc:
            self._fail(goal_id, None, getattr(exc, "code", "provider_error"), str(exc))
            return
        if self._is_cancelled(goal_id):
            return
        self.goals.publish(self._event(
            goal_id, None, "log", {"level": "info", "message": reply, "turn": True},
        ))
        self._complete_turn(goal_id)

    def _conductor_dispatch(
        self, goal_id: str, goal: Goal, root: str, skills: SkillSet
    ) -> dict[str, Any]:
        """One conductor run's tool table, bound to this service.

        The tools themselves — and the authority each one does and does not
        carry — live on `ConductorTools`, one named method each, so a single
        tool can be tested without standing the whole table up. This method is
        only the binding, and it is rebuilt per run on purpose: `state`, which is
        what one move hands the next inside a run, must not survive into the
        next turn, and a tool cannot be defined without appearing in the menu,
        because the menu *is* `ConductorTools.NAMES`.
        """
        # The tools are bound to the whole executor (they call a stage, a librarian read, the write
        # gate), and this layer is only ever the top of the `ExecutorService` it is instantiated as.
        tools = ConductorTools(cast("ExecutorService", self), goal_id, goal, root, skills)
        return {name: getattr(tools, name) for name in ConductorTools.NAMES}


    def _settings_int(self, key: str, default: int) -> int:
        """One engine setting, or the default.

        Every read of a setting here is best-effort on purpose: a goal must not
        fail because a row is missing, and a machine that has never been
        configured is the normal case rather than an error.
        """
        settings = getattr(self, "settings", None)
        if settings is None:
            return default
        try:
            return int(settings.get_int(key))
        except Exception:
            return default

    def _conductor_target(self) -> tuple[Any, str, Any] | None:
        """What a conductor would run on, or None when this install has none.

        None is the documented degradation (docs/09 §10.9): a provider with no
        tool support, no conductor model chosen, or a role whose configuration
        cannot be read. The conductor is an upgrade and never a prerequisite, so
        every caller has to be able to proceed without it.
        """
        targets = self._conductor_targets()
        return targets[0] if targets else None

    def _conductor_targets(self) -> list[tuple[Any, str, Any]]:
        """Every target this conductor may be called on, in order, best first.

        A role's chain is one row with a fallback column; the conductor's is two
        candidates, because it has no row. They come from different places on
        purpose: a conductor that borrowed the scribe's row inherits the scribe's
        own fallback, which is already configured and already has credentials
        resolved, while one configured on its own pair gets the explicit
        `conductor_fallback_*` pair.

        Every candidate is filtered rather than checked in order, so a primary
        that cannot serve a tool-calling loop at all — no key, a dead endpoint, a
        protocol that cannot call tools — does not hide a fallback that can. A
        target with no model, or with a provider that cannot call tools, is not
        a conductor at all; keeping it would only move the failure later and make
        it harder to read.
        """
        return self._resolve_conductor_targets()[0]

    def _resolve_conductor_targets(self) -> tuple[list[tuple[Any, str, Any]], list[str]]:
        """`_conductor_targets`, plus why each candidate the *settings named* was dropped.

        The reasons are only for a target a person chose (the conductor's own pair, or its own fallback
        pair). A conductor that borrows the scribe's row and cannot call tools is the documented quiet
        degradation to a plain answer; a conductor someone pointed at a provider and then silently
        ignored is a setting that appears to do nothing, so `_conduct` says why in the goal's log.
        """
        role = self._conductor_role()
        try:
            base = self.orchestrator.registry.get_config(role)
        except Exception:
            return [], []
        primary_cfg = self._conductor_config(base)
        candidates = [primary_cfg]
        borrowed = primary_cfg is base
        fallback_cfg = (
            self.orchestrator.registry.fallback_config_for(base)
            if borrowed
            else self._conductor_fallback_config(primary_cfg)
        )
        if fallback_cfg is not None:
            candidates.append(fallback_cfg)

        targets: list[tuple[Any, str, Any]] = []
        problems: list[str] = []
        for cfg in candidates:
            chosen = not borrowed and (cfg is primary_cfg or cfg is fallback_cfg)
            model = (cfg.model_name or "").strip()
            if not model:
                continue
            cfg = self._with_address(cfg)
            if not cfg.base_url and cfg.provider not in BUILTIN_PROVIDERS:
                # Still a label with no address: no role row defines this slug, and the
                # conductor's own pair carries none. Building it would give a provider
                # posting to nowhere, which reads as a dead endpoint instead of the
                # misconfiguration it is.
                if chosen:
                    problems.append(
                        f"the conductor is set to provider {cfg.provider!r}, but no role defines a provider by "
                        "that name (a custom provider's address lives on the role that introduces it), so it "
                        "was skipped"
                    )
                continue
            try:
                provider = self.orchestrator.registry.build_provider(cfg)
            except ProviderError as exc:
                if chosen:
                    problems.append(
                        f"the conductor's provider {cfg.provider!r} could not be built "
                        f"({exc.code}: {exc.message}), so it was skipped"
                    )
                continue
            if not getattr(provider, "supports_tools", False):
                if chosen:
                    problems.append(
                        f"the conductor's provider {cfg.provider!r} cannot call tools, so it was skipped"
                    )
                continue
            targets.append((provider, model, cfg))
        return targets, problems

    def _with_address(self, cfg: AgentConfig) -> AgentConfig:
        """`cfg` with a custom provider's address filled in from the role row that defines it.

        A built-in slug is explained by the catalogue, and a config that already has an address needs
        nothing. A custom slug is only a label: its endpoint, protocol and credential live on the role
        row (or fallback columns) that introduced it, and the conductor's own pair has nowhere to hold
        them — yet the Conductor card offers "Custom Provider…". So the slug means that row's address,
        whichever role holds it. The credential reference comes along only from a primary row, where it
        belongs to the provider being named; a fallback column carries none.
        """
        if cfg.base_url or cfg.provider in BUILTIN_PROVIDERS:
            return cfg
        try:
            rows = self.orchestrator.registry.list_configs()
        except Exception:
            return cfg
        found = custom_provider_address(cfg.provider, rows)
        if found is None:
            return cfg
        protocol, base_url, api_key_ref = found
        return cfg.model_copy(update={"protocol": protocol, "base_url": base_url, "api_key_ref": api_key_ref})

    def conductor_menu(self, goal_id: str) -> Callable[[], list[ToolSpec]]:
        """The moves offered for a goal, as a callable the loop asks each turn.

        Narrowed by state, and the one rule that matters: the four step moves do
        nothing before a step exists, so they are not offered until one does. A
        model choosing from eight tools chooses better than the same model
        choosing from twelve, and this is the cheapest reliability win available
        — no prompt work, no extra call.

        A method rather than a closure inside `_conduct` so the narrowing is
        testable without running a conductor, and so there is one definition of
        the menu rather than one per call site.
        """

        def menu() -> list[ToolSpec]:
            return [*BASE_TOOLS, *(STEP_TOOLS if self.goals.steps(goal_id) else ())]

        return menu

    def _complete_turn(self, goal_id: str) -> None:
        """Mark a turn finished, unless it already ended some other way.

        A turn ends with COMPLETED once its answer is published. A goal that failed while the conductor was
        running (a provider that died under a move, a stage that called `_fail`) is FAILED, and FAILED to
        COMPLETED is not a legal move: the unconditional write raised `illegal_status` out of the turn after
        the answer had already been shown. A terminal goal stays as it ended.
        """
        try:
            current = self.goals.get(goal_id)
        except ApiError:
            return
        if current.status in ("FAILED", "CANCELLED", "COMPLETED"):
            return
        self._set_status(goal_id, "COMPLETED", None)

    def _write_allowed(self, goal_id: str) -> tuple[bool, str]:
        """Whether a write may touch the filesystem for this goal (docs/00 §6.9).

        The check reads the goal's **stored status** — not anything the model
        was told, and not anything in the transcript. That is the whole point of
        putting the gate in the engine: a model cannot talk its way past a check
        it cannot write to. `RUNNING` is only reachable through
        `POST /goals/{id}/start`, which is a person saying yes to a plan.
        """
        try:
            goal = self.goals.get(goal_id)
        except ApiError:
            return False, "This goal no longer exists, so nothing was written."
        if goal.plan_only:
            return False, (
                "Nothing was written: this goal is plan-only, so execution is "
                "switched off for it. Say what you would change and stop."
            )
        if goal.status == "PAUSED":
            # Not "unapproved": the plan was approved, and something stopped the run. The critic asking for
            # changes pauses the goal, and so does the person's own Pause. Saying the plan awaits approval
            # sent a model that had just been told to act on the critic's reasons to report the wrong thing.
            return False, (
                "Nothing was written. This goal is paused: the critic asking for changes pauses it, and so "
                "does the user's Pause button. Only the user resumes it, with Start, and you will be asked "
                "again then. Tell them what is waiting for them and stop."
            )
        if goal.status in ("FAILED", "CANCELLED", "COMPLETED"):
            return False, (
                f"Nothing was written. This goal is {goal.status.lower()}, so there is nothing left to "
                "write for. Say so plainly and stop."
            )
        if goal.status != "RUNNING":
            return False, (
                "Nothing was written. This plan has not been approved yet — it is "
                "in front of the user waiting for them to start it. Say what the "
                "steps are, say plainly that no file has been changed, and stop. "
                "They approve it by starting it, and you will be asked again then."
            )
        return True, ""

    def conductor_can_drive(self, goal_id: str) -> bool:
        """Whether an approved plan should be driven by the conductor.

        Off when `conductor_drives_execution` is 0, and off when there is no
        conductor at all — in which case the engine's own sequence walks the
        steps exactly as it did before the conductor existed. That fallback is
        what makes this safe to default on.
        """
        if self._settings_int("conductor_drives_execution", 1) == 0:
            return False
        return self._conductor_target() is not None

    async def run_conductor_resume(self, goal_id: str) -> None:
        """Drive an already-approved plan. The other half of the approval seam.

        `write` refuses while a goal is not RUNNING, so a plan the conductor
        produced during a turn cannot be written by that turn. The user approves
        it, `POST /goals/{id}/start` moves the goal to RUNNING and lands here,
        and *now* the same conductor can execute what it planned.

        The run is re-derived from rows — the goal, its steps, the conversation
        — rather than a persisted conductor transcript. That is the choice
        `turn_history` already makes, and it means there is no second copy of the
        plan to fall out of step with the first.
        """
        goal = self.goals.get(goal_id)
        ws = self.workspaces.get(goal.workspace_id)
        steps = self.goals.steps(goal_id)
        listed = "\n".join(
            f"- {s.id} [{s.status}] {s.title}\n  {s.description}" for s in steps
        )
        prompt = (
            f"The user has approved this plan and started it: {goal.title}\n\n"
            f"Steps:\n{listed}\n\n"
            "Execute it now, in order. For each step: `write` it, `verify` it, "
            "`review` it, and `summarize` it once the critic approves. If "
            "verification fails, take the failure back to `write` rather than "
            "moving on. Do not re-plan, and do not ask for approval again — "
            "that is exactly what the user just gave you.\n"
            "When you are done, say what changed and what you verified."
        )
        result = await self._conduct(
            goal_id, goal, ws.root_path or "",
            prompt_override=prompt, intent="code_change",
        )
        if result.cancelled:
            return
        if result.answer:
            self.goals.publish(self._event(
                goal_id, None, "log",
                {"level": "info", "message": _as_prose(result.answer), "turn": True},
            ))
        # No status settling here, on purpose. Whatever the conductor did not
        # finish is still the engine's responsibility, and the caller owns that
        # decision because the caller is where the recipe lives and it can see
        # how many steps are left. Settling the goal here would mark a
        # half-driven plan PAUSED and take away the chance to finish it.
        remaining = [s for s in self.goals.steps(goal_id) if s.status != "COMPLETED"]
        if remaining:
            self._log(
                goal_id, None, "info",
                f"the conductor finished with {len(remaining)} step(s) still "
                "open — the engine will finish them: "
                + "; ".join(s.title for s in remaining),
            )

    def _memory_brief(self, goal: Goal) -> str:
        """This workspace's history, put in front of the conductor unprompted.

        The injection half of the memory model (docs/10 §6): the tools exist,
        and a model that does not know to ask never benefits from them. The
        brief carries the two grains — earlier threads, and what this
        workspace's own outcomes have taught — composed by
        `recall.build_brief` from the same reads the tools serve, so it cannot
        disagree with what `recall` or `recall_threads` would answer.

        Empty when there is nothing to say, so a first-ever turn gains no
        section at all. Logged so the transcript shows what the turn was told.
        """
        try:
            brief = build_brief(
                self.goals.thread_recall(goal.workspace_id),
                self.goals.recall_events(goal.workspace_id),
                observation_rows=self.goals.observation_rows(goal.workspace_id),
                current_thread_id=goal.conversation_id,
            )
        except Exception as exc:  # noqa: BLE001 — a brief is an upgrade, never a prerequisite
            # A brief that cannot be computed must not fail the turn, and the
            # warn needs a goal to attach to — which this goal is.
            self._log(
                goal.id, None, "warn", f"memory brief unavailable: {exc}",
            )
            return ""
        if brief:
            self._log(
                goal.id, None, "info",
                f"conductor briefed with workspace memory ({len(brief)} chars)",
            )
        return brief

    def _intent_brief(self, intent: str) -> str:
        """What the gate decided, told to the conductor as advice, and what to do with it.

        This was missing at first, and a live run found it where the suite could
        not: the conductor was never told the gate's verdict, so a request the
        gate had already read as `code_change` arrived looking like any other
        prompt, and the model answered it with a clarifying question instead of
        planning. Handing it the verdict is what makes computing it worth
        anything.

        It is *advice*, and the second live run found the other half. The brief
        used to say that anything but a `question` "means the user wants the
        workspace changed", so a greeting the gate labelled `other` — or, with
        the execution mode it was once handed, `code_change` — sent a 7B to load
        `ship-a-change` and a librarian to analyse the repository for "hi",
        78 s later. The label is a small classifier's guess, and the model's own
        system prompt already says to answer directly whenever nothing needs
        changing; a brief that contradicts it wins on being later and more
        specific. So only the labels that describe a change (`CHANGE_INTENTS`)
        point at the recipe, and even they say to go by the user's words.
        """
        if intent == "question":
            return (
                "The pre-flight gate read this request as a question, so answering "
                "directly is usually right. If it turns out to need the workspace "
                "changed, the `ship-a-change` skill is how that is done."
            )
        if intent in CHANGE_INTENTS:
            return (
                f"The pre-flight gate read this request as {intent!r}. That label "
                "is a small classifier's guess, not a fact — it has called a plain "
                "greeting a code change — so go by the user's actual words: if "
                "they are a greeting, thanks or a question, just reply in prose. "
                "If they do ask for the workspace to be changed, read the "
                "`ship-a-change` skill with `use_skill` and follow it: recon, then "
                "plan, then stop so they can approve the plan. Prefer acting over "
                "asking — ask only when the request genuinely cannot be planned "
                "without more information, and say plainly what you are blocked on."
            )
        return (
            "The pre-flight gate could not tell what this request asks for "
            f"(it read it as {intent or 'unlabelled'!r}). Go by the user's actual "
            "words: a greeting, thanks or a question is answered directly, in "
            "prose, with no tools. If they ask for the workspace to be changed, "
            "`ship-a-change` is the skill for that."
        )

    def _intent_nudge(self, intent: str) -> str | None:
        """The one reminder armed for a request the gate read as a change, else nothing.

        A small model asked to plan will sometimes describe the plan and stop
        instead of making it, so a change request arrives with a reminder. Nothing
        is armed for anything else: answering *is* the action for a question, a
        greeting or an unlabelled request, and a reminder there would order a
        model that had just said "Hi!" to call `recon` on the repository.
        """
        if intent not in CHANGE_INTENTS:
            return None
        return (
            "You have not done anything yet: no move has been called and there "
            "is no plan. Do not ask the user what to do and do not describe what "
            "you are about to do — call `recon` now saying what you need to find "
            "out, then call `plan`. If you genuinely cannot proceed without an "
            "answer from them, ask for it in one sentence and stop."
        )

    async def _conduct(
        self,
        goal_id: str,
        goal: Goal,
        root: str,
        prompt_override: str | None = None,
        intent: str = "question",
    ) -> _Conducted:
        """Run one conductor loop over the whole menu.

        Returns what it produced rather than a bare string, because the caller
        has to tell three outcomes apart: it answered, it planned, or it failed.
        The last is the engine's cue to fall back to its own sequence, and "it
        answered instead of planning" is a decision that must be honoured rather
        than overridden — a conductor that declines to change anything has
        decided, and re-planning over the top of it would make the brain a
        suggestion.
        """
        targets, problems = self._resolve_conductor_targets()
        for problem in problems:
            self._log(goal_id, None, "warn", problem)
        if not targets:
            return _Conducted(answer=None, exhausted=False, planned=False, unavailable=True)
        provider, model, cfg = targets[0]
        fallback = targets[1] if len(targets) > 1 else None
        role = self._conductor_role()

        skills = load_skills(root)
        for problem in skills.problems:
            self._log(goal_id, None, "warn", problem)
        for shadowed in skills.shadows:
            self._log(
                goal_id, None, "info",
                f"the workspace's {shadowed!r} skill replaces the built-in one",
            )

        menu = self.conductor_menu(goal_id)

        history = (
            self.goals.turn_history(goal.conversation_id, TURN_HISTORY_TURNS)
            if goal.conversation_id else []
        )
        prompt = prompt_override if prompt_override is not None else self._turn_prompt(goal)
        prompt = f"{prompt}\n\n{self._memory_brief(goal)}\n\n{self._intent_brief(intent)}"
        nudge = self._intent_nudge(intent)
        conductor = Conductor(
            provider, model, root,
            dispatch=self._conductor_dispatch(goal_id, goal, root, skills),
            system_prompt=(
                CONDUCTOR_SYSTEM_PROMPT
                + "\n\nSkills available in this workspace:\n" + skills.menu()
                + "\n\nCall `use_skill` with a skill's name when you want its full "
                "instructions."
            ),
            menu=menu,
            nudge=nudge,
            needs_action=lambda: not self.goals.steps(goal_id),
            max_turns=self._settings_int("conductor_max_turns", DEFAULT_MAX_TURNS),
            max_moves=self._settings_int("conductor_max_moves", DEFAULT_MAX_MOVES),
            on_text=lambda text: self._publish_turn_delta(goal_id, text),
            on_tool=lambda name, args: self._log(
                goal_id, None, "info", f"conductor called {name}({_clip_text(args, 200)})"
            ),
            fallback=(fallback[0], fallback[1]) if fallback is not None else None,
            on_fallback=self._conductor_fallback_notice(goal_id, role, targets),
            cancelled=lambda: self._is_cancelled(goal_id),
            num_ctx=cfg.ollama_num_ctx,
            keep_alive=cfg.ollama_keep_alive,
            ledger=self.orchestrator.tool_call_ledger(goal_id, None, targets),
        )
        # Announced before the first call, so a run that spends its whole budget
        # is still legible as "the conductor looked at things" rather than a
        # pause with nothing in it.
        self.goals.publish(self._event(
            goal_id, None, "agent_assigned",
            {"role": role, "provider": cfg.provider, "model": model, "conductor": True},
        ))
        try:
            answer = await conductor.run(prompt, history)
        except ProviderError as exc:
            self._log(
                goal_id, None, "warn",
                f"the conductor could not run ({exc.code}: {exc.message})",
            )
            return _Conducted(
                answer=None, exhausted=conductor.exhausted,
                # It may have planned before the provider failed: whether a plan exists is a fact about the
                # goal's rows, not something the failure path may assume.
                planned=bool(self.goals.steps(goal_id)),
            )
        return _Conducted(
            answer=answer,
            exhausted=conductor.exhausted,
            planned=bool(self.goals.steps(goal_id)),
            cancelled=conductor.was_cancelled or self._is_cancelled(goal_id),
        )

    def _conductor_fallback_notice(
        self, goal_id: str, role: AgentRole, targets: list[tuple[Any, str, Any]]
    ) -> Callable[[ProviderError, Any, str], None] | None:
        """What the conductor's loop says when it moves onto its fallback.

        The same two events a role's fallback publishes, and for the same reason:
        a silent switch would credit the turn's answer to a model that never
        produced it. `agent_assigned` follows the move so the call that is about
        to happen is attributed to the target that will serve it, which is what
        makes the usage books honest rather than merely complete.

        The slugs come from the *configs*, not from the provider objects: a
        provider knows how to talk to its endpoint and nothing about which of
        them it is, and an event naming an empty provider is the same dishonesty
        in a smaller font.

        None when there is only one target, which is most installs: reading the
        second target out of a one-element list raised `IndexError` while the
        loop was still being *built*, so every goal the conductor drove died
        before its first call.
        """
        if len(targets) < 2:
            return None
        from_provider = targets[0][2].provider
        from_model = targets[0][1]
        to_provider = targets[1][2].provider
        to_model = targets[1][1]

        def notice(exc: ProviderError, provider: Any, model: str) -> None:
            self.goals.publish(self._event(
                goal_id, None, "provider_fallback",
                {
                    "role": role,
                    "from": {"provider": from_provider, "model": from_model},
                    "to": {"provider": to_provider, "model": to_model},
                    "code": exc.code,
                    "detail": exc.message,
                },
            ))
            self.goals.publish(self._event(
                goal_id, None, "agent_assigned",
                {
                    "role": role,
                    "provider": to_provider,
                    "model": to_model,
                    "conductor": True,
                    "fallback": True,
                },
            ))
            self._log(
                goal_id, None, "warn",
                f"the conductor moved from {from_provider}/{from_model} to "
                f"{to_provider}/{to_model} ({exc.code})",
            )

        return notice

    def _publish_turn_delta(self, goal_id: str, text: str) -> None:
        """Stream a chunk of the conductor's prose onto the turn's event log.

        A named method rather than a lambda in the call, so the callback's
        return type is `None` and mypy does not read `publish`'s `Event` as a
        disagreement about what a callback returns.
        """
        self.goals.publish(
            self._event(goal_id, None, "model_delta", {"text": text, "role": "conductor"})
        )

    async def _turn_reply(self, goal_id: str, goal: Goal) -> str:
        """One model call, streamed, for a turn. The prose a person reads.

        Routed through `run_agent` with a borrowed role purely for *which
        configuration* to use. `scribe` is the honest choice: it is the one role
        whose entire job is writing prose for a person rather than structure for
        a later stage, so a fresh install that has configured nothing sensible
        still gets a sensible answer, and the usage books attribute the call to
        a role that really was writing a summary. The conductor's own settings
        override it when they are set (see `_conductor_role`).
        """
        role = self._conductor_role()
        raw = await self.orchestrator.run_agent(
            role, goal_id, None, self._turn_prompt(goal),
            system=CHAT_SYSTEM_PROMPT, raw_output=True,
        )
        return _as_prose(str(raw)) or "(no answer)"

    def _turn_prompt(self, goal: Goal) -> str:
        """The user's words, plus this thread's earlier turns.

        The history is capped by `GoalService.turn_history` and trimmed here, so
        a long thread cannot grow a prompt without bound — the oldest turns go
        first, because the most recent exchange is what a follow-up refers to.
        """
        parts: list[str] = []
        if goal.conversation_id:
            history = self.goals.turn_history(goal.conversation_id, TURN_HISTORY_TURNS)
            if history:
                lines = ["Earlier in this conversation:"]
                for turn in history:
                    said = _clip_text(turn["prompt"], 600)
                    got = _clip_text(turn["reply"], 900) or "(no answer)"
                    # Two different speakers. Labelling both lines "You:"
                    # leaves the model unable to tell what the user asked from
                    # what it said itself, which is the one distinction the
                    # history exists to carry — "now do the other one" resolves
                    # against *its own* last answer.
                    lines.append(f"User: {said}")
                    lines.append(f"Assistant: {got}")
                parts.append("\n".join(lines))
        parts.append(f"The user says: {goal.description}")
        return "\n\n".join(parts)

    def _conductor_config(self, base: AgentConfig) -> AgentConfig:
        """The conductor's own provider/model, if the settings name one.

        A turn and the conductor's loop both borrow the `scribe` row for their
        *configuration* (see `_conductor_role`), which is a reasonable default
        and a poor place to keep a permanent preference: a user who wants the
        conductor on a stronger model than they want their commit subjects on
        has nowhere to say so. `conductor_provider` / `conductor_model` are that
        somewhere, in `engine_settings` rather than `agent_configs` because
        docs/00 §6.1 fixes `AgentRole` at eight and this is not a role.

        Only the provider, the model and the credential are taken. The base row's
        temperature and token cap are not used by the loop at all: a loop that
        calls tools wants a low temperature, so `Conductor` calls the model at a
        fixed `temperature=0.2, max_tokens=2048` whichever row it borrowed. (This
        docstring used to say the base row's temperature applied, which is how a
        scribe row tuned for wording at 0.4 / 1024 came to be blamed for the
        conductor's behaviour; it never reached it.) The system prompt is passed
        in by `_conduct` regardless.

        Naming a *different* provider drops the borrowed row's `base_url` and
        `api_key_ref` rather than carrying them. Both belong to the provider the
        row already points at: `ProviderFactory` prefers `config.base_url` over
        the built-in catalog, so a conductor set to `openai` on a scribe row
        parked on a local endpoint would have posted OpenAI-shaped JSON to that
        endpoint, and `keychain.get(api_key_ref)` returns a key by reference
        without ever asking which provider it is for. `fallback_config_for` drops
        the same field for the same reason; this is the primary's half of that
        argument.
        """
        if self.settings is None:
            return base
        try:
            provider = self.settings.get_str("conductor_provider")
            model = self.settings.get_str("conductor_model")
        except Exception:
            return base
        if not (provider and model):
            return base
        moved = provider != base.provider
        return base.model_copy(update={
            "provider": provider,
            "protocol": BUILTIN_PROVIDERS.get(
                provider, BUILTIN_PROVIDERS.get(base.provider, {})
            ).get("protocol", base.protocol),
            "model_name": model,
            "base_url": None if moved else base.base_url,
            "api_key_ref": None if moved else base.api_key_ref,
        })

    def _conductor_fallback_config(self, base: AgentConfig) -> AgentConfig | None:
        """The conductor's fallback target, or None when none is configured.

        Only reached when the conductor has a primary pair of its own: a
        conductor still borrowing the scribe's row takes the scribe's own
        fallback instead, which `_conductor_targets` decides rather than this.

        Built the way `AgentRegistryService.fallback_config_for` builds a role's
        — same row with the target fields swapped, no second constructor that
        could disagree about credentials or protocol. The endpoint and key
        reference are dropped rather than inherited: a fallback is a different
        provider, and both of those fields name the one being left behind.
        """
        if self.settings is None:
            return None
        try:
            provider = self.settings.get_str("conductor_fallback_provider")
            model = self.settings.get_str("conductor_fallback_model")
        except Exception:
            return None
        if not (provider and model):
            return None
        return base.model_copy(update={
            "provider": provider,
            "protocol": BUILTIN_PROVIDERS.get(provider, {}).get("protocol", "openai_compat"),
            "model_name": model,
            "base_url": None,
            "api_key_ref": None,
        })

    def _conductor_role(self) -> AgentRole:
        """Whose configuration a turn or the conductor's own calls use.

        `scribe` unless the conductor has been pointed at a provider and model
        through engine settings, in which case the *librarian* row is not the
        right answer either — so the nearest honest thing is to borrow the
        scribe's row and say so. A ninth `AgentConfig` would be a ninth
        `AgentRole`, and docs/00 §6.1 fixes that at eight.
        """
        return "scribe"
