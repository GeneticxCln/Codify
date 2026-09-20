import React, { useState } from "react";
import { Goal } from "../types";
import { PlanStepList } from "./PlanStepList";
import { LiveEventStream } from "./LiveEventStream";
import { Play, Pause, XCircle, AlertTriangle } from "lucide-react";

interface GoalDetailViewProps {
  goal: Goal;
  onStart: () => Promise<void>;
  onPause: () => Promise<void>;
  onCancel: () => Promise<void>;
  onRetryStep: (stepId: string) => Promise<void>;
}

export const GoalDetailView: React.FC<GoalDetailViewProps> = ({
  goal,
  onStart,
  onPause,
  onCancel,
  onRetryStep,
}) => {
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runAction = async (action: () => Promise<void>) => {
    setActionLoading(true);
    setError(null);
    try {
      await action();
    } catch (err: any) {
      setError(err.message || "Action failed");
    } finally {
      setActionLoading(false);
    }
  };

  const canStart = goal.status === "PENDING" || goal.status === "PAUSED";
  const canPause = goal.status === "RUNNING";
  const canCancel =
    goal.status === "RUNNING" || goal.status === "PAUSED" || goal.status === "PENDING";

  return (
    <div className="flex flex-col gap-6">
      {/* Header Card */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-5 flex flex-col gap-4">
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <span className="text-xs px-2.5 py-0.5 rounded-full border border-blue-800 bg-blue-950/40 text-blue-400 font-semibold font-mono">
                {goal.status}
              </span>
              {goal.dry_run && (
                <span className="text-xs px-2 py-0.5 rounded border border-amber-800 bg-amber-950/40 text-amber-400 font-semibold">
                  DRY RUN
                </span>
              )}
              <span className="text-xs text-gray-500 font-mono">v{goal.version}</span>
            </div>
            <h2 className="text-lg font-bold text-gray-100">{goal.title}</h2>
            {goal.description && (
              <p className="text-xs text-gray-400 mt-1">{goal.description}</p>
            )}
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2">
            {canStart && (
              <button
                onClick={() => runAction(onStart)}
                disabled={actionLoading}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 hover:bg-green-500 text-white text-xs font-semibold rounded shadow-sm transition-colors disabled:opacity-50"
              >
                <Play className="w-3.5 h-3.5" /> Start Execution
              </button>
            )}

            {canPause && (
              <button
                onClick={() => runAction(onPause)}
                disabled={actionLoading}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-600 hover:bg-amber-500 text-white text-xs font-semibold rounded shadow-sm transition-colors disabled:opacity-50"
              >
                <Pause className="w-3.5 h-3.5" /> Pause
              </button>
            )}

            {canCancel && (
              <button
                onClick={() => runAction(onCancel)}
                disabled={actionLoading}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-[#21262d] hover:bg-red-900/40 text-red-400 border border-[#30363d] text-xs font-semibold rounded transition-colors disabled:opacity-50"
              >
                <XCircle className="w-3.5 h-3.5" /> Cancel
              </button>
            )}
          </div>
        </div>

        {error && (
          <div className="bg-red-950/30 border border-red-800 text-red-300 px-3 py-2 rounded text-xs flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-red-400" />
            <span>{error}</span>
          </div>
        )}
      </div>

      {/* Main Grid: Steps on Left, Live WebSocket Stream on Right */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="flex flex-col gap-3">
          <h3 className="text-sm font-bold text-gray-300 uppercase tracking-wider">
            Execution Plan ({goal.steps?.length || 0} Steps)
          </h3>
          <PlanStepList
            steps={goal.steps || []}
            goalStatus={goal.status}
            onRetryStep={(stepId) => runAction(() => onRetryStep(stepId))}
          />
        </div>

        <div className="flex flex-col gap-3">
          <h3 className="text-sm font-bold text-gray-300 uppercase tracking-wider">
            Live Telemetry
          </h3>
          <LiveEventStream goalId={goal.id} />
        </div>
      </div>
    </div>
  );
};
