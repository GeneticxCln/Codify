import { useState, useEffect, useCallback } from "react";
import { AgentConfig, AgentConfigPatch, AgentRole } from "../types";
import { tauriInvoke } from "../api";

export function useAgentConfigs() {
  const [configs, setConfigs] = useState<AgentConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      const res = await tauriInvoke<AgentConfig[]>("codify_list_agent_configs");
      setConfigs(res);
      setError(null);
    } catch (err: any) {
      setError(err.message || "Failed to load agent configs");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const update = async (role: AgentRole, patch: AgentConfigPatch): Promise<AgentConfig> => {
    const updated = await tauriInvoke<AgentConfig>("codify_update_agent_config", {
      role,
      patch,
    });
    setConfigs((cs) => cs.map((c) => (c.role === role ? updated : c)));
    return updated;
  };

  const testConnection = async (role: AgentRole): Promise<{ ok: boolean; message: string }> => {
    return tauriInvoke<{ ok: boolean; message: string }>("codify_test_agent_connection", {
      role,
    });
  };

  return { configs, update, testConnection, loading, error, refresh };
}
