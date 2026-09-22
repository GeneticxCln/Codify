/**
 * The pipeline slots. Each is a different ability, not a different persona:
 * librarian reads but never writes, fixer is the only writer, verifier is the only
 * role that runs a command, critic is the only one that can stop a step. `laya` is
 * the pre-flight gate, not a pipeline stage.
 */
export type AgentRole =
  | "laya"
  | "librarian"
  | "planner"
  | "fixer"
  | "verifier"
  | "critic"
  | "scribe";

/**
 * What the engine did when asked to fix the roles that cannot run, and why.
 *
 * `left_alone` is not decoration: an action that reports only its changes leaves you
 * unable to tell "nothing needed fixing" from "it skipped something".
 */
export interface RepairReport {
  changed: boolean;
  target: { provider: string; model: string } | null;
  target_reason: string;
  repaired: Array<{ role: AgentRole; reason: string; provider: string; model: string }>;
  /** Roles that need fixing but that no discovered model could be pointed at. */
  unfixable: Array<{ role: AgentRole; reason: string }>;
  left_alone: Array<{ role: AgentRole; reason: string }>;
  notes: string[];
}

/** What the engine says each slot is for, and when it runs. */
export interface RoleInfo {
  role: AgentRole;
  display_name: string;
  job: string;
  timing: string;
  order: number;
}
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
  | "provider_fallback"
  | "plan_updated"
  | "laya_decision"
  | "library_evidence"
  | "fix_retry"
  | "fixer_pass"
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
  /**
   * The second target this role may be called on when the primary cannot be used
   * — no credential, endpoint down, model retired, or a reply the contract cannot
   * parse. A fallback needs both a provider and a model to count as one; the
   * engine decides that, so the UI must not offer a half-configured one.
   */
  fallback_provider?: string | null;
  fallback_model_name?: string;
  fallback_protocol?: ProviderProtocol | null;
  fallback_base_url?: string | null;
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
  /** null clears the fallback, which also clears its protocol and endpoint. */
  fallback_provider?: string | null;
  fallback_model_name?: string;
  fallback_protocol?: ProviderProtocol | null;
  fallback_base_url?: string | null;
}

/** What the engine reports when a role could not use its primary target. */
export interface ProviderFallbackPayload {
  role: AgentRole;
  from: { provider: string; model: string };
  to: { provider: string; model: string };
  code: string;
  detail: string;
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
  /** plan_only goals block /start until execution is explicitly enabled. */
  plan_only: boolean;
  version: number;
  canStart: boolean;
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

export interface LayaStatus {
  sdk_installed: boolean;
  sdk_disabled: boolean;
  sdk_error?: string | null;
  questions: Record<string, any>;
  policy: {
    injection_block_threshold: number;
    risk_warn_level: number;
    clarify_warn_threshold: number;
  };
}

/** Payload of a `laya_decision` event (one pre-flight gate verdict). */
export interface LayaDecision {
  engine: "sdk" | "llm-fallback" | "skipped";
  answers: Record<string, any>;
  routing?: Record<string, any>;
  blocked: boolean;
  block_reason?: string | null;
  warnings: string[];
  skipped_reason?: string | null;
  provider?: string | null;
  model?: string | null;
  policy?: Record<string, number>;
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

export interface ProviderKeyStatus {
  provider: string;
  has_key: boolean;
  protocol: string;
  base_url: string;
  needs_key: boolean;
  /** Where a saved key actually lands: the OS keychain, or a 0600 local file. */
  storage: "keyring" | "file";
  /** Human-readable description of that destination, for the settings screen. */
  storage_detail: string;
  /**
   * *Why* it lands there. `file` has three causes and only one is "no keyring" —
   * a redirected run (CODIFY_HOME) deliberately skips the keychain, so telling its
   * user to install `keyring` would be a wrong fix for a deliberate setting.
   */
  storage_reason?: "keyring" | "no_keyring" | "isolated_run";
}

/**
 * A model that a provider actually reported. There is no built-in catalog: the
 * engine discovers these from each provider's own API on demand, so `provider`
 * and `protocol` tell you who serves it and how it will be called.
 */
export interface ModelOption {
  id: string;
  name: string;
  provider: string;
  protocol?: ProviderProtocol;
  description: string;
  /** Epoch seconds when the provider dates its models; drives newest-first order. */
  created?: number | null;
  /** false when the provider marks it as non-chat (e.g. an embeddings model). */
  supports_chat?: boolean | null;
}

/**
 * A model that recently answered, read from the engine's `agent_assigned` events.
 *
 * Deliberately not the goal's requested model: roles run on their own configured
 * models, so "what the command bar asked for" and "what actually answered" are
 * different facts.
 */
export interface RecentRunModel {
  provider: string;
  model: string;
  role?: AgentRole | null;
  ran_at: number;
}

/** Per-provider discovery outcome — one provider failing never empties the list. */
export interface ProviderModelStatus {
  provider: string;
  protocol: string;
  ok: boolean;
  count: number;
  error?: string | null;
}

export interface ModelCatalog {
  models: ModelOption[];
  providers: ProviderModelStatus[];
  /** Epoch seconds of the discovery run that produced this. */
  fetched_at: number;
  /** True when served from the engine's short-lived cache. */
  cached: boolean;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: number;
  goal?: Goal;
  events?: Event[];
  isStreaming?: boolean;
}

