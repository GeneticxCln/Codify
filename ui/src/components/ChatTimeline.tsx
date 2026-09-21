import React, { useRef, useEffect } from "react";
import { ChatMessage, PlanStep } from "../types";
import { DiffViewer } from "./DiffViewer";
import {
  User,
  Bot,
  Sparkles,
  CheckCircle2,
  Clock,
  PlayCircle,
  AlertCircle,
  Terminal,
  Play,
  Pause,
  RotateCcw,
  XCircle,
  FileCode,
  ShieldCheck,
  Check,
} from "lucide-react";

interface ChatTimelineProps {
  messages: ChatMessage[];
  onStartGoal: (goalId: string, version: number) => void;
  onPauseGoal: (goalId: string, version: number) => void;
  onCancelGoal: (goalId: string, version: number) => void;
  onRetryStep: (goalId: string, stepId: string, version: number) => void;
  onQuickPrompt: (prompt: string) => void;
}

export const ChatTimeline: React.FC<ChatTimelineProps> = ({
  messages,
  onStartGoal,
  onPauseGoal,
  onCancelGoal,
  onRetryStep,
  onQuickPrompt,
}) => {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center p-6 text-center max-w-2xl mx-auto">
        <div className="w-12 h-12 rounded-2xl bg-blue-600/10 border border-blue-500/20 flex items-center justify-center text-blue-400 mb-4 shadow-inner">
          <Sparkles className="w-6 h-6" />
        </div>
        <h2 className="text-xl font-bold text-gray-100 mb-2">
          What would you like to build or fix?
        </h2>
        <p className="text-xs text-gray-400 max-w-md mb-8 leading-relaxed">
          Select a project folder and your preferred model below. Codify will inspect your codebase, plan atomic steps, propose file diffs, and verify tests automatically.
        </p>

        {/* Quick Suggestion Pills */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 w-full">
          {[
            "Add JWT authentication and protected routes",
            "Find failing tests and fix syntax errors",
            "Refactor API error handling and logging",
            "Generate unit tests for core services",
          ].map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => onQuickPrompt(suggestion)}
              className="text-left p-3 rounded-xl bg-[#161b22] border border-[#30363d] hover:border-blue-500/40 hover:bg-[#21262d] transition-all text-xs text-gray-300 group"
            >
              <div className="flex items-center gap-2 font-medium text-gray-200 group-hover:text-blue-400">
                <FileCode className="w-3.5 h-3.5 text-blue-400 flex-shrink-0" />
                <span className="truncate">{suggestion}</span>
              </div>
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-4 sm:p-6 space-y-6 max-w-4xl mx-auto w-full">
      {messages.map((msg) => (
        <div key={msg.id} className="space-y-4">
          {/* User Message */}
          {msg.role === "user" ? (
            <div className="flex items-start gap-3 justify-end">
              <div className="bg-blue-600 text-white px-4 py-2.5 rounded-2xl rounded-tr-sm max-w-xl text-sm leading-relaxed shadow-sm">
                {msg.content}
              </div>
              <div className="w-7 h-7 rounded-full bg-blue-500/20 border border-blue-400/30 flex items-center justify-center text-blue-300 flex-shrink-0">
                <User className="w-4 h-4" />
              </div>
            </div>
          ) : (
            /* Assistant Execution Card */
            <div className="flex items-start gap-3">
              <div className="w-7 h-7 rounded-full bg-purple-500/20 border border-purple-400/30 flex items-center justify-center text-purple-300 flex-shrink-0 mt-1">
                <Bot className="w-4 h-4" />
              </div>

              <div className="flex-1 bg-[#161b22] border border-[#30363d] rounded-2xl p-4 sm:p-5 shadow-lg space-y-4">
                {/* Header: Title and Status */}
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#30363d]/60 pb-3">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-sm text-gray-200">
                      {msg.goal?.title || "Agent Execution"}
                    </span>
                    {msg.goal && (
                      <span
                        className={`text-[10px] font-mono px-2 py-0.5 rounded-full border ${
                          msg.goal.status === "COMPLETED"
                            ? "bg-green-950/40 text-green-400 border-green-800"
                            : msg.goal.status === "RUNNING"
                            ? "bg-blue-950/40 text-blue-400 border-blue-800 animate-pulse"
                            : msg.goal.status === "PAUSED"
                            ? "bg-amber-950/40 text-amber-400 border-amber-800"
                            : msg.goal.status === "FAILED"
                            ? "bg-red-950/40 text-red-400 border-red-800"
                            : "bg-[#21262d] text-gray-400 border-[#30363d]"
                        }`}
                      >
                        {msg.goal.status}
                      </span>
                    )}
                    {msg.goal?.dry_run && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-950/40 border border-amber-800 text-amber-300 font-semibold">
                        DRY RUN
                      </span>
                    )}
                  </div>

                  {/* Goal Action Buttons */}
                  {msg.goal && (
                    <div className="flex items-center gap-1.5">
                      {(msg.goal.status === "PENDING" || msg.goal.status === "PAUSED") && (
                        <button
                          type="button"
                          onClick={() => onStartGoal(msg.goal!.id, msg.goal!.version)}
                          className="flex items-center gap-1 px-2.5 py-1 bg-green-600 hover:bg-green-500 text-white text-xs font-semibold rounded-lg shadow transition-colors"
                        >
                          <Play className="w-3 h-3" /> Start
                        </button>
                      )}

                      {msg.goal.status === "RUNNING" && (
                        <button
                          type="button"
                          onClick={() => onPauseGoal(msg.goal!.id, msg.goal!.version)}
                          className="flex items-center gap-1 px-2.5 py-1 bg-amber-600 hover:bg-amber-500 text-white text-xs font-semibold rounded-lg shadow transition-colors"
                        >
                          <Pause className="w-3 h-3" /> Pause
                        </button>
                      )}

                      {(msg.goal.status === "RUNNING" || msg.goal.status === "PENDING" || msg.goal.status === "PAUSED") && (
                        <button
                          type="button"
                          onClick={() => onCancelGoal(msg.goal!.id, msg.goal!.version)}
                          className="p-1 text-gray-400 hover:text-red-400 rounded-lg transition-colors"
                          title="Cancel Goal"
                        >
                          <XCircle className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  )}
                </div>

                {/* Plan Steps Accordion */}
                {msg.goal?.steps && msg.goal.steps.length > 0 && (
                  <div className="space-y-2">
                    <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
                      Execution Plan ({msg.goal.steps.length} Steps)
                    </div>
                    <div className="space-y-2">
                      {msg.goal.steps.map((step: PlanStep) => (
                        <div
                          key={step.id}
                          className="bg-[#0d1117] border border-[#30363d] rounded-xl p-3 flex flex-col gap-1.5"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <div className="flex items-center gap-2">
                              {step.status === "COMPLETED" && (
                                <CheckCircle2 className="w-4 h-4 text-green-400 flex-shrink-0" />
                              )}
                              {step.status === "IN_PROGRESS" && (
                                <PlayCircle className="w-4 h-4 text-blue-400 flex-shrink-0 animate-pulse" />
                              )}
                              {step.status === "PENDING" && (
                                <Clock className="w-4 h-4 text-gray-500 flex-shrink-0" />
                              )}
                              {step.status === "FAILED" && (
                                <AlertCircle className="w-4 h-4 text-red-400 flex-shrink-0" />
                              )}
                              <span className="text-xs font-semibold text-gray-200">
                                Step {step.ordinal + 1}: {step.title}
                              </span>
                            </div>

                            {/* Step Status or Retry */}
                            <div className="flex items-center gap-2">
                              {step.status === "FAILED" || (step.status === "IN_PROGRESS" && step.review_notes) ? (
                                <button
                                  type="button"
                                  onClick={() => onRetryStep(msg.goal!.id, step.id, msg.goal!.version)}
                                  className="flex items-center gap-1 px-2 py-0.5 bg-amber-600/30 hover:bg-amber-600/50 text-amber-300 border border-amber-700/50 rounded text-[11px] font-semibold transition-colors"
                                >
                                  <RotateCcw className="w-3 h-3" /> Retry
                                </button>
                              ) : null}

                              <span className="text-[10px] uppercase font-mono px-1.5 py-0.5 rounded bg-[#161b22] text-gray-400">
                                {step.status}
                              </span>
                            </div>
                          </div>

                          <p className="text-xs text-gray-400 pl-6 leading-relaxed">
                            {step.description}
                          </p>

                          {step.review_notes && (
                            <div className="ml-6 mt-1 p-2 rounded-lg bg-amber-950/30 border border-amber-800/60 text-xs text-amber-300 flex items-start gap-1.5">
                              <ShieldCheck className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                              <div>
                                <span className="font-semibold">Reviewer Notes: </span>
                                {step.review_notes}
                              </div>
                            </div>
                          )}

                          {step.commit_message && (
                            <div className="ml-6 mt-1 text-[11px] font-mono text-gray-400 flex items-center gap-1">
                              <Check className="w-3 h-3 text-green-400" />
                              <span>Commit: {step.commit_message}</span>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Event Stream (Diffs & Results) */}
                {msg.events && msg.events.length > 0 && (
                  <div className="space-y-3 pt-2 border-t border-[#30363d]/60">
                    <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider flex items-center gap-1.5">
                      <Terminal className="w-3.5 h-3.5" /> Telemetry & Changes
                    </div>

                    <div className="space-y-2">
                      {msg.events.map((ev) => (
                        <div key={ev.sequence} className="text-xs">
                          {ev.type === "diff" && (
                            <DiffViewer
                              path={ev.payload.path}
                              diffText={ev.payload.unified_diff}
                            />
                          )}

                          {ev.type === "test_result" && (
                            <div className="p-2.5 rounded-lg bg-[#0d1117] border border-[#30363d] flex flex-col gap-1">
                              <div className="flex items-center gap-2">
                                <span
                                  className={`font-semibold uppercase text-[11px] px-1.5 py-0.5 rounded ${
                                    ev.payload.verdict === "pass"
                                      ? "bg-green-950 text-green-300 border border-green-800"
                                      : "bg-red-950 text-red-300 border border-red-800"
                                  }`}
                                >
                                  {ev.payload.verdict}
                                </span>
                                {ev.payload.argv && (
                                  <span className="font-mono text-gray-400">
                                    {ev.payload.argv.join(" ")}
                                  </span>
                                )}
                              </div>
                              {ev.payload.explanation && (
                                <p className="text-gray-400 pl-1">{ev.payload.explanation}</p>
                              )}
                            </div>
                          )}

                          {ev.type === "log" && (
                            <div
                              className={`font-mono text-[11px] pl-2 border-l-2 ${
                                ev.payload.level === "warn"
                                  ? "border-amber-500 text-amber-300"
                                  : ev.payload.level === "error"
                                  ? "border-red-500 text-red-300"
                                  : "border-blue-500 text-gray-300"
                              }`}
                            >
                              {ev.payload.message}
                            </div>
                          )}

                          {ev.type === "error" && (
                            <div className="p-2 rounded-lg bg-red-950/40 border border-red-800 text-red-300 font-semibold">
                              Error [{ev.payload.code}]: {ev.payload.message}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
};
