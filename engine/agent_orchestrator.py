"""The one place a role's model is called: fallback, re-ask, streaming, accounting and the trace.

`AgentOrchestrator` has no coupling to the executor's pipeline (it is constructed with the registry and the
goal service and nothing else), which is why it could leave `engine/executor.py` first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import time
import uuid
from collections.abc import Callable
from typing import Any

from engine.default_prompts import DEFAULT_PROMPTS
from engine.executor_support import AgentNotConfigured, AgentOutputInvalid
from engine.laya import GateCall
from engine.models import AgentConfig, AgentRole, Event, EventType
from engine.providers import BaseProvider, FALLBACK_TRIGGER_CODES, ProviderError
from engine.replies import (
    REPLY_KEYS,
    REPLY_TOLERATES_TRUNCATION,
    _repair_prompt,
    coerce_object,
    extract_json,
)
from engine.services import AgentRegistryService, GoalService

if TYPE_CHECKING:
    from engine.trace import TraceService


# `FALLBACK_TRIGGER_CODES` — which failures may be retried on a fallback — is
# defined in `engine/providers.py` and re-exported above, because the conductor
# asks the same question from a layer that cannot import this module. The
# `as` form is what keeps `from engine.executor import FALLBACK_TRIGGER_CODES`
# working for callers that already reach in this way, under a mypy that does not
# re-export implicitly.

# The codes that mean "this role was never configured", which the executor reports
# as its own error class rather than as a provider failure.
CONFIG_GAP_CODES = frozenset({
    "missing_api_key",
    "unknown_protocol",
    "invalid_base_url",
    "secrets_unwritable",
})


class _CallAccounting:
    """One model call's books: the `usage` event it publishes, and the numbers
    the recorder reads back for the same call.

    Shared by `run_agent` and the gate rather than written twice. The gate is the
    one role that does not go through `run_agent` — it answers a typed contract
    and never falls back to a second target — and while it called the provider
    itself it also called nothing that *booked* it. So its spend never reached
    the usage document or the Stats rollup, its call was missing from a
    recording that claimed to hold every call, and the audit judged it a "silent
    role": assigned, and never seen spending anything.
    """

    def __init__(
        self,
        publish: Callable[[dict[str, Any]], None],
        role: str,
        provider: str,
        model: str,
        started: float | None = None,
    ) -> None:
        self._publish = publish
        self._role = role
        self._provider = provider
        self._model = model
        # One clock for the call, so the `usage` event and the `agent_call_failed`
        # event for the same call cannot report durations that differ by the time
        # it took to notice one had happened.
        self._started = time.monotonic() if started is None else started
        # The same usage kept aside for the trace record, so a replay's token
        # counts match the run it replays rather than being absent.
        self.usage: dict[str, Any] = {}

    def sink(self, usage: dict[str, Any]) -> None:
        """The provider's `usage_sink`: report what one call cost."""
        self.usage.update(usage)
        self._publish(
            {
                "role": self._role,
                "provider": self._provider,
                "model": self._model,
                "duration_ms": self.duration_ms(),
                **usage,
            }
        )

    def duration_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)


class AgentOrchestrator:
    def __init__(
        self, registry: AgentRegistryService, goals: GoalService,
        tracer: TraceService | None = None,
    ):
        self.registry = registry
        self.goals = goals
        # Optional recorder (docs/04 §8). Attached by the app when tracing is
        # available; a goal is only recorded when it asked to be, which
        # `TraceService.enabled` answers per call rather than once at wiring.
        self.tracer = tracer

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

    def _accounting(
        self, goal_id: str, step_id: str | None, role: str,
        provider: str, model: str, started: float | None = None,
    ) -> _CallAccounting:
        """One call's books. Every model call in a run gets one of these."""
        def publish(payload: dict[str, Any]) -> None:
            self.goals.publish(self._event(goal_id, step_id, "usage", payload))

        return _CallAccounting(publish, role, provider, model, started)

    def gate_call(self, goal_id: str) -> GateCall:
        """The `GateCall` for the pre-flight gate, bound to one goal.

        The gate runs before any step exists, so its call is attributed to the
        goal rather than to a step. It is recorded exactly like a step's calls:
        the same `usage` event, the same trace entry, the same tokens — which is
        also what makes a replay able to *answer* the gate instead of re-deciding
        it in front of a model the recording never held.
        """

        async def call(
            provider: BaseProvider, provider_slug: str, system: str, user: str,
            model: str, temperature: float, max_tokens: int,
        ) -> str:
            books = self._accounting(goal_id, None, "laya", provider_slug, model)
            provider.usage_sink = books.sink
            try:
                raw = await provider.complete(
                    system_prompt=system,
                    user_prompt=user,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    # The gate's num_ctx and keep_alive travel through GateCall's
                    # caller — engine/laya.py reads the laya role's own config and
                    # adds the keywords there; this closure has no cfg in scope,
                    # and a getattr on a name that does not exist would be a lie
                    # mypy could not catch.
                    num_ctx=None,
                    keep_alive=None,
                )
            finally:
                # The provider is shared across roles and reused for a replay, so
                # the sink must not outlive the call it belongs to.
                provider.usage_sink = None
            if self.tracer is not None and self.tracer.enabled(goal_id):
                self.tracer.record(
                    goal_id, None,
                    role="laya", provider=provider_slug, model=model,
                    temperature=float(temperature), max_tokens=int(max_tokens),
                    system_prompt=system, user_prompt=user, response=str(raw),
                    usage=books.usage, duration_ms=books.duration_ms(),
                )
            return raw

        return call

    def _not_configured(self, role: AgentRole, target: AgentConfig, label: str) -> AgentNotConfigured:
        """The failure for a target that has no model to call.

        An empty model is the one failure that cannot be diagnosed from the
        provider's answer: the request never makes sense to send.
        """
        where = "fallback " if label == "fallback" else ""
        return AgentNotConfigured(
            role,
            f"no model is configured for the {role} role{(' (' + where.strip() + ')') if where else ''}. "
            f"Open Settings → Agent Roles and pick one of the models {target.provider} "
            f"currently serves.",
        )

    def _provider_failure(
        self, role: AgentRole, target: AgentConfig, label: str, exc: ProviderError
    ) -> ProviderError:
        """Name a call failure, keeping configuration gaps distinct from outages.

        Only a configuration gap is `agent_not_configured`; a rate limit or an
        unreachable endpoint keeps its own code, with the role attached for
        diagnosis — "not configured" would send the user to a setting that is fine.
        """
        where = f"fallback {target.provider}" if label == "fallback" else target.provider
        if exc.code in CONFIG_GAP_CODES:
            return AgentNotConfigured(
                role,
                f"{role} cannot call {where}: {exc.message}. "
                f"Open Settings → Provider Keys to store a credential.",
            )
        failure = ProviderError(
            exc.code,
            f"{role} could not call {where} ({target.model_name or 'no model'}): {exc.message}.",
        )
        failure.role = role
        return failure

    def _targets(self, role: AgentRole) -> tuple[AgentConfig, list[tuple[str, AgentConfig]]]:
        """The targets this role may be called on, primary first.

        The fallback is optional and configured per role (Settings → Agent Roles):
        a local model is a fine fallback for the scribe and a bad one for the
        fixer, so the choice cannot be a global setting.
        """
        cfg = self.registry.get_config(role)
        targets: list[tuple[str, Any]] = [("primary", cfg)]
        fallback = self.registry.fallback_config_for(cfg)
        if fallback is not None:
            targets.append(("fallback", fallback))
        return cfg, targets

    def _delta_publisher(
        self, goal_id: str, step_id: str | None, role: AgentRole,
        provider_name: str, model_name: str,
    # The flush takes an optional final text, which `Callable[[], None]` cannot
    # express — the kind of annotation that reads right and contradicts the call at
    # `flush_deltas(raw)`.
    ) -> tuple[Callable[[str], None], Callable[..., None]]:
        """A sink for provider token deltas plus the flush to call at the end.

        Model replies stream in fast; the chat's WebSocket polls the event
        store at 250ms, so persisting every token would bury both the store
        and the wire. Deltas are coalesced into *snapshot* events — the full
        text accumulated so far, roughly every 400ms — so any event the
        client sees is self-contained (a mid-run reconnect repaints the
        current text correctly instead of replaying overlapping fragments).
        A final event carries the complete text and `final: true`, and once it has
        landed the stream's intermediate snapshots have their text blanked
        (`GoalService.compact_superseded_deltas`): they exist so a client sees the
        reply as it forms, and each repeats everything before it.
        """
        state: dict[str, Any] = {"buf": "", "dirty": False, "last": 0.0}

        def on_delta(text: str) -> None:
            state["buf"] = text
            state["dirty"] = True
            now = time.monotonic()
            if now - state["last"] >= 0.4:
                state["last"] = now
                state["dirty"] = False
                self.goals.publish(self._event(
                    goal_id, step_id, "model_delta",
                    {"role": role, "provider": provider_name, "model": model_name,
                     "text": state["buf"], "final": False},
                ))

        def flush(text: str | None = None) -> None:
            # The final snapshot ships even when nothing new arrived: it is
            # what closes the stream for the client (and it is the only
            # event a fast model that finished between two throttles needs).
            # The text argument lets a non-streaming provider still produce
            # one complete reply card at the end.
            if text is not None:
                state["buf"] = text
            final = self._event(
                goal_id, step_id, "model_delta",
                {"role": role, "provider": provider_name, "model": model_name,
                 "text": state["buf"], "final": True},
            )
            self.goals.publish(final)
            # The stream is over and the final snapshot holds all of it, so the
            # intermediate ones (each a copy of the text so far) are blanked, not
            # deleted: the sequence stays dense. Best effort: a failure to tidy
            # must never fail the call that succeeded.
            try:
                self.goals.compact_superseded_deltas(goal_id, step_id, role, final.sequence)
            except Exception:
                pass

        return on_delta, flush

    def _publish_fallback(
        self, goal_id: str, step_id: str | None, role: AgentRole,
        primary: AgentConfig, target: AgentConfig, exc: ProviderError | AgentOutputInvalid,
    ) -> None:
        """Say that the primary was skipped, why, and where the call went instead.

        A silent switch would be worse than the failure it hides: the transcript
        would credit an answer to a model that never produced it.
        """
        self.goals.publish(self._event(
            goal_id, step_id, "provider_fallback",
            {
                "role": role,
                "from": {"provider": primary.provider, "model": primary.model_name},
                "to": {"provider": target.provider, "model": target.model_name},
                "code": exc.code,
                "detail": getattr(exc, "message", str(exc)),
            },
        ))

    def _all_targets_failed(
        self, role: AgentRole,
        failures: list[tuple[str, str, ProviderError | AgentOutputInvalid]],
    ) -> ProviderError | AgentOutputInvalid:
        """One error that names every attempt, or the original failure if there was one.

        With no fallback configured this returns exactly what the caller would
        have raised before, code and message intact, so a single failure is not
        dressed up as a more complicated one.
        """
        label, provider, first = failures[0]
        if len(failures) == 1:
            return first
        attempts = "; ".join(
            f"{label} {prov} ({err.code}): {getattr(err, 'message', str(err))}"
            for label, prov, err in failures
        )
        note = f" Both targets failed — {attempts}."
        first_message = getattr(first, "message", str(first))
        if isinstance(first, AgentNotConfigured):
            return AgentNotConfigured(role, first_message + note)
        if isinstance(first, AgentOutputInvalid):
            return AgentOutputInvalid(first_message + note, role=role)
        aggregate = ProviderError(first.code, first_message + note)
        aggregate.role = role
        return aggregate

    async def run_agent(
        self, role: AgentRole, goal_id: str, step_id: str | None, user_prompt: str,
        system: str | None = None, raw_output: bool = False,
        accept: Callable[[Any], None] | None = None,
    ) -> Any:
        """Run one sub-agent call, on its primary target or its fallback.

        Per-role registry config (Settings → Agents) is the single source of
        truth: model, temperature, max_tokens, and system-prompt override all
        come from the role's AgentConfig. The command-bar model selection does
        not override roles here, and nothing writes it onto them either: it is
        recorded on the goal (`goals.provider/model`) and a role runs on its own
        configuration (docs/00 §6.2).

        The fallback exists so a goal keeps running when the primary cannot be
        used — no key stored, the endpoint down, the model retired, or a reply the
        contract cannot parse. It is tried at most once, and only for        those failures; anything else is a bug that must not be papered over by running
        the same call somewhere else.

        `system` overrides the role's prompt for one call. It exists for the two
        model-holding components that are not stages and so have no entry in
        `DEFAULT_PROMPTS` — a turn and the conductor (docs/09 §10). Everything
        else about the call is unchanged, which is the point: a turn gets the
        fallback chain, the usage books, the trace record and the streamed
        deltas for free rather than by a second implementation of them.
        `role` still names whose *configuration* is used, so telemetry reads
        honestly even though the prompt is not theirs.

        `raw_output` skips `extract_json`, and exists because a turn's answer is
        prose. Every one of the eight roles is told to reply with JSON only
        because its output is structure a later stage consumes; a reply meant
        for a person is not that, and parsing it raised `agent_output_invalid` —
        which then tripped the *fallback* chain, so a perfectly good answer was
        discarded and retried against a second provider before the turn failed.
        The parse is the roles' contract, not `run_agent`'s.

        `accept` is the caller's own test of a reply that parsed: it is given the parsed value and raises
        `ValueError` or `AgentOutputInvalid` when the reply cannot be *used* — the fixer's edit that matches
        the wrong number of times, an entry the contract refuses. It gets the same one same-model re-ask a
        reply that would not parse gets, with its reason quoted to the model, because the caller knows
        exactly what is wrong and the model can act on it (audit of 2026-09-29, 3.3). Whatever `accept`
        does on success it must be able to do again after a failure: a refused reply writes nothing.
        """
        goal = self.goals.get(goal_id)
        _ = goal  # kept for interface symmetry; config comes from the registry
        cfg, targets = self._targets(role)
        system = system or cfg.system_prompt_override or DEFAULT_PROMPTS[role]
        failures: list[tuple[str, str, ProviderError | AgentOutputInvalid]] = []

        for label, target in targets:
            model_name = (target.model_name or "").strip()
            if not model_name:
                failures.append((label, target.provider, self._not_configured(role, target, label)))
                continue
            try:
                provider = self.registry.build_provider(target)
            except ProviderError as exc:
                failures.append((label, target.provider, self._provider_failure(role, target, label, exc)))
                continue
            if label == "fallback":
                self._publish_fallback(goal_id, step_id, role, cfg, target, failures[0][2])
            self.goals.publish(self._event(
                goal_id, step_id, "agent_assigned",
                {"role": role, "provider": target.provider, "model": model_name},
            ))
            # Token accounting: every successful completion reports its usage
            # here, attributed to the role and the target that served it. A
            # fresh closure per target so a fallback's usage is labeled with
            # the provider that actually ran, not the one that was asked first.
            # duration_ms rides along on the same event: the response time the
            # Settings screen reports as "last call", and the one number that
            # answers "is my fixer slow?" without touching a provider.
            call_started = time.monotonic()
            books = self._accounting(
                goal_id, step_id, role, target.provider, model_name, call_started,
            )
            provider.usage_sink = books.sink
            on_delta, flush_deltas = self._delta_publisher(goal_id, step_id, role, target.provider, model_name)
            provider.on_delta = on_delta
            try:
                raw = await provider.complete(
                    system_prompt=system,
                    user_prompt=user_prompt,
                    model=model_name,
                    temperature=target.temperature,
                    max_tokens=target.max_tokens,
                    num_ctx=target.ollama_num_ctx,
                    keep_alive=target.ollama_keep_alive,
                )
            except ProviderError as exc:
                flush_deltas()
                # The call's outcome, kept with the goal it failed in: the
                # Settings card reports the *last* thing that happened to a
                # role, and "last" needs a record of failures too — a role that
                # only ever fails leaves no usage event to read. A 429 that
                # succeeded on the fallback is exactly what a user needs to see
                # when their fixer has been "slow".
                self.goals.publish(self._event(
                    goal_id, step_id, "agent_call_failed",
                    {
                        "role": role,
                        "provider": target.provider,
                        "model": model_name,
                        "target": label,
                        "code": exc.code,
                        "message": exc.message,
                        "duration_ms": books.duration_ms(),
                    },
                ))
                failures.append((label, target.provider, self._provider_failure(role, target, label, exc)))
                if exc.code not in FALLBACK_TRIGGER_CODES:
                    break
                continue
            finally:
                provider.on_delta = None
            flush_deltas(raw)
            # After the call, before the parse: a reply the engine could not use is still what
            # the model said, and a recording that dropped the malformed ones would replay a run
            # that never failed.
            self._trace_call(goal_id, step_id, role, target, model_name, system, user_prompt, raw, books)
            if raw_output:
                return raw
            expect = REPLY_KEYS.get(role, ())
            tolerate_cut = role in REPLY_TOLERATES_TRUNCATION
            what = "non-JSON output"
            try:
                parsed = coerce_object(role, extract_json(raw, expect, repair_truncation=tolerate_cut))
            except (ValueError, TypeError) as exc:
                problem = str(exc)
            else:
                try:
                    if accept is not None:
                        accept(parsed)
                    return parsed
                except (ValueError, TypeError, AgentOutputInvalid) as exc:
                    problem = getattr(exc, "message", None) or str(exc)
                    what = "a reply that could not be used"

            # One re-ask, of the *same* target, before anything else is tried: a small model's single
            # formatting slip is the commonest way a step used to fail, and the remedy costs one short
            # call against a step's worth of work (audit of 2026-09-29, H4). The slip is recorded as
            # the failed call it was — the cost of a model that needs this is something to see — and
            # the second call has books of its own, so its tokens and duration are its own.
            self.goals.publish(self._event(
                goal_id, step_id, "agent_call_failed",
                {
                    "role": role,
                    "provider": target.provider,
                    "model": model_name,
                    "target": label,
                    "code": "agent_output_invalid",
                    "message": f"{role} returned {what}: {problem}; asking the same model once more",
                    "retrying": True,
                    "duration_ms": books.duration_ms(),
                },
            ))
            repair_started = time.monotonic()
            repair_books = self._accounting(
                goal_id, step_id, role, target.provider, model_name, repair_started,
            )
            provider.usage_sink = repair_books.sink
            repair_prompt = _repair_prompt(user_prompt, problem, str(raw))
            try:
                repaired = await provider.complete(
                    system_prompt=system,
                    user_prompt=repair_prompt,
                    model=model_name,
                    temperature=target.temperature,
                    max_tokens=target.max_tokens,
                    num_ctx=target.ollama_num_ctx,
                    keep_alive=target.ollama_keep_alive,
                )
            except ProviderError as exc:
                self.goals.publish(self._event(
                    goal_id, step_id, "agent_call_failed",
                    {
                        "role": role,
                        "provider": target.provider,
                        "model": model_name,
                        "target": label,
                        "code": exc.code,
                        "message": exc.message,
                        "duration_ms": repair_books.duration_ms(),
                    },
                ))
                failures.append((label, target.provider, self._provider_failure(role, target, label, exc)))
                if exc.code not in FALLBACK_TRIGGER_CODES:
                    break
                continue
            self._trace_call(
                goal_id, step_id, role, target, model_name, system, repair_prompt, repaired, repair_books,
            )
            try:
                parsed = coerce_object(role, extract_json(repaired, expect, repair_truncation=tolerate_cut))
            except (ValueError, TypeError) as exc:
                failures.append((
                    label, target.provider,
                    AgentOutputInvalid(
                        f"{role} returned non-JSON output after one repair attempt: {exc}", role=role,
                    ),
                ))
                if "agent_output_invalid" not in FALLBACK_TRIGGER_CODES:
                    break
                continue
            try:
                if accept is not None:
                    accept(parsed)
                return parsed
            except (ValueError, TypeError, AgentOutputInvalid) as exc:
                refused = AgentOutputInvalid(
                    f"{role} returned a reply that could not be used after one repair attempt: "
                    f"{getattr(exc, 'message', None) or exc}",
                    role=role,
                )
                # A refusal with a code of its own (a path the workspace refused) keeps it.
                refused.code = getattr(exc, "code", refused.code)
                failures.append((label, target.provider, refused))
                if "agent_output_invalid" not in FALLBACK_TRIGGER_CODES:
                    break
                continue

        raise self._all_targets_failed(role, failures)

    def _trace_call(
        self, goal_id: str, step_id: str | None, role: str, target: Any, model_name: str,
        system: str, user_prompt: str, raw: Any, books: _CallAccounting,
    ) -> None:
        """Hand one finished call to the recorder, when this goal asked to be recorded.

        Asked again here rather than remembered because the flag is a per-call read.
        """
        if self.tracer is None or not self.tracer.enabled(goal_id):
            return
        self.tracer.record(
            goal_id, step_id,
            role=role, provider=target.provider, model=model_name,
            temperature=float(target.temperature),
            max_tokens=int(target.max_tokens),
            system_prompt=system, user_prompt=user_prompt, response=str(raw),
            usage=books.usage,
            duration_ms=books.duration_ms(),
        )
