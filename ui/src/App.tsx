import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  Workspace,
  ChatMessage,
  EngineInfo,
  Event,
  ModelCatalog,
  ModelOption,
  AgentConfig,
  RecentRunModel,
} from "./types";
import { buildModelSignals } from "./modelSignals";
import {
  listWorkspaces,
  fetchAgentConfigs,
  fetchRecentRunModels,
  createWorkspace,
  createGoal,
  getGoal,
  startGoal,
  pauseGoal,
  cancelGoal,
  retryStep,
  patchStep,
  getEngineInfo,
  setEngineInfo,
  tauriInvoke,
  browseWorkspace,
  fetchModelCatalog,
  checkEngineHealth,
  refreshEngineInfoFromIpc,
  enableExecution,
  applyGoal,
} from "./api";
import { BottomCommandBar, ExecutionMode } from "./components/BottomCommandBar";
import { ChatTimeline } from "./components/ChatTimeline";
import { looksLikeAudit } from "./components/AuditReport";
import { SettingsModal } from "./components/SettingsModal";
import { openGoalStream, GoalStreamHandle } from "./goalStream";
import { Code, Settings, FolderGit2, AlertCircle } from "lucide-react";

export const App: React.FC = () => {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWs, setSelectedWs] = useState<Workspace | undefined>();
  // Model catalog: discovered from every configured provider by the engine.
  // Nothing here is hardcoded — an empty list means nothing is configured yet.
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog>({
    models: [],
    providers: [],
    fetched_at: 0,
    cached: false,
  });
  const [selectedModel, setSelectedModel] = useState<ModelOption | undefined>();
  const [modelsLoading, setModelsLoading] = useState(false);
  // Signals the model menu orders by. Both are things the engine already records:
  // which model each role is configured with, and which models actually answered.
  const [agentConfigs, setAgentConfigs] = useState<AgentConfig[]>([]);
  const [recentRuns, setRecentRuns] = useState<RecentRunModel[]>([]);
  const [mode, setMode] = useState<ExecutionMode>("direct");
  // Opt-in parallelism: independent (path-disjoint) steps of a goal run concurrently.
  const [parallel, setParallel] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  // Which settings tab to land on. A failure diagnosis sends the user straight
  // to the screen that holds the fix instead of making them find it.
  const [settingsTab, setSettingsTab] = useState<"keys" | "agents">("keys");
  const openSettings = (tab: "keys" | "agents" = "keys") => {
    setSettingsTab(tab);
    setIsSettingsOpen(true);
  };
  const [engine, setEngine] = useState<EngineInfo>(getEngineInfo());
  const [error, setError] = useState<string | null>(null);
  const [engineUp, setEngineUp] = useState<boolean | null>(null); // null = checking
  const [authOk, setAuthOk] = useState<boolean | null>(null);

  // Live goal streams (one per goal, with reconnect backoff).
  const goalStreams = useRef<Record<string, GoalStreamHandle>>({});
  // Set below; refs let the connection effects call the latest versions
  // without re-subscribing on every render.
  const loadWorkspacesRef = useRef<(() => void) | null>(null);
  const loadModelsRef = useRef<((refresh?: boolean) => void) | null>(null);
  const loadModelSignalsRef = useRef<(() => void) | null>(null);

  // Live engine liveness probe — replaces the old hardcoded green pill.
  // Polls /health every 3s; backs off while the tab is hidden.
  //
  // Self-healing: when the engine answers but rejects our token (auth-stale —
  // usually the engine restarted and rotated its boot token), re-fetch engine
  // info from Tauri IPC, which tracks the live process's handshake. If that
  // yields different connection info, re-probe immediately instead of waiting
  // for the next tick and warning the user about something we can fix.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const probe = async () => {
      if (cancelled) return;
      let health = await checkEngineHealth();
      if (cancelled) return;
      if (health.ok && !health.authenticated) {
        const fresh = await refreshEngineInfoFromIpc();
        if (cancelled) return;
        if (fresh) {
          setEngine(fresh);
          loadWorkspacesRef.current?.();
          // A fresh token may point at a different engine (and therefore a
          // different set of configured providers) — re-discover its models.
          loadModelsRef.current?.(true);
          // Connection info changed — re-probe right away with the new token.
          health = await checkEngineHealth();
          if (cancelled) return;
        }
      }
      setEngineUp(health.ok);
      setAuthOk(health.authenticated);
      const next = document.hidden ? 15000 : 3000;
      timer = setTimeout(probe, next);
    };
    probe();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  // Close every stream when the app unmounts — otherwise sockets leak forever.
  useEffect(() => {
    const streams = goalStreams;
    return () => {
      Object.values(streams.current).forEach((s) => s.close());
      streams.current = {};
    };
  }, []);

  // Fetch real engine info from Tauri IPC on mount with retry. The engine can
  // come up after the UI (desktop shell spawn order), so workspaces *and* the
  // model catalog are re-read here rather than only on mount.
  useEffect(() => {
    let attempts = 0;
    const fetchInfo = () => {
      tauriInvoke<EngineInfo>("codify_get_engine_info")
        .then((info) => {
          setEngineInfo(info);
          setEngine(info);
          // Engine just became reachable — re-read workspaces now that the
          // real token/port are set (clears the startup failure banner).
          loadWorkspacesRef.current?.();
          loadModelsRef.current?.(true);
        })
        .catch(() => {
          attempts++;
          if (attempts < 10) {
            setTimeout(fetchInfo, 500);
          }
        });
    };
    fetchInfo();
  }, []);

  // Load workspaces; never fabricate a hardcoded one — if the engine has no
  // workspaces yet, the user picks a folder via the command bar first.
  const loadWorkspaces = useCallback(async () => {
    try {
      const wsList = await listWorkspaces();
      setWorkspaces(wsList);
      if (wsList.length > 0) {
        setSelectedWs((prev) => prev ?? wsList[0]);
      } else {
        setWorkspaces([]);
        setSelectedWs(undefined);
      }
      setError(null);
    } catch (err: any) {
      // Backend might still be starting — App also retries engine info above.
      setError(
        `Cannot reach the Codify engine at 127.0.0.1:${getEngineInfo().port}. ` +
        "Start it with `make run-engine` (or relaunch the desktop app)."
      );
    }
  }, []);

  // Discover the models every configured provider serves. Called with
  // refresh=true whenever the app opens or the settings screen changes
  // something, so newly released models appear without a restart.
  const loadModels = useCallback(async (refresh = false) => {
    setModelsLoading(true);
    try {
      const catalog = await fetchModelCatalog(refresh);
      setModelCatalog(catalog);
      setSelectedModel((prev) => {
        // Keep the user's pick while it still exists. Ids repeat across
        // providers (e.g. a model served both locally and via an API), so the
        // pair is what identifies a choice.
        if (prev && catalog.models.some((m) => m.id === prev.id && m.provider === prev.provider)) {
          return prev;
        }
        // Otherwise prefer a local model: no tokens, no latency, no surprise.
        return catalog.models.find((m) => m.protocol === "ollama") ?? catalog.models[0];
      });
    } finally {
      setModelsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadWorkspacesRef.current = loadWorkspaces;
    loadModelsRef.current = loadModels;
  }, [loadWorkspaces, loadModels]);

  // Roles and recent runs, for the model menu's ordering. Failures are silent on
  // purpose: a hint about ordering must never be the reason the app shows an
  // error, and the menu works without it (provider order, as before).
  const loadModelSignals = useCallback(async () => {
    const [configs, runs] = await Promise.all([
      fetchAgentConfigs().catch(() => [] as AgentConfig[]),
      fetchRecentRunModels(5).catch(() => [] as RecentRunModel[]),
    ]);
    setAgentConfigs(configs);
    setRecentRuns(runs);
  }, []);

  // Derived, not stored: the same configs and runs already drive the settings
  // screen, so a second copy would only be a way for the two pickers to disagree.
  const modelSignals = useMemo(
    () => buildModelSignals(agentConfigs, recentRuns),
    [agentConfigs, recentRuns]
  );

  // A goal's stream closes on its own (see `onTerminal`), so the refresh there
  // reaches this through a ref rather than re-subscribing every goal to a
  // changing callback.
  useEffect(() => {
    loadModelSignalsRef.current = loadModelSignals;
  }, [loadModelSignals]);

  useEffect(() => {
    loadWorkspacesRef.current?.();
    // refresh=true on open: "what does each provider serve right now" is not a
    // question worth caching across app launches.
    loadModels(true);
    loadModelSignals();
  }, [loadModels, loadModelSignals]);

  // Native OS File Manager browser handler (opens Nautilus / portal)
  const handleBrowseWorkspace = async () => {
    try {
      const ws = await browseWorkspace();
      if (ws) {
        setWorkspaces((prev) => {
          const exists = prev.some((w) => w.id === ws.id);
          return exists ? prev : [...prev, ws];
        });
        setSelectedWs(ws);
      }
    } catch (err) {
      console.error("Failed to browse workspace", err);
    }
  };

  const handleCreateWorkspace = async (name: string, root_path: string) => {
    const ws = await createWorkspace(name, root_path);
    setWorkspaces((prev) => [...prev, ws]);
    setSelectedWs(ws);
  };

  // Subscribe to live goal events via WebSocket (reconnects with backoff,
  // closes itself when the goal reaches a terminal status).
  const subscribeToGoal = useCallback(
    (goalId: string, messageId: string, sinceSequence = 0) => {
      if (goalStreams.current[goalId]) return;

      const applyEvent = (ev: Event) => {
        setMessages((prev) =>
          prev.map((msg) => {
            if (msg.id !== messageId) return msg;
            const existingEvents = msg.events || [];
            if (existingEvents.some((item) => item.sequence === ev.sequence)) {
              return msg;
            }
            const updatedEvents = [...existingEvents, ev].sort(
              (a, b) => a.sequence - b.sequence
            );
            return { ...msg, events: updatedEvents };
          })
        );

        if (ev.type === "goal_status" || ev.type === "step_status" || ev.type === "plan_updated") {
          getGoal(goalId).then((refreshed) => {
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === messageId ? { ...msg, goal: refreshed } : msg
              )
            );
          });
        }
      };

      goalStreams.current[goalId] = openGoalStream({
        goalId,
        onEvent: applyEvent,
        sinceSequence,
        // Terminal status: flush one final goal close, then stop streaming.
        onTerminal: () => {
          // The goal just ran models, so "ran recently" is now stale. Refreshing
          // here is what makes the menu learn from the run you just watched
          // instead of from the run before it.
          loadModelSignalsRef.current?.();
          // Drop the handle: the stream closed itself, and leaving it here made
          // every future subscribeToGoal() for this goal a silent no-op (so a
          // retried or re-applied goal streamed no events).
          delete goalStreams.current[goalId];
          getGoal(goalId).then((refreshed) => {
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === messageId ? { ...msg, goal: refreshed } : msg
              )
            );
          });
        },
      });
    },
    []
  );

  // Goals whose execution we have already kicked off in direct mode. Without
  // this, the auto-start had to observe the PLANNING → PENDING transition, so a
  // plan that finished before the first poll tick (the common case with a fast
  // local model) never started at all.
  const autoStartedGoals = useRef<Set<string>>(new Set());

  // Poll active goals periodically
  useEffect(() => {
    const interval = setInterval(() => {
      messages.forEach(async (msg) => {
        if (
          msg.goal &&
          (msg.goal.status === "PLANNING" ||
            msg.goal.status === "RUNNING" ||
            (mode === "direct" && msg.goal.status === "PENDING"))
        ) {
          const goalId = msg.goal.id;
          try {
            const refreshed = await getGoal(goalId);
            setMessages((prev) =>
              prev.map((m) => (m.id === msg.id ? { ...m, goal: refreshed } : m))
            );

            // Direct mode means direct: start as soon as the plan is ready,
            // whatever we happened to observe first.
            if (
              mode === "direct" &&
              refreshed.status === "PENDING" &&
              !autoStartedGoals.current.has(goalId)
            ) {
              autoStartedGoals.current.add(goalId);
              await startGoal(refreshed.id, refreshed.version);
              const running = await getGoal(refreshed.id);
              setMessages((prev) =>
                prev.map((m) => (m.id === msg.id ? { ...m, goal: running } : m))
              );
            }
          } catch (e) {
            // Ignore polling errors
          }
        }
      });
    }, 1500);

    return () => clearInterval(interval);
  }, [messages, mode]);

  /** Import a downloaded audit JSON back into the transcript as a readable
   * report. A closed artifact: it needs no engine connection, so a run can be
   * reviewed long after the goal (or the machine that ran it) is gone. */
  const handleImportAudit = () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "application/json,.json";
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      try {
        const text = await file.text();
        const doc = JSON.parse(text);
        if (!looksLikeAudit(doc)) {
          setError(
            `"${file.name}" is not a Codify audit export — expected a JSON document with plan_edits / fallbacks / step_outcomes.`
          );
          return;
        }
        setError(null);
        const imported: ChatMessage = {
          id: `audit-${Date.now()}`,
          role: "assistant",
          content: `${file.name}`,
          timestamp: Date.now(),
          auditDoc: doc,
        };
        setMessages((prev) => [...prev, imported]);
      } catch (err) {
        setError(
          `Could not read "${file.name}": ${err instanceof Error ? err.message : String(err)}`
        );
      }
    };
    input.click();
  };

  /**
   * Returns false when the prompt was refused *before* anything was dispatched, so
   * the command bar can keep the text. Losing a long prompt because a picker is
   * unset is not a recoverable mistake for the user.
   */
  const handleSendMessage = async (promptText: string): Promise<boolean> => {
    if (!selectedWs) {
      setError("Select a project folder first (folder button in the command bar).");
      return false;
    }
    if (!selectedModel) {
      setError(
        "No model available. Add an API key in Settings → Provider Keys (or start Ollama) " +
        "and the provider's models will load automatically."
      );
      return false;
    }
    const wsToUse = selectedWs;
    setError(null);

    const userMsgId = `user-${Date.now()}`;
    const assistantMsgId = `assistant-${Date.now() + 1}`;

    const userMsg: ChatMessage = {
      id: userMsgId,
      role: "user",
      content: promptText,
      timestamp: Date.now(),
    };

    const assistantMsg: ChatMessage = {
      id: assistantMsgId,
      role: "assistant",
      content: "Analyzing workspace and planning atomic steps...",
      timestamp: Date.now() + 1,
      isStreaming: true,
      events: [],
    };

    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setIsLoading(true);

    try {
      const isDryRun = mode === "dry_run";
      const isPlanOnly = mode === "plan_only"; // now backed by a real engine field
      const parallelEnabled = parallel; // independent steps may run concurrently
      // The engine caps titles at 200 chars; send the full prompt as the
      // description so the planner sees every word regardless of length.
      const goal = await createGoal(
        wsToUse.id,
        promptText,
        promptText,
        isDryRun,
        selectedModel.provider,
        selectedModel.id,
        isPlanOnly,
        parallelEnabled
      );

      const fullGoal = await getGoal(goal.id);
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantMsgId ? { ...m, goal: fullGoal } : m))
      );

      // Connect WebSocket telemetry
      subscribeToGoal(goal.id, assistantMsgId);
    } catch (err: any) {
      const detail = err?.message || "Failed to dispatch agent";
      setError(detail);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsgId
            ? {
                ...m,
                content: `Execution failed: ${detail}`,
                isStreaming: false,
              }
            : m
        )
      );
    } finally {
      setIsLoading(false);
    }
    return true;
  };

  const handleEnableExecution = async (goalId: string, version: number) => {
    try {
      await enableExecution(goalId, version);
      let refreshed = await getGoal(goalId);
      // "Execute Plan" is one click: lift the guard, then start immediately.
      if (refreshed.status === "PENDING") {
        await startGoal(goalId, refreshed.version);
        refreshed = await getGoal(goalId);
      }
      setMessages((prev) =>
        prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: refreshed } : m))
      );
    } catch (err: any) {
      setError(err?.message || "Failed to enable execution");
    }
  };

  const handleApplyGoal = async (goalId: string) => {
    setError(null);
    try {
      await applyGoal(goalId);
      // The stream closed when the dry-run goal reached COMPLETED; open a new
      // one for the apply run. Find the assistant message carrying this goal.
      const msg = messages.find((m) => m.goal?.id === goalId);
      if (msg) {
        delete goalStreams.current[goalId];
        // Floor the replay at the last event already rendered so the server's
        // event replay can't close the stream before live events arrive.
        const lastSeq = Math.max(0, ...(msg.events || []).map((e) => e.sequence));
        subscribeToGoal(goalId, msg.id, lastSeq);
      }
      const refreshed = await getGoal(goalId);
      setMessages((prev) =>
        prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: refreshed } : m))
      );
    } catch (err: any) {
      setError(err?.message || "Failed to apply changes");
    }
  };

  const handleEditStep = useCallback(
    async (
      goalId: string,
      stepId: string,
      expectedVersion: number,
      patch: { title?: string; description?: string; suggested_paths?: string[] }
    ): Promise<boolean> => {
      setError(null);
      try {
        await patchStep(goalId, stepId, expectedVersion, patch);
        const refreshed = await getGoal(goalId);
        setMessages((prev) =>
          prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: refreshed } : m))
        );
        return true;
      } catch (err: any) {
        setError(err?.message || "Failed to update plan step");
        // False leaves the editor open with the user's edits intact: a refused
        // save (conflict, engine down) must not destroy what they typed.
        return false;
      }
    },
    []
  );

  // These four report through the app's error banner rather than `alert()`: a
  // blocking OS dialog is unstyled, unselectable, and invisible in a screenshot the
  // moment it is dismissed — and every other failure in this file already uses the
  // banner.
  const handleStartGoal = async (goalId: string, version: number) => {
    setError(null);
    try {
      await startGoal(goalId, version);
      const refreshed = await getGoal(goalId);
      setMessages((prev) =>
        prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: refreshed } : m))
      );
    } catch (err: any) {
      setError(err?.message || "Failed to start goal");
    }
  };

  const handlePauseGoal = async (goalId: string, version: number) => {
    setError(null);
    try {
      await pauseGoal(goalId, version);
      const refreshed = await getGoal(goalId);
      setMessages((prev) =>
        prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: refreshed } : m))
      );
    } catch (err: any) {
      setError(err?.message || "Failed to pause goal");
    }
  };

  const handleCancelGoal = async (goalId: string, version: number) => {
    setError(null);
    try {
      await cancelGoal(goalId, version);
      const refreshed = await getGoal(goalId);
      setMessages((prev) =>
        prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: refreshed } : m))
      );
    } catch (err: any) {
      setError(err?.message || "Failed to cancel goal");
    }
  };

  const handleRetryStep = async (goalId: string, stepId: string, version: number) => {
    setError(null);
    try {
      await retryStep(goalId, stepId, version);
      // A failed step belongs to a terminal goal, so its stream has already closed
      // itself and been dropped (`onTerminal`). Without re-opening one, the retry
      // runs with no live events in the chat at all — the exact bug that comment
      // warns about. The floor keeps the server's replay from closing the new
      // stream before the retry's own events arrive.
      const msg = messages.find((m) => m.goal?.id === goalId);
      if (msg) {
        delete goalStreams.current[goalId];
        const lastSeq = Math.max(0, ...(msg.events || []).map((e) => e.sequence));
        subscribeToGoal(goalId, msg.id, lastSeq);
      }
      const refreshed = await getGoal(goalId);
      setMessages((prev) =>
        prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: refreshed } : m))
      );
    } catch (err: any) {
      setError(err?.message || "Failed to retry step");
    }
  };

  return (
    // select-none REMOVED so text cursor and selection work normally in WebKitGTK
    <div className="flex flex-col h-screen bg-[#0d1117] text-gray-200 font-sans">
      {/* Top Header Bar */}
      <header className="bg-[#161b22] border-b border-[#30363d] px-4 py-2.5 flex items-center justify-between z-10 flex-shrink-0">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 font-bold text-sm tracking-tight text-white">
            <div className="w-6 h-6 rounded-lg bg-blue-600 flex items-center justify-center text-white shadow">
              <Code className="w-3.5 h-3.5" />
            </div>
            <span>CODIFY</span>
          </div>

          {selectedWs && (
            <div className="flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-[#21262d] border border-[#30363d] text-xs text-gray-300">
              <FolderGit2 className="w-3.5 h-3.5 text-blue-400" />
              <span className="font-semibold">{selectedWs.name}</span>
              <span className="text-[10px] text-gray-500 font-mono hidden md:inline truncate max-w-xs">
                ({selectedWs.root_path})
              </span>
            </div>
          )}
        </div>

        <div className="flex items-center gap-3">
          {/* Live engine connection indicator (probes /health every 3s) */}
          <button
            type="button"
            onClick={() => setIsSettingsOpen(true)}
            title={
              engineUp === false
                ? "Engine is not responding — click to check settings"
                : engineUp && authOk === false
                ? "Engine is up but the auth token is not accepted — click to check settings"
                : "Engine connected"
            }
            className={`flex items-center gap-1.5 text-[11px] font-mono px-2.5 py-1 rounded-full border transition-colors cursor-pointer ${
              engineUp === false
                ? "bg-red-950/40 text-red-400 border-red-800"
                : engineUp && authOk === false
                ? "bg-amber-950/40 text-amber-400 border-amber-800"
                : "bg-[#0d1117] text-gray-400 border-[#30363d]"
            }`}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                engineUp === false
                  ? "bg-red-500"
                  : engineUp && authOk === false
                  ? "bg-amber-500"
                  : "bg-green-500 animate-pulse"
              }`}
            />
            <span>
              {engineUp === false
                ? "Engine Offline"
                : engineUp && authOk === false
                ? "Auth Stale"
                : `Port ${engine.port}`}
            </span>
          </button>

          <button
            type="button"
            onClick={() => setIsSettingsOpen(true)}
            className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-semibold rounded-lg bg-[#21262d] hover:bg-[#30363d] text-gray-200 border border-[#30363d] transition-colors cursor-pointer"
          >
            <Settings className="w-3.5 h-3.5 text-gray-400" />
            <span>Keys & Endpoints</span>
          </button>
        </div>
      </header>

      {/* Center Chat View */}
      <main className="flex-1 flex flex-col overflow-hidden relative">
        {error && (
          <div className="mx-auto mt-3 mb-1 w-full max-w-4xl px-4">
            <div className="flex items-start gap-2 p-2.5 rounded-xl bg-red-950/40 border border-red-800 text-xs text-red-300">
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-red-400" />
              <span className="leading-relaxed">{error}</span>
            </div>
          </div>
        )}
        <ChatTimeline
          messages={messages}
          onStartGoal={handleStartGoal}
          onEnableExecution={handleEnableExecution}
          onApplyGoal={handleApplyGoal}
          onEditStep={handleEditStep}
          onPauseGoal={handlePauseGoal}
          onCancelGoal={handleCancelGoal}
          onRetryStep={handleRetryStep}
          onQuickPrompt={(text) => handleSendMessage(text)}
          onOpenSettings={openSettings}
          onImportAudit={handleImportAudit}
        />

        {/* Bottom Pinned Command Center — pickers live on the toolbar's left
            edge; their menus open UP over the chat, never over the textarea. */}
        <BottomCommandBar
          workspaces={workspaces}
          selectedWorkspace={selectedWs}
          onSelectWorkspace={setSelectedWs}
          onBrowseWorkspace={handleBrowseWorkspace}
          onCreateWorkspace={handleCreateWorkspace}
          availableModels={modelCatalog.models}
          selectedModel={selectedModel}
          onSelectModel={setSelectedModel}
          modelStatus={modelCatalog.providers}
          modelSignals={modelSignals}
          modelsLoading={modelsLoading}
          onRefreshModels={() => loadModels(true)}
          mode={mode}
          onChangeMode={setMode}
          parallel={parallel}
          onToggleParallel={setParallel}
          onSubmit={handleSendMessage}
          isLoading={isLoading}
          onOpenSettings={() => openSettings("keys")}
        />
      </main>

      {/* Global Settings Modal */}
      <SettingsModal
        isOpen={isSettingsOpen}
        initialTab={settingsTab}
        recentRuns={recentRuns}
        onClose={() => {
          setIsSettingsOpen(false);
          // Keys or endpoints may have changed — re-discover, don't re-read cache.
          loadModels(true);
          // A model may have been reassigned to a role in there, which changes
          // which ids the menus lead with.
          loadModelSignals();
        }}
      />
    </div>
  );
};
export default App;
