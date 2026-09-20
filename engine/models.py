from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

AgentRole = Literal["planner", "coder", "tester", "reviewer", "summarizer"]
ProviderProtocol = Literal["anthropic", "openai_compat", "ollama", "google"]
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
    "error",
]

SYSTEM_PROMPT_OVERRIDE_MAX = 32768
PROVIDER_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
ROLES: tuple[AgentRole, ...] = (
    "planner",
    "coder",
    "tester",
    "reviewer",
    "summarizer",
)

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
}


class AgentConfig(BaseModel):
    role: AgentRole
    display_name: str = Field(..., min_length=1, max_length=80)
    provider: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    protocol: ProviderProtocol
    model_name: str = Field(..., min_length=1, max_length=128)
    api_key_ref: Optional[str] = None
    base_url: Optional[str] = None
    system_prompt_override: Optional[str] = Field(None, max_length=SYSTEM_PROMPT_OVERRIDE_MAX)
    temperature: float = Field(0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(4096, gt=0, le=200000)
    updated_at: float = 0


class AgentConfigUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    display_name: Optional[str] = Field(None, min_length=1, max_length=80)
    provider: Optional[str] = Field(None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    protocol: Optional[ProviderProtocol] = None
    model_name: Optional[str] = Field(None, min_length=1, max_length=128)
    api_key: Optional[str] = Field(None, min_length=1, max_length=4096)
    base_url: Optional[str] = None
    system_prompt_override: Optional[str] = Field(None, max_length=SYSTEM_PROMPT_OVERRIDE_MAX)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(None, gt=0, le=200000)


class Workspace(BaseModel):
    id: str
    name: str = Field(..., min_length=1, max_length=120)
    root_path: str
    created_at: float


class Goal(BaseModel):
    id: str
    workspace_id: str
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=20000)
    status: GoalStatus
    dry_run: bool = False
    version: int = Field(0, ge=0)
    created_at: float
    updated_at: float


class PlanStep(BaseModel):
    id: str
    goal_id: str
    ordinal: int = Field(..., ge=0)
    title: str
    description: str
    suggested_paths: list[str] = []
    status: StepStatus = "PENDING"
    review_notes: Optional[str] = None
    commit_message: Optional[str] = None
    last_agent_role: Optional[AgentRole] = None


class Event(BaseModel):
    id: str
    goal_id: str
    step_id: Optional[str] = None
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
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=20000)
    dry_run: bool = False


class VersionedAction(BaseModel):
    model_config = {"extra": "forbid"}
    expected_version: int = Field(..., ge=0)


class GoalDetail(Goal):
    steps: list[PlanStep] = []


class ErrorBody(BaseModel):
    code: str
    message: str


DEFAULT_AGENTS: list[AgentConfig] = [
    AgentConfig(
        role="planner",
        display_name="Planner Agent",
        provider="anthropic",
        protocol="anthropic",
        model_name="claude-opus-5",
        temperature=0.3,
        max_tokens=4096,
        updated_at=0,
    ),
    AgentConfig(
        role="coder",
        display_name="Coder Agent",
        provider="anthropic",
        protocol="anthropic",
        model_name="claude-sonnet-4-6",
        temperature=0.1,
        max_tokens=8192,
        updated_at=0,
    ),
    AgentConfig(
        role="tester",
        display_name="Tester Agent",
        provider="openai",
        protocol="openai_compat",
        model_name="gpt-4.1-mini",
        temperature=0.0,
        max_tokens=2048,
        updated_at=0,
    ),
    AgentConfig(
        role="reviewer",
        display_name="Reviewer Agent",
        provider="anthropic",
        protocol="anthropic",
        model_name="claude-sonnet-4-6",
        temperature=0.2,
        max_tokens=4096,
        updated_at=0,
    ),
    AgentConfig(
        role="summarizer",
        display_name="Summarizer Agent",
        provider="deepseek",
        protocol="openai_compat",
        model_name="deepseek-chat",
        temperature=0.4,
        max_tokens=1024,
        updated_at=0,
    ),
]
