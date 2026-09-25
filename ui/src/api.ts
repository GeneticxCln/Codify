import type {
  AgentCallStat,
  AgentConfig,
  DeletedGoal,
  DeletedWorkspace,
  EngineInfo,
  EngineStatus,
  StatsHistoryDay,
  StatsImportState,
  StatsOverview,
  EngineSettings,
  Event,
  Goal,
  GoalMode,
  GoalStatus,
  LayaStatus,
  ModelCatalog,
  PlanStep,
  ProviderCatalog,
  ProviderKeyStatus,
  RepairReport,
  RecentRunModel,
  RoleInfo,
  Workspace,
} from "./types.ts";

let currentEngine: EngineInfo = {
  port: parseInt(localStorage.getItem("CODIFY_PORT") || "7430", 10),
  token: localStorage.getItem("CODIFY_TOKEN") || "",
};

export function setEngineInfo(info: EngineInfo) {
  currentEngine = info;
  localStorage.setItem("CODIFY_PORT", info.port.toString());
  localStorage.setItem("CODIFY_TOKEN", info.token);
}

export function getEngineInfo(): EngineInfo {
  return currentEngine;
}

/**
 * Re-fetch engine connection info from Tauri IPC and apply it if changed.
 *
 * The Tauri shell parses the boot handshake (`CODIFY_ENGINE token=… port=…`)
 * from the *live* engine process's stdout, so this returns the current boot
 * token even after the engine restarted and invalidated the one we cached.
 * This is what makes the auth-stale state self-healing.
 *
 * Returns the applied EngineInfo, or null when IPC is unavailable (standalone
 * browser fallback) or the info is unchanged from what we already hold.
 */
export async function refreshEngineInfoFromIpc(): Promise<EngineInfo | null> {
  try {
    const info = await tauriInvoke<EngineInfo>("codify_get_engine_info");
    if (!info?.token || !info?.port) return null;
    if (info.token === currentEngine.token && info.port === currentEngine.port) return null;
    setEngineInfo(info);
    return info;
  } catch {
    return null;
  }
}

/**
 * Ask the desktop shell why the engine is not running, or null when there is
 * nothing to report.
 *
 * `codify_get_engine_info` can only say "not ready", which is indistinguishable
 * from "still starting" — so a launch that already failed showed up as a red pill
 * with no reason. The shell is the only party that knows (it chose the working
 * directory and read, or failed to read, the handshake), and this is how its
 * diagnosis reaches the banner.
 *
 * Outside Tauri there is no shell to ask — the standalone browser build talks to
 * an engine it did not spawn — so this returns null and the caller keeps whatever
 * message it already had.
 */
export async function engineFailureReason(): Promise<string | null> {
  if (typeof window === "undefined" || !(window as any).__TAURI_INTERNALS__) return null;
  try {
    const status = await tauriInvoke<EngineStatus>("codify_engine_status");
    return status?.error ?? null;
  } catch {
    return null;
  }
}

export async function tauriInvoke<T>(cmd: string, args?: Record<string, any>): Promise<T> {
  if (typeof window !== "undefined" && (window as any).__TAURI_INTERNALS__) {
    const { invoke } = await import("@tauri-apps/api/core");
    return invoke<T>(cmd, args);
  }
  // Fallback to direct HTTP API if running outside Tauri
  return fallbackHttpInvoke<T>(cmd, args);
}

async function fallbackHttpInvoke<T>(cmd: string, args?: Record<string, any>): Promise<T> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${currentEngine.token}`,
  };

  switch (cmd) {
    case "codify_list_agent_configs": {
      const res = await fetch(`${base}/settings/agents`, { headers });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }
    case "codify_update_agent_config": {
      const { role, patch } = args || {};
      const res = await fetch(`${base}/settings/agents/${role}`, {
        method: "PUT",
        headers,
        body: JSON.stringify(patch),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.message || `HTTP ${res.status}`);
      }
      return res.json();
    }
    case "codify_repair_agent_configs": {
      const res = await fetch(`${base}/settings/agents/repair`, { method: "POST", headers });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.message || `HTTP ${res.status}`);
      }
      return res.json();
    }
    case "codify_test_agent_connection": {
      const { role } = args || {};
      const res = await fetch(`${base}/settings/agents/${role}/test-connection`, {
        method: "POST",
        headers,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        return { ok: false, message: err.message || `HTTP ${res.status}` } as any;
      }
      return res.json();
    }
    case "codify_get_engine_info": {
      return currentEngine as any;
    }
    default:
      throw new Error(`Unknown command: ${cmd}`);
  }
}

/**
 * Parse an engine error body into a useful message. The engine reports
 * failures as `{code, message}` or `{detail}`; fall back to the HTTP status
 * so a failure is never silent or empty.
 */
async function engineError(res: Response, fallback: string): Promise<Error> {
  let code = "";
  let message = "";
  let extra: Record<string, unknown> = {};
  try {
    const body = await res.json();
    code = typeof body?.code === "string" ? body.code : "";
    message =
      typeof body?.message === "string"
        ? body.message
        : typeof body?.detail === "string"
        ? body.detail
        : "";
    // Keep whatever else the engine attached. The delete routes refuse with
    // structured facts (`workspace_not_empty` carries the goal count) that a
    // confirm dialog has to read — a stringified "409: ..." would lose the one
    // number the user needs before agreeing to a cascade.
    if (body && typeof body === "object") {
      const { code: _c, message: _m, detail: _d, ...rest } = body as Record<string, unknown>;
      extra = rest;
    }
  } catch {
    /* non-JSON body — fall through to status text below */
  }
  const detail = message || `HTTP ${res.status}`;
  const err = new Error(code ? `${code}: ${detail}` : `${fallback}: ${detail}`);
  // Still a plain Error for every existing caller; the fields are additive.
  (err as Error & { code?: string; status?: number; extra?: Record<string, unknown> }).code = code;
  (err as Error & { code?: string; status?: number; extra?: Record<string, unknown> }).status = res.status;
  (err as Error & { code?: string; status?: number; extra?: Record<string, unknown> }).extra = extra;
  return err;
}

/**
 * Models that actually answered recently, newest first.
 *
 * Read from the engine's `agent_assigned` events, not from the goals' requested
 * model — a role runs on its own configured model, so "what was asked for" and
 * "what answered" are different facts, and only the second belongs on a badge
 * that says "last run".
 *
 * Throws on HTTP/network failure with the engine's code/message — callers
 * that treat this as an ordering hint catch and fall back to [].
 */
export async function fetchRecentRunModels(limit = 5): Promise<RecentRunModel[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/models/recent?limit=${limit}`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to fetch recent models");
  return res.json();
}

export async function fetchProviders(): Promise<ProviderCatalog> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/settings/providers`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/**
 * Capability report for the Laya System-1 gate. Loading no weights, so this is
 * cheap enough to call when the settings screen opens.
 *
 * Throws with the engine's code/message on failure — callers render the
 * "unavailable" state from the catch, not from a silent null.
 */
export async function getLayaStatus(): Promise<LayaStatus> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/settings/laya`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to fetch Laya status");
  return res.json();
}

/** Engine-wide settings (each value with its clamp bounds). Throws on failure. */
export async function getEngineSettings(): Promise<EngineSettings> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/settings/engine`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to fetch engine settings");
  return res.json();
}

/** Persist engine-wide settings; the response echoes the clamped values. */
export async function saveEngineSettings(
  patch: { parallel_width?: number; stats_retention_days?: number }
): Promise<{ saved: Record<string, number> }> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/settings/engine`, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${currentEngine.token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json();
}

/**
 * Every role's stored config, straight from the engine.
 *
 * Read-only and used by the failure diagnosis panel, so it deliberately does not
 * go through the Tauri IPC command the settings hook uses: diagnosing a failure
 * should work the same way in the desktop shell and a plain browser.
 *
 * Throws with the engine's code/message on failure — callers decide the fallback.
 */
export async function fetchAgentConfigs(): Promise<AgentConfig[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/settings/agents`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to fetch agent configs");
  return res.json();
}

/**
 * The pipeline slots and what each is allowed to do, as the engine defines it.
 *
 * Served rather than hardcoded here: two lists is how a settings screen ends up
 * promising an ability the engine stopped granting.
 *
 * Throws with the engine's code/message on failure.
 */
export async function fetchRoles(): Promise<RoleInfo[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/settings/roles`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to fetch roles");
  return res.json();
}

/** Cross-goal statistics: success rate, spend per role/model, and the daily
 * trend, aggregated by the engine from the goals and events it already stores. */
export async function fetchStatsOverview(windowDays: number): Promise<StatsOverview> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/stats/overview?window=${windowDays}`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

/** One frozen document per past day, oldest first — the trend that survives
 * engine restarts and outlives the log entries it was computed from. A limit of
 * zero asks the engine for every stored day, for the full-history export. */
/**
 * The stats-history document currently imported into the engine, if any.
 *
 * The engine keeps it, so an import survives closing the tab and restarting
 * the engine. `imported: false` is the normal empty state, not an error.
 */
export async function fetchStatsImport(): Promise<StatsImportState | null> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  try {
    const res = await fetch(`${base}/stats/import`, {
      headers: { Authorization: `Bearer ${currentEngine.token}` },
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

/** Validate and persist an exported stats-history document (replaces any previous). */
export async function saveStatsImport(
  doc: unknown,
  source: string,
): Promise<{ imported: true; days: number; source: string }> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/stats/import`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${currentEngine.token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ...(doc as object), source }),
  });
  if (!res.ok) throw await engineError(res, "Failed to save the imported history");
  return res.json();
}

/** Forget the engine-stored import. Idempotent. */
export async function clearStatsImport(): Promise<{ imported: false; cleared: number }> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/stats/import`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to clear the imported history");
  return res.json();
}

export async function fetchStatsHistory(limit: number = 60): Promise<StatsHistoryDay[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/stats/history?limit=${limit}`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  const body = await res.json();
  return body?.days ?? [];
}

/** What actually happened to each role the last time it ran — last call, last
 * error, and how often each occurred. Read from the goal event log; a role
 * that never ran (or a pre-stats engine) simply has nothing to show.
 * Throws with the engine's code/message on failure. */
export async function fetchAgentCallStats(): Promise<AgentCallStat[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/settings/agents/stats`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to fetch agent call stats");
  const body = await res.json();
  return body?.stats ?? [];
}

/**
 * Point every role that cannot run at a discovered model, in one request.
 *
 * The engine decides which roles those are and reports what it did *and* what it
 * left alone — so this stays one action rather than a client-side loop that could
 * disagree with the engine about what "cannot run" means.
 */
export async function repairAgentConfigs(): Promise<RepairReport> {
  return tauriInvoke<RepairReport>("codify_repair_agent_configs");
}

export async function fetchProviderKeys(): Promise<ProviderKeyStatus[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/settings/keys`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to fetch provider keys");
  return res.json();
}

export async function saveProviderKey(provider: string, api_key: string): Promise<void> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/settings/keys`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ provider, api_key }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
}

export async function browseWorkspace(): Promise<Workspace | null> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/workspaces/browse`, {
    method: "POST",
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) return null;
  const data = await res.json();
  if (data.cancelled || !data.workspace) return null;
  return data.workspace;
}

/**
 * Discover the models every configured provider serves.
 *
 * `refresh` bypasses the engine's short cache; the app passes it on open so a
 * model released since the last launch is present without a reinstall, and the
 * "failed providers" list stays accurate rather than cached.
 *
 * Throws with the engine's code/message on failure — callers that can run
 * without a catalog catch and fall back to an empty one.
 */
export async function fetchModelCatalog(refresh = false): Promise<ModelCatalog> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/models${refresh ? "?refresh=true" : ""}`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to fetch model catalog");
  return res.json();
}

export async function listWorkspaces(): Promise<Workspace[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/workspaces`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/**
 * Delete a goal and everything recorded about it (events, steps, dry-run
 * proposals). Refused with `goal_in_progress` while it is PLANNING or RUNNING —
 * cancel it first. Files on disk are never touched.
 */
export async function deleteGoal(goal_id: string): Promise<DeletedGoal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to delete goal");
  return res.json();
}

/**
 * Delete a workspace from Codify's records. The directory is left alone.
 *
 * A workspace that still has goals is refused with `workspace_not_empty`
 * carrying the count, so the UI can show what the cascade will cost before
 * the user agrees to it; pass `deleteGoals` only once they have.
 */
export async function deleteWorkspace(
  workspace_id: string,
  opts: { deleteGoals?: boolean } = {},
): Promise<DeletedWorkspace> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const qs = opts.deleteGoals ? "?delete_goals=true" : "";
  const res = await fetch(`${base}/workspaces/${workspace_id}${qs}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to delete workspace");
  return res.json();
}

export async function createWorkspace(name: string, root_path: string): Promise<Workspace> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/workspaces`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ name, root_path }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

/**
 * Pin the brand contract the design agent must obey, or clear it with `""`.
 *
 * A workspace property, not an agent one: the right `DESIGN.md` is a fact about
 * the repository, so two projects can hold different brand contracts while the
 * design role keeps one config. The engine refuses an escape
 * (`design_contract_escape`) and a path that is not a readable text file
 * (`design_contract_missing` / `design_contract_binary`) while this screen is
 * still open, rather than mid-goal where the setting is nowhere in sight.
 */
export async function setWorkspaceDesignContract(
  workspace_id: string,
  path: string,
): Promise<Workspace> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/workspaces/${workspace_id}/design-contract`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) throw await engineError(res, "Failed to pin the brand contract");
  return res.json();
}

export async function createGoal(
  workspace_id: string,
  title: string,
  description: string,
  dry_run: boolean,
  provider?: string,
  model?: string,
  plan_only: boolean = false,
  parallel: boolean = false,
  /** `design` makes the workspace's own brand contract the deliverable. */
  mode: GoalMode = "normal"
): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const payload: Record<string, any> = { workspace_id, title, description, dry_run, plan_only };
  if (provider) payload.provider = provider;
  if (model) payload.model = model;
  if (parallel) payload.parallel = true;
  // Sent only when it is not the default: an older engine validating the body
  // strictly would reject an unknown key, and "normal" is what it assumes.
  if (mode !== "normal") payload.mode = mode;

  const res = await fetch(`${base}/goals`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function getGoal(goal_id: string): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export interface UsageBucket {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
}

export interface GoalUsage {
  goal_id: string;
  calls: number;
  totals: { input_tokens: number; output_tokens: number; total_tokens: number };
  by_role: Record<string, UsageBucket>;
  by_model: Record<string, UsageBucket>;
  /** Most steps that were in flight at once (1 = fully sequential). */
  parallel_peak: number;
  /** How many times a step started with none already running. */
  parallel_waves: number;
}

export async function getGoalUsage(goal_id: string): Promise<GoalUsage> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/usage`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** The goal's full audit trail — plan edits, fallbacks, failures, outcomes — as one document. */
export async function getGoalAudit(goal_id: string): Promise<Record<string, unknown>> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/audit`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function startGoal(goal_id: string, expected_version: number): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/start`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ expected_version }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function pauseGoal(goal_id: string, expected_version: number): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/pause`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ expected_version }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function cancelGoal(goal_id: string, expected_version: number): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/cancel`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ expected_version }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

export interface HealthStatus {
  ok: boolean;
  authenticated: boolean;
}

/**
 * Liveness probe for the header indicator. Deliberately does NOT throw on 401:
 * an unauthenticated 200-level response still proves the engine process is up,
 * which is exactly what the pill communicates. (The engine returns 401 for
 * missing tokens on /health; any HTTP response means "alive".)
 */
export async function checkEngineHealth(): Promise<HealthStatus> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  try {
    const res = await fetch(`${base}/health`, {
      headers: { Authorization: `Bearer ${currentEngine.token}` },
    });
    if (res.status === 401) return { ok: true, authenticated: false };
    if (!res.ok) return { ok: false, authenticated: false };
    return res.json();
  } catch {
    return { ok: false, authenticated: false };
  }
}

/**
 * Replay a completed dry-run's proposed changes for real. Resolves when the
 * apply run has been dispatched; progress arrives over the goal's event
 * stream (re-subscribe to /ws/goals/{id} after calling this).
 */
export async function applyGoal(
  goal_id: string,
  expected_version: number,
): Promise<{ applied: boolean; goal_id: string }> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/apply`, {
    method: "POST",
    headers: { Authorization: `Bearer ${currentEngine.token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ expected_version }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

/** Lift the plan-only guard so the goal can be started. */
export async function patchStep(
  goal_id: string,
  step_id: string,
  expected_version: number,
  patch: { title?: string; description?: string; suggested_paths?: string[] }
): Promise<PlanStep> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/steps/${step_id}`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ expected_version, ...patch }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function enableExecution(goal_id: string, expected_version: number): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/enable-execution`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ expected_version }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

/** Goal history, newest first (active goals lead). The index the restore path
 * reads: without it, a restart forgets every goal the engine still has on disk. */
export async function listGoals(params: {
  workspace_id?: string;
  status?: GoalStatus;
  limit?: number;
  offset?: number;
} = {}): Promise<Goal[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const qs = new URLSearchParams();
  if (params.workspace_id) qs.set("workspace_id", params.workspace_id);
  if (params.status) qs.set("status", params.status);
  if (params.limit != null) qs.set("limit", String(params.limit));
  if (params.offset != null) qs.set("offset", String(params.offset));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await fetch(`${base}/goals${suffix}`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

/** Every event a goal ever published, oldest first. Feeds the transcript
 * hydration when a past goal is reopened — the same log the live stream appends
 * to, so a restored card and a live card are one format. */
export async function getGoalEvents(goal_id: string): Promise<Event[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/events?after=0`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function retryStep(
  goal_id: string,
  step_id: string,
  expected_version: number
): Promise<PlanStep> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/steps/${step_id}/retry`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ expected_version }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}
