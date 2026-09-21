import React, { useState, useEffect, useCallback, useRef } from "react";
import { Workspace, ChatMessage, EngineInfo, Event, ModelOption } from "./types";
import {
  listWorkspaces,
  createWorkspace,
  createGoal,
  getGoal,
  startGoal,
  pauseGoal,
  cancelGoal,
  retryStep,
  getEngineInfo,
  setEngineInfo,
  tauriInvoke,
  browseWorkspace,
  fetchAvailableModels,
} from "./api";
import { BottomCommandBar, AVAILABLE_MODELS, ExecutionMode } from "./components/BottomCommandBar";
import { ChatTimeline } from "./components/ChatTimeline";
import { SettingsModal } from "./components/SettingsModal";
import { Code, Settings, FolderGit2 } from "lucide-react";

export const App: React.FC = () => {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWs, setSelectedWs] = useState<Workspace | undefined>();
  const [availableModels, setAvailableModels] = useState<ModelOption[]>(AVAILABLE_MODELS);
  const [selectedModel, setSelectedModel] = useState<ModelOption>(AVAILABLE_MODELS[0]);
  const [mode, setMode] = useState<ExecutionMode>("direct");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [engine, setEngine] = useState<EngineInfo>(getEngineInfo());

  const activeSockets = useRef<Record<string, WebSocket>>({});

  // Fetch real engine info from Tauri IPC on mount with retry
  useEffect(() => {
    let attempts = 0;
    const fetchInfo = () => {
      tauriInvoke<EngineInfo>("codify_get_engine_info")
        .then((info) => {
          setEngineInfo(info);
          setEngine(info);
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

  // Load workspaces and auto-select or auto-seed
  const loadWorkspaces = useCallback(async () => {
    try {
      const wsList = await listWorkspaces();
      setWorkspaces(wsList);
      if (wsList.length > 0) {
        if (!selectedWs) {
          setSelectedWs(wsList[0]);
        }
      } else {
        // Auto-seed current project directory so workspace is never null
        const defaultWs = await createWorkspace("Codify", "/home/quinton/Projects/Codify");
        setWorkspaces([defaultWs]);
        setSelectedWs(defaultWs);
      }
    } catch (err) {
      // Backend might still be starting
    }
  }, [selectedWs]);

  // Load available models (detects local Ollama models like qwen2.5-coder!)
  const loadModels = useCallback(async () => {
    try {
      const models = await fetchAvailableModels();
      if (models && models.length > 0) {
        setAvailableModels(models);
        // Prefer installed local Ollama model if available
        const localModel = models.find((m) => m.provider === "ollama");
        if (localModel) {
          setSelectedModel(localModel);
        } else {
          setSelectedModel(models[0]);
        }
      }
    } catch (err) {
      // Keep defaults
    }
  }, []);

  useEffect(() => {
    loadWorkspaces();
    loadModels();
  }, [loadWorkspaces, loadModels]);

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

  // Subscribe to live goal events via WebSocket
  const subscribeToGoal = useCallback(
    (goalId: string, messageId: string) => {
      if (activeSockets.current[goalId]) return;

      const engineInfo = getEngineInfo();
      const wsUrl = `ws://127.0.0.1:${engineInfo.port}/ws/goals/${goalId}`;
      const ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        ws.send(JSON.stringify({ type: "auth", token: engineInfo.token }));
      };

      ws.onmessage = (e) => {
        try {
          const ev: Event = JSON.parse(e.data);
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

          if (ev.type === "goal_status" || ev.type === "step_status") {
            getGoal(goalId).then((refreshed) => {
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === messageId ? { ...msg, goal: refreshed } : msg
                )
              );
            });
          }
        } catch (err) {
          console.error("WS event parse error", err);
        }
      };

      activeSockets.current[goalId] = ws;
    },
    []
  );

  // Poll active goals periodically
  useEffect(() => {
    const interval = setInterval(() => {
      messages.forEach(async (msg) => {
        if (msg.goal && (msg.goal.status === "PLANNING" || msg.goal.status === "RUNNING")) {
          try {
            const refreshed = await getGoal(msg.goal.id);
            setMessages((prev) =>
              prev.map((m) => (m.id === msg.id ? { ...m, goal: refreshed } : m))
            );

            // Auto-start if mode is direct and planning just finished
            if (
              mode === "direct" &&
              msg.goal.status === "PLANNING" &&
              refreshed.status === "PENDING"
            ) {
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

  const handleSendMessage = async (promptText: string) => {
    let wsToUse = selectedWs;
    if (!wsToUse) {
      // Auto-create workspace if none selected
      try {
        wsToUse = await createWorkspace("Codify", "/home/quinton/Projects/Codify");
        setWorkspaces([wsToUse]);
        setSelectedWs(wsToUse);
      } catch (err) {
        // Fall back to first
        wsToUse = workspaces[0];
      }
    }
    if (!wsToUse) return;

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
      const goal = await createGoal(
        wsToUse.id,
        promptText,
        "",
        isDryRun,
        selectedModel.provider,
        selectedModel.id
      );

      const fullGoal = await getGoal(goal.id);
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantMsgId ? { ...m, goal: fullGoal } : m))
      );

      // Connect WebSocket telemetry
      subscribeToGoal(goal.id, assistantMsgId);
    } catch (err: any) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsgId
            ? {
                ...m,
                content: `Execution failed: ${err.message || "Failed to dispatch agent"}`,
                isStreaming: false,
              }
            : m
        )
      );
    } finally {
      setIsLoading(false);
    }
  };

  const handleStartGoal = async (goalId: string, version: number) => {
    try {
      await startGoal(goalId, version);
      const refreshed = await getGoal(goalId);
      setMessages((prev) =>
        prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: refreshed } : m))
      );
    } catch (err: any) {
      alert(err.message || "Failed to start goal");
    }
  };

  const handlePauseGoal = async (goalId: string, version: number) => {
    try {
      await pauseGoal(goalId, version);
      const refreshed = await getGoal(goalId);
      setMessages((prev) =>
        prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: refreshed } : m))
      );
    } catch (err: any) {
      alert(err.message || "Failed to pause goal");
    }
  };

  const handleCancelGoal = async (goalId: string, version: number) => {
    try {
      await cancelGoal(goalId, version);
      const refreshed = await getGoal(goalId);
      setMessages((prev) =>
        prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: refreshed } : m))
      );
    } catch (err: any) {
      alert(err.message || "Failed to cancel goal");
    }
  };

  const handleRetryStep = async (goalId: string, stepId: string, version: number) => {
    try {
      await retryStep(goalId, stepId, version);
      const refreshed = await getGoal(goalId);
      setMessages((prev) =>
        prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: refreshed } : m))
      );
    } catch (err: any) {
      alert(err.message || "Failed to retry step");
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
          <div className="flex items-center gap-1.5 text-[11px] font-mono text-gray-400 bg-[#0d1117] px-2.5 py-1 rounded-full border border-[#30363d]">
            <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
            <span>Port {engine.port}</span>
          </div>

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
        <ChatTimeline
          messages={messages}
          onStartGoal={handleStartGoal}
          onPauseGoal={handlePauseGoal}
          onCancelGoal={handleCancelGoal}
          onRetryStep={handleRetryStep}
          onQuickPrompt={(text) => handleSendMessage(text)}
        />

        {/* Bottom Pinned Command Center */}
        <BottomCommandBar
          workspaces={workspaces}
          selectedWorkspace={selectedWs}
          onSelectWorkspace={setSelectedWs}
          onBrowseWorkspace={handleBrowseWorkspace}
          onCreateWorkspace={handleCreateWorkspace}
          availableModels={availableModels}
          selectedModel={selectedModel}
          onSelectModel={setSelectedModel}
          mode={mode}
          onChangeMode={setMode}
          onSubmit={handleSendMessage}
          isLoading={isLoading}
          onOpenSettings={() => setIsSettingsOpen(true)}
        />
      </main>

      {/* Global Settings Modal */}
      <SettingsModal
        isOpen={isSettingsOpen}
        onClose={() => {
          setIsSettingsOpen(false);
          loadModels();
        }}
      />
    </div>
  );
};
export default App;
