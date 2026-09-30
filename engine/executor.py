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
import json
import os
import re
import subprocess
from typing import TYPE_CHECKING, Any
from collections.abc import Callable


from engine.chat_prompts import CHAT_SYSTEM_PROMPT, CONDUCTOR_SYSTEM_PROMPT
from engine.conductor import (
    BASE_TOOLS,
    DEFAULT_MAX_MOVES,
    DEFAULT_MAX_TURNS,
    STEP_TOOLS,
    Conductor,
)
# Re-exported (the `as` form is what tells mypy so): `from engine.executor import extract_json` still resolves.
from engine.replies import (
    REPLY_KEYS as REPLY_KEYS,
    REPLY_TOLERATES_TRUNCATION as REPLY_TOLERATES_TRUNCATION,
    extract_json as extract_json,
)
from engine.default_prompts import (
    DESIGN_BRIEF_PROMPT,
    KNOWLEDGE_BRIEF_PROMPT,
)
from engine.recall import (
    build_brief,
)
from engine.fs import FileSystemService, PathEscapeError, ProtectedRootError
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
from engine.laya import LayaDecision, build_state
from engine.models import (
    AgentConfig,
    AgentRole,
    BUILTIN_PROVIDERS,
    Goal,
    PlanStep,
)
from engine.providers import (
    FALLBACK_TRIGGER_CODES as FALLBACK_TRIGGER_CODES,
    ProviderError,
)
from engine.sandbox import CommandNotAllowed
from engine.services import ApiError
from engine.skills import SkillSet, load_skills
from engine.toolcall import ToolSpec
from engine.executor_core import (
    DEFAULT_PARALLEL_WIDTH as DEFAULT_PARALLEL_WIDTH,
    _ExecutorCore,
    _env_parallel_width as _env_parallel_width,
)
from engine.conductor_tools import ConductorTools as ConductorTools, _Conducted as _Conducted
from engine.executor_support import (
    AgentNotConfigured,
    AgentOutputInvalid as AgentOutputInvalid,
    CriticRejection,
    PathRefused,
    STAGE_OUTCOMES as STAGE_OUTCOMES,
    TestsFailed,
    WriteWithdrawn,
    _verifier_outcome,
)

if TYPE_CHECKING:
    pass


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



# The gate labels that describe a *change*, the only ones that point the conductor at
# `ship-a-change` and arm its reminder. `question`, `other` and an unlabelled request
# are answered directly (see `_intent_brief`). The fallback below, for a turn with no
# conductor or one that could not finish, is a different decision with its own
# rationale — planning is a superset of answering — and is not this set.
CHANGE_INTENTS = ("code_change", "ops_command")

class ExecutorService(_ExecutorCore):

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
                    self._set_status(goal_id, "COMPLETED", None)
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
        self._set_status(goal_id, "COMPLETED", None)

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
        tools = ConductorTools(self, goal_id, goal, root, skills)
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
        for row in rows:
            if row.provider == cfg.provider and row.base_url:
                return cfg.model_copy(update={
                    "protocol": row.protocol, "base_url": row.base_url, "api_key_ref": row.api_key_ref,
                })
        for row in rows:
            if row.fallback_provider == cfg.provider and row.fallback_base_url:
                return cfg.model_copy(update={
                    "protocol": row.fallback_protocol or "openai_compat",
                    "base_url": row.fallback_base_url, "api_key_ref": None,
                })
        return cfg

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

    async def _librarian(
        self, goal_id: str, goal: Goal, ws: Any, task: str = "",
    ) -> dict[str, Any]:
        """Look around before anything is planned or changed.

        Rounds: the librarian asks for material (reads, searches, read-only git or
        inspect commands), the engine fetches it, and it asks again — capped at
        MAX_LIBRARY_ROUNDS so "look around" cannot become an unbounded crawl. It
        finishes early by setting `enough`, or by asking for nothing.

        Everything it claims is checked against what it was actually shown: a file
        it never opened is dropped and logged rather than passed on as fact.

        `task` is what the conductor said it wanted found out, and it exists for
        the same reason `_fixer`'s `guidance` does: the conductor is the one
        dispatching this run, so a "look around the workspace" that ignored what
        it was sent to look for was not doing its job. It is placed *beside* the
        goal and labelled as an addition, never in place of it — the goal is what
        the user asked for, and a conductor that could rewrite it could send the
        librarian after something the user never requested. Empty (the engine's
        own path, with no conductor) leaves the prompt byte-identical to before.
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
        # The conductor's ask, beside the goal rather than instead of it, and
        # ahead of the tree so it is read as an instruction about the material
        # that follows rather than as another thing to look at.
        ask_section = ""
        if task.strip():
            ask_section = (
                "The conductor asked for this specifically, in addition to the "
                f"goal above:\n{task.strip()}\n\n"
            )
        prompt = (
                f"Goal: {goal.title}\nDescription:\n{goal.description}\n\n"
                f"{ask_section}"
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
            text, opened_now, matched_now, refused = await self._serve_library_requests(goal_id, lib, requests)
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

    async def _serve_library_requests(
        self, goal_id: str, lib: LibraryService, requests: list[tuple[str, Any]],
    ) -> tuple[str, set[str], set[str], int]:
        """Fetch what the librarian asked for, within one round's budget.

        A refusal is information, never a failure: an escape attempt, a forbidden
        command, or a file that is not there is reported back so the librarian can
        ask for something else instead of the goal dying over it.

        Async because three of the four kinds are slow off the CPU: `git` and
        `run` start a **process** through the sandbox, and a search walks the
        tree. Run in the event loop, one twenty-second command held every
        WebSocket tick, every `/health` probe and every cancel request behind it
        — the app spent the command's whole duration reporting itself offline.
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
                    res = await asyncio.to_thread(
                        lib.search, item["query"],
                        glob=item.get("glob"), regex=bool(item.get("regex")),
                    )
                    matched.update(m["path"] for m in res["matches"])
                    text = format_search(res)
                elif kind == "searches":
                    res = await asyncio.to_thread(lib.search, label)
                    matched.update(m["path"] for m in res["matches"])
                    text = format_search(res)
                else:
                    args = [str(a) for a in item] if isinstance(item, list) else [str(item)]
                    run = lib.git if kind == "git" else lib.run
                    res = await asyncio.to_thread(run, args)
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
        task: str = "",
    ) -> dict[str, Any]:
        """Lock the direction this goal is built against, and publish it.

        A brand contract the workspace already has wins over a proposal: it is
        handed over whole and framed as binding, and a reply cannot relabel
        where it came from. `applies: false` — or a reply naming no direction —
        is an answer, not a failure, and publishes nothing.

        `task` is the conductor's ask, on the same terms as `_librarian`'s: it
        narrows what the designer decides about, beside the goal, and it is
        placed *before* the contract block so the workspace's own binding brand
        is still the last thing in the prompt and still wins.
        """
        brand = self._brand_contract(goal_id, ws)
        prompt = self._design_prompt(goal, evidence, brand, deliverable=False, task=task)
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
        # Read once and used twice: the prompt quotes these bytes, and the
        # published contract carries them, so a user comparing the two is
        # comparing a document against itself rather than against a second,
        # possibly newer, copy of the same file.
        known = read_knowledge(ws.root_path)
        prompt = self._knowledge_prompt(goal, evidence, ws, known)
        out = await self.orchestrator.run_agent("design", goal_id, None, prompt)
        contract = self._design_contract(out)
        if not (contract.get("design_md") or "").strip():
            raise AgentOutputInvalid(
                f"a knowledge-deliverable goal must author the complete {ROOT_KNOWLEDGE_MD} "
                f"body in design_md — without one there is nothing to deliver",
                role="design",
            )
        published = {**contract, "mode": "knowledge"}
        if known:
            # What the drafter was rewriting, published so the user can see it.
            # The body alone answers "what will this file say" and leaves "what
            # did it say" to a diff the user has to reconstruct — and for this
            # file that is the whole review, because a prior that only looks
            # plausible is exactly what this run exists to replace (docs/04
            # §4.9.2).
            #
            # The text is the fresh read the prompt quotes. The stale paths are
            # the pack's verdict on that same file, not the fresh read's: the
            # drafter's copy was taken with no tree listing, so its own
            # `stale_paths` is empty by construction, while the pack's is the
            # check the drafter was *also* given and has to live with. Publishing
            # the fresh read's empty list would read as "nothing here is stale",
            # which is the opposite of the truth.
            pack = evidence.get("knowledge") or {}
            published["revises"] = {
                "path": known["path"],
                "text": known["text"],
                "chars": known["chars"],
                "truncated": known["truncated"],
                "stale_paths": pack.get("stale_paths") or [],
            }
        self.goals.publish(self._event(goal_id, None, "design_contract", published))
        return published

    def _knowledge_prompt(
        self, goal: Goal, evidence: dict[str, Any], ws: Any,
        known: dict[str, Any] | None = None,
    ) -> str:
        """The knowledge call's prompt: the goal, what is known, what came before.

        The existing file is read here rather than from the pack because the
        pack deliberately does not carry it: a prior is not evidence, and the
        evidence block is the one part of these prompts the roles are told they
        may rely on. Mixing the two would spend the pack's credibility on a file
        nobody has checked.

        `known` is passed in rather than read here so the caller can publish the
        very bytes this prompt quotes. A second read would be a second file: one
        read for the prompt and another for the transcript, disagreeing whenever
        anything wrote in between, and a reviewer comparing them would be
        comparing a document against a slightly older copy of itself.
        """
        if known is None:
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
        *, deliverable: bool, task: str = "",
    ) -> str:
        """The design call's prompt: the goal, what is known, and the contract.

        Two shapes, because the two goals want opposite things from the same
        file. A normal goal is *bound* by a contract the workspace already has;
        a design goal is writing it, so the same text is revision material and
        must not be mistaken for a law to obey.

        `task` is optional and last-of-the-instructions: everything the workspace
        has to say about the contract follows it, so a conductor's ask can shape
        what is decided without ever outranking the contract.
        """
        parts = [
            f"Goal: {goal.title}\nDescription:\n{goal.description}",
            f"What the librarian found:\n{self._evidence_text(evidence)}",
        ]
        if task.strip():
            parts.append(
                "The conductor asked for this specifically, in addition to the "
                f"goal above:\n{task.strip()}"
            )
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
