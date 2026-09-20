import React from "react";
import { PlanStep, StepStatus } from "../types";
import { CheckCircle2, Clock, PlayCircle, AlertCircle, RotateCcw, ShieldAlert } from "lucide-react";

interface PlanStepListProps {
  steps: PlanStep[];
  goalStatus: string;
  onRetryStep: (stepId: string) => void;
}

const STATUS_ICONS: Record<StepStatus, React.ReactNode> = {
  PENDING: <Clock className="w-4 h-4 text-gray-500" />,
  IN_PROGRESS: <PlayCircle className="w-4 h-4 text-blue-400 animate-pulse" />,
  COMPLETED: <CheckCircle2 className="w-4 h-4 text-green-400" />,
  FAILED: <AlertCircle className="w-4 h-4 text-red-400" />,
};

const STATUS_BADGE: Record<StepStatus, string> = {
  PENDING: "bg-[#21262d] text-gray-400 border-[#30363d]",
  IN_PROGRESS: "bg-blue-950/40 text-blue-300 border-blue-800",
  COMPLETED: "bg-green-950/40 text-green-300 border-green-800",
  FAILED: "bg-red-950/40 text-red-300 border-red-800",
};

export const PlanStepList: React.FC<PlanStepListProps> = ({ steps, goalStatus: _goalStatus, onRetryStep }) => {
  if (steps.length === 0) {
    return (
      <div className="p-8 text-center text-gray-500 text-sm border border-dashed border-[#30363d] rounded-lg">
        Planner has not generated steps yet.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {steps.map((step) => {
        const canRetry =
          step.status === "FAILED" || (step.status === "IN_PROGRESS" && Boolean(step.review_notes));

        return (
          <div
            key={step.id}
            className="bg-[#161b22] border border-[#30363d] rounded-lg p-4 flex flex-col gap-2.5 transition-colors"
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <span className="text-xs font-mono font-bold text-gray-400 bg-[#21262d] px-2 py-0.5 rounded border border-[#30363d]">
                  Step {step.ordinal + 1}
                </span>
                <h4 className="text-sm font-semibold text-gray-200">{step.title}</h4>
              </div>

              <div className="flex items-center gap-2">
                <span
                  className={`text-xs px-2.5 py-0.5 rounded-full border font-medium flex items-center gap-1.5 ${
                    STATUS_BADGE[step.status]
                  }`}
                >
                  {STATUS_ICONS[step.status]}
                  {step.status}
                </span>

                {canRetry && (
                  <button
                    onClick={() => onRetryStep(step.id)}
                    className="flex items-center gap-1 px-2 py-1 bg-amber-600 hover:bg-amber-500 text-white rounded text-xs font-semibold shadow-sm transition-colors"
                  >
                    <RotateCcw className="w-3.5 h-3.5" /> Retry Step
                  </button>
                )}
              </div>
            </div>

            <p className="text-xs text-gray-400">{step.description}</p>

            {step.suggested_paths.length > 0 && (
              <div className="flex items-center gap-1.5 flex-wrap pt-1">
                <span className="text-xs text-gray-500">Target paths:</span>
                {step.suggested_paths.map((p) => (
                  <span
                    key={p}
                    className="text-xs font-mono bg-[#0d1117] text-gray-300 px-1.5 py-0.5 rounded border border-[#30363d]"
                  >
                    {p}
                  </span>
                ))}
              </div>
            )}

            {step.review_notes && (
              <div className="mt-1 bg-amber-950/30 border border-amber-800/60 rounded p-2.5 text-xs text-amber-300 flex items-start gap-2">
                <ShieldAlert className="w-4 h-4 flex-shrink-0 mt-0.5 text-amber-400" />
                <div>
                  <div className="font-semibold text-amber-200">Reviewer Requested Changes:</div>
                  <div className="whitespace-pre-wrap">{step.review_notes}</div>
                </div>
              </div>
            )}

            {step.commit_message && (
              <div className="mt-1 bg-[#0d1117] border border-[#21262d] rounded p-2 text-xs font-mono text-gray-400 flex items-center gap-2">
                <span className="text-gray-500">Commit:</span>
                <span className="text-green-400">{step.commit_message}</span>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};
