import React, { useRef, useEffect, useState } from "react";
import { AgentRole, ChatMessage, PlanStep } from "../types";
import { FailureDiagnosisPanel } from "./FailureDiagnosisPanel";
import { DiffViewer } from "./DiffViewer";
import { LayaDecision } from "../types";
import { getGoalUsage, GoalUsage } from "../api";
import {
  User,
  Bot,
  Route,
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
  BookOpen,
  ShieldCheck,
  ShieldAlert,
  Check,
  Pencil,
  X,
  Coins,
} from "lucide-react";

/**
 * Laya's pre-flight verdict, rendered as one honest line of chat: which engine
 * answered, the typed answers it produced, and whether that tripped policy.
 *
 * Answers arrive either nested ({"intent": {"choice": …}}) or flat, so both
 * shapes are read here — same tolerance the engine applies.
 */
function layaAnswer(payload: Record<string, any> | undefined, key: string): unknown {
  const raw = payload?.answers?.[key];
  if (raw && typeof raw === "object") {
    return raw.choice ?? raw.score ?? raw.noul ?? null;
  }
  return raw ?? null;
}

function layaNumber(value: unknown): number | null {
  const n = typeof value === "string" ? parseFloat(value) : value;
  return typeof n === "number" && Number.isFinite(n) ? n : null;
}

const LayaGateCard: React.FC<{ payload: Record<string, any> }> = ({ payload }) => {
  const decision = payload as Partial<LayaDecision>;
  const engine = decision.engine ?? "skipped";
  const blocked = Boolean(decision.blocked);
  const warnings = decision.warnings ?? [];
  const injection = layaNumber(layaAnswer(payload, "prompt_injection"));
  const risk = layaNumber(layaAnswer(payload, "risk"));
  const intent = layaAnswer(payload, "intent");
  const threshold = payload?.policy?.injection_block_threshold;

  if (engine === "skipped") {
    return (
      <div className="flex items-start gap-1.5 pl-2 text-gray-500">
        <ShieldCheck className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
        <span>Laya gate skipped — {decision.skipped_reason || "no decision engine available"}</span>
      </div>
    );
  }

  const tone = blocked
    ? "text-red-300"
    : warnings.length
    ? "text-amber-300"
    : "text-emerald-300";
  const engineLabel = engine === "sdk" ? "Laya SDK" : "Laya via fallback model";

  return (
    <div className={`flex flex-col gap-0.5 pl-2 ${tone}`}>
      <div className="flex items-center gap-1.5">
        <ShieldCheck className="w-3.5 h-3.5 flex-shrink-0" />
        <span>
          Laya gate <span className="font-semibold">{blocked ? "blocked" : "passed"}</span>
          <span className="font-mono text-gray-500"> ({engineLabel})</span>
        </span>
      </div>
      <div className="pl-5 text-[11px] text-gray-400 flex flex-wrap gap-x-3 gap-y-0.5">
        {intent !== null && intent !== undefined && (
          <span>
            intent <span className="text-gray-200">{String(intent)}</span>
          </span>
        )}
        {risk !== null && <span>risk <span className="text-gray-200">{risk.toFixed(2)}</span></span>}
        {injection !== null && (
          <span>
            injection <span className="text-gray-200">{injection.toFixed(2)}</span>
            {typeof threshold === "number" && (
              <span className="text-gray-600"> / block at {threshold}</span>
            )}
          </span>
        )}
      </div>
      {decision.block_reason && <div className="pl-5 text-[11px]">{decision.block_reason}</div>}
      {warnings.map((w, i) => (
        <div key={i} className="pl-5 text-[11px] text-amber-400">
          {w}
        </div>
      ))}
    </div>
  );
};

/** Edit a plan step's title, description, and target paths before execution. */
const PlanStepEditor: React.FC<{
  step: PlanStep;
  version: number;
  onSave: (patch: {
    title?: string;
    description?: string;
    suggested_paths?: string[];
  }) => boolean | void | Promise<boolean | void>;
  onCancel: () => void;
}> = ({ step, version, onSave, onCancel }) => {
  const [title, setTitle] = useState(step.title);
  const [description, setDescription] = useState(step.description);
  const [paths, setPaths] = useState(step.suggested_paths.join(", "));
  const [saving, setSaving] = useState(false);

  const inputCls =
    "w-full bg-[#161b22] border border-[#30363d] rounded-lg px-2.5 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500/60";

  const handleSave = async () => {
    // Send only what the user actually changed, so the engine's plan_updated
    // event names the fields that moved (and a no-change save does not
    // reannounce the whole step).
    const patch: {
      title?: string;
      description?: string;
      suggested_paths?: string[];
    } = {};
    if (title.trim() && title.trim() !== step.title) patch.title = title.trim();
    if (description.trim() && description.trim() !== step.description)
      patch.description = description.trim();
    if (paths !== step.suggested_paths.join(", "))
      patch.suggested_paths = paths.split(",").map((p) => p.trim()).filter(Boolean);
    if (Object.keys(patch).length === 0) {
      // Nothing changed: closing is the whole job, and calling the engine with
      // an empty patch would only earn a 422.
      onCancel();
      return;
    }
    setSaving(true);
    try {
      const ok = await onSave(patch);
      // False = the save was refused (version conflict, engine down). The editor
      // stays open with the user's edits intact — closing here threw away the
      // exact text they were trying to save.
      if (ok === false) return;
      onCancel();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-center gap-1.5 text-[11px] font-semibold text-blue-300 uppercase tracking-wider">
        <Pencil className="w-3 h-3" /> Editing Step {step.ordinal + 1}
      </div>
      <label className="flex flex-col gap-1">
        <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Title</span>
        <input
          className={inputCls}
          value={title}
          maxLength={200}
          onChange={(e) => setTitle(e.target.value)}
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Description</span>
        <textarea
          className={`${inputCls} min-h-[60px] resize-y`}
          value={description}
          maxLength={20000}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">
          Target Paths (comma-separated)
        </span>
        <input
          className={`${inputCls} font-mono`}
          value={paths}
          placeholder="src/foo.ts, src/bar.ts"
          onChange={(e) => setPaths(e.target.value)}
        />
      </label>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={handleSave}
          disabled={saving}
          className="flex items-center gap-1 px-2.5 py-1 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded-lg shadow transition-colors disabled:opacity-50"
        >
          <Check className="w-3 h-3" /> {saving ? "Saving…" : "Save Step"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="flex items-center gap-1 px-2.5 py-1 bg-[#21262d] hover:bg-[#30363d] text-gray-300 border border-[#30363d] text-xs font-semibold rounded-lg transition-colors"
        >
          <X className="w-3 h-3" /> Cancel
        </button>
        <span className="text-[10px] text-gray-500 font-mono">plan v{version}</span>
      </div>
    </div>
  );
};

const UsageCard: React.FC<{ goalId: string }> = ({ goalId }) => {
  const [usage, setUsage] = useState<GoalUsage | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getGoalUsage(goalId)
      .then((u) => {
        if (!cancelled) setUsage(u);
      })
      .catch(() => {
        // No usage to show is normal (older engine, silent server). A failed
        // fetch is information, not an error the chat should shout about.
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [goalId]);

  if (failed || !usage) return null;

  const fmt = (n: number) => n.toLocaleString();
  const roles = Object.entries(usage.by_role).sort(
    (a, b) => b[1].total_tokens - a[1].total_tokens
  );

  return (
    <div className="p-2.5 rounded-xl bg-[#0d1117] border border-[#30363d] text-xs">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-gray-400">
          <Coins className="w-3.5 h-3.5 text-amber-400" />
          Token Usage
        </div>
        <div className="font-mono text-[11px] text-gray-300">
          {fmt(usage.totals.total_tokens)} tokens
          <span className="text-gray-500">
            {" "}
            ({fmt(usage.totals.input_tokens)} in / {fmt(usage.totals.output_tokens)} out) ·{" "}
            {usage.calls} call{usage.calls === 1 ? "" : "s"}
          </span>
        </div>
      </div>
      {roles.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
          {roles.map(([role, b]) => (
            <span key={role} className="text-[11px] text-gray-400">
              <span className="font-mono text-gray-300">{role}</span> {fmt(b.total_tokens)}
              {b.calls > 1 && <span className="text-gray-500"> ({b.calls})</span>}
            </span>
          ))}
        </div>
      )}
    </div>
  );
};

interface ChatTimelineProps {
  messages: ChatMessage[];
  onStartGoal: (goalId: string, version: number) => void;
  onEnableExecution: (goalId: string, version: number) => void;
  onApplyGoal: (goalId: string) => void;
  onEditStep: (
    goalId: string,
    stepId: string,
    expectedVersion: number,
    patch: { title?: string; description?: string; suggested_paths?: string[] }
  ) => boolean | void | Promise<boolean | void>;
  onPauseGoal: (goalId: string, version: number) => void;
  onCancelGoal: (goalId: string, version: number) => void;
  onRetryStep: (goalId: string, stepId: string, version: number) => void;
  onQuickPrompt: (prompt: string) => void;
  /** Opens Settings on the tab that holds the fix. */
  onOpenSettings: (tab: "keys" | "agents") => void;
}

export const ChatTimeline: React.FC<ChatTimelineProps> = ({
  messages,
  onStartGoal,
  onEnableExecution,
  onApplyGoal,
  onEditStep,
  onPauseGoal,
  onCancelGoal,
  onRetryStep,
  onQuickPrompt,
  onOpenSettings,
}) => {
  const bottomRef = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState<{ goalId: string; stepId: string } | null>(null);
  // The failure being investigated, if any.
  const [diagnosis, setDiagnosis] = useState<{
    code: string;
    message: string;
    role: AgentRole | null;
  } | null>(null);

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
                    {msg.goal?.plan_only && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-950/40 border border-blue-800 text-blue-300 font-semibold">
                        PLAN ONLY
                      </span>
                    )}
                  </div>

                  {/* Goal Action Buttons */}
                  {msg.goal && (
                    <div className="flex items-center gap-1.5">
                      {msg.goal.plan_only &&
                        (msg.goal.status === "PENDING" || msg.goal.status === "PAUSED") && (
                          <button
                            type="button"
                            onClick={() => onEnableExecution(msg.goal!.id, msg.goal!.version)}
                            className="flex items-center gap-1 px-2.5 py-1 bg-amber-600 hover:bg-amber-500 text-white text-xs font-semibold rounded-lg shadow transition-colors"
                            title="Lift the plan-only guard and begin execution"
                          >
                            <Play className="w-3 h-3" /> Execute Plan
                          </button>
                        )}

                      {!msg.goal.plan_only &&
                        (msg.goal.status === "PENDING" || msg.goal.status === "PAUSED") && (
                          <button
                            type="button"
                            onClick={() => onStartGoal(msg.goal!.id, msg.goal!.version)}
                            className="flex items-center gap-1 px-2.5 py-1 bg-green-600 hover:bg-green-500 text-white text-xs font-semibold rounded-lg shadow transition-colors"
                          >
                            <Play className="w-3 h-3" /> Start
                          </button>
                        )}

                      {msg.goal.dry_run &&
                        (msg.goal.status === "COMPLETED" || msg.goal.status === "FAILED") && (
                          <button
                            type="button"
                            onClick={() => onApplyGoal(msg.goal!.id)}
                            className="flex items-center gap-1 px-2.5 py-1 bg-amber-600 hover:bg-amber-500 text-white text-xs font-semibold rounded-lg shadow transition-colors"
                            title="Write the proposed changes for real, then re-run tests, review, and commit"
                          >
                            <Check className="w-3 h-3" /> Apply these changes
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

                            {/* Edit Plan affordance: plan-only goals still awaiting execution */}
                            {msg.goal?.plan_only &&
                              msg.goal.status === "PENDING" &&
                              step.status === "PENDING" &&
                              !(editing?.goalId === msg.goal.id && editing?.stepId === step.id) && (
                                <button
                                  type="button"
                                  onClick={() =>
                                    setEditing({ goalId: msg.goal!.id, stepId: step.id })
                                  }
                                  className="flex items-center gap-1 px-2 py-0.5 bg-blue-600/20 hover:bg-blue-600/40 text-blue-300 border border-blue-700/50 rounded text-[11px] font-semibold transition-colors"
                                  title="Edit this step's title, description, and target paths"
                                >
                                  <Pencil className="w-3 h-3" /> Edit
                                </button>
                              )}

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

                          {editing?.goalId === msg.goal?.id && editing?.stepId === step.id ? (
                            <PlanStepEditor
                              step={step}
                              version={msg.goal!.version}
                              onSave={(patch) =>
                                onEditStep(msg.goal!.id, step.id, msg.goal!.version, patch)
                              }
                              onCancel={() => setEditing(null)}
                            />
                          ) : (
                            <>
                              <p className="text-xs text-gray-400 pl-6 leading-relaxed">
                                {step.description}
                              </p>

                              {step.review_notes && (
                                <div className="ml-6 mt-1 p-2 rounded-lg bg-amber-950/30 border border-amber-800/60 text-xs text-amber-300 flex items-start gap-1.5">
                                  <ShieldCheck className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                                  <div>
                                    <span className="font-semibold">Critic Notes: </span>
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
                            </>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Token usage, once the goal has run: totals + per-role split,
                    from the engine's usage events. Hidden until terminal so a
                    running goal shows a number that is still moving. */}
                {msg.goal &&
                  (msg.goal.status === "COMPLETED" ||
                    msg.goal.status === "FAILED" ||
                    msg.goal.status === "CANCELLED") && (
                    <UsageCard goalId={msg.goal.id} />
                  )}

                {/* Event Stream (Diffs & Results) */}
                {msg.events && msg.events.length > 0 && (
                  <div className="space-y-3 pt-2 border-t border-[#30363d]/60">
                    <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider flex items-center gap-1.5">
                      <Terminal className="w-3.5 h-3.5" />
                      {msg.goal?.dry_run
                        ? "Proposed Changes (Dry Run — nothing written)"
                        : "Telemetry & Changes"}
                    </div>

                    {/*
                      The single place every EventType is rendered. If you add a
                      type to the EventType union, render it here — there is no
                      second renderer to keep in sync.
                    */}
                    <div className="space-y-2">
                      {(() => {
                        // Streaming replies render as ONE live card per role
                        // showing the newest snapshot, not one bubble per
                        // throttle tick; non-streaming events map as before.
                        const streamHeads: Record<string, { text: string; role: string; final: boolean }> = {};
                        const rendered: React.ReactNode[] = [];
                        msg.events.forEach((ev) => {
                          if (ev.type === "model_delta") {
                            streamHeads[ev.payload.role] = {
                              text: ev.payload.text,
                              role: ev.payload.role,
                              final: !!ev.payload.final,
                            };
                            return;
                          }
                          rendered.push(
                            <div key={ev.sequence} className="text-xs">
                          {ev.type === "diff" && (
                            <DiffViewer
                              path={ev.payload.path}
                              diffText={ev.payload.unified_diff}
                            />
                          )}

                          {/* What the librarian found, and — just as important — what
                              it claimed but never opened, which the engine dropped. */}
                          {ev.type === "library_evidence" && (
                            <div className="p-2.5 rounded-lg bg-[#0d1117] border border-[#30363d] flex flex-col gap-1.5">
                              <div className="flex items-center gap-1.5">
                                <BookOpen className="w-3.5 h-3.5 text-cyan-400" />
                                <span className="font-semibold text-[11px] uppercase tracking-wider text-gray-400">
                                  Reconnaissance
                                </span>
                                <span className="text-[10px] text-gray-500">
                                  {ev.payload.files?.length ?? 0} file(s) cited from{" "}
                                  {ev.payload.counts?.considered ?? 0} considered ·{" "}
                                  {ev.payload.rounds ?? 1} round(s)
                                  {ev.payload.counts?.opened != null &&
                                    ` · ${ev.payload.counts.opened} opened, ${ev.payload.counts.matched} matched by search`}
                                </span>
                              </div>
                              {ev.payload.summary && (
                                <p className="text-[11px] text-gray-300 leading-relaxed">
                                  {ev.payload.summary}
                                </p>
                              )}
                              {(ev.payload.files?.length ?? 0) > 0 && (
                                <div className="flex flex-col gap-0.5">
                                  {ev.payload.files.map(
                                    (f: { path: string; why?: string; evidence?: string }, i: number) => (
                                      <div key={i} className="text-[11px] flex items-start gap-1.5">
                                        <span className="font-mono text-cyan-300/90">{f.path}</span>
                                        {f.evidence && (
                                          <span className="text-[10px] text-gray-500">[{f.evidence}]</span>
                                        )}
                                        {f.why && <span className="text-gray-400">— {f.why}</span>}
                                      </div>
                                    )
                                  )}
                                </div>
                              )}
                              {ev.payload.test_command && (
                                <div className="text-[11px] font-mono text-gray-400">
                                  test command: {ev.payload.test_command.join(" ")}
                                  <span className="text-[10px] text-gray-500 ml-1">(unverified)</span>
                                </div>
                              )}
                              {ev.payload.conventions && ev.payload.conventions.length > 0 && (
                                <div className="text-[11px] text-gray-400">
                                  conventions: {ev.payload.conventions.join("; ")}
                                </div>
                              )}
                              {ev.payload.risks && ev.payload.risks.length > 0 && (
                                <div className="text-[11px] text-amber-400/90">
                                  risks: {ev.payload.risks.join("; ")}
                                </div>
                              )}
                              {ev.payload.dropped_paths && ev.payload.dropped_paths.length > 0 && (
                                <div className="flex items-start gap-1.5 text-[11px] text-amber-400/90">
                                  <ShieldAlert className="w-3 h-3 flex-shrink-0 mt-0.5" />
                                  <span className="font-mono">
                                    never opened, dropped: {ev.payload.dropped_paths.join(", ")}
                                  </span>
                                </div>
                              )}
                            </div>
                          )}

                          {ev.type === "test_result" && (
                            <div className="p-2.5 rounded-lg bg-[#0d1117] border border-[#30363d] flex flex-col gap-1">
                              <div className="flex items-center gap-2">
                                <span
                                  className={`font-semibold uppercase text-[11px] px-1.5 py-0.5 rounded ${
                                    ev.payload.verdict === "pass"
                                      ? "bg-green-950 text-green-300 border border-green-800"
                                      : ev.payload.verdict === "fail"
                                      ? "bg-red-950 text-red-300 border border-red-800"
                                      : "bg-[#21262d] text-gray-300 border border-[#30363d]"
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
                              {/* A verdict with no command behind it must not read like a
                                  run suite: say that the sandbox refused the command. */}
                              {ev.payload.refused && ev.payload.refused.length > 0 && (
                                <div className="flex flex-col gap-0.5 pt-0.5">
                                  {ev.payload.refused.map((r: string, i: number) => (
                                    <div
                                      key={i}
                                      className="flex items-start gap-1.5 text-[11px] text-amber-400/90"
                                    >
                                      <ShieldAlert className="w-3 h-3 flex-shrink-0 mt-0.5" />
                                      <span className="font-mono">refused: {r}</span>
                                    </div>
                                  ))}
                                  {ev.payload.ran === false && (
                                    <span className="text-[11px] text-gray-500 pl-4">
                                      No command ran — this verdict is the tester's judgement, not a
                                      test result.
                                    </span>
                                  )}
                                </div>
                              )}
                            </div>
                          )}

                          {ev.type === "laya_decision" && (
                            <LayaGateCard payload={ev.payload} />
                          )}

                          {/* The fix→verify loop said a test run failed and is
                              being retried: shown so a goal that takes longer
                              than usual explains itself. */}
                          {ev.type === "fixer_pass" && (
                            <div className="flex items-start gap-1.5 pl-2 text-sky-300">
                              <ShieldAlert className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                              <span className="leading-relaxed">
                                Fixer asked for another pass
                                <span className="font-mono text-gray-400">
                                  {' '}(pass {ev.payload.attempt}, {ev.payload.passes_left} of {ev.payload.max_passes} left)
                                </span>
                              </span>
                            </div>
                          )}

                          {ev.type === "plan_consult" && (
                            <div className="flex items-start gap-1.5 pl-2 text-cyan-300">
                              <BookOpen className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                              <span className="leading-relaxed">
                                Planner asked the librarian for a follow-up
                                {ev.payload.refused > 0 && (
                                  <span className="font-mono text-gray-400">
                                    {' '}({ev.payload.refused} request{ev.payload.refused === 1 ? "" : "s"} refused)
                                  </span>
                                )}
                              </span>
                            </div>
                          )}

                          {ev.type === "fix_retry" && (
                            <div className="flex items-start gap-1.5 pl-2 text-amber-300">
                              <ShieldAlert className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                              <span className="leading-relaxed">
                                Tests failed — asking the fixer to try again
                                <span className="font-mono text-gray-400">
                                  {' '}(attempt {ev.payload.attempt}/{ev.payload.max_attempts})
                                </span>
                                {ev.payload.reason && (
                                  <span className="block text-[11px] text-gray-500 font-mono">
                                    {ev.payload.reason}
                                  </span>
                                )}
                              </span>
                            </div>
                          )}

                          {ev.type === "agent_assigned" && (
                            <div className="flex items-center gap-1.5 pl-2 text-purple-300">
                              <Bot className="w-3.5 h-3.5 flex-shrink-0" />
                              <span>
                                Sub-agent assigned: <span className="font-semibold">{ev.payload.role}</span>
                                {ev.payload.provider && ev.payload.model && (
                                  <span className="font-mono text-gray-500"> ({ev.payload.provider}/{ev.payload.model})</span>
                                )}
                              </span>
                            </div>
                          )}

                          {/* The primary target was skipped. Shown as its own line
                              because the reply that follows came from a different
                              model, and the transcript must not credit the wrong one. */}
                          {ev.type === "provider_fallback" && (
                            <div className="flex items-start gap-1.5 pl-2 text-teal-300">
                              <Route className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                              <span className="leading-relaxed">
                                <span className="font-semibold">{ev.payload.role}</span> fell back to{" "}
                                <span className="font-mono text-gray-300">
                                  {ev.payload.to?.provider}/{ev.payload.to?.model}
                                </span>{" "}
                                — <span className="font-mono text-gray-400">
                                  {ev.payload.from?.provider}/
                                  {ev.payload.from?.model || "no model"}
                                </span>{" "}
                                could not be used
                                {ev.payload.detail && (
                                  <span className="text-gray-500"> ({ev.payload.detail})</span>
                                )}
                              </span>
                            </div>
                          )}

                          {ev.type === "file_change_summary" && (
                            <div className="flex items-start gap-1.5 pl-2 text-blue-300">
                              <FileCode className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                              <span>
                                Files touched: {ev.payload.paths?.length ? ev.payload.paths.join(", ") : "none"}
                                {ev.payload.dry_run && (
                                  <span className="ml-1 text-amber-400">(dry run — nothing written)</span>
                                )}
                                {/* The fixer proposed these and the contents already
                                    matched, so nothing was written and nothing is
                                    claimed. Named rather than dropped: "none" alone
                                    reads as though the fixer said nothing. */}
                                {!!ev.payload.unchanged?.length && (
                                  <span className="ml-1 text-gray-400">
                                    ({ev.payload.unchanged.join(", ")} already matched — left alone)
                                  </span>
                                )}
                              </span>
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
                            <div className="p-2 rounded-lg bg-red-950/40 border border-red-800 text-red-300 font-semibold flex flex-col gap-2">
                              <span>
                                Error [{ev.payload.code}]: {ev.payload.message}
                              </span>
                              {/* The engine names the role responsible, so the
                                  answer to "why" is a lookup, not a guess. */}
                              <button
                                type="button"
                                onClick={() =>
                                  setDiagnosis({
                                    code: ev.payload.code,
                                    message: ev.payload.message,
                                    role: ev.payload.role ?? null,
                                  })
                                }
                                className="flex w-fit items-center gap-1.5 px-2 py-1 bg-[#21262d] hover:bg-[#30363d] border border-[#30363d] text-gray-200 text-[11px] font-semibold rounded transition-colors"
                              >
                                <ShieldAlert className="w-3 h-3 text-red-400" />
                                Why did this fail?
                              </button>
                            </div>
                          )}
                        </div>
                          );
                        });
                        // The live reply cards, one per role, newest snapshot:
                        const streamRoles = Object.keys(streamHeads);
                        if (streamRoles.length > 0) {
                          rendered.push(
                            <div key="model-stream" className="space-y-1.5">
                              {streamRoles.map((r) => {
                                const head = streamHeads[r];
                                return (
                                  <div
                                    key={r}
                                    className={`p-2 rounded-lg border font-mono text-[11px] leading-relaxed ${
                                      head.final
                                        ? "bg-[#161b22] border-[#30363d] text-gray-400"
                                        : "bg-[#0d1117] border-blue-800/60 text-blue-100"
                                    }`}
                                  >
                                    <div
                                      className={`flex items-center gap-1.5 text-[10px] uppercase tracking-wider mb-1 ${
                                        head.final ? "text-gray-500" : "text-blue-400"
                                      }`}
                                    >
                                      <Terminal className="w-3 h-3" />
                                      {head.role} replied
                                      {!head.final && <span className="animate-pulse">▍</span>}
                                    </div>
                                    <div className="whitespace-pre-wrap break-words max-h-48 overflow-y-auto">
                                      {head.text || "…"}
                                    </div>
                                  </div>
                                );
                              })}
                            </div>
                          );
                        }
                        return rendered;
                      })()}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      ))}
      <div ref={bottomRef} />

      {diagnosis && (
        <FailureDiagnosisPanel
          error={diagnosis}
          onClose={() => setDiagnosis(null)}
          onOpenSettings={(tab) => {
            setDiagnosis(null);
            onOpenSettings(tab);
          }}
        />
      )}
    </div>
  );
};
