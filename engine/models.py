from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# The pipeline slots. Each one has a different *ability*, not a different
# persona: the librarian is the only role that reads the workspace, the design
# agent the only one that locks a direction, the fixer the only one that writes
# it, the verifier the only one that runs a command, and the critic the only one
# that can stop a step. `laya` is the pre-flight gate, not a pipeline stage (see
# docs/05).
AgentRole = Literal[
    "laya",
    "librarian",
    "design",
    "planner",
    "fixer",
    "verifier",
    "critic",
    "scribe",
]
ProviderProtocol = Literal["anthropic", "openai_compat", "ollama", "google"]
# What a goal is for. "design" inverts the design role's usual relationship to
# the brand file: instead of deriving a contract from an existing one (or from
# nothing), its output becomes the workspace's DESIGN.md — reviewed by the
# critic before it is offered to be pinned.
#
# "chat" is not a pipeline like the other three. It is a *turn*: the Laya gate
# classifies the request, and a question is answered from one model call instead
# of being planned, fixed, verified and critiqued. It is still a goal row, and
# that is deliberate rather than a shortcut — `events.goal_id` is NOT NULL, so
# the event log *is* the WebSocket, the audit trail and the stats feed, and a
# turn that was not a goal would have nowhere to put a single event. Reusing the
# row buys the whole streaming and audit surface for free. See docs/09 §10.
GoalMode = Literal["normal", "design", "knowledge", "chat"]

GoalStatus = Literal[
    "PLANNING", "PENDING", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"
]
StepStatus = Literal["PENDING", "IN_PROGRESS", "COMPLETED", "FAILED"]
EventType = Literal[
    "goal_status",
    "step_status",
    "log",
    "diff",
    "test_result",
    "file_change_summary",
    "agent_assigned",
    "provider_fallback",
    "library_evidence",
    "design_contract",
    "stage_result",
    "plan_updated",
    "laya_decision",
    "fix_retry",
    "fixer_pass",
    "plan_consult",
    "agent_call_failed",
    "usage",
    "model_delta",
    "error",
]

SYSTEM_PROMPT_OVERRIDE_MAX = 32768
PROVIDER_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
# Ollama's `keep_alive`, as its own duration grammar rather than a bare number.
# Ollama accepts a bare count of seconds ("300"), a suffixed duration ("30s",
# "5m"), a compound one ("1h30m", so the number-with-unit pair repeats), and two
# sentinels: "-1" keeps the model resident until the server stops, "0" unloads
# it on the next request. Validated here rather than passed through because this
# value goes straight into a request body, and a field that cannot say anything
# but a duration is a field that cannot be used to inject one.
_NUM = r"\d{1,6}(?:\.\d{1,3})?"
_UNIT = r"(?:ns|us|ms|s|m|h)"
KEEP_ALIVE_RE = re.compile(rf"^(-1|0|{_NUM}(?:{_UNIT}{_NUM})*{_UNIT}?)$")
ROLES: tuple[AgentRole, ...] = (
    "laya",
    "librarian",
    "design",
    "planner",
    "fixer",
    "verifier",
    "critic",
    "scribe",
)

# What each slot is allowed to do, in one line, for the settings screen. The
# engine is the only place this is defined: a second copy in the UI is how the
# screen and the enforcement drift apart.
ROLE_JOB: dict[str, str] = {
    "laya": "Typed pre-flight decisions (intent, risk, injection). Can stop the goal before any model runs.",
    "librarian": "Reads the workspace: file reads, pattern search, git history, read-only inspect commands. Never writes, never commits.",
    "design": "Locks the creative direction a goal is built against: artifact, design system, tokens, components, acceptance. Reasons only — the fixer still writes the files.",
    "planner": "Turns the goal plus the librarian's evidence into ordered steps. Reasons only — no tools.",
    "fixer": "Writes the file edits a step asks for. The only role whose changes reach the disk.",
    "verifier": "Runs one allowlisted command and reports what actually happened. Can run commands nothing else may.",
    "critic": "Reviews the diff against the request. Can request changes and pause the step.",
    "scribe": "Writes the human summary and the commit subject. Wording only.",
}

# When each slot runs. This is the part users get wrong from the name alone: the
# librarian and planner run once for a goal, the rest run for every step.
ROLE_TIMING: dict[str, str] = {
    "laya": "once per goal, before any model call",
    "librarian": "once per goal, before planning",
    "design": "once per goal, after the librarian",
    "planner": "once per goal",
    "fixer": "once per step",
    "verifier": "once per step",
    "critic": "once per step",
    "scribe": "once per step",
}

# Display order: the gate, then the pipeline in execution order.
ROLE_ORDER: tuple[AgentRole, ...] = ROLES

BUILTIN_PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com",
        "needs_key": True,
        "local_only": False,
    },
    "openai": {
        "protocol": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "needs_key": True,
        "local_only": False,
    },
    "deepseek": {
        "protocol": "openai_compat",
        "base_url": "https://api.deepseek.com",
        "needs_key": True,
        "local_only": False,
    },
    "ollama": {
        "protocol": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "needs_key": False,
        "local_only": True,
    },
    "google": {
        "protocol": "google",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "needs_key": True,
        "local_only": False,
    },
    "openrouter": {
        "protocol": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "needs_key": True,
        "local_only": False,
    },
    "groq": {
        "protocol": "openai_compat",
        "base_url": "https://api.groq.com/openai/v1",
        "needs_key": True,
        "local_only": False,
    },
    # NVIDIA NIM, which is an OpenAI-compatible endpoint rather than a bespoke
    # dialect: it serves /v1/models and /v1/chat/completions, so the existing
    # openai_compat provider reads it unchanged and the catalogue is discovered
    # live like every other. Added because it is a provider people ask for and
    # were previously reaching for as a "custom" endpoint — which asks them for a
    # protocol and a base URL the engine already knows.
    "nvidia": {
        "protocol": "openai_compat",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "needs_key": True,
        "local_only": False,
    },
    # NVIDIA NIM, which is an OpenAI-compatible endpoint rather than a bespoke
    # dialect: it serves /v1/models and /v1/chat/completions, so the existing
    # openai_compat provider reads it unchanged and the catalogue is discovered
    # live like every other. Added because it is a provider people ask for and
    # were previously reaching for as a "custom" endpoint — which asks them for a
    # protocol and a base URL the engine already knows.
}


class AgentConfig(BaseModel):
    role: AgentRole
    display_name: str = Field(..., min_length=1, max_length=80)
    provider: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    protocol: ProviderProtocol
    # Empty means "not chosen yet", which is a legitimate state: there is no
    # compiled model list to default to (see DEFAULT_AGENTS), and the app says so
    # at run time instead of calling an endpoint with an empty id.
    model_name: str = Field("", max_length=128)
    api_key_ref: str | None = None
    base_url: str | None = None
    system_prompt_override: str | None = Field(None, max_length=SYSTEM_PROMPT_OVERRIDE_MAX)
    temperature: float = Field(0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(4096, gt=0, le=200000)
    # Ollama's context window for this role, in tokens. Ollama defaults to a
    # small window (4096) and *silently truncates* anything longer — the
    # librarian's evidence pack and the design role's contract simply do not
    # fit, and the model degrades a plan rather than erroring. Every seeded role
    # therefore ships a value (see `DEFAULT_AGENTS`); None is what a role is
    # left at once someone clears it, which is a real setting and the one the
    # server's own default is honestly described by. Only the Ollama provider
    # reads it; OpenAI-style APIs size the window server-side.
    num_ctx: int | None = Field(None, gt=0, le=1_000_000)
    # How long Ollama holds this model resident after a request finishes, as its
    # own duration string ("30m"), a bare number of seconds, "-1" for until the
    # server stops, or "0" to unload immediately. None sends nothing and leaves
    # Ollama's own default window — five minutes — in force, which is the honest
    # setting: role calls inside one goal are seconds apart and already warm, so
    # this is not a fix for a cold first call (that is the checkpoint load, and
    # raising `num_ctx` is what shrinks it). What it is for is the gap *between*
    # goals: a role called every few minutes pays a full reload each time, and on
    # a 7b model that is tens of seconds of nothing happening. Only Ollama reads
    # it — a hosted API has no process to keep alive.
    keep_alive: str | None = Field(None, max_length=32, pattern=KEEP_ALIVE_RE.pattern)
    # A second target this role may be called on when its primary is unusable —
    # no credential stored, the endpoint down, the model retired, or a reply the
    # contract cannot parse. Configured per role because the honest fallback
    # differs by job: a local model is fine for the scribe and a bad idea for the
    # fixer. Temperature and max_tokens stay the role's own: they describe the
    # job, not the model answering it.
    fallback_provider: str | None = Field(
        None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$"
    )
    fallback_model_name: str = Field("", max_length=128)
    fallback_protocol: ProviderProtocol | None = None
    fallback_base_url: str | None = None
    updated_at: float = 0

    @property
    def ollama_num_ctx(self) -> int | None:
        """The context window to request from Ollama, or None for its default.

        One indirection so a call site reads as intent (`num_ctx=cfg.ollama_num_ctx`)
        rather than as a field dump, and so a future policy — a floor for roles
        with big packs, say — has one home instead of five.
        """
        return self.num_ctx

    @property
    def ollama_keep_alive(self) -> str | None:
        """The residency window to ask Ollama for, or None to leave its default.

        The normalisation is a guard rather than the main path — `keep_alive` is
        validated on the way in, so a blank normally cannot reach here. It exists
        because the value goes straight into a request body, and an empty
        `keep_alive` is not "unset" to Ollama: it is a value the server has to
        reject, which would turn a cleared field into a failed goal.
        """
        value = (self.keep_alive or "").strip()
        return value or None

    @property
    def has_fallback(self) -> bool:
        """A fallback exists only if it names both a provider and a model.

        Half a fallback is not a fallback: it would fail at exactly the moment it
        was needed, so the engine treats an incomplete pair as unset rather than
        promising a rescue it cannot perform.
        """
        return bool((self.fallback_provider or "").strip() and (self.fallback_model_name or "").strip())


class AgentConfigUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    display_name: str | None = Field(None, min_length=1, max_length=80)
    provider: str | None = Field(None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    protocol: ProviderProtocol | None = None
    model_name: str | None = Field(None, max_length=128)
    api_key: str | None = Field(None, min_length=1, max_length=4096)
    base_url: str | None = None
    system_prompt_override: str | None = Field(None, max_length=SYSTEM_PROMPT_OVERRIDE_MAX)
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(None, gt=0, le=200000)
    num_ctx: int | None = Field(None, gt=0, le=1_000_000)
    # Explicit null clears back to Ollama's default window, like `num_ctx`.
    keep_alive: str | None = Field(None, max_length=32, pattern=KEEP_ALIVE_RE.pattern)
    # the patch through a typed struct where an explicit `null` and an absent
    # field are the same value, so "no fallback" needs a value of its own — and
    # the pattern allows exactly that one exception.
    fallback_provider: str | None = Field(
        None, max_length=64, pattern=r"^([a-z][a-z0-9_-]*)?$"
    )
    fallback_model_name: str | None = Field(None, max_length=128)
    fallback_protocol: ProviderProtocol | None = None
    fallback_base_url: str | None = None


class Workspace(BaseModel):
    id: str
    name: str = Field(..., min_length=1, max_length=120)
    root_path: str
    # The workspace's brand contract: a path *relative to the root*, or "" for
    # unpinned. When set, the design agent reads it before deciding a direction
    # and must obey it instead of proposing one of its own. Convention is the
    # fallback — a `DESIGN.md` at the workspace root is discovered without being
    # pinned — so this field records a deliberate choice, not the only way to have
    # a contract. See docs/04 §4.0a.
    design_contract_path: str = ""
    created_at: float


class WorkspaceDesignContract(BaseModel):
    """Pin, or (with an empty path) unpin, a workspace's brand contract."""
    model_config = {"extra": "forbid"}
    path: str = Field("", max_length=400)


class Conversation(BaseModel):
    """A thread of turns in a workspace — the thing a tab points at.

    Deliberately not a goal. A goal is one run: a plan, steps, a verifier, a
    commit. A conversation is the question several runs answer, and a chat that
    could only hold one run at a time is what the transcript's single message
    array was. Held in the engine rather than the client so a thread survives the
    window that opened it.
    """

    id: str
    workspace_id: str
    # Empty until a turn names it; `GoalService.split_title_description` is the
    # normaliser. No default title is invented here — a placeholder in the UI is
    # more honest than a string the user never chose.
    title: str = Field("", max_length=200)
    # Archived rather than deleted: a goal belongs to the thread it was asked in,
    # and dropping the thread would orphan the history.
    archived: bool = False
    # The thread this one was branched from, when it was started from inside
    # another thread rather than from an empty panel. `None` is the ordinary
    # case and is not a lesser one: most threads are top-level, and this says
    # where the *others* came from rather than ranking them.
    parent_id: str | None = None
    # The parent's name at the time this was read, joined in by the query rather
    # than left for the client to resolve.
    #
    # `parent_id` alone is not enough to label a row. The panel lists one
    # workspace's live threads and hides archived ones, so a child whose parent
    # has been archived cannot find its parent's name in that list — and the
    # label degrades to a generic word *permanently*, with the name still in the
    # database and nothing able to reach it. Carrying the name means the lineage
    # is data: it survives the parent being archived, and it is only `None` when
    # the parent row is genuinely gone.
    parent_title: str | None = None
    created_at: float
    updated_at: float


class ConversationCreate(BaseModel):
    """Starting a thread, optionally from inside another one.

    `parent_id` is the whole difference between "a new thread on the chat I am
    looking at" and "a brand-new chat": both are a fresh conversation, and
    without a link from the child to its parent they are the same row. It is
    set here and only here — `ConversationUpdate` is `extra: "forbid"`, so
    there is no route by which a thread can be re-parented after the fact, and
    the shape of a thread's history cannot be rewritten from the client.
    """

    model_config = {"extra": "forbid"}
    workspace_id: str
    title: str = Field("", max_length=20000)
    parent_id: str | None = None


class ConversationUpdate(BaseModel):
    """The one mutable thing about a conversation is what it is called.

    `extra: "forbid"` here is load-bearing in the same way it is on
    `GoalCreate`: a client cannot reach around the declared shape to set
    `archived` on a rename, or smuggle an `agent_config` through a rename route
    that was never meant to carry one.
    """

    model_config = {"extra": "forbid"}
    title: str = Field(..., min_length=1, max_length=20000)


class ConversationTurn(BaseModel):
    """One turn: the user's question and the goal that answered it.

    Read-only and derived — the stored facts are the goal and its events, so a
    turn is a shape the API draws rather than a row anyone could get out of step
    with. A goal with no `conversation_id` is its own single-turn thread, which
    is how pre-migration history reads.
    """

    goal_id: str
    conversation_id: str | None = None
    prompt: str
    status: GoalStatus
    created_at: float


class ShellTab(BaseModel):
    """One open tab, as the engine holds it: an identity, a place, and a payload.

    What the engine stores and what the window shows are deliberately different
    amounts of the same thing. `key` and `position` are the shared part — which
    tabs exist, and in what order — because that is the part two windows must
    agree about. `payload` is the rest (a chat tab's thread, a browser tab's
    address and history stack) and is a string of JSON the engine does not
    interpret: it is bounded and parsed for validity, and nothing more. A tab's
    shape belongs to the window showing it, and a store that learns a view's
    shape is how the two drift apart.
    """

    key: str = Field(..., min_length=1, max_length=64)
    position: int = Field(..., ge=0, le=10_000)
    kind: Literal["chat", "browser"]
    # Bounded because this is client-authored text the engine stores on its
    # behalf: 32k is far past any real tab (a 100-entry history is a few
    # kilobytes) and small enough that a client cannot use a tab row to fill the
    # database.
    payload: str = Field(..., max_length=32_000)
    updated_at: float


class ShellTabWrite(BaseModel):
    """One tab's arrival, as a client sends it.

    `extra: "forbid"` for the reason `ConversationUpdate` carries it: a body
    that tried to set `updated_at` here would be a client inventing the one
    fact the engine owns.
    """

    model_config = {"extra": "forbid"}
    key: str = Field(..., min_length=1, max_length=64)
    position: int = Field(..., ge=0, le=10_000)
    kind: Literal["chat", "browser"]
    payload: str = Field(..., max_length=32_000)


class Goal(BaseModel):
    id: str
    workspace_id: str
    # The thread this run belongs to. `None` for a goal that predates
    # conversations, which reads as its own thread.
    conversation_id: str | None = None
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=20000)
    status: GoalStatus
    dry_run: bool = False
    # plan_only: run planning only; execution is blocked until the user
    # explicitly enables it via POST /goals/{id}/enable-execution.
    plan_only: bool = False
    # parallel: independent steps of this goal may run concurrently.
    parallel: bool = False
    # mode: what this goal is *for*. "normal" is the default pipeline; "design"
    # makes the workspace's own brand contract the deliverable — the design
    # agent proposes/revises DESIGN.md, a step writes it for real, and the
    # critic reviews it before anyone pins it. "knowledge" inverts the same
    # shape around CODIFY.md: the design agent authors the file, a step writes
    # it, the critic reviews it, and every later goal's librarian reads it as a
    # prior. "chat" is a turn rather than a run — see `GoalMode`. docs/04 §4.0a.2.
    mode: GoalMode = "normal"
    # trace: record this goal's model calls so the run can be replayed without
    # a provider (docs/04 §8). Off by default and per goal, because a
    # recording is a copy of model output about the user's code — something to
    # switch on for one run, not a thing the engine quietly does to every goal.
    trace: bool = False
    version: int = Field(0, ge=0)
    created_at: float
    updated_at: float
    provider: str | None = None
    model: str | None = None


class PlanStep(BaseModel):
    id: str
    goal_id: str
    ordinal: int = Field(..., ge=0)
    title: str
    description: str
    suggested_paths: list[str] = []
    status: StepStatus = "PENDING"
    review_notes: str | None = None
    commit_message: str | None = None
    last_agent_role: AgentRole | None = None


class PlanStepUpdate(BaseModel):
    """Patch for a single plan step (pre-execution editing of plan-only goals)."""
    model_config = {"extra": "forbid"}
    expected_version: int
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, min_length=1, max_length=20000)
    suggested_paths: list[str] | None = Field(None, max_length=50)


class Event(BaseModel):
    id: str
    goal_id: str
    step_id: str | None = None
    type: EventType
    payload: dict[str, Any]
    timestamp: float
    sequence: int


class WorkspaceCreate(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(..., min_length=1, max_length=120)
    root_path: str


class GoalConversationUpdate(BaseModel):
    """Which thread an existing goal belongs to.

    The write `POST /goals` cannot make: a goal that predates conversations —
    or one restored from history — starts with no thread, and the link has to
    be settable after the row exists (docs/09 §6). `extra: "forbid"` for
    exactly the reason `GoalCreate` and `ConversationUpdate` carry it: this
    route moves one link and refuses to be a general goal editor, the same
    reach-around invariant 2 closes on `POST /goals`.
    """

    model_config = {"extra": "forbid"}
    conversation_id: str = Field(..., min_length=1)


class GoalCreate(BaseModel):
    model_config = {"extra": "forbid"}
    workspace_id: str
    # Which thread to append this run to. Optional, and a *known* field: the
    # `extra: "forbid"` above is invariant 2 (docs/00 §6.2) and adding a field
    # the API actually accepts does not weaken it — `agent_config` and anything
    # else unlisted are still refused.
    conversation_id: str | None = None
    # Chat UIs send the whole prompt as `title`; accept up to the description
    # cap and let GoalService.split_title_description normalize it down to a
    # 200-char title. The Goal model (storage/response) still enforces 200.
    title: str = Field(..., min_length=1, max_length=20000)
    description: str = Field("", max_length=20000)
    dry_run: bool = False
    plan_only: bool = False
    parallel: bool = False
    # Which pipeline this goal runs. "normal", "design" and "knowledge" exist; a
    # client sending anything else gets a 422 from the model itself, not a goal
    # that quietly runs the default.
    #
    # "chat" is refused here even though it is a legal `GoalMode`, because this
    # route is the door to *running a pipeline* and a chat goal has no pipeline.
    # The turn route creates it. One door, so "what can a client start" stays a
    # question with one answer.
    mode: GoalMode = "normal"

    @field_validator("mode")
    @classmethod
    def _refuse_chat(cls, value: str) -> str:
        if value == "chat":
            raise ValueError(
                "mode 'chat' is created by POST /conversations/{id}/turns, which "
                "classifies the request itself; POST /goals starts a pipeline and "
                "cannot make a turn"
            )
        return value
    # Opt this goal into tracing (docs/04 §8). A client may also turn it on
    # later with `PUT /goals/{id}/trace` before the run starts; both are the
    # user asking for a recording they can delete.
    trace: bool = False
    provider: str | None = None
    model: str | None = None


class ProviderKeyUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    provider: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    api_key: str = Field(..., min_length=1, max_length=4096)


class TurnCreate(BaseModel):
    """One turn in a thread: what the user said, and nothing else.

    Deliberately *not* a `GoalCreate`. The flags that shape a run — `dry_run`,
    `plan_only`, `parallel`, `mode` — are absent rather than defaulted, because
    the whole point of this route is that the caller does not choose the
    pipeline: the gate classifies the request and the engine picks. A turn body
    that could say `mode: "design"` would be a second door onto goal creation,
    and `docs/09` §10 exists so there is exactly one.

    `extra: "forbid"` is invariant 2 (docs/00 §6.2) for the same reason it is on
    every other write here: `agent_config` and anything else unlisted is a 422,
    not a silently accepted field.
    """

    model_config = {"extra": "forbid"}
    # The user's words. A turn with nothing to say is not a turn — `min_length=1`
    # after the strip below is what makes "  " a 422 rather than a goal whose
    # planner is handed an empty request.
    prompt: str = Field(..., min_length=1, max_length=20000)
    # The command bar's model choice, recorded on the goal as what the user asked for, exactly as
    # `POST /goals` does. It is written onto no role (docs/00 §6.2): a role runs on its own
    # configuration, which only the settings routes change.
    provider: str | None = None
    model: str | None = None
    # Record this turn's model calls (docs/04 §8), same opt-in as a goal.
    trace: bool = False


class VersionedAction(BaseModel):
    model_config = {"extra": "forbid"}
    expected_version: int = Field(..., ge=0)


class SpeakRequest(BaseModel):
    """Text to read aloud (`POST /audio/speak`). The voice and model are settings, not request fields."""

    model_config = {"extra": "forbid"}
    # Bounded here for memory; the friendlier limit (`speech.MAX_SPEAK_CHARS`) is the route's.
    text: str = Field(..., max_length=20000)


class TraceToggle(BaseModel):
    """Whether a goal records its model calls (docs/04 §8).

    A separate body from `VersionedAction`: turning recording on is not a
    status change and carries no version to guard, because the thing it
    touches (this goal's own recording) is not shared with anybody else.
    """

    model_config = {"extra": "forbid"}
    enabled: bool


class GoalDetail(Goal):
    steps: list[PlanStep] = []


class ErrorBody(BaseModel):
    """The engine's own refusal: a stable `code` and a sentence a person can act on.

    Every coded refusal — every `ApiError`, from a route or from deep inside a
    service — is returned as this, by one exception handler, so the shape does not
    depend on which layer noticed the problem.

    This is declared rather than implied because the alternative is what this file
    carried for a while: a model named `ErrorBody` that nothing referenced, an
    OpenAPI schema that described successes and nothing else, and a client that
    had to guess whether a given failure carried `code`, `message` or `detail`.

    Extras are allowed on purpose, and that is part of the contract rather than a
    loophole. A refusal may attach the facts a caller needs in order to act:
    `workspace_not_empty` carries the goal count, so a confirm dialog can name
    the cascade before the user agrees to it. `code` and `message` are what every
    caller may rely on; the rest is the refusal explaining itself, and a client
    that ignores it loses detail, never correctness.
    """

    model_config = {"extra": "allow"}
    code: str
    message: str


def _role(
    role: AgentRole,
    display_name: str,
    temperature: float,
    max_tokens: int,
    num_ctx: int,
) -> AgentConfig:
    """A seeded role: a local provider and **no model name**.

    Seeding an id here would be a hardcoded model list, which is the exact thing
    this build has none of: a compiled default is wrong within weeks (retired,
    renamed), it cannot know what this machine can actually reach, and shipping
    one meant every role pointed at a provider whose key the user had not added
    yet — so a fresh install failed on the first prompt with nothing to explain
    it.

    The seeded provider is the local one because it is the only provider that
    needs no credential. Which *model* to call is discovered live and chosen in
    Settings → Agent Roles; until then the engine says so plainly instead of
    calling an endpoint with an empty model id.
    """
    return AgentConfig(
        role=role,
        display_name=display_name,
        provider="ollama",
        protocol="ollama",
        model_name="",
        temperature=temperature,
        max_tokens=max_tokens,
        num_ctx=num_ctx,
        updated_at=0,
    )


DEFAULT_AGENTS: list[AgentConfig] = [
    # **Context windows are seeded, not left to the server.** Ollama's own
    # default is 4096 and it *silently truncates* anything longer — the model
    # answers from a partial prompt and returns a degraded result rather than an
    # error, so nothing anywhere points at the cause. A fresh install that never
    # opened Settings therefore ran its whole pipeline truncated, which is the
    # worst possible way for this to be fixed: the field existed, was
    # documented, and was empty.
    #
    # The allocation is by *what the role is handed*, not by how important it
    # is, and that is not the same ranking. Four roles receive payloads built out
    # of the librarian's evidence pack — the librarian quoting what it read, the
    # designer reasoning over it, the planner planning against it, and the fixer
    # receiving it **plus the current contents of every file the step touches**,
    # inlined (`engine/executor.py`, `_write`). Those four get 32768, which is
    # qwen2.5-coder's ceiling, so raising it further would be asking for a
    # window the model cannot use.
    #
    # The two that read *results* rather than material get half: the verifier's
    # test output and the critic's diff are unbounded in principle but are
    # summaries in practice. The gate and the scribe are genuinely small — the
    # gate's state is clipped to 4000 characters by `build_state`, and the
    # scribe answers with a summary and a commit message.
    #
    # Seeding only reaches *new* installs, deliberately. A migration that
    # backfilled existing rows would silently raise memory use under a running
    # user who never chose it, and this value is the user's to set on an install
    # that already exists. An older install keeps Ollama's 4096 until someone
    # opens Settings → Agent Roles and raises it, which is the honest state: the
    # field is there, it says what is in force, and the choice is visible.
    #
    # These are seeds, not policy. Every one of them is a normal stored value the
    # user can change, and clearing the field returns the role to the server's
    # default — which is why `None` still means what it means in the model.
    #
    # Laya is a non-generative System-1 decision engine, not a chat model. When
    # `pip install laya` + local weights are present it runs in-process (~33 ms,
    # no tokens); otherwise this role's LLM answers the same typed contract as a
    # documented fallback. See engine/laya.py.
    _role("laya", "Laya — System-1 Gate", temperature=0.0, max_tokens=512, num_ctx=8192),
    # The seven role slots. Output budgets follow the job: the librarian answers
    # with evidence packs rather than prose but needs room for what it quotes,
    # the fixer emits whole files (largest), and the verifier and scribe answer
    # with a few lines.
    _role("librarian", "Librarian Agent", temperature=0.1, max_tokens=8192, num_ctx=32768),
    # Direction-setting is the most open-ended reasoning job in the pipeline, so
    # it runs warmer than the planner; and it can hand back a whole DESIGN.md
    # body for the fixer to write, which is why its budget is the librarian's
    # rather than the critic's.
    _role("design", "Design Agent", temperature=0.4, max_tokens=8192, num_ctx=32768),
    _role("planner", "Planner Agent", temperature=0.3, max_tokens=4096, num_ctx=32768),
    _role("fixer", "Fixer Agent", temperature=0.1, max_tokens=8192, num_ctx=32768),
    _role("verifier", "Verifier Agent", temperature=0.0, max_tokens=2048, num_ctx=16384),
    _role("critic", "Critic Agent", temperature=0.2, max_tokens=4096, num_ctx=16384),
    _role("scribe", "Scribe Agent", temperature=0.4, max_tokens=1024, num_ctx=8192),
]

# Role ids that earlier builds shipped, and the slot that inherited their job.
# Existing installs keep their configured provider and model (see
# `db.migrate_agent_roles`); without this they would silently lose them and every
# role would look unconfigured after an upgrade.
LEGACY_ROLE_RENAMES: dict[str, str] = {
    "coder": "fixer",
    "tester": "verifier",
    "reviewer": "critic",
    "summarizer": "scribe",
}
