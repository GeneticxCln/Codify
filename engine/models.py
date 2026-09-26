from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

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
GoalMode = Literal["normal", "design"]

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
    # An empty slug is how a client removes a fallback. The desktop shell sends
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


class Goal(BaseModel):
    id: str
    workspace_id: str
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
    # critic reviews it before anyone pins it. See docs/04 §4.0a.2.
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


class GoalCreate(BaseModel):
    model_config = {"extra": "forbid"}
    workspace_id: str
    # Chat UIs send the whole prompt as `title`; accept up to the description
    # cap and let GoalService.split_title_description normalize it down to a
    # 200-char title. The Goal model (storage/response) still enforces 200.
    title: str = Field(..., min_length=1, max_length=20000)
    description: str = Field("", max_length=20000)
    dry_run: bool = False
    plan_only: bool = False
    parallel: bool = False
    # Which pipeline this goal runs. Only "normal" and "design" exist; a client
    # sending anything else gets a 422 from the model itself, not a goal that
    # quietly runs the default.
    mode: GoalMode = "normal"
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


class VersionedAction(BaseModel):
    model_config = {"extra": "forbid"}
    expected_version: int = Field(..., ge=0)


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
    code: str
    message: str


def _role(
    role: AgentRole,
    display_name: str,
    temperature: float,
    max_tokens: int,
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
        updated_at=0,
    )


DEFAULT_AGENTS: list[AgentConfig] = [
    # Laya is a non-generative System-1 decision engine, not a chat model. When
    # `pip install laya` + local weights are present it runs in-process (~33 ms,
    # no tokens); otherwise this role's LLM answers the same typed contract as a
    # documented fallback. See engine/laya.py.
    _role("laya", "Laya — System-1 Gate", temperature=0.0, max_tokens=512),
    # The seven pipeline roles, in execution order. Token budgets follow the job:
    # the librarian answers with evidence packs rather than prose but needs room
    # for what it quotes, the fixer emits whole files (largest), and the verifier
    # and scribe answer with a few lines.
    _role("librarian", "Librarian Agent", temperature=0.1, max_tokens=8192),
    # Direction-setting is the most open-ended reasoning job in the pipeline, so
    # it runs warmer than the planner; and it can hand back a whole DESIGN.md
    # body for the fixer to write, which is why its budget is the librarian's
    # rather than the critic's.
    _role("design", "Design Agent", temperature=0.4, max_tokens=8192),
    _role("planner", "Planner Agent", temperature=0.3, max_tokens=4096),
    _role("fixer", "Fixer Agent", temperature=0.1, max_tokens=8192),
    _role("verifier", "Verifier Agent", temperature=0.0, max_tokens=2048),
    _role("critic", "Critic Agent", temperature=0.2, max_tokens=4096),
    _role("scribe", "Scribe Agent", temperature=0.4, max_tokens=1024),
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
