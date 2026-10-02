import { readErrorBody } from "./errorBody.ts";
import type {
  AgentCallStat,
  AgentConfig,
  AudioInputs,
  AudioStatus,
  Conversation,
  ConversationTurn,
  DeletedGoal,
  DeletedWorkspace,
  EngineInfo,
  EngineStatus,
  FailureBreakdown,
  StatsHistoryDay,
  StatsImportState,
  StatsOverview,
  EngineSettings,
  Event,
  EngineRuntime,
  Goal,
  GoalMode,
  ShellTabRow,
  GoalStatus,
  LayaStatus,
  ModelCatalog,
  PlanStep,
  ProviderCatalog,
  ProviderKeyStatus,
  RepairReport,
  RecentRunModel,
  RoleInfo,
  TraceSummary,
  Workspace,
  WorkspaceFile,
  WorkspaceFileList,
  WorkspaceFileSaved,
  SurfaceRequest,
} from "./types.ts";

/**
 * Whether the desktop shell is hosting this page.
 *
 * The shell is the party that watched the engine's handshake, and it answers
 * `codify_get_engine_info` on every health probe, so under it the boot token
 * never needs to be written anywhere: it is fetched, held in memory, and
 * fetched again after an engine restart. Only the standalone browser preview
 * has no such party, which is what the paste-into-the-console flow is for.
 */
export function underShell(): boolean {
  return typeof window !== "undefined" && Boolean((window as any).__TAURI_INTERNALS__);
}

/**
 * The connection this page starts with.
 *
 * The port is not a secret and is remembered either way. The token is read back
 * from `localStorage` only outside the shell; inside it, a copy left by an older
 * build is removed rather than trusted, since a token at rest in the webview's
 * profile is readable by anything that can read that directory.
 */
export function storedEngineInfo(shell: boolean = underShell()): EngineInfo {
  const port = parseInt(localStorage.getItem("CODIFY_PORT") || "7430", 10);
  if (shell) {
    localStorage.removeItem("CODIFY_TOKEN");
    return { port, token: "" };
  }
  return { port, token: localStorage.getItem("CODIFY_TOKEN") || "" };
}

/** Remember a connection: the port always, the token only where nothing else can supply it. */
export function rememberEngineInfo(info: EngineInfo, shell: boolean = underShell()): void {
  localStorage.setItem("CODIFY_PORT", info.port.toString());
  if (shell) localStorage.removeItem("CODIFY_TOKEN");
  else localStorage.setItem("CODIFY_TOKEN", info.token);
}

let currentEngine: EngineInfo = storedEngineInfo();

export function setEngineInfo(info: EngineInfo) {
  currentEngine = info;
  rememberEngineInfo(info);
}

/**
 * Re-read the connection from storage, for the standalone preview's
 * paste-and-retry flow (`StaleAuthBanner`). Under the shell there is nothing
 * pasted to read, and the in-memory token is the only copy, so this leaves it
 * alone rather than replacing it with a blank.
 */
export function resyncEngineInfoFromStorage(): EngineInfo {
  if (!underShell()) currentEngine = storedEngineInfo(false);
  return currentEngine;
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

/**
 * The engine's own last words: the tail of its stderr, as the shell tailed it.
 *
 * `engineFailureReason` above answers "why did the launch fail", which is the
 * launcher's story. This is the engine's: a traceback, a provider error, and
 * the bounded-shutdown backstop announcing that it stopped waiting for
 * in-flight work and exited anyway (`docs/04` §6.1). That line used to go to
 * whatever terminal the shell was started from, which for someone using the
 * app is nowhere — so an engine that vanished on a hung websocket showed a red
 * pill and no reason at all.
 *
 * Outside Tauri there is no shell holding a pipe to tail: the browser build
 * talks to an engine it did not spawn, and that engine's stderr belongs to
 * whatever started it. Empty is the honest answer there, and the caller keeps
 * the message it already had.
 */
export async function fetchEngineStderr(limit = 60): Promise<string[]> {
  if (typeof window === "undefined" || !(window as any).__TAURI_INTERNALS__) return [];
  try {
    const lines = await tauriInvoke<string[]>("codify_engine_log", { limit });
    return Array.isArray(lines) ? lines : [];
  } catch {
    return [];
  }
}

/**
 * Call a shell command.
 *
 * ## Argument names are camelCase, and that is not a style choice
 *
 * Tauri v2 converts the JS argument names to the Rust parameter names, and the
 * default conversion is camelCase → snake_case. So a Rust `workspace_id:
 * String` is reached with `{ workspaceId }`, and sending `{ workspace_id }`
 * fails with:
 *
 * > invalid args `workspaceId` for command `codify_terminal_open`: command
 * > codify_terminal_open missing required key workspaceId
 *
 * which names the *expected* key and says nothing about the one that was sent.
 * Every pane command in this file took an argument, so all seven of them failed
 * that way, while the no-argument commands (engine info, the agent configs)
 * kept working — which is why the app looked healthy and only the two panes
 * were broken. `invokeArgs.test.ts` holds the line for all of them at once.
 */
export async function tauriInvoke<T>(cmd: string, args?: Record<string, any>): Promise<T> {
  if (typeof window !== "undefined" && (window as any).__TAURI_INTERNALS__) {
    const { invoke } = await import("@tauri-apps/api/core");
    return invoke<T>(cmd, args);
  }
  // Fallback to direct HTTP API if running outside Tauri
  return fallbackHttpInvoke<T>(cmd, args);
}

/**
 * Why the panes answer this in a browser tab.
 *
 * A browser pane's page is a `WebviewWindow` and a terminal's shell is a PTY;
 * both live in the Rust process, and the engine has never exposed either over
 * HTTP. Reimplementing a shell on top of `POST /exec` would be a different
 * product, not a fallback.
 *
 * So outside the shell these refuse, and they refuse *by name* — every caller in
 * `App.tsx` already renders `err.message` into the pane that asked, so a
 * rejection here is a message on screen. Reaching `default:` instead threw
 * `Unknown command: codify_browser_open`, which claims the command does not
 * exist when it does; and because the first browser address is refused before
 * the pane has a page to show, the error landed in state keyed to an id no tab
 * had and the user saw nothing happen at all.
 */
const NEEDS_DESKTOP_SHELL =
  "The browser and terminal panes need the Codify desktop shell — the engine " +
  "serves no HTTP equivalent, so these commands exist only in the app.";

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
    // Grouped, not one line each: these have no HTTP shape to write, and six
    // near-identical branches would be six places for a future command to be
    // forgotten in. A missing name still has to be loud, which is what the
    // default below is for.
    case "codify_browser_open":
    case "codify_browser_navigate":
    case "codify_browser_focus":
    case "codify_browser_resize":
    case "codify_browser_close":
    case "codify_browser_devtools_open":
    case "codify_browser_devtools_close":
    case "codify_browser_devtools_state":
    case "codify_browser_devtools_available":
    case "codify_terminal_open":
    case "codify_terminal_write":
    case "codify_terminal_resize":
    case "codify_terminal_close": {
      throw new Error(NEEDS_DESKTOP_SHELL);
    }
    default:
      throw new Error(`Unknown command: ${cmd}`);
  }
}

/**
 * Parse an engine error body into a useful message. Falls back to the HTTP
 * status so a failure is never silent or empty.
 *
 * The reading itself is `readErrorBody` in `./errorBody`, which is where the
 * engine's declared refusal shape is described and where it is tested — it was
 * untestable in here, which is how it came to understand `detail` as a string
 * when a rejected body sends an array.
 */
async function engineError(res: Response, fallback: string): Promise<Error> {
  let code = "";
  let message = "";
  let extra: Record<string, unknown> = {};
  try {
    ({ code, message, extra } = readErrorBody(await res.json()));
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

/**
 * The engine's self-check: which interpreter is running, and what it can import.
 * Cheap — it probes for the SDK without loading weights — so it is safe to call
 * whenever the settings screen opens.
 *
 * Throws with the engine's code/message on failure; callers render the
 * "unavailable" state from the catch rather than from a silent null, because a
 * missing self-check and a healthy one must not look the same.
 */
export async function getEngineRuntime(): Promise<EngineRuntime> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/settings/runtime`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to fetch engine runtime");
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

/**
 * Persist engine-wide settings; the response echoes the clamped values.
 *
 * Numbers clamp at the engine and strings are stored as sent, so the echo is
 * the truth in both cases — read `saved` rather than assuming the value that
 * was sent is what was kept. `conductor_drives_execution` is a real boolean
 * because a checkbox sends one; the engine is the one place that accepts it.
 */
export async function saveEngineSettings(
  patch: {
    parallel_width?: number;
    stats_retention_days?: number;
    trace_retention_days?: number;
    conductor_provider?: string;
    conductor_model?: string;
    conductor_fallback_provider?: string;
    conductor_fallback_model?: string;
    conductor_max_turns?: number;
    conductor_max_moves?: number;
    conductor_drives_execution?: boolean;
    stt_provider?: string;
    stt_model?: string;
    stt_language?: string;
    tts_provider?: string;
    tts_model?: string;
    tts_voice?: string;
    audio_input?: string;
    stt_base_url?: string;
    tts_base_url?: string;
    tts_auto_read?: boolean;
  }
): Promise<{ saved: Record<string, number | string> }> {
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

/** What went wrong across every goal: by cause, by role, by stage, and how
 * many of the fix→verify retries the pipeline got back. Read-only and
 * windowed like the overview; the aggregation is the engine's
 * `engine/metrics.py`, tested there. */
export async function fetchFailureBreakdown(windowDays: number): Promise<FailureBreakdown> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/stats/failures?window=${windowDays}`, {
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
  // A refusal is not a cancel. The engine answers `{"cancelled": true}` when the person closed the
  // dialog, and 503 `picker_unavailable` (with the reason) when no dialog could open; returning null
  // for both made a picker that could not open indistinguishable from one that was dismissed.
  if (!res.ok) {
    // The code survives so the app can act on it: `picker_unavailable` means "type the path instead",
    // and the caller opens that form rather than leaving the person to find it.
    const { code, message } = readErrorBody(await res.json().catch(() => ({})));
    throw new ApiRequestError(res.status, code || null, message || `HTTP ${res.status}`);
  }
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

/**
 * Every file in a workspace, for the editor's quick-open.
 *
 * Paths relative to the root, sorted, with the folders that are not source left out (the engine's `SKIP_DIRS`), and cut
 * at `limit` with `truncated: true` rather than silently. Searched in the palette, never shown as a list.
 */
export async function listWorkspaceFiles(workspace_id: string, limit?: number): Promise<WorkspaceFileList> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const query = limit === undefined ? "" : `?limit=${limit}`;
  const res = await fetch(`${base}/workspaces/${encodeURIComponent(workspace_id)}/files${query}`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to list the workspace's files");
  return res.json();
}

/**
 * One file as the editor holds it: its exact text and the version a save must name.
 *
 * A refusal keeps the engine's own code, so the editor can say the right sentence: `file_missing`, `file_binary`,
 * `file_not_text`, `file_too_large`, `file_escape`.
 */
export async function readWorkspaceFile(workspace_id: string, path: string): Promise<WorkspaceFile> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(
    `${base}/workspaces/${encodeURIComponent(workspace_id)}/file?path=${encodeURIComponent(path)}`,
    { headers: { Authorization: `Bearer ${currentEngine.token}` } },
  );
  if (!res.ok) throw await engineError(res, "Failed to open the file");
  return res.json();
}

/**
 * A person's Save: replace one existing file's text, naming the version it was read at.
 *
 * **The one write to the workspace that is not the fixer's** (docs/00 §6.9), and only ever made because a person pressed
 * Save. A file that is no longer `base_version` is a 409 `file_changed`, and the version it is now is on the error as
 * `extra.current_version`.
 */
export async function saveWorkspaceFile(
  workspace_id: string,
  body: { path: string; content: string; base_version: string },
): Promise<WorkspaceFileSaved> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/workspaces/${encodeURIComponent(workspace_id)}/file`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await engineError(res, "Failed to save the file");
  return res.json();
}

/**
 * The next question the engine has for this window, or `null` when `wait` seconds pass with none.
 *
 * A long poll, and the window's heartbeat: a poll that arrives is how the engine learns the window is there
 * (`engine/surfaces.py`). Aborting it is how the loop stops. A refusal throws, so the loop backs off instead of spinning.
 */
export async function nextSurfaceRequest(wait: number, signal?: AbortSignal): Promise<SurfaceRequest | null> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/surfaces/next?wait=${wait}`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
    signal,
  });
  if (!res.ok) throw await engineError(res, "Failed to poll the engine");
  const body = await res.json();
  return body && typeof body.id === "string" ? (body as SurfaceRequest) : null;
}

/** Answer one question. `false` when the engine no longer wanted it: it ran out of patience, or the id was never its own. */
export async function answerSurface(body: {
  id: string;
  ok: boolean;
  result?: unknown;
  error?: string;
}): Promise<boolean> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/surfaces/answer`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await engineError(res, "Failed to answer the engine");
  const reply = await res.json();
  return reply?.accepted === true;
}

/**
 * The threads in a workspace, most recently touched first.
 *
 * Archived threads are hidden unless asked for: the side panel is what the user
 * is working on, and burying it under every finished thread is the mistake the
 * goal history avoids by leading with active runs.
 */
export async function fetchConversations(
  workspaceId: string,
  includeArchived: boolean = false,
): Promise<Conversation[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const params = new URLSearchParams({ workspace_id: workspaceId });
  if (includeArchived) params.set("include_archived", "true");
  const res = await fetch(`${base}/conversations?${params}`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to load conversations");
  return res.json();
}

/** Start a thread. An empty title is the ordinary case — it is named later. */
export async function createConversation(
  workspaceId: string,
  title: string = "",
  parentId?: string,
): Promise<Conversation> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/conversations`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    // `parent_id` only when there is one: the engine's `ConversationCreate` is
    // `extra: "forbid"`, and sending an explicit `null` for every top-level
    // thread would be a field the client had to keep meaning straight.
    body: JSON.stringify({
      workspace_id: workspaceId,
      title,
      ...(parentId ? { parent_id: parentId } : {}),
    }),
  });
  if (!res.ok) throw await engineError(res, "Failed to start a conversation");
  return res.json();
}

/**
 * A thread's turns, oldest first — what the transcript reads as.
 *
 * Derived by the engine from the goals that answer them (each turn is one run), so
 * this is the list a tab needs to rebuild a pane after a reload, and it is the
 * reason a thread is not empty just because nothing is streaming to it.
 */
export async function fetchConversationTurns(
  conversationId: string,
): Promise<ConversationTurn[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/conversations/${conversationId}/turns`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to load the thread's turns");
  return res.json();
}

/** Name a thread. The only mutable thing about one. */
export async function renameConversation(
  conversationId: string,
  title: string,
): Promise<Conversation> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/conversations/${conversationId}`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw await engineError(res, "Failed to rename the conversation");
  return res.json();
}

/** Hide a thread from the panel, or bring it back. Archived, never deleted. */
export async function archiveConversation(
  conversationId: string,
  archived: boolean = true,
): Promise<Conversation> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const params = new URLSearchParams({ archived: String(archived) });
  const res = await fetch(
    `${base}/conversations/${conversationId}/archive?${params}`,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${currentEngine.token}` },
    },
  );
  if (!res.ok) throw await engineError(res, "Failed to archive the conversation");
  return res.json();
}

/**
 * Drop the thread and keep its runs.
 *
 * The engine keeps every goal it held as a single-turn thread, so this is a way
 * to tidy a tab list and never a way to erase a history.
 */
export async function deleteConversation(
  conversationId: string,
): Promise<{ deleted: boolean }> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/conversations/${conversationId}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to delete the conversation");
  return res.json();
}

/**
 * Put an existing goal into a thread, server-side.
 *
 * The link that used to live only in the panel: restoring a goal that predates
 * conversations created the thread here, in the client, and the engine still
 * had NULL on the row — one restart later the run read as its own thread again.
 * This makes the store's copy match what the tab shows.
 */
export async function attachGoalToConversation(
  goal_id: string,
  conversationId: string,
): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/conversation`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ conversation_id: conversationId }),
  });
  if (!res.ok) throw await engineError(res, "Failed to attach the goal to its conversation");
  return res.json();
}

/**
 * Ask the shell to open a browser tab's webview window.
 *
 * Returns the webview label, which is `browser-<tabId>` and is the shell's own
 * business — nothing in `ui/` needs it, because the tab id is what every later
 * call takes and the close signal reports. It is typed as the shell types it
 * rather than as `void` so that a future caller which does need the label is
 * not casting a string to nothing.
 *
 * A refusal arrives as a thrown `Error` carrying the shell's own message, and
 * that message is shown to the user unchanged — the loopback guard's wording
 * ("browser webviews load http(s) on non-loopback hosts only") says more than
 * any message written here would, because it is the same sentence as the rule.
 */
/** Where a browser page sits inside the main window, in logical pixels. */
export interface BrowserBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * Open (embed) a browser page for a tab, or navigate the one that exists.
 *
 * `bounds` is the pane's content rectangle in **logical** pixels — the units
 * CSS speaks — measured by the UI, which owns the layout. A child webview is
 * placed where its pane says, not where a guess about the window's chrome
 * puts it.
 */
export async function openBrowserWebview(
  tabId: string,
  url: string,
  bounds: BrowserBounds
): Promise<string> {
  return tauriInvoke<string>("codify_browser_open", { tabId, url, bounds });
}

/**
 * Show this browser tab's page and hide the others.
 *
 * Visibility is the stacking order for embedded pages: only the active tab's
 * page is shown, so switching tabs is one call and no z-order bookkeeping.
 * An empty `tabId` means no browser tab is showing — every page hidden, which
 * is what a switch to a chat or terminal tab asks for.
 */
export async function focusBrowserWebview(tabId: string): Promise<string> {
  return tauriInvoke<string>("codify_browser_focus", { tabId });
}

/**
 * Resize every browser page to the pane's current content rectangle.
 *
 * One call for all pages, not one per tab: they share the same rectangle by
 * construction, and a resize while five browser tabs are open is five chances
 * to leave one behind. Sent on the pane's ResizeObserver, debounced upstream.
 */
export async function resizeBrowserWebviews(bounds: BrowserBounds): Promise<void> {
  return tauriInvoke<void>("codify_browser_resize", { bounds });
}

/**
 * Open the page's DevTools inspector.
 *
 * The inspector is a shell-side surface over the webview — it grants the
 * page nothing. The crate builds with tauri's `devtools` feature, so the
 * command exists in every build.
 */
export async function openBrowserDevtools(tabId: string): Promise<string> {
  return tauriInvoke<string>("codify_browser_devtools_open", { tabId });
}

/** Close the page's DevTools inspector. */
export async function closeBrowserDevtools(tabId: string): Promise<string> {
  return tauriInvoke<string>("codify_browser_devtools_close", { tabId });
}

/**
 * Does this build have a DevTools inspector at all?
 *
 * The pane asks instead of hardcoding the build shape; a build without the
 * feature answers `false` and the control is hidden rather than dead.
 */
export async function browserDevtoolsAvailable(): Promise<boolean> {
  return tauriInvoke<boolean>("codify_browser_devtools_available");
}

/**
 * Is the page's DevTools inspector open?
 *
 * Asked when the pane mounts a page, so the control shows the inspector's
 * real state rather than the pane's guess about it.
 */
export async function browserDevtoolsState(tabId: string): Promise<boolean> {
  return tauriInvoke<boolean>("codify_browser_devtools_state", { tabId });
}

/**
 * Send an open browser tab to a new address.
 *
 * Distinct from `openBrowserWebview` because the shell is: `open` on a tab that
 * already exists navigates it and focuses it, while `navigate` refuses a tab
 * that has no webview. The pane uses that refusal as its answer to "has this
 * tab got a page yet" — see `Tab.url` — so the two are called from different
 * places and neither has to catch an error to find out.
 */
export async function navigateBrowserWebview(
  tabId: string,
  url: string
): Promise<string> {
  return tauriInvoke<string>("codify_browser_navigate", { tabId, url });
}

/**
 * Start the user's shell in a workspace, at a character grid.
 *
 * The id comes back from the shell and is not ours to choose — `terminal.rs`
 * numbers its own sessions `term-1`, `term-2` — so it becomes the tab's id
 * rather than a second string to keep in step. `workspace_id` rather than a path
 * is the whole of the boundary: `pin_cwd` resolves it against the engine's
 * record and refuses anything that is not an existing absolute directory
 * (`docs/00` §6.6 is about the *agent's* shell, and `docs/07` §2.1 is about
 * this one).
 *
 * `cols`/`rows` are clamped by the caller through `gridFrom` rather than here,
 * because the pane is what knows the measurement; this function only has the
 * numbers it is handed, and a `u16` parameter is the last line of defence.
 */
export async function openTerminal(
  workspaceId: string,
  cols: number,
  rows: number
): Promise<string> {
  return tauriInvoke<string>("codify_terminal_open", {
    workspaceId,
    cols,
    rows,
  });
}

/**
 * Send keystrokes to an open terminal.
 *
 * Called once per `onData` batch, which is what xterm hands us: one string that
 * may be a character, a control sequence, a paste, or several of those at once.
 * Splitting it into characters would turn one paste into several hundred IPC
 * round trips and reorder the shell's own line editing with them.
 */
export async function writeTerminal(
  terminalId: string,
  data: string
): Promise<void> {
  return tauriInvoke<void>("codify_terminal_write", {
    terminalId,
    data,
  });
}

/**
 * Tell the PTY its window changed size, so the shell re-wraps what it has.
 *
 * A no-op on a terminal that has already exited, which is why the pane does not
 * have to check whether it is still running before every drag frame.
 */
export async function resizeTerminal(
  terminalId: string,
  cols: number,
  rows: number
): Promise<void> {
  return tauriInvoke<void>("codify_terminal_resize", {
    terminalId,
    cols,
    rows,
  });
}

/**
 * Kill a terminal's shell and reap it.
 *
 * Unlike `closeBrowserWebview` this one is safe to call for an id that is not
 * there: `terminal::close` returns `Ok(())` when the session is already gone,
 * because a terminal outlives its tab in the other direction too — a shell that
 * has exited is a normal state, and closing its tab must not be an error.
 */
export async function closeTerminal(terminalId: string): Promise<void> {
  return tauriInvoke<void>("codify_terminal_close", { terminalId });
}

/**
 * Close a browser tab's page.
 *
 * Closing the tab in the strip has to close the page too, or it keeps running
 * with nothing pointing at it — a hidden webview still spends memory and
 * still runs its scripts. An embedded page cannot close itself (a child has
 * no window events), so the strip is the only closer and this is the only
 * path.
 *
 * Errors when there is no page, which is a caller error rather than a fault
 * to report: `App.handleCloseTab` skips the call for a browser tab that never
 * got a URL, so the user is never shown the shell's "no browser tab is open"
 * for a tab they never gave a page to.
 */
export async function closeBrowserWebview(tabId: string): Promise<string> {
  return tauriInvoke<string>("codify_browser_close", { tabId });
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
  mode: GoalMode = "normal",
  /** Record this run's model calls, so the run can be replayed without a
   * provider. Opt-in, and a copy of the model's output about the user's code. */
  trace: boolean = false,
  /** The thread this run belongs to. Absent means "a thread of its own". */
  conversation_id?: string | null,
): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const payload: Record<string, any> = { workspace_id, title, description, dry_run, plan_only };
  if (provider) payload.provider = provider;
  if (model) payload.model = model;
  if (parallel) payload.parallel = true;
  // Sent only when set, like `mode` below: an older engine validating the body
  // strictly would reject an unknown key, and absent is what it assumes.
  if (conversation_id) payload.conversation_id = conversation_id;
  // Sent only when it is not the default: an older engine validating the body
  // strictly would reject an unknown key, and "normal" is what it assumes.
  if (mode !== "normal") payload.mode = mode;
  if (trace) payload.trace = true;

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

/**
 * Say something in a thread.
 *
 * This is what the composer posts to, and it is deliberately *not* `createGoal`:
 * a goal starts a pipeline, so every message used to start eight agents, and
 * "hi" produced a plan. The gate classifies the request and the engine picks
 * the shape — a question is answered from one model call, anything else runs
 * the full pipeline. So the client sends the words and nothing else: there is
 * no `mode` and no `dry_run` here to get wrong, and the engine's one door into
 * a turn is the only one (docs/09 §10).
 *
 * The response is a `Goal` with `mode: "chat"` — a turn is stored as a goal
 * because `events.goal_id` is NOT NULL, so the event log is the WebSocket, the
 * audit trail and the stats feed. Reusing the row is what makes the stream
 * below work with no turn-specific machinery at all.
 */
export async function createTurn(
  conversationId: string,
  prompt: string,
  provider?: string,
  model?: string,
  trace: boolean = false,
): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const payload: Record<string, any> = { prompt };
  if (provider) payload.provider = provider;
  if (model) payload.model = model;
  if (trace) payload.trace = true;
  const res = await fetch(`${base}/conversations/${conversationId}/turns`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw await engineError(res, "Failed to send");
  }
  return res.json();
}

/**
 * The engine answered a request with a coded refusal, and the code survived:
 * `version_conflict` and `illegal_status` are the two 409 codes that mean "the
 * goal moved under you" — a race the client lost, not a bug. The generic
 * failures this file used to throw flattened every refusal to `message`, so
 * the UI could not tell a lost race from a real one. `goalActions.ts` keys the
 * retry policy on this code.
 */
/**
 * What this goal recorded: the calls, in order, with a digest of the prompt
 * rather than the prompt. Never throws for "not recorded" — a goal that was
 * never traced is an ordinary state, and the UI reads it as zero calls.
 */
export async function fetchGoalTrace(goal_id: string): Promise<TraceSummary> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/trace`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to read the recording");
  return res.json();
}

/**
 * Forget a goal's recording. The user's call, and the only way to remove one:
 * a recording is a copy of the model's output about their code, so it is
 * always deletable and never deleted on their behalf.
 */
export async function deleteGoalTrace(goal_id: string): Promise<{ deleted: number }> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/trace`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to delete the recording");
  return res.json();
}

/**
 * Start or stop recording a goal. Throws ApiRequestError with `trace_locked`
 * if asked to start after the run has begun: a recording that starts halfway
 * is a trace of half a run, and the replay it would support is missing the
 * calls that shaped the first half. Stopping is always allowed.
 */
export async function setGoalTrace(goal_id: string, enabled: boolean): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/goals/${goal_id}/trace`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new ApiRequestError(res.status, err.code ?? null, err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

export class ApiRequestError extends Error {
  status: number;
  code: string | null;

  constructor(status: number, code: string | null, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/**
 * One versioned action POST with the engine's coded refusal preserved.
 *
 * The policy lives in `goalActions.ts` (retry the races, treat a state that no
 * longer needs the action as moot, surface the rest); this is the wire half it
 * drives. Returns the parsed response on success; throws ApiRequestError with
 * the body's `code` intact on refusal.
 */
export async function requestGoalAction(
  path: string,
  body: { expected_version: number },
): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({} as { code?: unknown; message?: unknown }));
    throw new ApiRequestError(
      res.status,
      typeof err.code === "string" ? err.code : null,
      typeof err.message === "string" ? err.message : `HTTP ${res.status}`,
    );
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
  return requestGoalAction(`/goals/${goal_id}/start`, { expected_version });
}

export async function pauseGoal(goal_id: string, expected_version: number): Promise<Goal> {
  return requestGoalAction(`/goals/${goal_id}/pause`, { expected_version });
}

export async function cancelGoal(goal_id: string, expected_version: number): Promise<Goal> {
  return requestGoalAction(`/goals/${goal_id}/cancel`, { expected_version });
}

export interface HealthStatus {
  ok: boolean;
  authenticated: boolean;
}

/** What the desktop shell says about itself. */
export interface AppFacts {
  name: string;
  version: string;
  /** The Tauri runtime's version, or "" when it did not say. */
  tauri: string;
}

/**
 * The app's own name and version, from the shell. `null` outside it.
 *
 * Read through Tauri's app API, not through `tauriInvoke`: that falls back to HTTP for the commands
 * the engine can answer, and this one has no engine equivalent, so outside the shell (the standalone
 * browser preview) the honest answer is "there is no desktop app here", not an error. A shell that
 * answers with nothing is treated the same, because a blank version printed as a fact would be worse
 * than none. The permissions are already in `core:default`, so this needs no capability of its own.
 */
export async function getAppFacts(): Promise<AppFacts | null> {
  if (!underShell()) return null;
  try {
    const app = await import("@tauri-apps/api/app");
    const [name, version, tauri] = await Promise.all([
      app.getName(),
      app.getVersion(),
      app.getTauriVersion(),
    ]);
    if (typeof version !== "string" || !version.trim()) return null;
    return {
      name: typeof name === "string" && name.trim() ? name : "Codify",
      version,
      tauri: typeof tauri === "string" ? tauri : "",
    };
  } catch {
    return null;
  }
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

/**
 * The open tab strip, and the engine's half of it (docs/09 §2.1).
 *
 * Three calls because the strip has three kinds of change and each one is a
 * different statement: a tab that appeared or moved or navigated is an upsert
 * of *that* tab, a tab that was closed is a delete of *that* key, and a window
 * that is catching up asks for the lot. Every one of the two writes returns the
 * merged strip, so the caller adopts what the other window did in the same
 * breath as its own change — the windows do not wait for each other, they meet
 * in the response.
 *
 * Keyed by the tab's durable `key`, never by its `id`: `id` is minted per
 * process and names this window's webview, which the engine has never heard of.
 */
export async function listShellTabs(): Promise<ShellTabRow[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/shell/tabs`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function upsertShellTab(tab: {
  key: string;
  position: number;
  kind: "chat" | "browser";
  payload: string;
}): Promise<ShellTabRow[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/shell/tabs`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${currentEngine.token}`,
    },
    body: JSON.stringify(tab),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function deleteShellTab(key: string): Promise<ShellTabRow[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/shell/tabs/${encodeURIComponent(key)}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return res.json();
}

// ── voice (docs/04 §3.0.2) ──────────────────────────────────────────────────
//
// Each of these throws through `engineError`, so a caller can read the engine's
// own `code` (`stt_not_configured`, `recorder_unavailable`, …) off the error and
// act on it — the mic button opens Settings → Audio for the first one — rather
// than parsing a sentence.

async function audioPost(path: string, fallback: string, body?: unknown): Promise<Response> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${currentEngine.token}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw await engineError(res, fallback);
  return res;
}

/** Whether dictation and read-aloud can run now, and if not, why. */
export async function getAudioStatus(): Promise<AudioStatus> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/audio/status`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to read the audio status");
  return res.json();
}

/** The microphones PipeWire knows, or why none can be listed. */
export async function getAudioInputs(): Promise<AudioInputs> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/audio/inputs`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw await engineError(res, "Failed to list microphones");
  return res.json();
}

/** Start recording the microphone for dictation. */
export async function startDictation(): Promise<{ recording: boolean; max_seconds: number }> {
  return (await audioPost("/audio/dictation/start", "Could not start dictation")).json();
}

/** Stop recording; answers with what was said. The recording is deleted either way. */
export async function stopDictation(): Promise<{ text: string; seconds: number }> {
  return (await audioPost("/audio/dictation/stop", "Could not transcribe the dictation")).json();
}

/** End a recording and delete it without sending it anywhere. */
export async function cancelDictation(): Promise<{ cancelled: boolean }> {
  return (await audioPost("/audio/dictation/cancel", "Could not cancel dictation")).json();
}

/** The read-aloud provider's WAV for `text`. */
export async function speak(text: string): Promise<Blob> {
  return (await audioPost("/audio/speak", "Could not read this aloud", { text })).blob();
}
