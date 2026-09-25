import React, { useRef, useEffect, useState } from "react";
import { AgentRole, ChatMessage, Event, Goal, PlanStep } from "../types";
import { deliverablePath, pinReadiness } from "../designDeliverable";
import { FailureDiagnosisPanel } from "./FailureDiagnosisPanel";
import { DiffViewer } from "./DiffViewer";
import { LayaDecision } from "../types";
import { getGoalUsage, GoalUsage, getGoalAudit } from "../api";
import { AuditReport } from "./AuditReport";
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
  Palette,
  Pin,
  ShieldCheck,
  ShieldAlert,
  Check,
  Pencil,
  X,
  Coins,
  Workflow,
  FileDown,
  FileUp,
  Trash2,
} from "lucide-react";

/**
 * Laya's pre-flight verdict, rendered as one honest line of chat: which engine
 * answered, the typed answers it produced, and whether that tripped policy.
 *
 * Answers arrive either nested ({"intent": {"choice": …}}) or flat, so both
 * shapes are read here — same tolerance the engine applies.
 *
 * `noul` is the engine's own field name (engine/laya.py: a calibrated
 * probability in [0,1]) — not a typo for "null".
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

/**
 * One step's execution window, reconstructed from the step_status events:
 * when it started, when it ended, and which other steps were in flight with
 * it (by step id). Windows from different runs of the same step (a retry)
 * are kept separately — a retry is a second bar, not a stretched first one.
 */
interface StepWindow {
  start: number;
  end: number | null; // null = still running
  overlaps: Set<string>; // step ids whose windows intersect this one
}

function buildStepWindows(events: Event[]): Map<string, StepWindow[]> {
  const open: { stepId: string; start: number; overlaps: Set<string> }[] = [];
  const byStep = new Map<string, StepWindow[]>();
  for (const ev of events) {
    if (ev.type !== "step_status" || !ev.step_id) continue;
    const status = ev.payload?.status;
    if (status === "IN_PROGRESS") {
      // The engine re-publishes IN_PROGRESS at every role transition inside a
      // step (fixer → verifier → critic → scribe), so this is idempotent per
      // step: only the FIRST start opens a window. Otherwise a step would
      // render five bars and count its own re-entries as "other steps".
      if (open.some((o) => o.stepId === ev.step_id)) continue;
      // Every currently-open window overlaps this one, and vice versa.
      const entry = { stepId: ev.step_id, start: ev.timestamp, overlaps: new Set<string>() };
      for (const o of open) {
        o.overlaps.add(ev.step_id);
        entry.overlaps.add(o.stepId);
      }
      open.push(entry);
    } else if (status === "COMPLETED" || status === "FAILED") {
      // Close this step's open window (a retry opens a fresh one later).
      const windows = byStep.get(ev.step_id) ?? [];
      const pendingIdx = open.findIndex((o) => o.stepId === ev.step_id);
      if (pendingIdx !== -1) {
        const [w] = open.splice(pendingIdx, 1);
        windows.push({ start: w.start, end: ev.timestamp, overlaps: w.overlaps });
        byStep.set(ev.step_id, windows);
      }
    }
  }
  // Still-open windows belong to a running goal (or a killed engine); render
  // them as running — the bar extends to "now" in the component.
  for (const o of open) {
    const windows = byStep.get(o.stepId) ?? [];
    windows.push({ start: o.start, end: null, overlaps: o.overlaps });
    byStep.set(o.stepId, windows);
  }
  return byStep;
}

/**
 * Timeline bars for one step: one row per execution window, drawn on a shared
 * axis from the goal's first step start to its last step end. The overlap
 * shading is computed from the event log itself, so a bar can only claim
 * concurrency when the log shows another step genuinely in flight.
 */
/**
 * Durations in chat-readable form: sub-second runs as milliseconds, then
 * seconds with one decimal, then whole seconds + minutes. The usage card and
 * the bars share the vocabulary so "2.4s" means the same thing in both.
 */
function formatDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

const StepTimelineBars: React.FC<{
  windows: StepWindow[];
  axis: { start: number; end: number | null };
  running: boolean;
}> = ({ windows, axis, running }) => {
  const span = Math.max((axis.end ?? Date.now() / 1000) - axis.start, 0.001);
  return (
    <div className="ml-6 mt-1 flex flex-col gap-1">
      {windows.map((w, i) => {
        const left = ((w.start - axis.start) / span) * 100;
        const endAt = w.end ?? Date.now() / 1000;
        const width = ((endAt - w.start) / span) * 100;
        const hasOverlap = w.overlaps.size > 0;
        const overlapCount = w.overlaps.size;
        const duration = formatDuration(Math.max(endAt - w.start, 0));
        return (
          <div key={i} className="flex items-center gap-2">
            {/* The bar itself, on the shared axis. */}
            <div
              className="relative h-2.5 flex-1 rounded bg-[#161b22] overflow-hidden"
              role="img"
              aria-label={
                hasOverlap
                  ? `Ran alongside ${overlapCount} other step${overlapCount === 1 ? "" : "s"} — ${duration}`
                  : `Ran alone — ${duration}`
              }
              title={
                hasOverlap
                  ? `Ran alongside ${overlapCount} other step${overlapCount === 1 ? "" : "s"} — ${duration}`
                  : `Ran alone — ${duration}`
              }
            >
              <div
                className={`absolute h-full rounded ${
                  w.end === null
                    ? "bg-blue-500 animate-pulse"
                    : hasOverlap
                    ? "bg-blue-600/70"
                    : "bg-[#30363d]"
                }`}
                style={{ left: `${Math.max(left, 0)}%`, width: `${Math.min(Math.max(width, 1), 100)}%` }}
              />
              {hasOverlap && (
                <div
                  className="absolute h-full bg-blue-400/40"
                  title="overlap window"
                  style={{ left: `${Math.max(left, 0)}%`, width: `${Math.min(Math.max(width, 1), 100)}%` }}
                />
              )}
            </div>
            {/* The runtime, readable without hovering. Still-running windows
                recompute on every render (the poll refresh re-renders the
                card), so a live goal counts up in place. */}
            <span
              className={`text-[9px] font-mono tabular-nums flex-shrink-0 ${
                w.end === null ? "text-blue-400" : "text-gray-500"
              }`}
            >
              {duration}
            </span>
          </div>
        );
      })}
      {running && (
        <span className="text-[9px] text-gray-500 font-mono">running…</span>
      )}
    </div>
  );
};

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
      {/* How wide this run actually was, from the step_status log — the
          after-the-fact answer to "did parallel mode do anything?". A
          sequential run is peak 1 and stays silent; the number only appears
          when it says something. */}
      {usage.parallel_peak > 1 && (
        <div className="mt-2 flex items-center gap-1.5 text-[11px] text-blue-300">
          <Workflow className="w-3 h-3" />
          Peak {usage.parallel_peak} step{usage.parallel_peak === 1 ? "" : "s"} in parallel
          {usage.parallel_waves > 1 && (
            <span className="text-gray-500"> · {usage.parallel_waves} wave{usage.parallel_waves === 1 ? "" : "s"}</span>
          )}
        </div>
      )}
    </div>
  );
};

/**
 * One-line audit summary for the goal card: how many plan edits, provider
 * fallbacks, and errors the goal's own event log records. Derived from the
 * message's events — the same log the engine's audit endpoint sweeps — so the
 * badges are live while running and stay correct after completion, with no
 * extra fetch. Only non-zero counts render.
 */
type AuditMarkerKind = "edits" | "fallbacks" | "errors";

/**
 * Scroll the transcript to the Nth transcript entry carrying
 * `data-audit-marker="<kind>"` for the given goal card, and flash it so the
 * eye lands. Used by the card's audit badges — a count is a claim; the jump
 * is the evidence behind it.
 *
 * `scope` (the card container) keeps the search inside ONE goal's transcript:
 * with several goal cards rendered, an unscoped query would jump to the first
 * card's entry regardless of which card's badge was clicked.
 *
 * `index` cycles through the matches: the badge's click handler advances a
 * per-badge cursor and wraps back to 0 after the last entry, so repeated
 * clicks walk through every match instead of landing on the first forever.
 *
 * Scrolls instantly on purpose: `behavior: "smooth"` is a silent no-op in
 * embedded Chromium (verified live — the click fired, the flash played, the
 * container never moved), so a smooth travel would leave users on some builds
 * staring at an unmoved transcript. The flash is the motion cue instead.
 * Called only from event handlers, never during render.
 */
function scrollToAuditMarker(kind: AuditMarkerKind, scope: ParentNode, index: number): void {
  const matches = scope.querySelectorAll(`[data-audit-marker="${kind}"]`);
  if (matches.length === 0) return;
  const el = matches[Math.min(index, matches.length - 1)];
  // scrollIntoView walks up to the nearest scrollable ancestor — the
  // transcript container — so the entry, not the page, is what moves.
  el.scrollIntoView({ block: "center" });
  // Force a reflow so re-clicking the badge restarts the animation.
  el.classList.remove("audit-flash", `audit-flash-${kind}`);
  void (el as HTMLElement).offsetWidth;
  el.classList.add("audit-flash", `audit-flash-${kind}`);
}

/**
 * Per-badge cycling cursor: which match a badge's next click jumps to.
 * Keyed by `messageId:kind` and held at module level — a view cursor, not
 * data — so it survives the re-renders that streaming events trigger every
 * few hundred milliseconds. Wraps via modulo in the click handler.
 */
const auditMarkerCursor = new Map<string, number>();

/**
 * The match each badge LAST SHOWED, keyed like `auditMarkerCursor`. The cursor
 * above points at the *next* match to show; this records the *previous* one, so
 * the tooltip can say "showing 2nd of 3" for the match just jumped to. Kept at
 * module level for the same reason: a view cursor, not data.
 */
const auditMarkerViewed = new Map<string, number>();

/**
 * The badge label's ordinal for the tooltip: 1-based human form.
 */
function ordinalLabel(n: number): string {
  return `${n}${n === 1 ? "st" : n === 2 ? "nd" : n === 3 ? "rd" : "th"}`;
}

/**
 * Whether one event is an "issue" for the transcript's issues-only filter: the
 * engine's own errors, warn/error log lines, and failed model calls. One rule,
 * module-level, so the toggle's visibility and the filtering itself can never
 * disagree about what counts — and so a new failure-shaped event type added to
 * the renderer has a deliberate choice to make about this list.
 */
function isIssueEvent(ev: Event): boolean {
  if (ev.type === "error" || ev.type === "agent_call_failed") return true;
  if (ev.type === "log") {
    return ev.payload?.level === "warn" || ev.payload?.level === "error";
  }
  return false;
}

function auditSummary(events: Event[] | undefined): {
  edits: number;
  fallbacks: number;
  errors: number;
} | null {
  if (!events || events.length === 0) return null;
  let edits = 0;
  let fallbacks = 0;
  let errors = 0;
  for (const ev of events) {
    if (ev.type === "plan_updated") {
      // Count edits that actually changed something — a no-op patch is not drift.
      const changes = (ev.payload?.changes ?? {}) as Record<
        string,
        { before?: unknown; after?: unknown }
      >;
      const changed = Object.values(changes).some(
        (ch) => JSON.stringify(ch.before) !== JSON.stringify(ch.after)
      );
      if (changed) edits += 1;
    } else if (ev.type === "provider_fallback") {
      fallbacks += 1;
    } else if (ev.type === "error") {
      errors += 1;
    }
  }
  if (edits === 0 && fallbacks === 0 && errors === 0) return null;
  return { edits, fallbacks, errors };
}

/** The outcome of the last pin attempt, per goal card. */
interface PinOutcome {
  goalId: string;
  ok: boolean;
  message: string;
}

/**
 * The pin action for a design deliverable.
 *
 * The two reasons it can be unavailable are rendered as text rather than only a
 * `title`: a disabled button does not reliably surface a tooltip, and "why can't
 * I pin this" is the question the card exists to answer. The body stays
 * collapsible so a full contract does not swallow the transcript.
 *
 * A workspace that already obeys this file gets a state instead of an action.
 * The pin is workspace state — it can be set from the workspace picker, or by an
 * earlier goal, or before this tab was reloaded — so the card reads it from the
 * workspace rather than from whether this transcript happened to watch it
 * happen. Offering the pin again there would be asking the user to do something
 * they have already done.
 */
const DesignDeliverablePin: React.FC<{
  goal: Goal;
  path: string;
  body: string;
  outcome: PinOutcome | null;
  /** The file this workspace already obeys, if any. */
  pinnedPath?: string;
  onPin: (goalId: string, workspaceId: string, path: string) => Promise<void>;
}> = ({ goal, path, body, outcome, pinnedPath, onPin }) => {
  const readiness = pinReadiness(goal, path, pinnedPath);
  return (
    <div className="flex flex-col gap-1.5 mt-0.5 pt-1.5 border-t border-[#21262d]">
      <div className="flex items-center gap-2 flex-wrap">
        {readiness.pinned ? (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-pink-500/10 border border-pink-500/30 text-pink-200/90 text-[11px]">
            <Check className="w-3 h-3" />
            {path} is this workspace&rsquo;s brand contract
          </span>
        ) : (
          <button
            type="button"
            disabled={!readiness.ready}
            onClick={() => void onPin(goal.id, goal.workspace_id, path)}
            className="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-pink-600/20 border border-pink-500/50 text-pink-300 hover:bg-pink-600/30 text-[11px] cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-pink-600/20"
          >
            <Pin className="w-3 h-3" />
            Pin as brand contract
          </button>
        )}
        {!readiness.ready && readiness.reason && (
          <span className="text-[10px] text-gray-500">{readiness.reason}</span>
        )}
        {readiness.ready && outcome !== null && outcome.goalId === goal.id && (
          <span className={`text-[10px] ${outcome.ok ? "text-green-400" : "text-red-400"}`}>
            {outcome.message}
          </span>
        )}
      </div>
      <details className="text-[11px] text-gray-400">
        <summary className="cursor-pointer text-gray-500">
          {path} ({body.length} chars) — {readiness.bodyLabel}
        </summary>
        <pre className="mt-1.5 p-2 rounded bg-[#161b22] border border-[#21262d] text-[10px] text-gray-300 whitespace-pre-wrap max-h-64 overflow-auto">
          {body}
        </pre>
      </details>
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
  /** Confirms, then deletes the goal and its recorded history. */
  onDeleteGoal: (goalId: string, title: string) => void;
  onRetryStep: (goalId: string, stepId: string, version: number) => void;
  onQuickPrompt: (prompt: string) => void;
  /** Opens Settings on the tab that holds the fix. */
  onOpenSettings: (tab: "keys" | "agents") => void;
  /** Opens a file picker and imports an exported audit JSON as a report message. */
  onImportAudit: () => void;
  /**
   * Pin a design goal's written deliverable as the workspace's brand contract.
   * Rejects with the engine's own message when the path escapes the workspace,
   * is missing, or is not readable text — so a draft that was never written
   * comes back as the reason it was refused rather than as a silent no-op.
   */
  onPinDesignContract: (workspaceId: string, path: string) => Promise<void>;
  /**
   * The brand contract each workspace currently obeys, keyed by workspace id.
   *
   * Read from `GET /workspaces` rather than from a click this transcript
   * remembers: a pin set from the workspace picker, or before a reload, is just
   * as real as one set here, and a card that only remembered its own clicks
   * would offer a pin that is already in force.
   */
  pinnedContracts: Record<string, string>;
}

export const ChatTimeline: React.FC<ChatTimelineProps> = ({
  messages,
  onStartGoal,
  onEnableExecution,
  onApplyGoal,
  onEditStep,
  onPauseGoal,
  onCancelGoal,
  onDeleteGoal,
  onRetryStep,
  onQuickPrompt,
  onOpenSettings,
  onImportAudit,
  onPinDesignContract,
  pinnedContracts,
}) => {
  const bottomRef = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState<{ goalId: string; stepId: string } | null>(null);
  // Outcome of a "pin this deliverable" click, keyed by goal. Pinning is a
  // settings change made from a transcript card, so the card has to say it
  // landed rather than send the user hunting in another screen for proof.
  const [pinState, setPinState] = useState<PinOutcome | null>(null);

  const handlePinDeliverable = async (
    goalId: string,
    workspaceId: string,
    path: string
  ) => {
    try {
      await onPinDesignContract(workspaceId, path);
      // Success is not reported from here. The card now renders the pinned
      // state from the workspace the engine just returned, which is the same
      // claim and survives a reload; a message held in this component's state
      // would say it twice now and outlive an unpin made from the workspace
      // picker, still claiming a contract that is gone.
      setPinState(null);
    } catch (err: any) {
      setPinState({
        goalId,
        ok: false,
        message: err?.message || `could not pin ${path}`,
      });
    }
  };
  // Transcript-wide "issues only" mode: long runs render hundreds of telemetry
  // entries, and the review question is usually just "what went wrong". One
  // toggle for the whole transcript — a filter that had to be found per card
  // would be three controls pretending to be one.
  const [issueOnly, setIssueOnly] = useState(false);
  // The failure being investigated, if any.
  // Per-goal-card DOM scopes for audit-badge jumps: a badge must scroll to
  // entries in ITS card's transcript, not the first card that happens to
  // render. Keyed by message id; entries clean up on unmount.
  const cardScopes = useRef(new Map<string, HTMLElement>());
  // Bumped on every audit-badge click: the cursor and viewed maps live at
  // module level, so without a state change the re-render (and thus the
  // updated "showing 2nd of 3" tooltip) would never happen. Streaming events
  // re-render often anyway, but a quiet goal must still update its tooltip on
  // click — this guarantees it.
  const [, setAuditTick] = useState(0);
  const [diagnosis, setDiagnosis] = useState<{
    code: string;
    message: string;
    role: AgentRole | null;
  } | null>(null);
  // Audit-export failures surface inline — a console-only error leaves the
  // user clicking a button that appears to do nothing.
  const [exportError, setExportError] = useState<string | null>(null);

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

        {/* Import an exported audit document back into the transcript as a
            readable report — a closed artifact, reviewable with no engine. */}
        <button
          type="button"
          onClick={onImportAudit}
          className="mt-4 flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[#161b22] border border-[#30363d] hover:border-blue-500/40 text-xs text-gray-400 hover:text-blue-400 transition-colors"
          title="Open an exported audit-trail JSON and view it as a report"
        >
          <FileUp className="w-3.5 h-3.5" />
          Import audit report…
        </button>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-4 sm:p-6 space-y-6 max-w-4xl mx-auto w-full">
      {exportError && (
        <div className="flex items-start gap-2 p-2.5 rounded-xl bg-red-950/40 border border-red-800 text-xs text-red-300">
          <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-red-400" />
          <span className="leading-relaxed">{exportError}</span>
        </div>
      )}
      {messages.map((msg) => {
        // Scope the audit-badge jumps to THIS card: several goal cards render
        // in one transcript, and an unscoped document query would jump to the
        // first card's entry regardless of which badge was clicked.
        const scopeRef = (node: HTMLDivElement | null) => {
          if (node) cardScopes.current.set(msg.id, node);
          else cardScopes.current.delete(msg.id);
        };
        // The toggle only appears on cards that have something to filter: a
        // pill that filters nothing is noise, not a control.
        const hasIssues = (msg.events ?? []).some(isIssueEvent);
        return (
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

              <div
                ref={scopeRef}
                className="flex-1 bg-[#161b22] border border-[#30363d] rounded-2xl p-4 sm:p-5 shadow-lg space-y-4"
              >
                {/* An imported audit document renders as a standalone report —
                    it has no live goal, so it short-circuits the whole
                    execution-card chrome. */}
                {msg.auditDoc ? (
                    <>
                      <div className="flex items-center gap-2 text-xs font-semibold text-gray-300 border-b border-[#30363d]/60 pb-3">
                        <FileUp className="w-3.5 h-3.5 text-blue-400" />
                        Imported audit report
                        {msg.content && (
                          <span className="ml-auto font-normal text-[10px] text-gray-500">{msg.content}</span>
                        )}
                      </div>
                      <AuditReport doc={msg.auditDoc as never} />
                    </>
                ) : (
                <>
                {/* Header: Title and Status */}
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#30363d]/60 pb-3">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-sm text-gray-200">
                      {msg.goal?.title || "Agent Execution"}
                    </span>
                    {msg.goal && (
                      <span
                        title={`Goal status: ${msg.goal.status}`}
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
                    {msg.goal?.parallel && (
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded bg-purple-950/40 border border-purple-800 text-purple-300 font-semibold"
                        title="Independent steps of this goal may run concurrently"
                      >
                        PARALLEL
                      </span>
                    )}
                    {/* One-line audit summary: the counts the audit export
                        would show, computed live from this message's events.
                        Each badge appears only when non-zero — and clicking
                        one scrolls the transcript to the matching entry, so
                        the count's evidence is one click away. */}
                    {(() => {
                      const summary = auditSummary(msg.events);
                      if (!summary) return null;
                      const badgeCls =
                        "text-[10px] px-1.5 py-0.5 rounded border font-semibold transition-colors cursor-pointer";
                      // Each click advances this badge's cursor through the
                      // matches and wraps. The wrap bound is the LIVE marker
                      // count in this card, not the badge number: both derive
                      // from the same events, but the DOM is the thing actually
                      // being cycled, so it can never disagree with itself.
                      // Where the badge's cycling stands, for the tooltip: the
                      // last-shown 1-based position, or null before the first
                      // click (nothing has been shown yet — no suffix). Read
                      // from the LIVE DOM, like the wrap bound, so tooltip and
                      // scroll target can never disagree.
                      const positionSuffix = (kind: AuditMarkerKind): string => {
                        const scope = cardScopes.current.get(msg.id);
                        const total = scope?.querySelectorAll(`[data-audit-marker="${kind}"]`).length ?? 0;
                        const viewed = auditMarkerViewed.get(`${msg.id}:${kind}`);
                        if (!scope || total === 0 || viewed === undefined) return "";
                        return ` — showing ${ordinalLabel(viewed + 1)} of ${total}`;
                      };
                      const jump = (kind: AuditMarkerKind) => () => {
                        const scope = cardScopes.current.get(msg.id);
                        if (!scope) return;
                        const total = scope.querySelectorAll(`[data-audit-marker="${kind}"]`).length;
                        if (total === 0) return;
                        const key = `${msg.id}:${kind}`;
                        const index = auditMarkerCursor.get(key) ?? 0;
                        scrollToAuditMarker(kind, scope, index);
                        auditMarkerViewed.set(key, index);
                        auditMarkerCursor.set(key, (index + 1) % total);
                        setAuditTick((t) => t + 1); // tooltip is DOM-derived; force the re-render
                      };
                      return (
                        <>
                          {summary.edits > 0 && (
                            <button
                              type="button"
                              onClick={jump("edits")}
                              className={`${badgeCls} bg-violet-950/40 border-violet-800 text-violet-300 hover:bg-violet-900/60`}
                              title={`Plan steps were edited after planning — click to cycle through each edit${positionSuffix("edits")}`}
                              aria-label={`Plan steps were edited after planning — click to cycle through each edit${positionSuffix("edits")}`}
                            >
                              {summary.edits} edit{summary.edits === 1 ? "" : "s"}
                            </button>
                          )}
                          {summary.fallbacks > 0 && (
                            <button
                              type="button"
                              onClick={jump("fallbacks")}
                              className={`${badgeCls} bg-teal-950/40 border-teal-800 text-teal-300 hover:bg-teal-900/60`}
                              title={`Model calls that fell back — click to cycle through each fallback${positionSuffix("fallbacks")}`}
                              aria-label={`Model calls that fell back — click to cycle through each fallback${positionSuffix("fallbacks")}`}
                            >
                              {summary.fallbacks} fallback{summary.fallbacks === 1 ? "" : "s"}
                            </button>
                          )}
                          {summary.errors > 0 && (
                            <button
                              type="button"
                              onClick={jump("errors")}
                              className={`${badgeCls} bg-red-950/40 border-red-800 text-red-300 hover:bg-red-900/60`}
                              title={`Errors recorded during the run — click to cycle through each error${positionSuffix("errors")}`}
                              aria-label={`Errors recorded during the run — click to cycle through each error${positionSuffix("errors")}`}
                            >
                              {summary.errors} error{summary.errors === 1 ? "" : "s"}
                            </button>
                          )}
                        </>
                      );
                    })()}
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
                          aria-label="Cancel goal"
                        >
                          <XCircle className="w-4 h-4" />
                        </button>
                      )}

                      {/* Delete is offered only once the goal has stopped, for
                          the same reason Cancel exists: an in-flight run has a
                          live coroutine writing files, and "delete" must never
                          be how a user stops work already in progress — Cancel
                          is, and it leaves a record of what was attempted. */}
                      {(msg.goal.status === "COMPLETED" ||
                        msg.goal.status === "FAILED" ||
                        msg.goal.status === "CANCELLED") && (
                        <button
                          type="button"
                          onClick={() => onDeleteGoal(msg.goal!.id, msg.goal!.title)}
                          className="p-1 text-gray-500 hover:text-red-400 rounded-lg transition-colors"
                          title="Delete this goal and its event log (your files are not touched)"
                          aria-label="Delete goal"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      )}

                      {/* Audit export: the run's structured trail (plan edits,
                          fallbacks, failures, outcomes) as a downloaded JSON
                          document. Available at any stage — an in-flight goal's
                          audit is a snapshot up to now. */}
                      <button
                        type="button"
                        onClick={async () => {
                          try {
                            setExportError(null);
                            const audit = await getGoalAudit(msg.goal!.id);
                            const blob = new Blob([JSON.stringify(audit, null, 2)], {
                              type: "application/json",
                            });
                            const url = URL.createObjectURL(blob);
                            const a = document.createElement("a");
                            const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
                            const slug = (msg.goal!.title || msg.goal!.id)
                              .toLowerCase()
                              .replace(/[^a-z0-9]+/g, "-")
                              .replace(/^-+|-+$/g, "")
                              .slice(0, 40) || msg.goal!.id.slice(0, 8);
                            a.href = url;
                            a.download = `audit-${slug}-${stamp}.json`;
                            a.click();
                            URL.revokeObjectURL(url);
                          } catch (err: any) {
                            setExportError(err?.message || "Could not export the audit trail.");
                          }
                        }}
                        className="p-1 text-gray-400 hover:text-blue-400 rounded-lg transition-colors"
                        title="Export audit trail (plan edits, fallbacks, failures)"
                        aria-label="Export audit trail"
                      >
                        <FileDown className="w-4 h-4" />
                      </button>
                    </div>
                  )}
                </div>

                {/* Plan Steps Accordion */}
                {msg.goal?.steps && msg.goal.steps.length > 0 && (
                  <div className="space-y-2">
                    <div className="flex items-center justify-between gap-2">
                      <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
                        Execution Plan ({msg.goal.steps.length} Steps)
                      </div>
                      {/* How many steps are in flight right now. >1 is the
                          parallel-mode proof; the whole point of the flag. */}
                      {msg.goal.status === "RUNNING" &&
                        msg.goal.steps.filter((s: PlanStep) => s.status === "IN_PROGRESS").length > 1 && (
                          <span className="flex items-center gap-1 text-[10px] font-mono px-2 py-0.5 rounded-full bg-blue-950/50 border border-blue-800 text-blue-300">
                            <PlayCircle className="w-3 h-3 animate-pulse" />
                            {msg.goal.steps.filter((s: PlanStep) => s.status === "IN_PROGRESS").length} steps in parallel
                          </span>
                        )}
                    </div>
                    <div className="space-y-2">
                      {(() => {
                        // Shared timeline axis: from the first step start to the
                        // last step end (or "now" while anything is running), so
                        // every bar sits on the same scale and overlap is visible
                        // as aligned bars, not just claimed by color.
                        const windowsByStep = msg.events?.length ? buildStepWindows(msg.events) : new Map();
                        let axisStart = Infinity;
                        let axisEnd: number | null = null;
                        for (const ws of windowsByStep.values()) {
                          for (const w of ws) {
                            axisStart = Math.min(axisStart, w.start);
                            axisEnd = w.end === null ? null : (axisEnd === null ? Math.max(axisEnd ?? 0, w.end) : Math.max(axisEnd, w.end));
                          }
                        }
                        const axis = Number.isFinite(axisStart)
                          ? { start: axisStart, end: axisEnd }
                          : null;
                        const anyRunning = msg.goal?.status === "RUNNING";
                        return msg.goal.steps.map((step: PlanStep) => {
                          const windows = windowsByStep.get(step.id) ?? [];
                          return (
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
                              {/* This step is one of several running right now —
                                  the visual proof that parallel mode is real. */}
                              {step.status === "IN_PROGRESS" &&
                                msg.goal?.steps &&
                                msg.goal.steps.filter((s: PlanStep) => s.status === "IN_PROGRESS").length > 1 && (
                                  <span
                                    className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-blue-950/60 border border-blue-700 text-blue-300 animate-pulse"
                                    title="Running concurrently with the other highlighted steps"
                                  >
                                    ⫴ PARALLEL
                                  </span>
                                )}
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

                              {/* When this step ran, on the goal's shared axis.
                                  Aligned bars across cards = overlap you can see;
                                  blue = ran alongside another step, gray = alone,
                                  pulsing = still running. One row per execution
                                  (a retry adds a second bar). */}
                              {axis && windows.length > 0 && (
                                <StepTimelineBars
                                  windows={windows}
                                  axis={axis}
                                  running={anyRunning && step.status === "IN_PROGRESS"}
                                />
                              )}

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
                          );
                        });
                      })()}
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
                      {hasIssues && (
                        <button
                          type="button"
                          onClick={() => setIssueOnly(!issueOnly)}
                          title={
                            issueOnly
                              ? "Showing warnings, errors, and failed calls only — click to show the full telemetry"
                              : "Show only warnings, errors, and failed calls"
                          }
                          className={`ml-auto flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-semibold normal-case tracking-normal transition-colors cursor-pointer ${
                            issueOnly
                              ? "bg-amber-950/40 text-amber-300 border-amber-800"
                              : "bg-[#0d1117] text-gray-400 border-[#30363d] hover:text-gray-200"
                          }`}
                        >
                          <AlertCircle className="w-3 h-3" />
                          Issues only
                        </button>
                      )}
                    </div>

                    {/*
                      The single place every EventType is rendered. If you add a
                      type to the EventType union, render it here — there is no
                      second renderer to keep in sync.
                    */}
                    <div className="space-y-2">
                      {(() => {
                        // Streaming replies render as ONE live card per (step,
                        // role) showing the newest snapshot, not one bubble per
                        // throttle tick. Keyed by step_id too: under a parallel
                        // goal two agents of the same role run at once, and a
                        // role-keyed map would interleave their text into one
                        // card. The step title rides along for the label.
                        const rendered: React.ReactNode[] = [];
                        const streamHeads: Record<
                          string,
                          { text: string; role: string; final: boolean; stepTitle?: string }
                        > = {};
                        const stepTitleById = new Map<string, string>();
                        (msg.goal?.steps ?? []).forEach((s: PlanStep) =>
                          stepTitleById.set(s.id, s.title)
                        );
                        // "Issues only": the (step, role) streams whose role hit a
                        // real failure this run. Their replies stay — the last words
                        // before a failure are its context — while finished streams
                        // from healthy roles are exactly the noise being filtered.
                        const failedKeys = new Set<string>();
                        if (issueOnly) {
                          msg.events.forEach((ev) => {
                            if (
                              (ev.type === "error" || ev.type === "agent_call_failed") &&
                              ev.payload?.role
                            ) {
                              failedKeys.add(`${ev.step_id ?? "goal"}::${ev.payload.role}`);
                            }
                          });
                        }
                        msg.events.forEach((ev) => {
                          if (ev.type === "model_delta") {
                            const headKey = `${ev.step_id ?? "goal"}::${ev.payload.role}`;
                            if (issueOnly && !failedKeys.has(headKey)) return;
                            streamHeads[headKey] = {
                              text: ev.payload.text,
                              role: ev.payload.role,
                              final: !!ev.payload.final,
                              stepTitle:
                                (ev.step_id && stepTitleById.get(ev.step_id)) || undefined,
                            };
                            return;
                          }
                          if (issueOnly && !isIssueEvent(ev)) return;
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

                          {/* The direction the planner planned against and the fixer
                              was told to obey: locked once, before any step exists,
                              and published so a step can be judged against the
                              contract that shaped it rather than from memory. */}
                          {ev.type === "design_contract" && (
                            <div className="p-2.5 rounded-lg bg-[#0d1117] border border-[#30363d] flex flex-col gap-1.5">
                              <div className="flex items-center gap-1.5">
                                <Palette className="w-3.5 h-3.5 text-pink-400" />
                                <span className="font-semibold text-[11px] uppercase tracking-wider text-gray-400">
                                  {/* A design-mode goal's contract is the artifact
                                      itself, not the direction a step is written
                                      against: the difference between input and
                                      deliverable, so it is named. */}
                                  {ev.payload.mode === "design"
                                    ? "Design deliverable"
                                    : "Design direction"}
                                </span>
                                <span className="text-[10px] font-mono text-gray-500">
                                  {ev.payload.artifact}
                                </span>
                              </div>
                              {ev.payload.direction && (
                                <p className="text-[11px] text-gray-300 leading-relaxed">
                                  {ev.payload.direction}
                                </p>
                              )}
                              {ev.payload.design_system?.name && (
                                <div className="text-[11px] text-gray-400">
                                  design system:{" "}
                                  <span className="font-mono text-gray-300">
                                    {ev.payload.design_system.name}
                                  </span>
                                  {/* Three states, said differently on purpose: a pin
                                      is the user's instruction, a discovery is a
                                      convention the engine noticed, and a proposal
                                      exists because there was nothing to obey. */}
                                  {ev.payload.design_system.origin === "pinned" ? (
                                    <span className="text-pink-300/90">
                                      {" "}
                                      — pinned at {ev.payload.design_system.source}
                                    </span>
                                  ) : ev.payload.design_system.origin === "discovered" ? (
                                    <span className="text-gray-500">
                                      {" "}
                                      — found at {ev.payload.design_system.source}
                                    </span>
                                  ) : ev.payload.design_system.source ? (
                                    <span className="text-gray-500">
                                      {" "}
                                      — from {ev.payload.design_system.source}
                                    </span>
                                  ) : (
                                    <span className="text-gray-500"> — proposed, no existing contract</span>
                                  )}
                                </div>
                              )}
                              {(ev.payload.tokens?.colors?.length ?? 0) > 0 && (
                                <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
                                  {ev.payload.tokens.colors.map(
                                    (c: { name: string; value: string }, i: number) => (
                                      <span key={i} className="text-[11px] flex items-center gap-1">
                                        <span
                                          className="w-2.5 h-2.5 rounded-sm border border-[#30363d]"
                                          style={{ background: c.value }}
                                        />
                                        <span className="font-mono text-gray-400">{c.name}</span>
                                        <span className="font-mono text-gray-500">{c.value}</span>
                                      </span>
                                    )
                                  )}
                                </div>
                              )}
                              {(ev.payload.tokens?.typography?.length ?? 0) > 0 && (
                                <div className="text-[11px] text-gray-400">
                                  type:{" "}
                                  {ev.payload.tokens.typography
                                    .map((t: { name: string; value: string }) => `${t.name} ${t.value}`)
                                    .join(" · ")}
                                </div>
                              )}
                              {(ev.payload.components?.length ?? 0) > 0 && (
                                <div className="flex flex-col gap-0.5">
                                  {ev.payload.components.map(
                                    (c: { name: string; purpose?: string }, i: number) => (
                                      <div key={i} className="text-[11px] flex items-start gap-1.5">
                                        <span className="font-mono text-pink-300/90">{c.name}</span>
                                        {c.purpose && <span className="text-gray-400">— {c.purpose}</span>}
                                      </div>
                                    )
                                  )}
                                </div>
                              )}
                              {(ev.payload.acceptance?.length ?? 0) > 0 && (
                                <div className="text-[11px] text-gray-400">
                                  acceptance: {ev.payload.acceptance.join("; ")}
                                </div>
                              )}
                              {(ev.payload.constraints?.length ?? 0) > 0 && (
                                <div className="text-[11px] text-amber-400/90">
                                  constraints: {ev.payload.constraints.join("; ")}
                                </div>
                              )}
                              {/* The deliverable's own body, and the one action
                                  that gives it authority. The engine drafts it
                                  and a step writes it; only the user can make
                                  it binding, and this is where the file they
                                  were just shown becomes the contract. */}
                              {ev.payload.mode === "design" &&
                                ev.payload.design_md &&
                                msg.goal && (
                                  <DesignDeliverablePin
                                    goal={msg.goal}
                                    path={deliverablePath(msg.goal)}
                                    body={ev.payload.design_md}
                                    outcome={pinState?.goalId === msg.goal.id ? pinState : null}
                                    pinnedPath={pinnedContracts[msg.goal.workspace_id]}
                                    onPin={handlePinDeliverable}
                                  />
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
                              {/* What the engine itself found comparing the written
                                  artifacts against a binding brand contract — advisory,
                                  so it renders as findings, never as the verdict. */}
                              {ev.payload.brand_drifts && ev.payload.brand_drifts.length > 0 && (
                                <div className="flex flex-col gap-0.5 pt-0.5">
                                  {ev.payload.brand_drifts.map((d: string, i: number) => (
                                    <div
                                      key={i}
                                      className="flex items-start gap-1.5 text-[11px] text-amber-400/90"
                                    >
                                      <Palette className="w-3 h-3 flex-shrink-0 mt-0.5" />
                                      <span className="font-mono">{d}</span>
                                    </div>
                                  ))}
                                </div>
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

                          {/* The plan was edited before execution. Paths gate
                              parallel batching, so a path edit is
                              execution-relevant drift: the transcript shows
                              what moved, not just that something did. */}
                          {ev.type === "plan_updated" && (() => {
                            const ch = ev.payload?.changes ?? {};
                            const stepTitle =
                              (msg.goal?.steps ?? []).find((s: PlanStep) => s.id === ev.step_id)?.title ??
                              ev.payload?.step_title ?? "step";
                            const paths = ch.suggested_paths;
                            const pathChanged =
                              paths && JSON.stringify(paths.before) !== JSON.stringify(paths.after);
                            const titleChanged = ch.title && ch.title.before !== ch.title.after;
                            const fmtPaths = (ps: string[]) =>
                              ps.length ? ps.join(", ") : "(none)";
                            // Every changed field gets its own line: a combined
                            // edit (paths + rename) must be auditable in full,
                            // not reduced to whichever branch won the if/else.
                            return (
                              <div
                                className="flex items-start gap-1.5 pl-2 text-violet-300 rounded"
                                data-audit-marker="edits"
                              >
                                <Pencil className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                                <span className="leading-relaxed">
                                  Plan edited —{" "}
                                  <span className="font-semibold">{stepTitle}</span>
                                  {pathChanged && (
                                    <span className="block font-mono text-[11px] text-gray-400">
                                      paths: <span className="text-red-400/80 line-through">{fmtPaths(paths.before)}</span>{" "}
                                      → <span className="text-green-400">{fmtPaths(paths.after)}</span>
                                    </span>
                                  )}
                                  {titleChanged && (
                                    <span className="block text-gray-400">
                                      renamed from “{ch.title.before}”
                                    </span>
                                  )}
                                  {ch.description &&
                                    ch.description.before !== ch.description.after && (
                                      <span className="block text-gray-400">
                                        description updated
                                      </span>
                                    )}
                                  {!pathChanged && !titleChanged &&
                                    !(ch.description && ch.description.before !== ch.description.after) && (
                                      <span className="text-gray-400">updated</span>
                                    )}
                                </span>
                              </div>
                            );
                          })()}

                          {ev.type === "agent_assigned" && (
                            <div className="flex items-center gap-1.5 pl-2 text-purple-300">
                              <Bot className="w-3.5 h-3.5 flex-shrink-0" />
                              <span>
                                Sub-agent assigned: <span className="font-semibold">{ev.payload.role}</span>
                                {ev.step_id && (
                                  <span className="text-gray-400">
                                    {" · "}
                                    {(msg.goal?.steps ?? []).find((s: PlanStep) => s.id === ev.step_id)?.title ??
                                      "step"}
                                  </span>
                                )}
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
                            <div
                              className="flex items-start gap-1.5 pl-2 text-teal-300 rounded"
                              data-audit-marker="fallbacks"
                            >
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

                          {/* A model call that failed outright — distinct from a
                              fallback (the work continued elsewhere) and from an
                              error event (the goal-level failure record). The
                              Settings screen's "last error" reads these too. */}
                          {ev.type === "agent_call_failed" && (
                            <div
                              className="flex items-start gap-1.5 pl-2 text-amber-300/90 rounded"
                              data-audit-marker="errors"
                            >
                              <ShieldAlert className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                              <span className="leading-relaxed">
                                <span className="font-semibold">{ev.payload.role}</span> could not call{" "}
                                <span className="font-mono text-gray-300">
                                  {ev.payload.provider}/{ev.payload.model}
                                </span>{" "}
                                ({ev.payload.target}) — <span className="font-mono">{ev.payload.code}</span>
                                {ev.payload.duration_ms != null && (
                                  <span className="text-gray-500">
                                    {" "}after {ev.payload.duration_ms < 1000
                                      ? `${Math.round(ev.payload.duration_ms)}ms`
                                      : `${(ev.payload.duration_ms / 1000).toFixed(1)}s`}
                                  </span>
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
                            <div
                              className="p-2 rounded-lg bg-red-950/40 border border-red-800 text-red-300 font-semibold flex flex-col gap-2"
                              data-audit-marker="errors"
                            >
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
                        // The live reply cards, one per (step, role), newest snapshot:
                        const streamKeys = Object.keys(streamHeads);
                        // Filtered down to nothing: say so, rather than rendering a
                        // bare section header over an empty box.
                        if (issueOnly && rendered.length === 0 && streamKeys.length === 0) {
                          rendered.push(
                            <div
                              key="issues-empty"
                              className="text-[11px] text-gray-500 pl-2 border-l-2 border-amber-500/60 py-0.5"
                            >
                              Nothing else to show — no warnings or errors in this run.
                            </div>
                          );
                        }
                        if (streamKeys.length > 0) {
                          rendered.push(
                            <div key="model-stream" className="space-y-1.5">
                              {streamKeys.map((k) => {
                                const head = streamHeads[k];
                                return (
                                  <div
                                    key={k}
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
                                      {head.stepTitle && (
                                        <span className="normal-case font-sans text-gray-500">
                                          · {head.stepTitle}
                                        </span>
                                      )}
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
                </>
                )}
              </div>
            </div>
          )}
        </div>
        );
      })}
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
