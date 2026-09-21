import {
  EngineInfo,
  Goal,
  ModelOption,
  PlanStep,
  ProviderCatalog,
  ProviderKeyStatus,
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

export async function fetchProviders(): Promise<ProviderCatalog> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/settings/providers`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
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

export async function fetchAvailableModels(): Promise<ModelOption[]> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const res = await fetch(`${base}/models`, {
    headers: { Authorization: `Bearer ${currentEngine.token}` },
  });
  if (!res.ok) return [];
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
  model?: string
): Promise<Goal> {
  const base = `http://127.0.0.1:${currentEngine.port}`;
  const payload: Record<string, any> = { workspace_id, title, description, dry_run };
  if (provider) payload.provider = provider;
  if (model) payload.model = model;

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
