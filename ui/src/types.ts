export type AgentRole = "planner" | "coder" | "tester" | "reviewer" | "summarizer";
export type ProviderProtocol = "anthropic" | "openai_compat" | "ollama" | "google";
export type GoalStatus =
  | "PLANNING"
  | "PENDING"
  | "RUNNING"
  | "PAUSED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export type StepStatus = "PENDING" | "IN_PROGRESS" | "COMPLETED" | "FAILED";

export type EventType =
  | "goal_status"
  | "step_status"
  | "log"
  | "diff"
  | "test_result"
  | "file_change_summary"
  | "agent_assigned"
  | "error";

export interface AgentConfig {
  role: AgentRole;
  display_name: string;
  provider: string;
  protocol: ProviderProtocol;
  model_name: string;
  api_key_ref?: string | null;
  base_url?: string | null;
  system_prompt_override?: string | null;
  temperature: number;
  max_tokens: number;
  updated_at: number;
}

export interface AgentConfigPatch {
  display_name?: string;
  provider?: string;
  protocol?: ProviderProtocol;
  model_name?: string;
  api_key?: string;
  base_url?: string;
  system_prompt_override?: string | null;
  temperature?: number;
  max_tokens?: number;
}

export interface Workspace {
  id: string;
  name: string;
  root_path: string;
  created_at: number;
}

export interface PlanStep {
  id: string;
  goal_id: string;
  ordinal: number;
  title: string;
  description: string;
  suggested_paths: string[];
  status: StepStatus;
  review_notes?: string | null;
  commit_message?: string | null;
  last_agent_role?: AgentRole | null;
}

export interface Goal {
  id: string;
  workspace_id: string;
  title: string;
  description: string;
  status: GoalStatus;
  dry_run: boolean;
  version: number;
  created_at: number;
  updated_at: number;
  steps?: PlanStep[];
}

export interface Event {
  id: string;
  goal_id: string;
  step_id?: string | null;
  type: EventType;
  payload: Record<string, any>;
  timestamp: number;
  sequence: number;
}

export interface ProviderMeta {
  slug: string;
  protocol: ProviderProtocol;
  base_url: string;
  needs_key: boolean;
  local_only: boolean;
}

export interface ProviderCatalog {
  builtins: ProviderMeta[];
  custom: string[];
}

export interface EngineInfo {
  port: number;
  token: string;
}
