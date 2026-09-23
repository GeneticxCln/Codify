import {
  AgentConfig,
  EngineInfo,
  Goal,
  LayaStatus,
  ModelCatalog,
  PlanStep,
  ProviderCatalog,
  ProviderKeyStatus,
  RepairReport,
  RecentRunModel,
  RoleInfo,
  Workspace,
} from "./types";

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
 * Models that actually answered recently, newest first.
 *
 * Read from the engine's `agent_assigned` events, not from the goals' requested
 * model — a role runs on its own configured model, so "what was asked for" and
 * "what answered" are different facts, and only the second belongs on a badge
 * that says "last run".
 */
export async function fetchRecentRunModels(limit = 5): Promise<RecentRunModel[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  try {
    const res = await fetch(`${base}/models/recent?limit=${limit}`, {
      headers: { Authorization: `Bearer ${currentEngine.token}` },
    });
    if (!res.ok) return [];
    return res.json();
  } catch {
    return [];
  }
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
 */
export async function getLayaStatus(): Promise<LayaStatus | null> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  try {
    const res = await fetch(`${base}/settings/laya`, {
      headers: { Authorization: `Bearer ${currentEngine.token}` },
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

/**
 * Every role's stored config, straight from the engine.
 *
 * Read-only and used by the failure diagnosis panel, so it deliberately does not
 * go through the Tauri IPC command the settings hook uses: diagnosing a failure
 * should work the same way in the desktop shell and a plain browser.
 */
export async function fetchAgentConfigs(): Promise<AgentConfig[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  try {
    const res = await fetch(`${base}/settings/agents`, {
      headers: { Authorization: `Bearer ${currentEngine.token}` },
    });
    if (!res.ok) return [];
    return res.json();
  } catch {
    return [];
  }
}

/**
 * The pipeline slots and what each is allowed to do, as the engine defines it.
 *
 * Served rather than hardcoded here: two lists is how a settings screen ends up
 * promising an ability the engine stopped granting.
 */
export async function fetchRoles(): Promise<RoleInfo[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  try {
    const res = await fetch(`${base}/settings/roles`, {
      headers: { Authorization: `Bearer ${currentEngine.token}` },
    });
    if (!res.ok) return [];
    return res.json();
  } catch {
    return [];
  }
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
  if (!res.ok) return [];
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
 */
export async function fetchModelCatalog(refresh = false): Promise<ModelCatalog> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const empty: ModelCatalog = { models: [], providers: [], fetched_at: 0, cached: false };
  try {
    const res = await fetch(`${base}/models${refresh ? "?refresh=true" : ""}`, {
      headers: { Authorization: `Bearer ${currentEngine.token}` },
    });
    if (!res.ok) return empty;
    return res.json();
  } catch {
    return empty;
  }
}

export async function listWorkspaces(): Promise<Workspace[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/workspaces`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
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

export async function createGoal(
  workspace_id: string,
  title: string,
  description: string,
  dry_run: boolean,
  provider?: string,
  model?: string,
  plan_only: boolean = false,
  parallel: boolean = false
): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const payload: Record<string, any> = { workspace_id, title, description, dry_run, plan_only };
  if (provider) payload.provider = provider;
  if (model) payload.model = model;
  if (parallel) payload.parallel = true;

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
}

export async function getGoalUsage(goal_id: string): Promise<GoalUsage> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/usage`, {
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
export async function applyGoal(goal_id: string): Promise<{ applied: boolean; goal_id: string }> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/apply`, {
    method: "POST",
    headers: { Authorization: `Bearer ${currentEngine.token}` },
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
