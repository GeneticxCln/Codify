import React, { useState, useEffect, useCallback } from "react";
import { Workspace, Goal } from "./types";
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
} from "./api";
import { WorkspaceSelector } from "./components/WorkspaceSelector";
import { GoalCreator } from "./components/GoalCreator";
import { GoalDetailView } from "./components/GoalDetailView";
import { SettingsPanel } from "./components/SettingsPanel";
import { Code, Sliders, Target, Shield } from "lucide-react";

export const App: React.FC = () => {
  const [tab, setTab] = useState<"goals" | "settings">("goals");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWs, setSelectedWs] = useState<Workspace | undefined>();
  const [activeGoal, setActiveGoal] = useState<Goal | undefined>();

  // Load workspaces
  const loadWorkspaces = useCallback(async () => {
    try {
      const wsList = await listWorkspaces();
      setWorkspaces(wsList);
      if (wsList.length > 0 && !selectedWs) {
        setSelectedWs(wsList[0]);
      }
    } catch (err) {
      console.error("Failed to load workspaces", err);
    }
  }, [selectedWs]);

  useEffect(() => {
    loadWorkspaces();
  }, [loadWorkspaces]);

  // Refresh active goal periodically
  useEffect(() => {
    if (!activeGoal) return;
    const interval = setInterval(async () => {
      try {
        const refreshed = await getGoal(activeGoal.id);
        setActiveGoal(refreshed);
      } catch (err) {
        // Ignore polling error
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [activeGoal?.id]);

  const handleCreateWorkspace = async (name: string, root_path: string) => {
    const ws = await createWorkspace(name, root_path);
    setWorkspaces((prev) => [...prev, ws]);
    setSelectedWs(ws);
  };

  const handleCreateGoal = async (title: string, description: string, dry_run: boolean) => {
    if (!selectedWs) return;
    const goal = await createGoal(selectedWs.id, title, description, dry_run);
    const full = await getGoal(goal.id);
    setActiveGoal(full);
  };

  const handleStartGoal = async () => {
    if (!activeGoal) return;
    const updated = await startGoal(activeGoal.id, activeGoal.version);
    setActiveGoal({ ...activeGoal, ...updated });
  };

  const handlePauseGoal = async () => {
    if (!activeGoal) return;
    const updated = await pauseGoal(activeGoal.id, activeGoal.version);
    setActiveGoal({ ...activeGoal, ...updated });
  };

  const handleCancelGoal = async () => {
    if (!activeGoal) return;
    const updated = await cancelGoal(activeGoal.id, activeGoal.version);
    setActiveGoal({ ...activeGoal, ...updated });
  };

  const handleRetryStep = async (stepId: string) => {
    if (!activeGoal) return;
    await retryStep(activeGoal.id, stepId, activeGoal.version);
    const refreshed = await getGoal(activeGoal.id);
    setActiveGoal(refreshed);
  };

  const engine = getEngineInfo();

  return (
    <div className="flex flex-col h-screen bg-[#0d1117] text-gray-200">
      {/* Top Navbar */}
      <header className="bg-[#161b22] border-b border-[#30363d] px-5 py-2.5 flex items-center justify-between z-10 flex-shrink-0">
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-2 font-bold text-base tracking-tight text-white">
            <div className="w-7 h-7 rounded bg-blue-600 flex items-center justify-center text-white shadow">
              <Code className="w-4 h-4" />
            </div>
            <span>CODIFY</span>
          </div>

          <nav className="flex items-center gap-1 bg-[#0d1117] p-1 rounded-lg border border-[#30363d]">
            <button
              onClick={() => setTab("goals")}
              className={`flex items-center gap-1.5 px-3 py-1 text-xs font-semibold rounded transition-colors ${
                tab === "goals"
                  ? "bg-[#21262d] text-white shadow-sm"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              <Target className="w-3.5 h-3.5 text-blue-400" />
              Goals & Execution
            </button>
            <button
              onClick={() => setTab("settings")}
              className={`flex items-center gap-1.5 px-3 py-1 text-xs font-semibold rounded transition-colors ${
                tab === "settings"
                  ? "bg-[#21262d] text-white shadow-sm"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              <Sliders className="w-3.5 h-3.5 text-purple-400" />
              Settings (Agents)
            </button>
          </nav>
        </div>

        <div className="flex items-center gap-4">
          {tab === "goals" && (
            <WorkspaceSelector
              workspaces={workspaces}
              selectedWorkspace={selectedWs}
              onSelect={setSelectedWs}
              onCreate={handleCreateWorkspace}
            />
          )}

          <div className="flex items-center gap-2 border-l border-[#30363d] pl-4 text-[11px] font-mono text-gray-400">
            <span className="w-2 h-2 rounded-full bg-green-500" />
            <span>Port: {engine.port}</span>
          </div>
        </div>
      </header>

      {/* Content Area */}
      <main className="flex-1 overflow-y-auto">
        {tab === "settings" ? (
          <SettingsPanel />
        ) : (
          <div className="max-w-7xl mx-auto py-6 px-6 flex flex-col gap-6">
            {!selectedWs ? (
              <div className="p-12 text-center flex flex-col items-center justify-center gap-4 border border-dashed border-[#30363d] rounded-xl my-12">
                <Shield className="w-10 h-10 text-gray-500" />
                <h3 className="text-base font-bold text-gray-200">No Workspace Selected</h3>
                <p className="text-xs text-gray-400 max-w-sm">
                  Please register or select a local workspace directory to start orchestrating coding goals.
                </p>
              </div>
            ) : (
              <>
                <div className="flex items-center justify-between border-b border-[#21262d] pb-4">
                  <div>
                    <h2 className="text-xl font-bold text-gray-100 flex items-center gap-2">
                      <span>{selectedWs.name}</span>
                      <span className="text-xs font-mono font-normal text-gray-400">
                        ({selectedWs.root_path})
                      </span>
                    </h2>
                    <p className="text-xs text-gray-400">
                      Multi-agent goal dispatch and telemetry dashboard
                    </p>
                  </div>

                  <GoalCreator
                    workspaceId={selectedWs.id}
                    onCreateGoal={handleCreateGoal}
                  />
                </div>

                {activeGoal ? (
                  <GoalDetailView
                    goal={activeGoal}
                    onStart={handleStartGoal}
                    onPause={handlePauseGoal}
                    onCancel={handleCancelGoal}
                    onRetryStep={handleRetryStep}
                  />
                ) : (
                  <div className="p-16 text-center border border-dashed border-[#30363d] rounded-xl flex flex-col items-center justify-center gap-3">
                    <Target className="w-10 h-10 text-gray-600" />
                    <h4 className="text-sm font-semibold text-gray-300">No Active Goal Selected</h4>
                    <p className="text-xs text-gray-500 max-w-sm">
                      Create a goal using the "New Goal" button above to engage the Planner and execute code changes.
                    </p>
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </main>
    </div>
  );
};
