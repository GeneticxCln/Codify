"""The goal pipeline: plan, write, verify, judge, record.

**Part of this file is a DRAFT RECONSTRUCTION, not the original code.** The
design work below — the design stage in `run_planning`, the `_design*` /
`_brand_contract` / `_brand_drifts` helpers, and the deliverable paths in
`_fixer` / `_verifier` / `_critic` — was written uncommitted and lost before it
reached a commit. Every block marked "DRAFT RECONSTRUCTION" was rebuilt from
its *specification*:

  * `tests/test_design_role.py` (72 tests), which pins the behaviour;
  * `scripts/fake_ollama.py`, whose prompt parser pins the exact fixer framing
    (`--- DESIGN.md (write exactly this) ---` … `--- end DESIGN.md ---`);
  * docs/01 §1.1a and docs/04 §4.0a, §4.0a.1, §4.0a.2, which specify the
    origin table, the stamping and body-dropping rules, the drift rules, and
    the four design-deliverable deltas.

What that buys is behaviour, not authorship. The original author's structure,
helper decomposition, prompt wording and reasoning are gone and are *not*
reproduced here; three constants (`MAX_CONTRACT_FILE_CHARS`,
`MAX_DRIFT_DIFF_CHARS`, `MAX_BRAND_DRIFTS`) have no documented value and carry
plausible guesses, since no test pins them. Treat this as a proposal for their
review rather than as their work restored: if the original returns, prefer it,
and expect to reconcile rather than to discard.

Everything else in this file is unmodified.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import time
import uuid
from typing import TYPE_CHECKING, Any
from collections.abc import AsyncIterator, Callable

from engine.default_prompts import (
    DEFAULT_PROMPTS,
    DESIGN_BRIEF_PROMPT,
    KNOWLEDGE_BRIEF_PROMPT,
)
from engine.fs import FileSystemService, PathEscapeError
from engine.git import GitService
from engine.library import (
    MAX_ROUND_CHARS,
    READ_ONLY_TIMEOUT_S,
    ROOT_KNOWLEDGE_MD,
    LibraryService,
    format_command,
    format_knowledge,
    format_read,
    format_search,
    read_knowledge,
)
from engine.laya import LayaDecision, LayaService, build_state
from engine.models import AgentConfig, AgentRole, Event, EventType, Goal, PlanStep
from engine.providers import ProviderError
from engine.sandbox import CommandNotAllowed, SandboxService
from engine.services import AgentRegistryService, ApiError, GoalService, WorkspaceService

if TYPE_CHECKING:
    from engine.services import SettingsService
    from engine.trace import TraceService


class AgentOutputInvalid(Exception):
    def __init__(self, message: str, role: str | None = None):
        super().__init__(message)
        self.code = "agent_output_invalid"
        # Same shape as ProviderError, so a caller reporting "what happened" on
        # either one does not need to know which it is holding.
        self.message = message
        # Which role produced it. Carried on the error event so the UI can show
        # that role's config, credential, and discovered models instead of
        # making the user work out from the message which agent to go and check.
        self.role = role


class TestsFailed(AgentOutputInvalid):
    """The verifier ran and reported a failure.

    Distinct from AgentOutputInvalid because the agent behaved correctly: the
    code under test is what failed. Reporting this as "agent output invalid"
    sent users looking for a malformed model reply that never existed.
    """

    def __init__(self, message: str, role: str | None = "verifier"):
        super().__init__(message, role)
        self.code = "tests_failed"


class AgentNotConfigured(ProviderError):
    """A role cannot be called at all: no model chosen, or no credential.

    That is a setup problem, not a bad model reply. Reporting it as
    `agent_output_invalid` sent the user hunting for malformed JSON that was
    never produced, while the real fix — pick a model / add a key — was two
    clicks away in Settings. Since no model names are seeded (see
    `models.DEFAULT_AGENTS`), a fresh install hits this on its first prompt.
    """

    def __init__(self, role: str, message: str):
        super().__init__("agent_not_configured", message)
        self.role = role


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
# Follow-up reconnaissance calls the planner may make while planning (see
# run_planning). The evidence pack is frozen once the librarian finishes; this
# lets the planner reopen it when the pack provably misses what a step needs,
# instead of planning a guess. Same serving machinery, own bound.
MAX_PLANNER_CONSULTS = 1

# Failures that mean the target could not be used at all, and so may be retried on
# the role's fallback. The list is deliberately made of *provider* problems — no
# credential, an endpoint that refuses, an error status, a reply the contract
# cannot parse. A failure that is not here (a bug in our own code) stops the role
# instead of silently running it somewhere else, because pointing an unknown
# failure at a second model is how a real defect gets buried under a retry.
# The codes the executor treats as provider/protocol faults: a call that fails
# with one of these may be retried on the role's fallback target. The test
# `test_failed_call_still_closes_the_stream` covers the opposite branch — a code
# outside this set surfaces as a raised ProviderError — so re-adding a code that
# belongs here flips that test's condition and fails it loudly.
FALLBACK_TRIGGER_CODES = frozenset({
    "missing_api_key",
    "unknown_protocol",
    "invalid_base_url",
    "secrets_unwritable",
    "provider_http",
    "provider_unreachable",
    "provider_bad_response",
    "agent_output_invalid",
})

# The codes that mean "this role was never configured", which the executor reports
# as its own error class rather than as a provider failure.
CONFIG_GAP_CODES = frozenset({
    "missing_api_key",
    "unknown_protocol",
    "invalid_base_url",
    "secrets_unwritable",
})

# Reconnaissance bounds. The librarian answers in rounds: it asks for material,
# the engine fetches it, and it asks again — so a goal spends at most this many
# model calls on looking before anything is planned. A cap the model cannot raise
# is what keeps "look around" from becoming an unbounded crawl.
MAX_LIBRARY_ROUNDS = 3
MAX_LIBRARY_READS_PER_ROUND = 12
MAX_LIBRARY_SEARCHES_PER_ROUND = 6
MAX_LIBRARY_GIT_PER_ROUND = 6
MAX_LIBRARY_RUNS_PER_ROUND = 4

# ── design (DRAFT RECONSTRUCTION — see docs/04 §4.0a) ───────────────────────
# The direction one goal is built against. Every list is trimmed rather than
# dropped whole — a malformed row is prompt material, not a broken goal — and
# `artifact` is a closed vocabulary because the app renders it: this is not a
# place to pass a model's invention through, so anything else reads as "other".
DESIGN_ARTIFACTS = (
    "web_prototype", "page", "dashboard", "deck", "mobile",
    "document", "component", "style_system", "other",
)
# The one shape convention gets for free: a non-empty DESIGN.md at the
# workspace root is the brand contract, with nothing pinned. Zero-config is the
# point — a repository that already documents its brand should not have to be
# told twice — and one name in one place keeps it predictable. Everything else
# (another name, a nested path, a tokens JSON) is what the pin is for.
ROOT_DESIGN_MD = "DESIGN.md"
MAX_DESIGN_COLORS = 24
MAX_DESIGN_TYPOGRAPHY = 12
MAX_DESIGN_SPACING = 12
MAX_DESIGN_RADII = 8
MAX_DESIGN_COMPONENTS = 40
# conventions / constraints / acceptance: the three lists a human reads to judge
# the result, so they stay short enough to read.
MAX_DESIGN_LINES = 12
MAX_DESIGN_MD_CHARS = 8000
# A pinned brand contract is handed over whole, bounded. It is prose, not a
# corpus, and an unbounded read is an unbounded prompt — truncation is logged
# rather than silent, so a half-brand is visible in the transcript.
MAX_CONTRACT_FILE_CHARS = 20000
# The verifier's mechanical brand check (docs/04 §4.0a.1): how much of each
# changed file is read as evidence, and how many findings one step may publish.
# The check is advisory, and a transcript of forty findings is a finding nobody
# reads.
MAX_DRIFT_DIFF_CHARS = 20000
MAX_BRAND_DRIFTS = 8

# The one file each deliverable mode writes, and what that file *is* to the
# roles judging it. Both modes share a shape — an agent drafts the body, a step
# writes it verbatim, the verifier and critic read the bytes — and differ only
# in which file, so the two facts live in one table rather than in three `if
# mode == "design"` branches that could disagree about what is being written.
# A mode absent from this table has no deliverable, which is what stops a
# normal goal from being handed a document to write.
DELIVERABLE_FILES = {
    "design": ROOT_DESIGN_MD,
    "knowledge": ROOT_KNOWLEDGE_MD,
}
# What the file *is*, for the role that writes it, and what the critic is being
# asked to approve. Two tables rather than one because the two prompts genuinely
# say different things: the fixer is told to reproduce a draft, the critic to
# judge a document. Both design strings are byte-for-byte what they were before
# the second mode existed, because `tests/test_design_role.py` pins that wording
# and a contract test that breaks on a reword is a contract test that will be
# silenced rather than honoured.
DELIVERABLE_ROLE = {
    "design": "the workspace's brand contract, the file every later goal is planned against",
    "knowledge": (
        "the workspace's knowledge file, which every later run's librarian reads as a "
        "prior — wrong in it sends the next run to the wrong file"
    ),
}
DELIVERABLE_SUBJECT = {
    "design": (
        f"the workspace's {ROOT_DESIGN_MD} itself — the contract every later goal is "
        "planned against"
    ),
    "knowledge": (
        f"the workspace's {ROOT_KNOWLEDGE_MD} itself — the knowledge every later run's "
        "librarian reads as a prior, so a wrong claim in it misdirects the next run"
    ),
}

# How a deliverable is labelled to the two roles that judge it. The difference
# between these two strings is the difference between "this is the file" and
# "this is what the file will be" — and a role that guesses wrong is reviewing
# bytes that do not exist.
ARTIFACT_WRITTEN = "as written"
ARTIFACT_PROPOSED = "as proposed by this step (nothing was written to disk)"

# Function words carry no evidence. An acceptance line is prose ("every KPI
# renders in its own tile"), and matching its "in" and "its" against a diff
# would evidence a claim nothing in the change actually addressed.
_DRIFT_STOPWORDS = frozenset({
    "and", "are", "as", "at", "be", "been", "but", "by", "can", "for", "from",
    "has", "have", "into", "its", "may", "must", "not", "of", "on", "only",
    "or", "over", "should", "than", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "use", "used", "using", "was",
    "were", "what", "when", "where", "which", "while", "with", "without",
    "you", "your",
})


def _text_words(text: str) -> list[str]:
    """Casefolded alphanumeric words, for evidence matching."""
    return re.findall(r"[a-z0-9]+", text.casefold())


# ── stage outcomes (docs/04 §4.7) ───────────────────────────────────────────
# One closed vocabulary for "what did this stage achieve", so a per-role
# success rate is a count of declared outcomes rather than a guess at what a
# missing event meant. The eight stages, and nothing outside them:
#
#   laya        skipped | allow | block | cancelled | unavailable
#   librarian   pack | incomplete | invalid | cancelled | unavailable
#   design      contract | declined | invalid | cancelled | unavailable
#   planner     plan | consult | invalid | cancelled | unavailable
#   fixer       wrote | no_change | replayed | invalid | cancelled | unavailable
#   verifier    pass | fail | skip | refused | invalid | cancelled | unavailable
#   critic      approve | request_changes | invalid | cancelled | unavailable
#   scribe      committed | nothing_to_commit | not_a_repo | invalid | cancelled | unavailable
#
# `invalid` is a reply the engine could not use; `unavailable` is a call that
# could not be made or completed. They are different failures to the person
# choosing what to fix, and a per-role rate that merged them would hide a role
# whose prompt needs work behind a role that has no key.
STAGE_OUTCOMES: dict[str, tuple[str, ...]] = {
    "laya": ("skipped", "allow", "block", "cancelled", "unavailable"),
    "librarian": ("pack", "incomplete", "invalid", "cancelled", "unavailable"),
    "design": ("contract", "declined", "invalid", "cancelled", "unavailable"),
    "planner": ("plan", "consult", "invalid", "cancelled", "unavailable"),
    "fixer": ("wrote", "no_change", "replayed", "invalid", "cancelled", "unavailable"),
    "verifier": ("pass", "fail", "skip", "refused", "invalid", "cancelled", "unavailable"),
    "critic": ("approve", "request_changes", "invalid", "cancelled", "unavailable"),
    "scribe": ("committed", "nothing_to_commit", "not_a_repo", "skipped", "invalid", "cancelled", "unavailable"),
}
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


class CriticRejection(AgentOutputInvalid):
    def __init__(self, message: str, reasons: list[str], role: str | None = "critic"):
        super().__init__(message, role)
        self.reasons = reasons


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
    (AgentOutputInvalid, "invalid"),
)


def _stage_failure_outcome(exc: Exception) -> str:
    """The outcome a stage that raised without declaring is published under."""
    for exc_type, outcome in _STAGE_EXCEPTIONS:
        if isinstance(exc, exc_type):
            return outcome
    return "unavailable"


def _verifier_outcome(result: dict[str, Any]) -> str:
    """The verifier's stage outcome, from the verdict it published.

    Checked against the vocabulary rather than passed through: a verdict the
    engine did not already reject (`_test_result` refuses anything outside
    pass/fail/skip) would otherwise put a name in the metrics that no reader
    of the table can interpret, which is how a rate stops meaning anything.
    """
    verdict = str((result or {}).get("verdict") or "")
    return verdict if verdict in STAGE_OUTCOMES["verifier"] else "invalid"


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


def _as_read_int(value: Any, default: int | None) -> int | None:
    """A model-supplied read offset/limit coerced to a sane int, or the default.

    Models send "2", 2, 2.0, occasionally "two". Junk falls back to the default
    rather than raising — a malformed window is a wasted round, not a defect.
    """
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 1 else default


def extract_json(raw: str) -> Any:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    start = -1
    for i, ch in enumerate(raw):
        if ch in ("{", "["):
            start = i
            break
    if start != -1:
        end = max(raw.rfind("}"), raw.rfind("]"))
        if end > start:
            raw = raw[start : end + 1]
    return json.loads(raw)


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
        A final event carries the complete text and `final: true`.
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
            self.goals.publish(self._event(
                goal_id, step_id, "model_delta",
                {"role": role, "provider": provider_name, "model": model_name,
                 "text": state["buf"], "final": True},
            ))

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

    async def run_agent(self, role: AgentRole, goal_id: str, step_id: str | None, user_prompt: str) -> Any:
        """Run one sub-agent call, on its primary target or its fallback.

        Per-role registry config (Settings → Agents) is the single source of
        truth: model, temperature, max_tokens, and system-prompt override all
        come from the role's AgentConfig. The command-bar model selection no
        longer overrides roles here — GoalService.create seeds only roles the
        user has never configured, so defaults and explicit choices compose.

        The fallback exists so a goal keeps running when the primary cannot be
        used — no key stored, the endpoint down, the model retired, or a reply the
        contract cannot parse. It is tried at most once, and only for those
        failures; anything else is a bug that must not be papered over by running
        the same call somewhere else.
        """
        goal = self.goals.get(goal_id)
        _ = goal  # kept for interface symmetry; config comes from the registry
        cfg, targets = self._targets(role)
        system = cfg.system_prompt_override or DEFAULT_PROMPTS[role]
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
            # The same usage kept aside for the trace record, so a replay's
            # token counts match the run it replays rather than being absent.
            recorded_usage: dict[str, Any] = {}

            def _record(
                usage: dict[str, Any], _role: AgentRole = role, _provider: str = target.provider,
                _model: str = model_name, _started: float = call_started,
                _usage_out: dict[str, Any] = recorded_usage,
            ) -> None:
                _usage_out.update(usage)
                self.goals.publish(self._event(
                    goal_id, step_id, "usage",
                    {
                        "role": _role,
                        "provider": _provider,
                        "model": _model,
                        "duration_ms": round((time.monotonic() - _started) * 1000),
                        **usage,
                    },
                ))
            provider.usage_sink = _record
            on_delta, flush_deltas = self._delta_publisher(goal_id, step_id, role, target.provider, model_name)
            provider.on_delta = on_delta
            try:
                raw = await provider.complete(
                    system_prompt=system,
                    user_prompt=user_prompt,
                    model=model_name,
                    temperature=target.temperature,
                    max_tokens=target.max_tokens,
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
                        "duration_ms": round((time.monotonic() - call_started) * 1000),
                    },
                ))
                failures.append((label, target.provider, self._provider_failure(role, target, label, exc)))
                if exc.code not in FALLBACK_TRIGGER_CODES:
                    break
                continue
            finally:
                provider.on_delta = None
            flush_deltas(raw)
            if self.tracer is not None and self.tracer.enabled(goal_id):
                # After the call, before the parse: a reply the engine could not
                # use is still what the model said, and a recording that dropped
                # the malformed ones would replay a run that never failed.
                # Re-asked here rather than reusing `tracing` because the flag
                # is a per-call read and the two are the same question; the
                # narrowing this needs is the point.
                self.tracer.record(
                    goal_id, step_id,
                    role=role, provider=target.provider, model=model_name,
                    temperature=float(target.temperature),
                    max_tokens=int(target.max_tokens),
                    system_prompt=system, user_prompt=user_prompt, response=str(raw),
                    usage=recorded_usage,
                    duration_ms=int((time.monotonic() - call_started) * 1000),
                )
            try:
                return extract_json(raw)
            except (ValueError, TypeError) as exc:
                failures.append((
                    label, target.provider,
                    AgentOutputInvalid(f"{role} returned non-JSON output: {exc}", role=role),
                ))
                if "agent_output_invalid" not in FALLBACK_TRIGGER_CODES:
                    break
                continue

        raise self._all_targets_failed(role, failures)


class ExecutorService:
    def __init__(
        self,
        goals: GoalService,
        workspaces: WorkspaceService,
        registry: AgentRegistryService,
        sandbox: SandboxService,
        git: GitService | None = None,
        laya: LayaService | None = None,
        tracer: TraceService | None = None,
    ):
        self.goals = goals
        self.workspaces = workspaces
        self.orchestrator = AgentOrchestrator(registry, goals, tracer=tracer)
        self.sandbox = sandbox
        self.git = git or GitService()
        # System-1 pre-flight gate (see engine/laya.py). Optional by design: a
        # gate that cannot run is a skipped gate, never a broken pipeline.
        self.laya = laya or LayaService(registry=registry)
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

    async def run_planning(self, goal_id: str) -> None:
        goal = self.goals.get(goal_id)
        ws = self.workspaces.get(goal.workspace_id)

        # ── Laya: System-1 pre-flight gate ──────────────────────────────
        # Cheap typed decisions (intent / risk / injection probability) before
        # any LLM call. High-confidence injection stops the goal here.
        async with self._stage(goal_id, "laya", "laya") as laya_stage:
            try:
                decision = await self.laya.decide(build_state(goal, ws.root_path))
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

        prompt = (
            f"Title: {goal.title}\nDescription:\n{goal.description}\n\n"
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
                    out = await self.orchestrator.run_agent("planner", goal_id, None, prompt)
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
                    served, _opened, _matched, refused = self._serve_library_requests(
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
            # Planning is the planner's phase; anything raised here is its.
            self._fail(
                goal_id, None, getattr(exc, "code", "agent_output_invalid"), str(exc),
                role=getattr(exc, "role", None) or "planner",
            )
            return
        self._set_status(goal_id, "PENDING", None)

    # ── librarian ──────────────────────────────────────────────────────────────

    def _evidence_for(self, goal_id: str) -> dict[str, Any]:
        """The evidence pack this goal's librarian produced, read back from events.

        From the event log rather than memory: a goal resumed in another process
        must still hand its fixer the same material its planner planned from.
        """
        latest: dict[str, Any] = {}
        for ev in self.goals.events_after(goal_id, 0):
            if ev.type == "library_evidence":
                latest = ev.payload or {}
        return latest

    async def _librarian(self, goal_id: str, goal: Goal, ws: Any) -> dict[str, Any]:
        """Look around before anything is planned or changed.

        Rounds: the librarian asks for material (reads, searches, read-only git or
        inspect commands), the engine fetches it, and it asks again — capped at
        MAX_LIBRARY_ROUNDS so "look around" cannot become an unbounded crawl. It
        finishes early by setting `enough`, or by asking for nothing.

        Everything it claims is checked against what it was actually shown: a file
        it never opened is dropped and logged rather than passed on as fact.
        """
        lib = LibraryService(ws.root_path)
        tree = lib.tree()
        listed: set[str] = set(tree["files"])
        opened: set[str] = set()
        matched: set[str] = set()

        listing = "\n".join(tree["files"]) or "(no files)"
        # A workspace that wrote down what it learned gets that read first. It
        # is a prior, never evidence: it enters the prompt and the pack's
        # `knowledge` block, and it does NOT enter `files`, so it cannot borrow
        # the pack's "this path was actually opened" guarantee (docs/04 §4.9).
        knowledge = read_knowledge(ws.root_path, listed)
        if knowledge and knowledge["stale_paths"]:
            self._log(
                goal_id, None, "warn",
                f"{knowledge['path']} names {len(knowledge['stale_paths'])} path(s) this "
                f"workspace does not have ({', '.join(knowledge['stale_paths'][:3])}) — "
                "told the librarian to distrust them rather than to follow them",
            )
        prior = format_knowledge(knowledge)
        prompt = (
                f"Goal: {goal.title}\nDescription:\n{goal.description}\n\n"
                f"Workspace tree (depth 2, {len(tree['files'])} entries"
                f"{', TRUNCATED' if tree['truncated'] else ''}):\n{listing}\n\n"
                f"{prior}"
            )

        last: dict[str, Any] = {}
        rounds_used = 0
        capped = False
        for round_no in range(1, MAX_LIBRARY_ROUNDS + 1):
            rounds_used = round_no
            out = await self.orchestrator.run_agent("librarian", goal_id, None, prompt)
            last = out if isinstance(out, dict) else {}
            requests = self._library_requests(last)
            if last.get("enough") is True or not requests:
                break
            if round_no == MAX_LIBRARY_ROUNDS:
                self._log(
                    goal_id, None, "warn",
                    f"librarian reached the {MAX_LIBRARY_ROUNDS}-round cap with "
                    f"{len(requests)} request(s) still pending — using what it has",
                )
                # The pack is real but partial, and the difference matters: this
                # is the difference between "the workspace had nothing more" and
                # "the engine stopped asking", which the metrics read as two
                # different outcomes.
                capped = True
                break
            text, opened_now, matched_now, refused = self._serve_library_requests(goal_id, lib, requests)
            opened |= opened_now
            matched |= matched_now
            if refused:
                self._log(
                    goal_id, None, "warn",
                    f"librarian asked for {refused} thing(s) it may not have — refused, not run",
                )
            # The prior is repeated every round on purpose: a later round is a
            # fresh model call with a fresh prompt, and a librarian that
            # forgets the architecture note halfway through is worse than one
            # that never read it.
            prompt = (
                f"Goal: {goal.title}\nDescription:\n{goal.description}\n\n"
                f"Material you asked for:\n{text}\n\n"
                f"{prior}\n\n"
                "Now answer with the evidence pack. Set enough=true if you have what "
                "the goal needs, or keep asking by filling reads/searches/git/run."
            )

        evidence = self._evidence_pack(
            goal_id, last, opened, matched, listed, rounds_used,
            capped=capped, knowledge=knowledge,
        )
        self.goals.publish(self._event(goal_id, None, "library_evidence", evidence))
        self._log(
            goal_id, None, "info",
            f"librarian: {len(evidence['files'])} file(s) cited from {len(listed)} considered, "
            f"{rounds_used} round(s)"
            + (f", test command {evidence['test_command']}" if evidence.get("test_command") else ""),
        )
        return evidence

    def _library_requests(self, out: dict[str, Any]) -> list[tuple[str, Any]]:
        """What the librarian wants to see next, trimmed to the per-round caps."""
        caps = (
            ("reads", MAX_LIBRARY_READS_PER_ROUND),
            ("searches", MAX_LIBRARY_SEARCHES_PER_ROUND),
            ("git", MAX_LIBRARY_GIT_PER_ROUND),
            ("run", MAX_LIBRARY_RUNS_PER_ROUND),
        )
        requests: list[tuple[str, Any]] = []
        for kind, cap in caps:
            raw = out.get(kind) or []
            if isinstance(raw, (str, dict)):
                raw = [raw]
            if not isinstance(raw, list):
                continue
            for item in raw[:cap]:
                # A read may be a plain path string or {path, offset, limit} —
                # the line-range form that reaches the bottom half of a big file.
                if kind == "reads" and isinstance(item, dict) and item.get("path"):
                    requests.append((kind, {
                        "path": str(item["path"]),
                        "offset": _as_read_int(item.get("offset"), 1),
                        "limit": _as_read_int(item.get("limit"), None),
                    }))
                elif kind == "searches" and isinstance(item, dict) and item.get("query"):
                    # A search may be a plain string or {query, regex, glob} —
                    # the pattern form for structural questions. The regex
                    # itself is validated (and bounded) by the library, so a
                    # bad pattern arrives here as a refusal, not a crash.
                    requests.append((kind, {
                        "query": str(item["query"]),
                        "regex": bool(item.get("regex")),
                        "glob": str(item["glob"]) if item.get("glob") else None,
                    }))
                else:
                    requests.append((kind, item))
        return requests

    def _serve_library_requests(
        self, goal_id: str, lib: LibraryService, requests: list[tuple[str, Any]],
    ) -> tuple[str, set[str], set[str], int]:
        """Fetch what the librarian asked for, within one round's budget.

        A refusal is information, never a failure: an escape attempt, a forbidden
        command, or a file that is not there is reported back so the librarian can
        ask for something else instead of the goal dying over it.
        """
        chunks: list[str] = []
        opened: set[str] = set()
        matched: set[str] = set()
        refused = 0
        budget = MAX_ROUND_CHARS

        for kind, item in requests:
            if budget <= 0:
                chunks.append("… (this round's material budget is spent — ask again next round)")
                break
            label = " ".join(str(a) for a in item) if isinstance(item, list) else str(item)
            try:
                if kind == "reads" and isinstance(item, dict):
                    # Line-range read: the path plus the 1-based window.
                    res = lib.read(item["path"], offset=item.get("offset"), limit=item.get("limit"))
                    label = (
                        f"{item['path']} lines {item.get('offset') or 1}-"
                        f"{(item.get('offset') or 1) + (item.get('limit') or 400) - 1}"
                    )
                    opened.add(res["path"])
                    text = format_read(res)
                elif kind == "reads":
                    res = lib.read(label)
                    opened.add(res["path"])
                    text = format_read(res)
                elif kind == "searches" and isinstance(item, dict):
                    # Structured search: {query, regex, glob}.
                    res = lib.search(item["query"], glob=item.get("glob"), regex=bool(item.get("regex")))
                    matched.update(m["path"] for m in res["matches"])
                    text = format_search(res)
                elif kind == "searches":
                    res = lib.search(label)
                    matched.update(m["path"] for m in res["matches"])
                    text = format_search(res)
                else:
                    args = [str(a) for a in item] if isinstance(item, list) else [str(item)]
                    res = lib.git(args) if kind == "git" else lib.run(args)
                    text = format_command(res)
            except (PathEscapeError, CommandNotAllowed) as exc:
                refused += 1
                text = f"--- refused ({kind}): {label} — {exc}"
            except (OSError, ValueError) as exc:
                refused += 1
                text = f"--- could not read ({kind}): {label} — {exc}"
            chunks.append(text)
            budget -= len(text)

        return "\n\n".join(chunks), opened, matched, refused

    @staticmethod
    def _clean_cited_path(raw: Any) -> str:
        """Normalize a model-cited path without eating its leading dots.

        `lstrip("./")` strips *characters*, not the prefix: `.gitignore` came
        back as `gitignore` and `..env` as `env`, so a cited dotfile never
        matched what the librarian actually opened and was dropped as unseen —
        or planned under a name that does not exist. Only a true `./` prefix
        is removed.
        """
        text = str(raw or "").strip()
        return text[2:] if text.startswith("./") else text

    def _evidence_pack(
        self,
        goal_id: str,
        out: dict[str, Any],
        opened: set[str],
        matched: set[str],
        listed: set[str],
        rounds_used: int,
        capped: bool = False,
        knowledge: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Keep only the claims the engine can stand behind.

        A path is supported when the librarian actually opened it, when a search
        showed a matching line in it, or when it exists in the tree listing. A path
        in none of those was never seen — it is dropped and logged, because a
        confident list of files that do not exist is exactly how "planning from the
        repository" turns into planning from a hallucination.
        """
        def strength(path: str) -> str | None:
            if path in opened:
                return "opened"
            if path in matched:
                return "matched"
            if path in listed:
                return "listed"
            return None

        files: list[dict[str, Any]] = []
        unsupported: list[str] = []
        for entry in out.get("files") or []:
            if not isinstance(entry, dict):
                continue
            path = self._clean_cited_path(entry.get("path"))
            how = strength(path)
            if how:
                files.append(
                    {"path": path, "why": str(entry.get("why") or "")[:300], "evidence": how}
                )
            else:
                unsupported.append(path or "(empty)")
        if unsupported:
            self._log(
                goal_id, None, "warn",
                f"librarian cited {len(unsupported)} path(s) it never saw — dropped: "
                + ", ".join(unsupported[:5]),
            )

        symbols: list[dict[str, Any]] = []
        for s in out.get("symbols") or []:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            path = self._clean_cited_path(s.get("path"))
            if name and (not path or strength(path)):
                symbols.append({"name": name[:120], "path": path})

        raw_cmd = out.get("test_command")
        if isinstance(raw_cmd, str):
            raw_cmd = [raw_cmd]
        test_command = (
            [str(t) for t in raw_cmd][:12]
            if isinstance(raw_cmd, list) and raw_cmd
            else None
        )

        def strings(key: str, limit: int, width: int) -> list[str]:
            raw = out.get(key) or []
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                return []
            return [str(v)[:width] for v in raw if str(v).strip()][:limit]

        return {
            "summary": (str(out["summary"])[:1200] if out.get("summary") else None),
            "files": files[:20],
            "symbols": symbols[:20],
            "conventions": strings("conventions", 10, 200),
            "test_command": test_command,
            "risks": strings("risks", 10, 300),
            "rounds": rounds_used,
            "counts": {
                "opened": len(opened),
                "matched": len(matched),
                "considered": len(listed),
            },
            "dropped_paths": unsupported[:5],
            # True when the round cap stopped the search with material still
            # outstanding (docs/04 §4.0). It is the difference between "this
            # workspace has no more to say" and "the engine stopped asking",
            # and it is worth knowing in the pack rather than only in a log line.
            "capped": capped,
            # The workspace's own prior, summarised and kept OUT of `files`,
            # `symbols` and `dropped_paths`. Those three are the engine standing
            # behind a claim; this is a claim, with the staleness check attached
            # so the roles reading it can see how old it is. docs/04 §4.9.
            "knowledge": (
                {
                    "path": knowledge["path"],
                    "chars": knowledge["chars"],
                    "truncated": knowledge["truncated"],
                    "stale_paths": knowledge["stale_paths"],
                }
                if knowledge
                else None
            ),
        }

    def _suggested_paths_context(
        self, fs: FileSystemService, paths: list[str], limit: int = 4000
    ) -> tuple[str, str]:
        """(readable contents, note about the ones that could not be read).

        A planner's `suggested_paths` are a guess, and a guess can name a path that
        escapes the workspace or a file that is not text. Failing the step on that
        before the fixer is ever called means the model never gets to correct
        itself, so the guess is reported as material that was not available rather
        than raised.
        """
        lines: list[str] = []
        skipped: list[str] = []
        for p in paths:
            try:
                target = fs.resolve(p)
            except PathEscapeError:
                skipped.append(f"{p} (outside the workspace)")
                continue
            text = fs.read_text_or_none(p)
            if text is None:
                kind = "a directory" if target.is_dir() else "not readable as text"
                skipped.append(f"{p} ({kind})")
                continue
            lines.append(f"- {p}: {text[:limit]}")
        ctx = "\n".join(lines) if lines else "(no readable suggested path)"
        note = ""
        if skipped:
            note = (
                "\nSuggested paths that could not be read (do not assume their contents):\n"
                + "\n".join(f"- {s}" for s in skipped)
            )
        return ctx, note

    def _evidence_text(self, evidence: dict[str, Any]) -> str:
        """Render an evidence pack for a prompt. Never invents a section."""
        if not evidence:
            return (
                "(none — no reconnaissance was available, so keep steps small, prefer "
                "paths the goal names, and say what you could not verify)"
            )
        lines: list[str] = []
        if evidence.get("summary"):
            lines.append(f"Summary: {evidence['summary']}")
        files = evidence.get("files") or []
        if files:
            lines.append(f"Files ({len(files)}, each marked with how it was verified):")
            lines += [f"- {f['path']} [{f['evidence']}] — {f['why']}" for f in files]
        symbols = evidence.get("symbols") or []
        if symbols:
            # The parenthesised path is built from a string literal rather than a
            # nested f-string quoting itself with the outer one's character: reusing
            # the outer quote is PEP 701, which only Python 3.12+ accepts, and this
            # module has to import on the 3.10 minimum pyproject.toml declares.
            # `tests/test_min_python_syntax.py` guards the rule.
            lines.append("Symbols: " + ", ".join(
                f"{s['name']}" + (f" ({s['path']})" if s.get("path") else "")
                for s in symbols
            ))
        if evidence.get("conventions"):
            lines.append("Conventions: " + "; ".join(evidence["conventions"]))
        if evidence.get("test_command"):
            lines.append("Test command this repository runs: " + " ".join(evidence["test_command"]))
        known = evidence.get("knowledge")
        if known:
            # Last, and labelled: a prior that arrives first reads as a fact.
            lines += [
                "",
                f"PRIOR — not evidence: this workspace's {known['path']} was read this "
                f"run ({known['chars']} chars"
                + (", truncated" if known["truncated"] else "")
                + "). It was written by an earlier run and none of it is verified; "
                "use it to aim your steps' paths, and do not restate it as a finding.",
            ]
            if known.get("stale_paths"):
                lines.append(
                    "It names path(s) absent from this workspace: "
                    + ", ".join(known["stale_paths"][:5])
                    + ". Do not plan a step against those."
                )
        if evidence.get("risks"):
            lines.append("Risks: " + "; ".join(evidence["risks"]))
        if evidence.get("dropped_paths"):
            lines.append(
                "Paths the librarian mentioned but never opened (unverified, do not rely on "
                f"them): {', '.join(evidence['dropped_paths'])}"
            )
        return "\n".join(lines)

    # ── design (DRAFT RECONSTRUCTION — see docs/04 §4.0a) ───────────────────
    #
    # One bounded call, no tools: the design agent decides and the fixer stays
    # the only role whose changes reach the disk. The contract it locks is
    # published (`design_contract`) and read back per step (`_design_for`), so
    # the planner, the fixer and the critic work from the same direction rather
    # than from whichever prompt happened to produce it — including when a step
    # is driven in another process.

    def _design_for(self, goal_id: str) -> dict[str, Any]:
        """The contract this goal locked, read back from its event log.

        From events rather than memory, for the same reason `_evidence_for` is:
        a step driven in another process must be handed the direction its
        planner planned from. A goal planned before this stage existed has no
        contract, and an empty lookup is the answer, never a failure.
        """
        latest: dict[str, Any] = {}
        for ev in self.goals.events_after(goal_id, 0):
            if ev.type == "design_contract":
                latest = ev.payload or {}
        return latest

    async def _design(
        self, goal_id: str, goal: Goal, ws: Any, evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Lock the direction this goal is built against, and publish it.

        A brand contract the workspace already has wins over a proposal: it is
        handed over whole and framed as binding, and a reply cannot relabel
        where it came from. `applies: false` — or a reply naming no direction —
        is an answer, not a failure, and publishes nothing.
        """
        brand = self._brand_contract(goal_id, ws)
        prompt = self._design_prompt(goal, evidence, brand, deliverable=False)
        out = await self.orchestrator.run_agent("design", goal_id, None, prompt)
        contract = self._design_contract(out, brand)
        if not contract:
            self._log(
                goal_id, None, "info",
                "design: this goal locks no visual direction — the planner is told so "
                "rather than handed an invented one",
            )
            return {}
        published = {**contract, "mode": goal.mode}
        self.goals.publish(self._event(goal_id, None, "design_contract", published))
        # The published dict, not a narrower one: the local `design` and
        # `_design_for()` then agree on their shape, so a caller cannot read
        # `mode` from one and miss it in the other.
        return published

    async def _design_deliverable(
        self, goal_id: str, goal: Goal, ws: Any, evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """A design-deliverable goal: the design agent authors the file.

        Same slot, same single bounded call, same no-tools rule — only the
        relationship inverts. The contract the workspace already has is shown as
        *revision material* rather than as a law, and `_design_contract` is
        called WITHOUT the brand so the draft's body survives: dropping it is
        right for a normal goal (a second body would compete with a contract
        that exists as a file) and fatal here, since the body is the deliverable.

        A body is mandatory. A goal with nothing written has nothing to deliver,
        and that is the one thing this stage is loud about — the caller turns
        the raise into the usual non-fatal warning, so the goal still plans.
        """
        brand = self._brand_contract(goal_id, ws)
        prompt = self._design_prompt(goal, evidence, brand, deliverable=True)
        out = await self.orchestrator.run_agent("design", goal_id, None, prompt)
        contract = self._design_contract(out)
        if not (contract.get("design_md") or "").strip():
            raise AgentOutputInvalid(
                "a design-deliverable goal must author the complete DESIGN.md body in "
                "design_md — without one there is nothing to deliver",
                role="design",
            )
        published = {**contract, "mode": "design"}
        self.goals.publish(self._event(goal_id, None, "design_contract", published))
        return published

    async def _knowledge_deliverable(
        self, goal_id: str, goal: Goal, ws: Any, evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """A knowledge-deliverable goal: the design agent authors CODIFY.md.

        The same shape as a design deliverable with the relationship flipped
        again, and that is the whole point of the mode. A design goal revises a
        contract it is *bound* by; a knowledge goal rewrites a prior it is
        *superseding*, so the existing file is shown as revision material and
        the draft must stand on its own — a knowledge file that merely restates
        last month's file is worth less than none, because it looks current.

        The same body field carries it. `design_md` is the deliverable slot in
        the design reply, and the alternative — a second body field per file —
        would be a shape the model has to be told about twice.
        """
        prompt = self._knowledge_prompt(goal, evidence, ws)
        out = await self.orchestrator.run_agent("design", goal_id, None, prompt)
        contract = self._design_contract(out)
        if not (contract.get("design_md") or "").strip():
            raise AgentOutputInvalid(
                f"a knowledge-deliverable goal must author the complete {ROOT_KNOWLEDGE_MD} "
                f"body in design_md — without one there is nothing to deliver",
                role="design",
            )
        published = {**contract, "mode": "knowledge"}
        self.goals.publish(self._event(goal_id, None, "design_contract", published))
        return published

    def _knowledge_prompt(
        self, goal: Goal, evidence: dict[str, Any], ws: Any,
    ) -> str:
        """The knowledge call's prompt: the goal, what is known, what came before.

        The existing file is read here rather than from the pack because the
        pack deliberately does not carry it: a prior is not evidence, and the
        evidence block is the one part of these prompts the roles are told they
        may rely on. Mixing the two would spend the pack's credibility on a file
        nobody has checked.
        """
        known = read_knowledge(ws.root_path)
        parts = [
            f"Goal: {goal.title}\nDescription:\n{goal.description}",
            f"What the librarian found:\n{self._evidence_text(evidence)}",
            KNOWLEDGE_BRIEF_PROMPT,
        ]
        if known:
            parts.append(
                f"--- current {ROOT_KNOWLEDGE_MD} — revision material, NOT a prior you "
                "may trust ---\n"
                f"{known['text']}\n"
                f"--- end {ROOT_KNOWLEDGE_MD} ---\n"
                "The workspace has written knowledge down before. Check it against the "
                "evidence pack above and keep only what the evidence supports: a claim "
                f"this file makes that the pack cannot back is {ROOT_KNOWLEDGE_MD}'s "
                "whole failure mode, because the next run reads it as a prior. Write the "
                "file it should have."
            )
        else:
            parts.append(
                f"The workspace has no {ROOT_KNOWLEDGE_MD} today. That is the file this "
                "goal writes, so design_md must be a complete, standalone document."
            )
        return "\n\n".join(parts)

    def _design_prompt(
        self, goal: Goal, evidence: dict[str, Any], brand: dict[str, Any] | None,
        *, deliverable: bool,
    ) -> str:
        """The design call's prompt: the goal, what is known, and the contract.

        Two shapes, because the two goals want opposite things from the same
        file. A normal goal is *bound* by a contract the workspace already has;
        a design goal is writing it, so the same text is revision material and
        must not be mistaken for a law to obey.
        """
        parts = [
            f"Goal: {goal.title}\nDescription:\n{goal.description}",
            f"What the librarian found:\n{self._evidence_text(evidence)}",
        ]
        if deliverable:
            parts += [
                DESIGN_BRIEF_PROMPT,
                "Your draft is the deliverable, so it has to stand on its own. If the "
                "workspace already documents a brand it is shown below as revision "
                "material: write the one it should have, and put it in design_md.",
            ]
        if brand:
            if deliverable:
                parts.append(
                    f"--- current brand contract ({brand['origin']}) at {brand['path']} "
                    "— revision material, not a law to obey ---\n"
                    f"{brand['text']}\n"
                    "--- end brand contract ---\n"
                    "The workspace already documents a brand: revise or replace it. The "
                    "body you return is the draft a step will write, and the user pins it "
                    "from there — do not answer 'no change', because the deliverable is a "
                    "file."
                )
            else:
                parts.append(
                    f"--- Workspace brand contract ({brand['origin']}) at {brand['path']} "
                    "— BINDING ---\n"
                    f"{brand['text']}\n"
                    "--- end brand contract ---\n"
                    "That file is the workspace's own contract and is BINDING. Derive the "
                    "design system, tokens and components from it rather than inventing "
                    "new ones, and return the direction the rest of this goal obeys."
                )
        elif deliverable:
            parts.append(
                "The workspace has no brand contract today. That is the file this goal "
                "writes, so design_md must be a complete, standalone contract."
            )
        else:
            parts.append(
                "The workspace has no brand contract. Propose one, and put the DESIGN.md "
                "body a step should publish in design_md so the file exists to be pinned."
            )
        return "\n\n".join(parts)

    def _brand_contract(self, goal_id: str, ws: Any) -> dict[str, Any] | None:
        """The brand contract this workspace already has, or None.

        Resolution order (docs/04 §4.0a): the user's pin first, then a non-empty
        `DESIGN.md` at the workspace root. The pin wins outright — naming a file
        convention would not find is the whole reason to pin one.

        A pin whose file has gone missing is a warning and a fall back to
        proposing, never a silent downgrade to convention: the `origin` published
        with the contract says which of the two the result actually is, so nobody
        reads the answer as "the pinned brand was used".
        """
        fs = FileSystemService(ws.root_path)
        pinned = str(getattr(ws, "design_contract_path", "") or "").strip()
        if pinned:
            text = fs.read_text_or_none(pinned)
            if not text or not text.strip():
                self._log(
                    goal_id, None, "warn",
                    f"pinned brand contract {pinned!r} is not readable as text in this "
                    "workspace — proposing a brand instead, and the published contract "
                    "will not claim the pin",
                )
                return None
            return self._brand_file(goal_id, pinned, "pinned", text)
        text = fs.read_text_or_none(ROOT_DESIGN_MD)
        # An empty file is not a brand. Binding every goal to nothing would also
        # silently drop the body a fixer was supposed to write.
        if not text or not text.strip():
            return None
        return self._brand_file(goal_id, ROOT_DESIGN_MD, "discovered", text)

    def _brand_file(
        self, goal_id: str, path: str, origin: str, text: str,
    ) -> dict[str, Any]:
        """A resolved contract file, truncated to a prompt-safe bound and said so."""
        if len(text) > MAX_CONTRACT_FILE_CHARS:
            self._log(
                goal_id, None, "warn",
                f"brand contract {path!r} is {len(text)} chars — using the first "
                f"{MAX_CONTRACT_FILE_CHARS}",
            )
            text = text[:MAX_CONTRACT_FILE_CHARS]
        return {"path": path, "origin": origin, "text": text}

    def _design_contract(
        self, raw: object, brand: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Normalize one design reply into the contract the engine publishes.

        Trims rather than fails: this is prompt material, and a row the model
        got slightly wrong should not cost the goal its direction. Two things
        are the engine's fact rather than the model's claim — where the brand
        came from (`source`), and whether a body may compete with a contract
        file that already exists.
        """
        if not isinstance(raw, dict):
            raise AgentOutputInvalid(
                f"design contract must be a JSON object, got {type(raw).__name__}",
                role="design",
            )
        if not raw.get("applies"):
            # "This goal changes no rendered surface" is a real answer.
            return {}
        direction = str(raw.get("direction") or "").strip()
        if not direction:
            # `applies: true` with nothing to say is a decline, not a contract:
            # the planner plans against values, not against a heading.
            return {}

        artifact = str(raw.get("artifact") or "").strip()
        if artifact not in DESIGN_ARTIFACTS:
            artifact = "other"
        tokens_in = raw.get("tokens")
        if not isinstance(tokens_in, dict):
            tokens_in = {}
        system_in = raw.get("design_system")
        if not isinstance(system_in, dict):
            system_in = {}

        name = str(system_in.get("name") or "").strip()
        source: str | None = None
        origin: str | None = None
        if brand:
            source = str(brand.get("path") or "") or None
            origin = str(brand.get("origin") or "") or None
            if not name:
                # A pinned file must render as *something*: the filename is a
                # fact, where a made-up label would not be.
                name = os.path.splitext(os.path.basename(source or ""))[0]
        # The body is passed on as written: it is a file, and a file whose
        # trailing newline the engine decided to trim is a file that differs
        # from the one the agent authored.
        body_raw = raw.get("design_md")
        body = body_raw if isinstance(body_raw, str) else ""
        if brand:
            # A goal cannot answer a contract that exists as a file by writing a
            # second one over it — the exact drift the pin exists to stop.
            body = ""
        elif len(body) > MAX_DESIGN_MD_CHARS:
            body = body[:MAX_DESIGN_MD_CHARS]

        return {
            "applies": True,
            "artifact": artifact,
            "direction": direction,
            "design_system": {"name": name, "source": source, "origin": origin},
            "tokens": {
                "colors": self._design_rows(tokens_in.get("colors"), MAX_DESIGN_COLORS),
                "typography": self._design_rows(
                    tokens_in.get("typography"), MAX_DESIGN_TYPOGRAPHY,
                ),
                "spacing": self._design_lines(tokens_in.get("spacing"), MAX_DESIGN_SPACING),
                "radii": self._design_lines(tokens_in.get("radii"), MAX_DESIGN_RADII),
            },
            "components": [
                {"name": row["name"], "purpose": str(row.get("purpose") or "").strip()}
                for row in self._design_rows(raw.get("components"), MAX_DESIGN_COMPONENTS)
            ],
            "conventions": self._design_lines(raw.get("conventions"), MAX_DESIGN_LINES),
            "constraints": self._design_lines(raw.get("constraints"), MAX_DESIGN_LINES),
            "acceptance": self._design_lines(raw.get("acceptance"), MAX_DESIGN_LINES),
            "design_md": body or None,
        }

    @staticmethod
    def _design_rows(raw: Any, cap: int) -> list[dict[str, Any]]:
        """Named rows from a reply's list, trimmed to `cap`.

        A row with no `name` is dropped: an unnamed token is a value nothing can
        reference, so passing it on only gives the roles noise to be told to
        ignore.
        """
        rows: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            return rows
        for item in raw[:cap]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            rows.append({**item, "name": name})
        return rows

    @staticmethod
    def _design_lines(raw: Any, cap: int) -> list[str]:
        """Non-empty strings from a reply's list, trimmed to `cap`."""
        lines: list[str] = []
        if not isinstance(raw, list):
            return lines
        for item in raw[:cap]:
            text = str(item or "").strip()
            if text:
                lines.append(text)
        return lines

    def _design_text(self, contract: dict[str, Any]) -> str:
        """Render a contract for the roles that must work from it.

        One renderer for the planner, the fixer and the critic, so three roles
        cannot end up with three different descriptions of one direction. An
        empty contract says so rather than saying nothing: "no direction" is
        information, and the alternative is each role inventing one.

        `pinned` and `discovered` are labelled differently on purpose. One is
        the user's instruction, the other a convention the engine noticed, and
        the roles' weight for it follows from which one it is.
        """
        if not contract:
            return (
                "Design contract: (none — this goal locked no visual direction). There "
                "is nothing to obey, so invent no visual direction: follow the "
                "repository's own conventions and keep every step's look consistent "
                "with the others."
            )
        system = contract.get("design_system") or {}
        source = str(system.get("source") or "")
        name = str(system.get("name") or "").strip() or "unnamed"
        if system.get("origin") == "pinned" and source:
            where = f"{name} (pinned at {source} — binding)"
        elif source:
            where = f"{name} (found at {source} — binding)"
        else:
            where = f"{name} (proposed by this run, not yet the workspace's)"

        lines = [
            "Design contract — locked before this plan was made, and binding for it.",
            f"Design system: {where}",
            f"Direction: {contract.get('direction', '')}",
        ]
        tokens = contract.get("tokens") or {}
        colors = [
            f"{c.get('name')} {c.get('value')}".strip() for c in tokens.get("colors") or []
        ]
        if colors:
            lines.append("Colors: " + ", ".join(colors))
        type_rows = [
            f"{t.get('name')} = {t.get('value')}".strip() for t in tokens.get("typography") or []
        ]
        if type_rows:
            lines.append("Typography: " + ", ".join(type_rows))
        scale = [*(tokens.get("spacing") or []), *(tokens.get("radii") or [])]
        if scale:
            lines.append("Spacing and radii: " + ", ".join(str(s) for s in scale))
        components = [
            f"{c.get('name')} — {c.get('purpose')}".strip(" —")
            for c in contract.get("components") or []
        ]
        if components:
            lines.append("Components: " + "; ".join(components))
        for label, key in (
            ("Conventions", "conventions"),
            ("Constraints", "constraints"),
            ("Acceptance", "acceptance"),
        ):
            rows = contract.get(key) or []
            if rows:
                lines.append(f"{label}: " + "; ".join(str(r) for r in rows))
        body = str(contract.get("design_md") or "")
        if body:
            # Named from the mode, not hardcoded: a planner told to write
            # "DESIGN.md body" during a knowledge goal plans a step that writes
            # the wrong file, and the fixer is then handed a document it has no
            # instruction to write.
            target = DELIVERABLE_FILES.get(str(contract.get("mode") or ""), ROOT_DESIGN_MD)
            lines += [
                "",
                f"--- {target} body (the file a step must produce) ---",
                body,
                f"--- end {target} body ---",
                "One planned step writes that file: write it verbatim in its own step, "
                "do not paste it into other files, and do not substitute a summary for it.",
            ]
        return "\n".join(lines)

    @staticmethod
    def _deliverable_write_path(step: PlanStep, contract: dict[str, Any]) -> str:
        """The deliverable a step means to write, or "" when it means none.

        `suggested_paths` is the only honest signal a plan gives about intent,
        and it is the same signal for all three roles that care — so one helper
        recognizes it for the fixer, the verifier and the critic rather than
        three that could disagree. Which filename counts is the mode's business,
        from `DELIVERABLE_FILES`: a step that happens to name CODIFY.md in a
        design goal is writing a document the pipeline did not ask for.
        """
        wanted = DELIVERABLE_FILES.get(str(contract.get("mode") or ""))
        if not wanted:
            return ""
        for raw in step.suggested_paths or []:
            path = str(raw or "").strip()
            if os.path.basename(path.replace("\\", "/")).casefold() == wanted.casefold():
                return path
        return ""

    def _deliverable_artifact(
        self, goal_id: str, step: PlanStep, ws_root: str, path: str,
    ) -> tuple[str | None, str]:
        """(content, "as written" | "as proposed") for a deliverable step.

        A dry run reaches the disk not at all, so there is no file for the
        verifier to read — but the step's proposal is stored, and it is exactly
        the bytes Apply replays. Reading it here, rather than re-rendering the
        diff, is what makes "reviewed" a fact about the deliverable instead of a
        claim about the pipeline. Both judges come through this one helper, so
        the role that decides the step and the role that reports on it cannot be
        reading different things.
        """
        if not path:
            return (None, "")
        written = FileSystemService(ws_root).read_text_or_none(path)
        if written is not None:
            return (written, ARTIFACT_WRITTEN)
        proposed = self.goals.proposed_content(goal_id, step.id, path)
        if proposed is not None:
            return (proposed, ARTIFACT_PROPOSED)
        return (None, "")

    def _brand_drifts(
        self, diffs: list[dict[str, Any]], design: dict[str, Any], root_path: str,
    ) -> list[str]:
        """What the written artifacts fail to evidence about a binding contract.

        Only what text comparison can prove is reported; everything else stays
        the critic's judgment. The evidence is the *changes*, never the whole
        tree — a workspace already full of the brand must not pass a step for
        that reason. A proposed brand draws nothing at all: enforcement is for
        what a workspace signed, not for advice.

        Advisory by construction: these findings ride on the verifier's own
        record and never flip the verdict. Only the tests (or the critic) fail a
        step, and a text-matching heuristic is not the evidence that should stop
        one.
        """
        system = design.get("design_system") or {}
        if system.get("origin") not in ("pinned", "discovered"):
            return []
        corpus = self._brand_corpus(diffs, root_path)
        if not corpus:
            # Nothing readable to check is not evidence of anything. A step that
            # wrote a binary blob, or wrote nothing, is reported as silent rather
            # than as a contract the whole workspace failed.
            return []

        tokens = design.get("tokens") or {}
        colors = [c for c in (tokens.get("colors") or []) if isinstance(c, dict)]
        faces = [t for t in (tokens.get("typography") or []) if isinstance(t, dict)]
        # The contract's own token names are not evidence of it: an acceptance
        # line naming `accent` must not pass because `accent` appears in the diff
        # as an identifier.
        token_names = {
            str(c.get("name") or "").casefold() for c in colors
        } | {str(t.get("name") or "").casefold() for t in faces}

        drifts: list[str] = []
        for color in colors:
            value = str(color.get("value") or "").strip()
            if value and value.casefold() not in corpus:
                drifts.append(
                    f"token color {color.get('name')} {value} appears nowhere in the changes"
                )
        for face in faces:
            stack = str(face.get("value") or "").strip()
            words = [
                w for w in _text_words(f"{stack} {face.get('name') or ''}") if len(w) > 2
            ]
            if words and not any(w in corpus for w in words):
                drifts.append(
                    f"typography {face.get('name')} ({stack}) appears nowhere in the changes"
                )
        scale = [
            str(s).strip() for s in (tokens.get("spacing") or []) + (tokens.get("radii") or [])
            if str(s).strip()
        ]
        if scale and not any(s.casefold() in corpus for s in scale):
            drifts.append("none of the contract's spacing/radius tokens appear in the changes")
        for label, key in (("acceptance", "acceptance"), ("constraint", "constraints")):
            for raw in design.get(key) or []:
                line = str(raw).strip()
                if line and not self._line_evidenced(line, corpus, token_names):
                    drifts.append(f"{label} not evidenced by any change: {line}")
        return drifts[:MAX_BRAND_DRIFTS]

    def _brand_corpus(self, diffs: list[dict[str, Any]], root_path: str) -> str:
        """The text this step wrote, casefolded, each file bounded.

        The applied content first (it is what a replay will write), then the
        diff, then the artifact on disk for a change whose diff carries no text —
        a binary write is still subject to the contract. A file that cannot be
        decoded contributes nothing rather than an empty string: absence of
        evidence is not evidence of absence.
        """
        fs = FileSystemService(root_path)
        chunks: list[str] = []
        for d in diffs:
            text = d.get("resolved_content")
            if not isinstance(text, str) or not text:
                text = d.get("unified_diff")
            if not isinstance(text, str) or not text:
                text = fs.read_text_or_none(str(d.get("path") or ""))
            if isinstance(text, str) and text:
                chunks.append(text[:MAX_DRIFT_DIFF_CHARS])
        return "\n".join(chunks).casefold()

    @staticmethod
    def _line_evidenced(line: str, corpus: str, token_names: set[str]) -> bool:
        """Does any substantive word of this line appear in what was written?"""
        words = [
            w for w in _text_words(line)
            if len(w) > 2 and w not in _DRIFT_STOPWORDS and w not in token_names
        ]
        if not words:
            # Nothing checkable in the line: not reportable as unaddressed
            # either, because a finding the checker cannot support is worse than
            # no finding at all.
            return True
        return any(w in corpus for w in words)

    def _cancelled(self, goal_id: str) -> bool:
        """True when the goal was cancelled (or otherwise left RUNNING) mid-step.

        `_run_steps` only checks status *between* steps, so without this a cancel
        landing mid-step changed nothing: the step ran to the end — including the
        scribe's `git commit` of changes the user had just asked to stop. Writing
        to the user's repository is the one act a cancel must be able to prevent,
        so the stages that lead to it re-check before doing irreversible work.
        """
        return self.goals.get(goal_id).status != "RUNNING"

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
        self.goals.update_status(goal_id, expected_version, "RUNNING")
        self._reset_step(goal_id, step)
        await self.run_step(goal_id, step_id)
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
    ) -> tuple[list[dict[str, Any]], bool]:
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
        out = await self.orchestrator.run_agent(
            "fixer", goal_id, step.id,
            f"Step: {step.title}\n{step.description}\n\n"
            f"{feedback_text}"
            f"What the librarian found:\n{self._evidence_text(evidence or {})}\n"
            f"Suggested paths (current contents):\n{ctx}{unreadable}"
            f"{design_section}",
        )
        files = self._parse_files(out)
        # The fixer may declare itself unfinished: multi-stage changes (a config
        # file in one pass, the code that reads it in the next) do not fit one
        # reply. It asks by returning needs_another_pass=true WITH a plan note;
        # an empty files list is still just a no-op, so the two can't be confused.
        wants_pass = bool(isinstance(out, dict) and out.get("needs_another_pass"))
        if not files and wants_pass:
            raise AgentOutputInvalid(
                "needs_another_pass requires files in the same reply — ask for "
                "another pass alongside the changes you just made",
                role="fixer",
            )
        if not files:
            self._log(goal_id, step.id, "warn", "fixer returned an empty files list — no changes will be written")
        try:
            # Apply runs BEFORE storage now: an `edit` op is only a description
            # ("replace this exact text") until the engine resolves it against
            # the file as it exists, and only the resolved full content is a
            # proposal "Apply" can replay deterministically later.
            summaries = fs.apply(files, dry_run=dry_run)
        except ValueError as exc:
            # An edit whose old_text does not match the file is the fixer's
            # mistake — a contract failure, not an internal error.
            raise AgentOutputInvalid(str(exc), role="fixer") from exc
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
                    "cargo test, go test ./..., read-only git status/diff/log -1."
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
            },
        ))
        if verdict == "fail":
            raise TestsFailed(f"tests failed: {explanation}")

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
        self._set_step(goal_id, step, "IN_PROGRESS", commit_message=commit_message, last_agent_role="scribe")
        self._log(goal_id, step.id, "info", summary)

        if dry_run or not root_path or not self.git.is_git_repo(root_path):
            # A dry run commits nothing by design, and a workspace that is not a
            # repository has nothing to commit into. Both are the scribe doing its
            # job, so neither is reported as a failure — a per-role rate that
            # counted "not a repo" as a broken scribe would punish every user whose
            # workspace is a plain directory.
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

    # --- parsing ------------------------------------------------------

    def _parse_steps(self, out: Any) -> list[dict[str, Any]]:
        steps = (out or {}).get("steps")
        if not isinstance(steps, list) or not (1 <= len(steps) <= 20):
            raise AgentOutputInvalid("planner must return 1..20 steps", role="planner")
        parsed = []
        for s in steps:
            title = s.get("title")
            desc = s.get("description")
            paths = s.get("suggested_paths") or []
            if not title or not desc:
                raise AgentOutputInvalid("planner step missing title/description", role="planner")
            if not isinstance(paths, list):
                raise AgentOutputInvalid("planner suggested_paths must be a list", role="planner")
            parsed.append({"title": title, "description": desc, "suggested_paths": [str(p) for p in paths]})
        return parsed

    def _parse_files(self, out: Any) -> list[dict[str, Any]]:
        files = (out or {}).get("files")
        if not isinstance(files, list):
            raise AgentOutputInvalid("fixer must return files list", role="fixer")
        parsed = []
        for f in files:
            path = f.get("path")
            action = f.get("action")
            content = f.get("content")
            if not path or action not in ("create", "update", "delete", "edit"):
                raise AgentOutputInvalid("fixer file entry invalid", role="fixer")
            if action == "delete" and content is not None:
                raise AgentOutputInvalid("fixer delete must have null content", role="fixer")
            if action == "edit":
                edits = f.get("edits")
                if not isinstance(edits, list) or not edits:
                    raise AgentOutputInvalid("fixer edit requires a non-empty edits list", role="fixer")
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

    def _reset_step(self, goal_id: str, step: PlanStep) -> None:
        self.goals._db.execute(
            "UPDATE plan_steps SET status='PENDING', review_notes=NULL, commit_message=NULL, last_agent_role=NULL WHERE id = ?",
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

    def _set_status(self, goal_id: str, status: str, step_id: str | None) -> None:
        # update_status publishes the goal_status event itself.
        current = self.goals.get(goal_id)
        self.goals.update_status(goal_id, current.version, status, step_id)

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
