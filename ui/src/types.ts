/**
 * The pipeline slots. Each is a different ability, not a different persona:
 * librarian reads but never writes, design locks a direction and writes nothing,
 * fixer is the only writer, verifier is the only role that runs a command, critic
 * is the only one that can stop a step. `laya` is the pre-flight gate, not a
 * pipeline stage.
 */
export type AgentRole =
  | "laya"
  | "librarian"
  | "design"
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

/** What a goal is for. `design` inverts the design agent's usual relationship
 * to the brand file: instead of deriving a direction from an existing
 * contract, it authors DESIGN.md — a step writes it, the critic reviews it
 * before it is pinned, and the pin stays a user action. */
/**
 * Which pipeline a goal runs. "design" and "knowledge" are the two
 * deliverable shapes: the design agent authors a file, a step writes it
 * verbatim, the critic reviews it before anyone relies on it. They differ only
 * in which file — DESIGN.md is the contract every later goal obeys, CODIFY.md is
 * the prior every later run's librarian reads.
 */
export type GoalMode = "normal" | "design" | "knowledge" | "chat";

/** True for a goal that is a turn rather than a run: it was typed into a thread
 * (`conversation_id`), and it is the route the composer sends to.
 *
 * It says where the goal came from, not what it became. A turn that asked for a
 * change delegates to `run_planning` on the same row, so it comes back with
 * steps and a PENDING status like any run. A caller that wants "this was
 * answered as a conversation" wants `turnTranscript.isConversationalTurn` —
 * this mode *and* no steps — rather than this alone. */
export function isChatMode(mode: GoalMode | undefined): boolean {
  return mode === "chat";
}

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
  | "design_contract"
  | "fix_retry"
  | "agent_call_failed"
  | "fixer_pass"
  | "plan_consult"
  | "usage"
  | "model_delta"
  | "error"
  | "todo_updated"
  /**
   * The engine's own per-stage measurement (docs/04 §4.7). Listed here because
   * the engine publishes it into this goal's log, so `GET /goals/{id}/events`
   * can return one and the `Event` type has to be able to say so.
   *
   * It is not read from an event on purpose: the stage table and the per-role
   * rates come from `StatsOverview.by_stage`, which the engine derives from these
   * events. A per-goal stage card would want this one; nothing currently does.
   */
  | "stage_result";

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
   * Ollama's context window for this role, in tokens; null/absent = the
   * server's default (4096, silently truncating longer prompts). Only the
   * Ollama provider reads it — OpenAI-style APIs size the window server-side.
   */
  num_ctx?: number | null;
  /**
   * How long Ollama holds the model loaded after a request finishes — a duration
   * ("30m"), bare seconds, or "-1" for until the server stops. Undefined/null
   * leaves the server's own five-minute window. It is not a speed setting for a
   * single call: it covers the gap *between* calls. Only Ollama reads it.
   */
  keep_alive?: string | null;
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
  /** null clears an explicit window and returns the role to Ollama's default. */
  num_ctx?: number | null;
  /** null returns the role to Ollama's own residency window. */
  keep_alive?: string | null;
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
  /**
   * Workspace-relative path of the pinned brand contract, `""` when unpinned.
   * Unpinned is not "no brand": a `DESIGN.md` at the root is discovered by
   * convention, and the transcript says which of the two happened.
   */
  design_contract_path: string;
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

/**
 * A thread of turns in a workspace — what a chat tab points at.
 *
 * The engine owns it, so it survives the window that opened it. Deliberately not
 * a goal: a goal is one run with a plan and a verifier, a conversation is the
 * question several runs answer.
 */
export interface Conversation {
  id: string;
  workspace_id: string;
  /** Empty until a turn names it; the UI shows "New chat" rather than inventing. */
  title: string;
  /** Hidden from the panel but still fetchable — archived, never deleted. */
  archived: boolean;
  /**
   * The thread this one was started on, or `null` for a top-level thread.
   *
   * The field that makes "a new thread" and "a new chat" different objects: a
   * thread with a parent is a continuation of that conversation, and the panel
   * says so. `null` is the ordinary case, not a missing one.
   */
  parent_id: string | null;
  /**
   * The parent's name, joined in by the engine.
   *
   * Sent rather than resolved by the panel because the panel *cannot* resolve
   * it: it lists one workspace's live threads and hides archived ones, so
   * archiving a parent left every child unable to find the name and degrading
   * its label to a generic word permanently. `null` now means the parent row is
   * genuinely gone, which is the only case where the name is really unknown.
   */
  parent_title: string | null;
  created_at: number;
  updated_at: number;
}

/**
 * One turn: the user's question and the goal that answered it.
 *
 * Derived by the engine from the goal rather than stored separately, so there is
 * no second record to get out of step with the first.
 */
export interface ConversationTurn {
  goal_id: string;
  conversation_id: string | null;
  prompt: string;
  status: GoalStatus;
  created_at: number;
}

/**
 * One open tab, as the engine holds it (docs/09 §2.1).
 *
 * `payload` is a JSON string on purpose: it is the tab's own facts — a thread,
 * an address, a back/forward stack — and the engine bounds and parses it without
 * understanding it. Typing its innards here would be a second, drifting
 * definition of a tab's shape in a file the engine does not read.
 */
export interface ShellTabRow {
  key: string;
  position: number;
  kind: "chat" | "browser";
  payload: string;
  updated_at: number;
}

export interface Goal {
  id: string;
  workspace_id: string;
  /**
   * The thread this run belongs to. Absent for a goal that predates
   * conversations, which reads as its own single-turn thread.
   */
  conversation_id?: string | null;
  title: string;
  description: string;
  status: GoalStatus;
  dry_run: boolean;
  /** plan_only goals block /start until execution is explicitly enabled. */
  plan_only: boolean;
  /** Independent (path-disjoint) steps may run concurrently. */
  parallel: boolean;
  /** What the goal is for. Optional: an engine predating the mode omits it,
   * and the UI reads an absent value as the normal pipeline. */
  mode?: GoalMode;
  /** Is this run's model calls being recorded? Optional for the same reason. */
  trace?: boolean;
  version: number;
  /** Optional: older engines omit this; the UI derives startability from status. */
  canStart?: boolean;
  created_at: number;
  updated_at: number;
  steps?: PlanStep[];
}

/** One recorded model call, as the UI shows it. The prompt is a digest, never
 * text: the UI can prove a replay will match, but it cannot read the prompt
 * back out of the recording, which is the point (docs/04 §8). */
export interface TraceCall {
  seq: number;
  role: string;
  model: string;
  prompt_hash: string;
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  at: number;
}

/** What a goal recorded — the engine's GET /goals/{id}/trace response. */
export interface TraceSummary {
  goal_id: string;
  calls: number;
  by_role: Record<string, number>;
  /** True only when the prompt *text* was kept, which needs
   * CODIFY_TRACE_PROMPTS=1. The UI says so rather than implying a transcript. */
  prompts_kept: boolean;
  /** Why a recording that should have calls has none — a write that failed.
   * Null is the normal case; an engine that predates the field omits it, so
   * the UI reads absence as "no known problem". */
  recording_error?: string | null;
  recorded: TraceCall[];
}

/** What actually happened to one role the last time it was called, read from
 * the goal event log — the engine's GET /settings/agents/stats response. */
export interface AgentCallStat {
  role: AgentRole;
  /** The newest completed call. duration_ms is null on events written before
   * the field existed — unknown, never "instant". */
  last_call: {
    duration_ms: number | null;
    provider: string | null;
    model: string | null;
    at: number;
  } | null;
  calls_seen: number;
  failures_seen: number;
  last_error: {
    code: string | null;
    message: string | null;
    provider: string | null;
    model: string | null;
    at: number;
  } | null;
  /** Measured stage runs for this role (docs/04 §4.7), over every run the log
   * still holds. Absent on an engine too old to measure stages, which is why
   * every read of it is optional. */
  runs?: number;
  /** Did the role do its job, percent of finished runs — not did the goal
   * succeed. Null when nothing has finished: 0% would be a claim about a role
   * that has never been asked. */
  success_rate?: number | null;
  /** The evidence for the rate: outcome → count, always shipped with it. */
  outcomes?: Record<string, number>;
  tokens?: number;
}

/** One per-role (or per-model) lane of the cross-goal usage rollup. */
export interface UsageLane {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
  /** Mean call duration in ms over the calls that measured one; null when
   * none did — unknown, never "instant". */
  avg_duration_ms: number | null;
}

/** One stage of a goal's pipeline, as measured by the engine's
 * `stage_result` events (docs/04 §4.7): what it achieved, what it spent, and
 * how long it took. `avg`/`p95` are over the *stage's* wall clock — the model
 * call plus the engine work around it — so they are not the mean of the
 * `usage` events' durations. */
export interface StageCost {
  stage: string;
  role: string;
  runs: number;
  tokens: number;
  calls: number;
  /** Mean and 95th-percentile stage wall clock, ms. Null when no run
   * measured one: unknown, never "instant". */
  avg_duration_ms: number | null;
  p95_duration_ms: number | null;
  /** Whole percent of the window's stage spend. The rows sum to 100. */
  token_share: number;
  /** Outcome → count, always shipped beside the rate so a percentage can be
   * checked against what produced it. */
  outcomes: Record<string, number>;
}

/** How often one role did its job, and what that cost. The rate is about the
 * ROLE (a verifier that reported `fail` did its job), not about the goal —
 * the goal-level rate is `StatsGoals.success_rate`. `outcomes` is the
 * evidence for the rate, and `cancelled` is deliberately outside the
 * denominator: an unfinished run has not happened yet. */
export interface RoleOutcome {
  role: string;
  runs: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  /** Percent of finished runs. Null when the role has never finished one. */
  success_rate: number | null;
  outcomes: Record<string, number>;
  avg_duration_ms: number | null;
  p95_duration_ms: number | null;
  tokens: number;
}

/** One cause of failure, most recent message and timestamp, ranked by count. */
export interface FailureCause {
  code: string;
  count: number;
  message: string;
  last_seen: number | null;
}

/** The engine's GET /stats/failures response: what went wrong, and how much
 * of it the retry loops got back. An install that has never failed has
 * `total: 0` and no rows — the empty state is stated, not rendered as a table
 * of zeros that reads as "nothing is wrong". */
export interface FailureBreakdown {
  window_days: number;
  generated_at: number;
  total: number;
  by_code: Record<string, number>;
  by_role: Record<string, number>;
  by_stage: Record<string, { stage: string; failed: number; outcomes: Record<string, number> }>;
  causes: FailureCause[];
  /** Steps the fix→verify loop retried, and how many it then got past. */
  retries: number;
  recovered: number;
  /** Percent of retries recovered. Null when nothing was retried: no retries
   * is not a perfect record, it is no evidence. */
  recovery_rate: number | null;
}

/** The engine's GET /stats/overview response: cross-goal outcomes, spend, and
 * a sparse daily trend, all read from the persisted goal and event stores. */
export interface StatsOverview {
  window_days: number;
  generated_at: number;
  goals: StatsGoals;
  usage: UsageLane & { failures: number; by_role: Record<string, UsageLane>; by_model: Record<string, UsageLane> };
  /** Per-stage cost and latency. Absent on an engine too old to measure
   * stages, which is why every read of it is optional. */
  by_stage?: StageCost[];
  /** Per-role success and cost, keyed by role. */
  by_role_outcome?: Record<string, RoleOutcome>;
  /** Sparse UTC days (only days with activity), oldest first. */
  daily: {
    date: string;
    created: number;
    succeeded: number;
    failed: number;
    cancelled: number;
    total_tokens: number;
    calls: number;
  }[];
}

/** The outcome lane of any stats document (live overview or frozen day). */
export interface StatsGoals {
  goals: number;
  active: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  /** COMPLETED / terminal, percent. Null when nothing terminal yet. */
  success_rate: number | null;
}

/** One frozen day from GET /stats/history: the complete overview document
 * that day ended with, keyed by its UTC date. `day_stats` is that day's own
 * outcomes and spend (from the document's daily rows); the top-level `goals`
 * and `usage` blocks are cumulative-to-that-day, NOT the day alone. */
export interface StatsHistoryDay {
  day: string;
  /** The frozen calendar day's own row — what a per-day chart must read. */
  day_stats: {
    date: string;
    created: number;
    succeeded: number;
    failed: number;
    cancelled: number;
    total_tokens: number;
    calls: number;
  };
  /** Cumulative through the end of this day (the whole frozen document). */
  goals: StatsGoals;
  usage: UsageLane;
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

/**
 * The engine's self-check: what process this is, and what that interpreter can
 * import. Separate from `LayaStatus` because the two answer different questions —
 * that one is "which engine gates goals", this one is "which interpreter is this,
 * and could it use the SDK at all". A gate can be off because it was told to be
 * (`disabled_by_env`) or because the package is not there (`importable`), and
 * those need different fixes.
 */
export interface EngineRuntime {
  interpreter: {
    /** The interpreter actually running, which is what an install has to match. */
    executable: string;
    version: string;
    version_info: [number, number];
    implementation: string;
    in_virtualenv: boolean;
    prefix: string;
    base_prefix: string;
  };
  project_root: string;
  /** Where the checkout's own interpreter would be, whether or not it exists. */
  checkout_interpreter: string;
  laya_sdk: {
    importable: boolean;
    import_error?: string | null;
    version?: string | null;
    disabled_by_env: boolean;
  };
  /** Empty on a healthy install. A warning that is always present is not read. */
  warnings: string[];
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

/** One engine-wide setting: the live value plus the band the engine clamps to. */
export interface EngineSettingValue {
  value: number;
  min: number;
  max: number;
}

/** A string engine setting: no band to clamp into, so the length it accepts. */
export interface EngineStringSettingValue {
  value: string;
  max: number;
}

export interface EngineSettings {
  parallel_width: EngineSettingValue;
  /** How many daily stats snapshots to keep; 0 = keep everything. */
  stats_retention_days: EngineSettingValue;
  /** How long a goal's recording is kept (0 = forever). Optional: an engine
   * that predates the setting omits it, and the panel hides the field. */
  trace_retention_days?: EngineSettingValue;
  /** The conductor's own provider and model. Optional for the same reason: an
   * engine that predates the setting answers without them, and a hidden card
   * beats a broken one. The conductor is a loop, not a ninth role, so these are
   * engine settings rather than a ninth `AgentConfig` row (docs/01 §5). */
  conductor_provider?: EngineStringSettingValue;
  conductor_model?: EngineStringSettingValue;
  /** The conductor's second target, used only when the first cannot serve a
   * call. Two keys rather than one because a fallback provider with no model is
   * the same inert half-pair the primary would be. */
  conductor_fallback_provider?: EngineStringSettingValue;
  conductor_fallback_model?: EngineStringSettingValue;
  /** How many tool-calling turns one conductor run may take. */
  conductor_max_turns?: EngineSettingValue;
  /** How many stage moves one conductor run may make. */
  conductor_max_moves?: EngineSettingValue;
  /** 1 = the conductor drives an approved plan, 0 = the engine's own sequence. */
  conductor_drives_execution?: EngineSettingValue;
  /** Voice (Settings → Audio, docs/04 §3.0.2): who turns speech into text, and
   * text into speech. Optional like the others: an engine that predates voice
   * answers without them and the Audio tab says so instead of saving blind. */
  stt_provider?: EngineStringSettingValue;
  stt_model?: EngineStringSettingValue;
  stt_language?: EngineStringSettingValue;
  tts_provider?: EngineStringSettingValue;
  tts_model?: EngineStringSettingValue;
  tts_voice?: EngineStringSettingValue;
  /** A PipeWire source's node name; empty is the session's default microphone. */
  audio_input?: EngineStringSettingValue;
  /** A custom speech provider's own address; built-in providers ignore it. */
  stt_base_url?: EngineStringSettingValue;
  tts_base_url?: EngineStringSettingValue;
  /** 1 = read each answer aloud as it arrives. Off by default. */
  tts_auto_read?: EngineSettingValue;
  /** Whether the conductor's `fetch_page` may read the web: 0 never, 1 only the sites in `web_fetch_hosts`, 2 any
   * public site (docs/12). Off by default, and optional like the others: an engine that predates it answers
   * without it and the card is simply absent. */
  web_fetch?: EngineSettingValue;
  /** The sites `fetch_page` may read when `web_fetch` is 1: site names, each covering its subdomains. */
  web_fetch_hosts?: EngineStringSettingValue;
}

/** The Settings screen's tabs, in the order they are shown. One definition, so a new tab is one edit. */
export type SettingsTab = "keys" | "agents" | "audio" | "appearance" | "about";

/** Whether one voice feature can run now, and if not, the engine's reason (`GET /audio/status`). */
export interface SpeechReadiness {
  provider: string;
  model: string;
  configured: boolean;
  reason: string | null;
}

/** `GET /audio/status`. */
export interface AudioStatus {
  dictation: SpeechReadiness;
  read_aloud: SpeechReadiness;
  recorder: {
    available: boolean;
    reason: string | null;
    recording: boolean;
    max_seconds: number;
  };
  auto_read: boolean;
}

/** One microphone PipeWire knows. */
export interface AudioInput {
  name: string;
  description: string;
  default: boolean;
}

/** `GET /audio/inputs`. */
export interface AudioInputs {
  available: boolean;
  reason: string | null;
  inputs: AudioInput[];
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

/** `GET /workspaces/{id}/files`: every file path under the root, for the editor's quick-open. */
export interface WorkspaceFileList {
  files: string[];
  /** The tree was bigger than `limit` and was cut. */
  truncated: boolean;
  limit: number;
}

/** `GET /workspaces/{id}/file`: one file as the editor holds it. `version` is what a save must name. */
export interface WorkspaceFile {
  path: string;
  content: string;
  version: string;
  size: number;
}

/** `PUT /workspaces/{id}/file`, a person's Save. */
export interface WorkspaceFileSaved {
  path: string;
  version: string;
  size: number;
}

/**
 * One question the engine puts to the app window (`GET /surfaces/next`): which surface, which of its fixed operations,
 * which workspace it is about, and the arguments the engine has already checked against that operation's shape.
 */
export interface SurfaceRequest {
  id: string;
  surface: string;
  op: string;
  workspace_id: string;
  args: Record<string, unknown>;
}

export interface EngineInfo {
  port: number;
  token: string;
}

/**
 * The desktop shell's view of the engine it spawned.
 *
 * `error` is why it is not running, in the shell's own words — the only place
 * that reason exists, since it is the shell that guessed the working directory
 * and read (or failed to read) the handshake.
 */
export interface EngineStatus {
  running: boolean;
  port: number | null;
  error: string | null;
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
 * The engine-level channel's one frame: a provider's model list moved.
 *
 * A diff, never the catalogue. Eight providers at five hundred models each is a
 * payload no screen asked for, sent on every change — so the engine names what
 * gained and lost, and the reader re-reads `GET /models`, which is a cache hit
 * because the engine's watcher is what warmed it.
 *
 * `removed` matters as much as `added`: a provider that stopped answering is
 * reported as everything it had, removed, which is a fact a reader of a model
 * list needs. A provider that has gone quiet and a provider with nothing new
 * look identical otherwise.
 */
export interface ModelCatalogChanged {
  added: Record<string, string[]>;
  removed: Record<string, string[]>;
  /** Epoch seconds: when the providers were actually asked. */
  fetched_at: number;
}

/**
 * Engine-level events (`/ws/engine`), kept apart from the goal `Event` union.
 *
 * A catalogue belongs to no goal, and the goal stream is a durable sequenced log
 * that replays from 0 — a frame with no `sequence` in it is a frame a deduping
 * client drops. So this is its own closed set, and a new member is a decision in
 * two files rather than a typo in one.
 *
 * `model_catalog_checked` is not a change and says so: it carries only the time
 * the providers were asked. It exists so a screen that no longer polls can still
 * report the age of its own list truthfully — otherwise "checked 40s ago" is a
 * number nobody is keeping true.
 */
export type EngineEvent =
  | { type: "model_catalog_changed"; payload: ModelCatalogChanged }
  | { type: "model_catalog_checked"; payload: { fetched_at: number } };

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

/** The stats-history document the engine currently holds (GET /stats/import).
 * `imported: false` is the ordinary empty state, not a missing resource. */
export interface StatsImportState {
  imported: boolean;
  days: StatsHistoryDay[];
  source: string | null;
  imported_at?: number | null;
  exported_at?: string;
}

/** What a goal deletion actually removed, counted by the engine. */
export interface DeletedGoal {
  deleted: true;
  goal_id: string;
  title: string;
  workspace_id: string;
  steps: number;
  events: number;
  proposed_files: number;
  /** Always false: deleting a goal's record never touches files on disk. */
  files_touched: false;
}

/** What a workspace deletion removed, including the goal history it took. */
export interface DeletedWorkspace {
  deleted: true;
  workspace_id: string;
  name: string;
  goals: number;
  events: number;
  /** Always false: the directory at root_path is never removed. */
  files_touched: false;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: number;
  /**
   * The thread this message belongs to.
   *
   * Held on the message rather than by the store holding the messages, so the
   * live-run path stays a flat map by message id. A goal streams into the thread
   * it was asked in even while the user is looking at another tab — which is the
   * one thing a store keyed by "the conversation currently on screen" would get
   * wrong.
   */
  conversationId?: string | null;
  goal?: Goal;
  events?: Event[];
  isStreaming?: boolean;
  /** A downloaded audit document imported into the transcript for review. */
  auditDoc?: object;
}

