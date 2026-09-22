from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from engine.default_prompts import DEFAULT_PROMPTS
from engine.fs import FileSystemService, PathEscapeError
from engine.git import GitService
from engine.library import (
    MAX_ROUND_CHARS,
    READ_ONLY_TIMEOUT_S,
    LibraryService,
    format_command,
    format_read,
    format_search,
)
from engine.laya import LayaDecision, LayaService, build_state
from engine.models import AgentRole, Event, EventType, PlanStep
from engine.providers import ProviderError
from engine.sandbox import CommandNotAllowed, SandboxService
from engine.services import AgentRegistryService, ApiError, GoalService, WorkspaceService


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

# Failures that mean the target could not be used at all, and so may be retried on
# the role's fallback. The list is deliberately made of *provider* problems — no
# credential, an endpoint that refuses, an error status, a reply the contract
# cannot parse. A failure that is not here (a bug in our own code) stops the role
# instead of silently running it somewhere else, because pointing an unknown
# failure at a second model is how a real defect gets buried under a retry.
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


class CriticRejection(AgentOutputInvalid):
    def __init__(self, message: str, reasons: list[str], role: str | None = "critic"):
        super().__init__(message, role)
        self.reasons = reasons


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
    def __init__(self, registry: AgentRegistryService, goals: GoalService):
        self.registry = registry
        self.goals = goals

    def _event(self, goal_id: str, step_id: str | None, type_: EventType, payload: dict) -> Event:
        return Event(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            step_id=step_id,
            type=type_,
            payload=payload,
            timestamp=time.time(),
            sequence=self.goals.next_sequence(goal_id),
        )

    def _not_configured(self, role: AgentRole, target, label: str) -> AgentNotConfigured:
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

    def _provider_failure(self, role: AgentRole, target, label: str, exc: ProviderError) -> ProviderError:
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

    def _targets(self, role: AgentRole):
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

    def _publish_fallback(
        self, goal_id: str, step_id: str | None, role: AgentRole,
        primary, target, exc: ProviderError,
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

    def _all_targets_failed(self, role: AgentRole, failures: list[tuple[str, str, ProviderError]]) -> ProviderError:
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
        failures: list[tuple[str, str, ProviderError]] = []

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
            def _record(usage: dict, _role=role, _provider=target.provider, _model=model_name):
                self.goals.publish(self._event(
                    goal_id, step_id, "usage",
                    {
                        "role": _role,
                        "provider": _provider,
                        "model": _model,
                        **usage,
                    },
                ))
            provider.usage_sink = _record
            try:
                raw = await provider.complete(
                    system_prompt=system,
                    user_prompt=user_prompt,
                    model=model_name,
                    temperature=target.temperature,
                    max_tokens=target.max_tokens,
                )
            except ProviderError as exc:
                failures.append((label, target.provider, self._provider_failure(role, target, label, exc)))
                if exc.code not in FALLBACK_TRIGGER_CODES:
                    break
                continue
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
    ):
        self.goals = goals
        self.workspaces = workspaces
        self.orchestrator = AgentOrchestrator(registry, goals)
        self.sandbox = sandbox
        self.git = git or GitService()
        # System-1 pre-flight gate (see engine/laya.py). Optional by design: a
        # gate that cannot run is a skipped gate, never a broken pipeline.
        self.laya = laya or LayaService(registry=registry)

    def _event(self, goal_id: str, step_id: str | None, type_: EventType, payload: dict) -> Event:
        return Event(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            step_id=step_id,
            type=type_,
            payload=payload,
            timestamp=time.time(),
            sequence=self.goals.next_sequence(goal_id),
        )

    # --- public -------------------------------------------------------

    async def run_planning(self, goal_id: str) -> None:
        goal = self.goals.get(goal_id)
        ws = self.workspaces.get(goal.workspace_id)

        # ── Laya: System-1 pre-flight gate ──────────────────────────────
        # Cheap typed decisions (intent / risk / injection probability) before
        # any LLM call. High-confidence injection stops the goal here.
        try:
            decision = await self.laya.decide(build_state(goal, ws.root_path))
        except Exception as exc:  # pragma: no cover - decide() already guards
            decision = LayaDecision(engine="skipped", skipped_reason=f"gate error: {exc}")
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
            evidence = await self._librarian(goal_id, goal, ws)
        except (AgentOutputInvalid, ProviderError, ValueError) as exc:
            # A librarian that cannot run must not kill a goal that might still
            # work: the planner is told there is no evidence and proceeds.
            self._log(
                goal_id, None, "warn",
                f"librarian unavailable ({getattr(exc, 'code', 'error')}: {exc}) — "
                "planning without evidence",
            )
            evidence = {}

        prompt = (
            f"Title: {goal.title}\nDescription:\n{goal.description}\n\n"
            f"Librarian evidence:\n{self._evidence_text(evidence)}"
        )
        try:
            out = await self.orchestrator.run_agent("planner", goal_id, None, prompt)
            # A cancel that landed while the planner was thinking must win: a
            # goal the user cancelled must not reappear as PENDING with a plan
            # they explicitly stopped. (PLANNING is a legal cancel state.)
            if self.goals.get(goal_id).status == "CANCELLED":
                self._log(goal_id, None, "info", "cancelled during planning — discarding the plan")
                return
            steps = self._parse_steps(out)
            self._insert_steps(goal_id, steps)
            self._log(goal_id, None, "info", f"planner produced {len(steps)} steps")
        except (AgentOutputInvalid, ProviderError, ValueError) as exc:
            # Planning is the planner's phase; anything raised here is its.
            self._fail(
                goal_id, None, getattr(exc, "code", "agent_output_invalid"), str(exc),
                role=getattr(exc, "role", None) or "planner",
            )
            return
        self._set_status(goal_id, "PENDING", None)

    # ── librarian ──────────────────────────────────────────────────────────────

    def _evidence_for(self, goal_id: str) -> dict:
        """The evidence pack this goal's librarian produced, read back from events.

        From the event log rather than memory: a goal resumed in another process
        must still hand its fixer the same material its planner planned from.
        """
        latest: dict = {}
        for ev in self.goals.events_after(goal_id, 0):
            if ev.type == "library_evidence":
                latest = ev.payload or {}
        return latest

    async def _librarian(self, goal_id: str, goal: Goal, ws: Any) -> dict:
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
        prompt = (
                f"Goal: {goal.title}\nDescription:\n{goal.description}\n\n"
                f"Workspace tree (depth 2, {len(tree['files'])} entries"
                f"{', TRUNCATED' if tree['truncated'] else ''}):\n{listing}"
            )

        last: dict = {}
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
                break
            text, opened_now, matched_now, refused = self._serve_library_requests(goal_id, lib, requests)
            opened |= opened_now
            matched |= matched_now
            if refused:
                self._log(
                    goal_id, None, "warn",
                    f"librarian asked for {refused} thing(s) it may not have — refused, not run",
                )
            prompt = (
                f"Goal: {goal.title}\nDescription:\n{goal.description}\n\n"
                f"Material you asked for:\n{text}\n\n"
                "Now answer with the evidence pack. Set enough=true if you have what "
                "the goal needs, or keep asking by filling reads/searches/git/run."
            )

        evidence = self._evidence_pack(goal_id, last, opened, matched, listed, rounds_used)
        self.goals.publish(self._event(goal_id, None, "library_evidence", evidence))
        self._log(
            goal_id, None, "info",
            f"librarian: {len(evidence['files'])} file(s) cited from {len(listed)} considered, "
            f"{rounds_used} round(s)"
            + (f", test command {evidence['test_command']}" if evidence.get("test_command") else ""),
        )
        return evidence

    def _library_requests(self, out: dict) -> list[tuple[str, Any]]:
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

    def _evidence_pack(
        self,
        goal_id: str,
        out: dict,
        opened: set[str],
        matched: set[str],
        listed: set[str],
        rounds_used: int,
    ) -> dict:
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

        files: list[dict] = []
        unsupported: list[str] = []
        for entry in out.get("files") or []:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "").strip().lstrip("./")
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

        symbols: list[dict] = []
        for s in out.get("symbols") or []:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            path = str(s.get("path") or "").strip().lstrip("./")
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

    def _evidence_text(self, evidence: dict) -> str:
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
            lines.append("Symbols: " + ", ".join(
                f"{s['name']}{f' ({s['path']})' if s.get('path') else ''}" for s in symbols
            ))
        if evidence.get("conventions"):
            lines.append("Conventions: " + "; ".join(evidence["conventions"]))
        if evidence.get("test_command"):
            lines.append("Test command this repository runs: " + " ".join(evidence["test_command"]))
        if evidence.get("risks"):
            lines.append("Risks: " + "; ".join(evidence["risks"]))
        if evidence.get("dropped_paths"):
            lines.append(
                "Paths the librarian mentioned but never opened (unverified, do not rely on "
                f"them): {', '.join(evidence['dropped_paths'])}"
            )
        return "\n".join(lines)

    def _cancelled(self, goal_id: str) -> bool:
        """True when the goal was cancelled (or otherwise left RUNNING) mid-step.

        `_run_steps` only checks status *between* steps, so without this a cancel
        landing mid-step changed nothing: the step ran to the end — including the
        scribe's `git commit` of changes the user had just asked to stop. Writing
        to the user's repository is the one act a cancel must be able to prevent,
        so the stages that lead to it re-check before doing irreversible work.
        """
        return self.goals.get(goal_id).status != "RUNNING"

    async def run_step(self, goal_id: str, step_id: str, stored_files: list[dict] | None = None) -> None:
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
                outcome: dict | None = None
                summaries: list[dict] | None = None
                prior_failure: dict | None = None
                passes_left = MAX_FIXER_PASSES
                for attempt in range(1, MAX_FIX_ATTEMPTS + 2):  # attempts, plus the final one
                    final = attempt > MAX_FIX_ATTEMPTS
                    try:
                        summaries, wants_pass = await self._fixer(
                            goal_id, step, fs, goal.dry_run, evidence,
                            failure_feedback=prior_failure,
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
                            summaries, wants_pass = await self._fixer(
                                goal_id, step, fs, goal.dry_run, evidence,
                                failure_feedback=prior_failure,
                            )
                        outcome = await self._verifier(
                            goal_id, step, ws, evidence, prior_failure=prior_failure,
                        )
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
                outcome = await self._verifier(goal_id, step, ws, evidence)
            # The fixer has already written by the time the verifier runs, so
            # those stages are not cancel-safe and cancelling mid-flight leaves
            # their work on disk (documented behavior of a mid-run cancel).
            # But verification, judgment and *the commit* are separable: a
            # cancel that arrives while tests run must stop the goal before the
            # scribe records changes the user asked not to make.
            if self._cancelled(goal_id):
                self._log(goal_id, step.id, "info", "cancelled — skipping review and commit for this step")
                return
            await self._critic(goal_id, step, fs, summaries, evidence, outcome, ws_root=ws.root_path)
            if self._cancelled(goal_id):
                self._log(goal_id, step.id, "info", "cancelled — skipping the summary for this step")
                return
            await self._scribe(goal_id, step, summaries, ws.root_path, goal.dry_run, outcome)
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

        rows = self.goals._db.execute(
            "SELECT step_id, path, action, content FROM proposed_files WHERE goal_id = ? ORDER BY rowid",
            (goal_id,),
        ).fetchall()
        if not rows:
            raise ApiError(409, "nothing_to_apply", "dry-run produced no proposed changes")

        by_step: dict[str, list[dict]] = {}
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

        for step in self.goals.steps(goal_id):
            refreshed = self.goals.get(goal_id)
            if refreshed.status != "RUNNING":
                return refreshed
            await self.run_step(goal_id, step.id, stored_files=by_step.get(step.id, []))
            refreshed = self.goals.get(goal_id)
            if refreshed.status != "RUNNING":
                return refreshed

        try:
            self._set_status(goal_id, "COMPLETED", None)
        except ApiError:
            pass
        return self.goals.get(goal_id)

    def _replay_files(self, goal_id: str, step: PlanStep, fs: FileSystemService, files: list[dict], dry_run: bool) -> list[dict]:
        """Apply stored file operations and emit the same diff events as _fixer."""
        summaries = fs.apply(files, dry_run=dry_run)
        self._publish_changes(goal_id, step.id, summaries, dry_run)
        self._set_step(goal_id, step, "IN_PROGRESS", last_agent_role="fixer")
        return summaries

    async def retry_step(self, goal_id: str, step_id: str, expected_version: int) -> PlanStep:
        step = self._step(goal_id, step_id)
        if not (step.status == "FAILED" or (step.status == "IN_PROGRESS" and bool(step.review_notes))):
            raise ApiError(409, "step_not_retryable", f"step {step_id} is not in a retryable state (status={step.status})")
        self.goals.update_status(goal_id, expected_version, "RUNNING")
        self._reset_step(goal_id, step)
        await self.run_step(goal_id, step_id)
        return self._step(goal_id, step_id)

    # --- stages -------------------------------------------------------

    def _last_test_result(self, goal_id: str, step_id: str) -> dict:
        """The verifier's most recent published record for this step.

        Read from the event log, not memory, like every other piece of goal
        state — a resumed goal replays the same evidence.
        """
        latest: dict = {}
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
        evidence: dict | None = None,
        failure_feedback: dict | None = None,
    ) -> list[dict]:
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
                lines.append(
                    f"Command that was run: {' '.join(outcome['argv'])} (exit {outcome.get('exit_code')})"
                )
            lines.append(
                "Fix what the failure describes. Do not start over from scratch — "
                "edit your previous approach."
            )
            feedback_text = "\n".join(lines) + "\n\n"
        out = await self.orchestrator.run_agent(
            "fixer", goal_id, step.id,
            f"Step: {step.title}\n{step.description}\n\n"
            f"{feedback_text}"
            f"What the librarian found:\n{self._evidence_text(evidence or {})}\n"
            f"Suggested paths (current contents):\n{ctx}{unreadable}",
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

    def _publish_changes(self, goal_id: str, step_id: str, summaries: list[dict], dry_run: bool) -> None:
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
        self, goal_id: str, step: PlanStep, ws: Any, evidence: dict | None = None,
        prior_failure: dict | None = None,
    ) -> dict:
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
        argv: list[str] | None = None
        result: dict | None = None
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
                }
                # Explicit fields rather than **-splatting: `outcome` also
                # carries `ran`/`refused` (event-payload keys the critic's
                # rendering uses) that the publisher has no parameters for.
                self._test_result(
                    goal_id, step, argv=outcome["argv"],
                    verdict=outcome["verdict"], explanation=outcome["explanation"],
                    exit_code=outcome["exit_code"], refusals=outcome["refused"],
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

            try:
                result = self.sandbox.run_command(ws.root_path, proposed, timeout_s=timeout_s)
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
                if proposals_left <= 0:
                    prompt = (
                        f"The command was NOT run — the sandbox refused it ({refused_because}). "
                        "It was your last attempt. Return the verdict with argv null."
                    )
                else:
                    prompt = (
                        f"The command {proposed!r} was NOT run: the sandbox refused it "
                        f"({refused_because}). Propose a different command from the allowed set, or "
                        "return the verdict with argv null and say that no permitted test "
                        "command exists. Allowed: pytest, python -m pytest, npm test, pnpm test, "
                        "cargo test, go test ./..., read-only git status/diff/log -1."
                    )
                continue

            argv = proposed
            prompt = (
                f"Command ran. exit_code={result['exit_code']}\nstdout:\n{result['stdout']}\n"
                f"stderr:\n{result['stderr']}\nNow return the verdict with argv null."
            )

    def _test_result(
        self,
        goal_id: str,
        step: PlanStep,
        argv: list[str] | None,
        verdict: str | None,
        explanation: str | None,
        exit_code: int | None = None,
        refusals: list[str] | None = None,
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
            },
        ))
        if verdict == "fail":
            raise TestsFailed(f"tests failed: {explanation}")

    async def _critic(
        self,
        goal_id: str,
        step: PlanStep,
        fs: FileSystemService,
        diffs: list[dict],
        evidence: dict | None = None,
        test_outcome: dict | None = None,
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
                    result = self.sandbox.run_command(
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
                prompt = f"{base_prompt}\n\n--- Your requested inspection round ---\n{output}\n\nNow give your decision (or one more read-only command)."
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

    async def _scribe(
        self, goal_id: str, step: PlanStep, diffs: list[dict], root_path: str = "",
        dry_run: bool = False, test_outcome: dict | None = None,
    ) -> None:
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

        if not dry_run and root_path and self.git.is_git_repo(root_path):
            # The last guard before the one irreversible act. A cancel that
            # landed during the critic's call must not end in a commit the user
            # asked to stop; the summary above still records what was done.
            if self._cancelled(goal_id):
                self._log(
                    goal_id, step.id, "info",
                    "cancelled — skipping the commit for this step",
                )
            else:
                # Only what this step wrote. A bare `git add -A` would commit the
                # user's own half-finished work under this step's message.
                paths = [d["path"] for d in diffs]
                commit_hash = self.git.commit(root_path, commit_message, paths)
                if commit_hash:
                    self._log(goal_id, step.id, "info", f"git committed {commit_hash[:7]}: {commit_message}")
                elif paths:
                    # Non-empty paths and no commit: either there was nothing left to
                    # record (the files matched what is already committed) or git
                    # refused. Silence here reads as "committed" to anyone watching.
                    self._log(
                        goal_id, step.id, "warn",
                        "git commit produced nothing — the step's files match the last commit, "
                        "or git refused (check its user.name/user.email config)",
                    )

    # --- parsing ------------------------------------------------------

    def _parse_steps(self, out: Any) -> list[dict]:
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

    def _parse_files(self, out: Any) -> list[dict]:
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

    def _insert_steps(self, goal_id: str, steps: list[dict]) -> None:
        for ordinal, s in enumerate(steps):
            self.goals._db.execute(
                "INSERT INTO plan_steps (id, goal_id, ordinal, title, description, suggested_paths, status, review_notes, commit_message, last_agent_role) "
                "VALUES (?, ?, ?, ?, ?, ?, 'PENDING', NULL, NULL, NULL)",
                (str(uuid.uuid4()), goal_id, ordinal, s["title"], s["description"], json.dumps(s["suggested_paths"])),
            )
        self.goals._db.commit()

    def _store_proposed_files(
        self, goal_id: str, step_id: str, files: list[dict], summaries: list[dict] | None = None,
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

    def _set_step(self, goal_id: str, step: PlanStep, status: str, **fields) -> None:
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
