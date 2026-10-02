import React, { useRef, useEffect, useState } from "react";
import type { AgentRole, ChatMessage, Event, PlanStep } from "../types";
import {
  DesignDeliverableCard,
  PinOutcome,
} from "./DesignDeliverableCard";
import { KnowledgeDeliverableCard } from "./KnowledgeDeliverableCard";
import { canStopGoal, isGoalActive } from "../goalActions";
import { foldStages, stageStates, callLabel } from "../pipeline";
import { traceGoalId } from "../tracePanel";
import { statusTone, stepTone } from "../statusTone";
import { replyPreview } from "../replyPreview";
import { Badge, toneText } from "./ui/Badge";
import { Wordmark } from "./ui/Wordmark";
import { FailureDiagnosisPanel } from "./FailureDiagnosisPanel";
import { DiffViewer } from "./DiffViewer";
import type { LayaDecision } from "../types";
import { getAudioStatus, getGoalUsage, GoalUsage, getGoalAudit } from "../api";
import { AuditReport } from "./AuditReport";
import { TracePanel } from "./TracePanel";
import { canArmTrace } from "../traceSummary";
import { COMPOSER_ANCHOR_ID } from "../composerAnchor";
import {
  isConversationalTurn,
  turnAlerts,
  turnLiveText,
  turnReply,
} from "../turnTranscript";
import {
  User,
  Bot,
  Route,
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
  ShieldCheck,
  ShieldAlert,
  Check,
  Pencil,
  X,
  Coins,
  Workflow,
  ChevronRight,
  FileDown,
  FileUp,
  Trash2,
  Radio,
} from "lucide-react";
import { readRejection } from "../rejection.ts";
import { answersToRead, type AutoReadMemory } from "../speech.ts";
import { SpeakButton } from "./SpeakButton";
import { Markdown } from "./Markdown";
import { PauseBanner } from "./PauseBanner";
import { TodoCard } from "./TodoCard";
import { visibleTodos } from "../todoList.ts";
import { pauseReasonOf } from "../pauseReason.ts";

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
function layaAnswer(
  payload: Record<string, any> | undefined,
  key: string,
): unknown {
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
      const entry = {
        stepId: ev.step_id,
        start: ev.timestamp,
        overlaps: new Set<string>(),
      };
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
        windows.push({
          start: w.start,
          end: ev.timestamp,
          overlaps: w.overlaps,
        });
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
              className="relative h-2.5 flex-1 rounded bg-codify-surface overflow-hidden"
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
                    ? "bg-codify-accent animate-pulse"
                    : hasOverlap
                      ? "bg-codify-accent/70"
                      : "bg-codify-border"
                }`}
                style={{
                  left: `${Math.max(left, 0)}%`,
                  width: `${Math.min(Math.max(width, 1), 100)}%`,
                }}
              />
              {hasOverlap && (
                <div
                  className="absolute h-full bg-codify-accent/40"
                  title="overlap window"
                  style={{
                    left: `${Math.max(left, 0)}%`,
                    width: `${Math.min(Math.max(width, 1), 100)}%`,
                  }}
                />
              )}
            </div>
            {/* The runtime, readable without hovering. Still-running windows
                recompute on every render (the poll refresh re-renders the
                card), so a live goal counts up in place. */}
            <span
              className={`text-2xs font-mono tabular-nums flex-shrink-0 ${
                w.end === null ? "text-codify-info" : "text-codify-muted"
              }`}
            >
              {duration}
            </span>
          </div>
        );
      })}
      {running && (
        <span className="text-2xs text-codify-muted font-mono">running…</span>
      )}
    </div>
  );
};

const LayaGateCard: React.FC<{ payload: Record<string, any> }> = ({
  payload,
}) => {
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
      <div className="flex items-start gap-1.5 pl-2 text-codify-muted">
        <ShieldCheck className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
        <span>
          Laya gate skipped —{" "}
          {decision.skipped_reason || "no decision engine available"}
        </span>
      </div>
    );
  }

  const tone = blocked
    ? "text-codify-danger"
    : warnings.length
      ? "text-codify-warning"
      : "text-codify-success";
  const engineLabel = engine === "sdk" ? "Laya SDK" : "Laya via fallback model";

  return (
    <div className={`flex flex-col gap-0.5 pl-2 ${tone}`}>
      <div className="flex items-center gap-1.5">
        <ShieldCheck className="w-3.5 h-3.5 flex-shrink-0" />
        <span>
          Laya gate{" "}
          <span className="font-semibold">
            {blocked ? "blocked" : "passed"}
          </span>
          <span className="font-mono text-codify-muted"> ({engineLabel})</span>
        </span>
      </div>
      <div className="pl-5 text-xs text-codify-muted flex flex-wrap gap-x-3 gap-y-0.5">
        {intent !== null && intent !== undefined && (
          <span>
            intent <span className="text-codify-secondary">{String(intent)}</span>
          </span>
        )}
        {risk !== null && (
          <span>
            risk <span className="text-codify-secondary">{risk.toFixed(2)}</span>
          </span>
        )}
        {injection !== null && (
          <span>
            injection{" "}
            <span className="text-codify-secondary">{injection.toFixed(2)}</span>
            {typeof threshold === "number" && (
              <span className="text-codify-muted"> / block at {threshold}</span>
            )}
          </span>
        )}
      </div>
      {decision.block_reason && (
        <div className="pl-5 text-xs">{decision.block_reason}</div>
      )}
      {warnings.map((w, i) => (
        <div key={i} className="pl-5 text-xs text-codify-warning">
          {w}
        </div>
      ))}
    </div>
  );
};

/**
 * A turn, rendered as the conversation it is.
 *
 * The alternative was the execution card, and for "hi" that card is a lie of
 * shape: a status badge over a question, a spine naming the roles dispatched to
 * answer it, and a Laya verdict card — three pieces of run furniture a person
 * asking a question never asked for, and the reason the answer read as a side
 * effect of a pipeline rather than as a reply. What a turn *does* keep is
 * everything a person has to act on: the gate's card when the engine published
 * one (which now means it blocked or warned — see `turnTranscript`), and any
 * warning or error. The engine's narration of its own tool calls is dropped
 * here on purpose; it is in the event log and in the audit export.
 *
 * The rules live in `turnTranscript.ts` and are asserted there; this draws them.
 */
const TurnExchange: React.FC<{
  msg: ChatMessage;
  /** This message's recording panel is open. */
  traceOpen: boolean;
  onToggleTrace: () => void;
  /** Auto-read chose this answer: read it as soon as it is drawn. */
  autoRead: boolean;
  /** A link in the answer was clicked: open this address in a browser tab. */
  onOpenLink?: (url: string) => void;
}> = ({ msg, traceOpen, onToggleTrace, autoRead, onOpenLink }) => {
  const reply = turnReply(msg.events);
  // Only while there is nothing to show yet: the streamed snapshot *is* the
  // answer being produced, and once the engine publishes the reply it is the
  // same words said twice.
  const live = reply === null ? turnLiveText(msg.events) : null;
  const alerts = turnAlerts(msg.events);
  const active = isGoalActive(msg.goal?.status);

  return (
    <div className="space-y-2">
      <div className="rounded-2xl rounded-tl-sm border border-codify-border bg-codify-surface px-4 py-2.5 text-sm leading-relaxed text-codify-primary break-words">
        {/* The answer is Markdown, drawn as elements and never as HTML (`markdown.ts` says why), so
            the streamed snapshot and the finished reply are the same component and an unterminated
            fence mid-stream already looks like code. */}
        {reply ?? live ? (
          <Markdown text={(reply ?? live) as string} onOpenLink={onOpenLink} />
        ) : active ? (
          <span className="text-codify-muted italic animate-pulse">Thinking…</span>
        ) : (
          <span className="text-codify-muted">(no answer)</span>
        )}
      </div>
      {alerts.length > 0 && (
        <div className="space-y-1.5">
          {alerts.map((alert, i) =>
            alert.kind === "gate" ? (
              <LayaGateCard key={`gate-${i}`} payload={alert.payload ?? {}} />
            ) : (
              <div
                key={`${alert.kind}-${i}`}
                className={`flex items-start gap-1.5 pl-2 text-xs ${
                  alert.kind === "error" ? "text-codify-danger" : "text-codify-warning"
                }`}
              >
                <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                <span className="leading-relaxed font-mono">{alert.text}</span>
              </div>
            ),
          )}
        </div>
      )}
      {/* Only the finished answer: the streamed snapshot is still changing, and a voice reading a
          draft that is then replaced is a voice that said the wrong thing. */}
      {reply !== null && <SpeakButton id={msg.id} text={reply} autoPlay={autoRead} />}
      {/* A turn can be recorded like any other goal, so the recording stays
          reachable from the turn's own card. Nothing to arm: a turn spends its
          life between PLANNING and COMPLETED, and the composer is where its
          recording is asked for. */}
      {msg.goal?.trace && (
        <button
          type="button"
          onClick={onToggleTrace}
          aria-expanded={traceOpen}
          className="flex items-center gap-1.5 pl-2 text-2xs text-codify-muted hover:text-codify-info transition-colors"
          title={traceOpen ? "Hide the recording" : "Show this turn's recording"}
        >
          <Radio className="w-3 h-3 text-codify-danger" />
          Recording
        </button>
      )}
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
    "w-full bg-codify-surface border border-codify-border rounded-lg px-2.5 py-1.5 text-xs text-codify-secondary focus:outline-none focus:border-codify-info/60";

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
      patch.suggested_paths = paths
        .split(",")
        .map((p) => p.trim())
        .filter(Boolean);
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
      <div className="flex items-center gap-1.5 text-xs font-semibold text-codify-info uppercase tracking-wider">
        <Pencil className="w-3 h-3" /> Editing Step {step.ordinal + 1}
      </div>
      <label className="flex flex-col gap-1">
        <span className="text-2xs font-semibold text-codify-muted uppercase tracking-wider">
          Title
        </span>
        <input
          className={inputCls}
          value={title}
          maxLength={200}
          onChange={(e) => setTitle(e.target.value)}
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-2xs font-semibold text-codify-muted uppercase tracking-wider">
          Description
        </span>
        <textarea
          className={`${inputCls} min-h-[3.75rem] resize-y`}
          value={description}
          maxLength={20000}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-2xs font-semibold text-codify-muted uppercase tracking-wider">
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
          className="flex items-center gap-1 px-2.5 py-1 bg-codify-accent text-codify-bg hover:brightness-110 text-xs font-semibold rounded-lg shadow transition-colors disabled:opacity-50"
        >
          <Check className="w-3 h-3" /> {saving ? "Saving…" : "Save Step"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="flex items-center gap-1 px-2.5 py-1 bg-codify-raised hover:bg-codify-border text-codify-secondary border border-codify-border text-xs font-semibold rounded-lg transition-colors"
        >
          <X className="w-3 h-3" /> Cancel
        </button>
        <span className="text-2xs text-codify-muted font-mono">
          plan v{version}
        </span>
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
    (a, b) => b[1].total_tokens - a[1].total_tokens,
  );

  return (
    <div className="p-2.5 rounded-xl bg-codify-bg border border-codify-border text-xs">
      {/* Wraps rather than squeezes: in a narrow centre column the label used to be broken one
          letter to a line while the totals kept their width. The label does not break; the totals drop
          to the next line instead. */}
      <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
        <div className="flex items-center gap-1.5 whitespace-nowrap text-xs font-semibold uppercase tracking-wider text-codify-muted">
          <Coins className="w-3.5 h-3.5 text-codify-warning" />
          Token Usage
        </div>
        <div className="font-mono text-xs text-codify-secondary">
          {fmt(usage.totals.total_tokens)} tokens
          <span className="text-codify-muted">
            {" "}
            ({fmt(usage.totals.input_tokens)} in /{" "}
            {fmt(usage.totals.output_tokens)} out) · {usage.calls} call
            {usage.calls === 1 ? "" : "s"}
          </span>
        </div>
      </div>
      {roles.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
          {roles.map(([role, b]) => (
            <span key={role} className="text-xs text-codify-muted">
              <span className="font-mono text-codify-secondary">{role}</span>{" "}
              {fmt(b.total_tokens)}
              {b.calls > 1 && (
                <span className="text-codify-muted"> ({b.calls})</span>
              )}
            </span>
          ))}
        </div>
      )}
      {/* How wide this run actually was, from the step_status log — the
          after-the-fact answer to "did parallel mode do anything?". A
          sequential run is peak 1 and stays silent; the number only appears
          when it says something. */}
      {usage.parallel_peak > 1 && (
        <div className="mt-2 flex items-center gap-1.5 text-xs text-codify-info">
          <Workflow className="w-3 h-3" />
          Peak {usage.parallel_peak} step{usage.parallel_peak === 1 ? "" : "s"}{" "}
          in parallel
          {usage.parallel_waves > 1 && (
            <span className="text-codify-muted">
              {" "}
              · {usage.parallel_waves} wave
              {usage.parallel_waves === 1 ? "" : "s"}
            </span>
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
function scrollToAuditMarker(
  kind: AuditMarkerKind,
  scope: ParentNode,
  index: number,
): void {
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
        (ch) => JSON.stringify(ch.before) !== JSON.stringify(ch.after),
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

interface ChatTimelineProps {
  messages: ChatMessage[];
  onStartGoal: (goalId: string, version: number) => void;
  onEnableExecution: (goalId: string, version: number) => void;
  onApplyGoal: (goalId: string) => void;
  onEditStep: (
    goalId: string,
    stepId: string,
    expectedVersion: number,
    patch: { title?: string; description?: string; suggested_paths?: string[] },
  ) => boolean | void | Promise<boolean | void>;
  onPauseGoal: (goalId: string, version: number) => void;
  onCancelGoal: (goalId: string, version: number) => void;
  /**
   * Arm or disarm a goal's recording. The engine allows this only before the
   * run starts, so the control is offered exactly when it can succeed.
   */
  onSetGoalTrace: (goalId: string, enabled: boolean) => void;
  /** Confirms, then deletes the goal and its recorded history. */
  onDeleteGoal: (goalId: string, title: string) => void;
  onRetryStep: (goalId: string, stepId: string, version: number) => void;
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
  /**
   * Open an address from a link in an answer, in the app's own browser tab. Optional: without it a
   * link in an answer is its words only (`markdownLinks.ts`).
   */
  onOpenLink?: (url: string) => void;
}

export const ChatTimeline: React.FC<ChatTimelineProps> = ({
  messages,
  onStartGoal,
  onEnableExecution,
  onApplyGoal,
  onEditStep,
  onPauseGoal,
  onCancelGoal,
  onSetGoalTrace,
  onDeleteGoal,
  onRetryStep,
  onOpenSettings,
  onImportAudit,
  onPinDesignContract,
  pinnedContracts,
  onOpenLink,
}) => {
  const bottomRef = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState<{
    goalId: string;
    stepId: string;
  } | null>(null);
  // Outcome of a "pin this deliverable" click, keyed by goal. Pinning is a
  // settings change made from a transcript card, so the card has to say it
  // landed rather than send the user hunting in another screen for proof.
  const [pinState, setPinState] = useState<PinOutcome | null>(null);
  // Which goal's recording panel is open. One at a time, keyed by goal — the
  // same shape `pinState` and `editing` take, because these are properties of
  // a card rather than of the transcript.
  const [traceFor, setTraceFor] = useState<string | null>(null);

  const handlePinDeliverable = async (
    goalId: string,
    workspaceId: string,
    path: string,
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
        message: readRejection(err, `could not pin ${path}`),
      });
    }
  };
  // Transcript-wide "issues only" mode: long runs render hundreds of telemetry
  // entries, and the review question is usually just "what went wrong". One
  // toggle for the whole transcript — a filter that had to be found per card
  // would be three controls pretending to be one.
  const [issueOnly, setIssueOnly] = useState(false);
  // Which role replies the reader has opened. Keyed by the same `headKey` the
  // stream is grouped by, so two goals in one transcript cannot open each
  // other's payloads.
  //
  // A *finished* reply starts closed and shows one summary line. A reply still
  // arriving starts open, because that is output in flight and the user is
  // watching it land. Measured before this existed: one goal filled 937px of a
  // 654px panel, and 456px of it was two unexpanded JSON documents nobody had
  // asked to read.
  const [openReplies, setOpenReplies] = useState<Record<string, boolean>>({});
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

  // Read answers aloud as they arrive, when Settings → Audio says to. `answersToRead` decides which
  // (only turns this window saw unfinished, each once); whether to is asked of the engine when one
  // is due rather than held here, so the switch takes effect for the next answer without a reload.
  // When several land together only the newest is read: one voice at a time, and the latest is the
  // one the person is waiting for.
  const autoReadMemory = useRef<AutoReadMemory>({ watched: new Set(), read: new Set() });
  const [autoReadId, setAutoReadId] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    const due = answersToRead(messages, autoReadMemory.current);
    if (due.length === 0) return;
    const newest = due[due.length - 1].id;
    getAudioStatus()
      .then((status) => {
        if (mounted.current && status.auto_read === true) setAutoReadId(newest);
      })
      .catch(() => {
        // An engine that cannot say is an engine that did not ask for it: the answer is on screen,
        // with its own button.
      });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="relative flex-1 flex flex-col items-center justify-center p-6 text-center max-w-2xl mx-auto">
        {/* No rain here, and that is the fix rather than a loss: the OLED theme's
            backdrop is mounted once in the shell (`App.tsx`), behind the sidebar
            and the transcript alike. It used to sit in this branch, which made it
            a `max-w-2xl` column of glyphs that vanished the moment a message
            existed — the mount point was the bug, not the animation. */}
        {/* The name, and nothing else. This used to be an icon, a heading and a
            paragraph describing the pipeline — "select a project folder… inspect
            your codebase, plan atomic steps, propose file diffs, and verify
            tests" — to someone who had not yet typed anything. An empty window
            that explains itself is a window asking to be read before it has been
            used, and the wordmark takes the theme's own colours, so it is also
            the one thing on screen that shows which theme is active.

            It also used to carry four starter-prompt pills underneath, and those
            were the same mistake in a quieter costume: four sentences about work
            this codebase might plausibly be asked to do, sitting under a window
            that has not been asked to do any of it yet. They made the empty state
            a menu, so the one thing worth reading — the name, in the theme you
            just picked — had to compete with four rows of small type. The four
            went, and so did the `mb-10` that existed only to clear them: the
            import button below owns the only margin here now, because it is the
            only thing with something under it.

            Removing the pills left this with one focusable control, and it is the
            wrong one to be the only one: "Import audit report…" opens a file
            picker, so a keyboard user who tabbed into the middle of the window
            hit a dead end. The composer is auto-focused, so on load they do land
            on it — but that is a one-shot on mount, and nothing brings focus back
            once it has gone. Hence the skip link, which is the one control here
            that is *about* the window rather than *in* it, and which is invisible
            until a keyboard asks for it. */}
        <a
          href={`#${COMPOSER_ANCHOR_ID}`}
          className="sr-only focus:not-sr-only focus:absolute focus:left-1/2 focus:top-2 focus:z-10 focus:-translate-x-1/2 focus:rounded-lg focus:border focus:border-codify-accent focus:bg-codify-raised focus:px-3 focus:py-1.5 focus:text-xs focus:font-medium focus:text-codify-primary"
        >
          Skip to the prompt
        </a>

        <Wordmark />

        {/* Import an exported audit document back into the transcript as a
            readable report — a closed artifact, reviewable with no engine. */}
        <button
          type="button"
          onClick={onImportAudit}
          className="relative mt-4 flex items-center gap-2 px-3 py-1.5 rounded-lg bg-codify-surface border border-codify-border hover:border-codify-accent text-xs text-codify-muted hover:text-codify-accent transition-colors"
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
        <div className="flex items-start gap-2 p-2.5 rounded-xl bg-codify-danger/20 border border-codify-danger/60 text-xs text-codify-danger-ink">
          <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-codify-danger" />
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
        // Which goal's trace panel is open, if any. Computed here, once, and passed
        // down as a plain id: the line that used to do this inline sat outside the
        // `msg.goal` guard and read `.id` off undefined for every user message.
        const openTraceId = traceGoalId(msg.goal, traceFor);

        // The pipeline spine: one row per role the orchestrator actually dispatched,
        // with the call count folded in. The per-dispatch lines used to render here
        // instead, and a planner that consults the librarian four times produced four
        // identical "Sub-agent assigned" lines — which reads as four agents, the exact
        // opposite of what one orchestrator walking one pipeline actually did.
        //
        // Roles are taken from the log, never from a list in this file: `ROLES` lives
        // in engine/models.py, and a second copy is how a screen drifts from its
        // enforcement. See ui/src/pipeline.ts.
        const stages = foldStages(
          msg.events ?? [],
          (stepId) =>
            (msg.goal?.steps ?? []).find((s: PlanStep) => s.id === stepId)
              ?.title,
        );
        // While the goal is live the most recent dispatch is the one in flight. Once
        // it is terminal there is no active stage, and every row reads done — a
        // finished run has nothing pending, and saying otherwise would be a status
        // pill inventing a diagnosis.
        const lastDispatch = [...(msg.events ?? [])]
          .reverse()
          .find(
            (e) =>
              e.type === "agent_assigned" &&
              typeof e.payload?.role === "string",
          );
        const activeRole = isGoalActive(msg.goal?.status)
          ? ((lastDispatch?.payload?.role as string | undefined) ?? null)
          : null;
        // A turn that was answered is a conversation; a turn that planned is a
        // run and keeps the card. See `turnTranscript.isConversationalTurn`.
        const conversational = !msg.auditDoc && isConversationalTurn(msg.goal);
        // The recording panel, drawn by whichever card the message gets.
        const tracePanel =
          openTraceId && msg.goal ? (
            <TracePanel
              goalId={openTraceId}
              goal={msg.goal}
              onSetTrace={(enabled) => onSetGoalTrace(msg.goal!.id, enabled)}
              onClose={() => setTraceFor(null)}
            />
          ) : null;
        return (
          <div key={msg.id} className="space-y-4">
            {/* User Message */}
            {msg.role === "user" ? (
              /* The user's own words, on a surface the theme owns.
                 This used to be `bg-blue-600 text-white` with a blue-tinted
                 avatar beside it, and both were hardcoded — which is why the
                 prompt was a saturated blue slab in a theme that has no blue in
                 it. Nothing in the transcript's *chrome* may pick a hue: a
                 theme supplies surfaces and text, so anything that is going to
                 carry words composes from `raised`/`primary` and is opaque by
                 construction.                 Who is speaking is said by side and by the bubble's tail
                 corner, not by colour.

                 The border is load-bearing, not decoration: on OLED, `raised`
                 (#061009) and `bg` (#000000) are two steps apart, so a fill-only
                 bubble has no edge and `shadow-sm` does nothing on pure black.
                 The border is what draws the panel on every theme. */
              <div className="flex items-start gap-3 justify-end">
                <div className="bg-codify-raised border border-codify-border text-codify-primary px-4 py-2.5 rounded-2xl rounded-tr-sm max-w-xl min-w-0 text-sm leading-relaxed shadow-sm whitespace-pre-wrap break-words">
                  {msg.content}
                </div>
                <div
                  className="w-7 h-7 rounded-full bg-codify-raised border border-codify-border-strong flex items-center justify-center text-codify-secondary flex-shrink-0"
                  aria-hidden="true"
                >
                  <User className="w-4 h-4" />
                </div>
              </div>
            ) : (
              /* The assistant's side: a conversation for a turn that answered,
                 an execution card for everything else. */
              <div className="flex items-start gap-3">
                <div
                  className="w-7 h-7 rounded-full bg-codify-raised border border-codify-border-strong flex items-center justify-center text-codify-secondary flex-shrink-0 mt-1"
                  aria-hidden="true"
                >
                  <Bot className="w-4 h-4" />
                </div>

                <div
                  ref={scopeRef}
                  className={
                    conversational
                      ? "flex-1 min-w-0 space-y-4"
                      : "flex-1 bg-codify-surface border border-codify-border rounded-2xl p-4 sm:p-5 shadow-lg space-y-4"
                  }
                >
                  {/* An imported audit document renders as a standalone report —
                    it has no live goal, so it short-circuits the whole
                    execution-card chrome; a turn that answered renders as the
                    conversation it is, and everything else as the run it was. */}
                  {conversational ? (
                    <TurnExchange
                      msg={msg}
                      autoRead={autoReadId === msg.id}
                      traceOpen={traceFor === msg.goal?.id}
                      onToggleTrace={() =>
                        setTraceFor(
                          traceFor === msg.goal?.id ? null : (msg.goal?.id ?? null),
                        )
                      }
                      onOpenLink={onOpenLink}
                    />
                  ) : msg.auditDoc ? (
                    <>
                      <div className="flex items-center gap-2 text-xs font-semibold text-codify-secondary border-b border-codify-border/60 pb-3">
                        <FileUp className="w-3.5 h-3.5 text-codify-info" />
                        Imported audit report
                        {msg.content && (
                          <span className="ml-auto font-normal text-2xs text-codify-muted">
                            {msg.content}
                          </span>
                        )}
                      </div>
                      <AuditReport doc={msg.auditDoc as never} />
                    </>
                  ) : (
                    <>
                      {/* Why the goal is paused, above the buttons that resume it. Only an engine pause has a
                          reason; the person's own Pause shows nothing here. */}
                      {(() => {
                        const pause = pauseReasonOf(msg.goal?.status, msg.events);
                        return pause ? <PauseBanner pause={pause} /> : null;
                      })()}
                      {/* Header: Title and Status */}
                      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-codify-border/60 pb-3">
                        <div className="flex items-center gap-2">
                          <span className="font-semibold text-sm text-codify-secondary">
                            {msg.goal?.title || "Agent Execution"}
                          </span>
                          {msg.goal && (
                            <span title={`Goal status: ${msg.goal.status}`}>
                              <Badge tone={statusTone(msg.goal.status)}>
                                {msg.goal.status}
                              </Badge>
                            </span>
                          )}
                          {msg.goal?.dry_run && (
                            <span className="text-2xs px-1.5 py-0.5 rounded bg-codify-warning/20 border border-codify-warning/60 text-codify-warning-ink font-semibold">
                              DRY RUN
                            </span>
                          )}
                          {msg.goal?.plan_only && (
                            <span className="text-2xs px-1.5 py-0.5 rounded bg-codify-info/20 border border-codify-info/60 text-codify-info-ink font-semibold">
                              PLAN ONLY
                            </span>
                          )}
                          {msg.goal?.parallel && (
                            <span
                              className="text-2xs px-1.5 py-0.5 rounded bg-codify-design/20 border border-codify-design/60 text-codify-design-ink font-semibold"
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
                              "text-2xs px-1.5 py-0.5 rounded border font-semibold transition-colors cursor-pointer";
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
                            const positionSuffix = (
                              kind: AuditMarkerKind,
                            ): string => {
                              const scope = cardScopes.current.get(msg.id);
                              const total =
                                scope?.querySelectorAll(
                                  `[data-audit-marker="${kind}"]`,
                                ).length ?? 0;
                              const viewed = auditMarkerViewed.get(
                                `${msg.id}:${kind}`,
                              );
                              if (!scope || total === 0 || viewed === undefined)
                                return "";
                              return ` — showing ${ordinalLabel(viewed + 1)} of ${total}`;
                            };
                            const jump = (kind: AuditMarkerKind) => () => {
                              const scope = cardScopes.current.get(msg.id);
                              if (!scope) return;
                              const total = scope.querySelectorAll(
                                `[data-audit-marker="${kind}"]`,
                              ).length;
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
                                    className={`${badgeCls} bg-codify-design/20 border-codify-design/60 text-codify-design-ink hover:bg-codify-design/30`}
                                    title={`Plan steps were edited after planning — click to cycle through each edit${positionSuffix("edits")}`}
                                    aria-label={`Plan steps were edited after planning — click to cycle through each edit${positionSuffix("edits")}`}
                                  >
                                    {summary.edits} edit
                                    {summary.edits === 1 ? "" : "s"}
                                  </button>
                                )}
                                {summary.fallbacks > 0 && (
                                  <button
                                    type="button"
                                    onClick={jump("fallbacks")}
                                    className={`${badgeCls} bg-codify-knowledge/20 border-codify-knowledge/60 text-codify-knowledge-ink hover:bg-codify-knowledge/30`}
                                    title={`Model calls that fell back — click to cycle through each fallback${positionSuffix("fallbacks")}`}
                                    aria-label={`Model calls that fell back — click to cycle through each fallback${positionSuffix("fallbacks")}`}
                                  >
                                    {summary.fallbacks} fallback
                                    {summary.fallbacks === 1 ? "" : "s"}
                                  </button>
                                )}
                                {summary.errors > 0 && (
                                  <button
                                    type="button"
                                    onClick={jump("errors")}
                                    className={`${badgeCls} bg-codify-danger/20 border-codify-danger/60 text-codify-danger-ink hover:bg-codify-danger/30`}
                                    title={`Errors recorded during the run — click to cycle through each error${positionSuffix("errors")}`}
                                    aria-label={`Errors recorded during the run — click to cycle through each error${positionSuffix("errors")}`}
                                  >
                                    {summary.errors} error
                                    {summary.errors === 1 ? "" : "s"}
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
                              (msg.goal.status === "PENDING" ||
                                msg.goal.status === "PAUSED") && (
                                <button
                                  type="button"
                                  onClick={() =>
                                    onEnableExecution(
                                      msg.goal!.id,
                                      msg.goal!.version,
                                    )
                                  }
                                  className="flex items-center gap-1 px-2.5 py-1 bg-codify-warning text-codify-bg hover:brightness-110 text-xs font-semibold rounded-lg shadow transition-colors"
                                  title="Lift the plan-only guard and begin execution"
                                >
                                  <Play className="w-3 h-3" /> Execute Plan
                                </button>
                              )}

                            {!msg.goal.plan_only &&
                              (msg.goal.status === "PENDING" ||
                                msg.goal.status === "PAUSED") && (
                                <button
                                  type="button"
                                  onClick={() =>
                                    onStartGoal(msg.goal!.id, msg.goal!.version)
                                  }
                                  className="flex items-center gap-1 px-2.5 py-1 bg-codify-success text-codify-bg hover:brightness-110 text-xs font-semibold rounded-lg shadow transition-colors"
                                >
                                  <Play className="w-3 h-3" /> Start
                                </button>
                              )}

                            {msg.goal.dry_run &&
                              (msg.goal.status === "COMPLETED" ||
                                msg.goal.status === "FAILED") && (
                                <button
                                  type="button"
                                  onClick={() => onApplyGoal(msg.goal!.id)}
                                  className="flex items-center gap-1 px-2.5 py-1 bg-codify-warning text-codify-bg hover:brightness-110 text-xs font-semibold rounded-lg shadow transition-colors"
                                  title="Write the proposed changes for real, then re-run tests, review, and commit"
                                >
                                  <Check className="w-3 h-3" /> Apply these
                                  changes
                                </button>
                              )}

                            {msg.goal.status === "RUNNING" && (
                              <button
                                type="button"
                                onClick={() =>
                                  onPauseGoal(msg.goal!.id, msg.goal!.version)
                                }
                                className="flex items-center gap-1 px-2.5 py-1 bg-codify-warning text-codify-bg hover:brightness-110 text-xs font-semibold rounded-lg shadow transition-colors"
                              >
                                <Pause className="w-3 h-3" /> Pause
                              </button>
                            )}

                            {/* Cancel while there is still something to cancel, which
                          includes PLANNING. That state was missing here, and it is
                          the wrong one to be missing: a planning goal has not
                          written a file yet, so cancelling costs nothing and is the
                          best moment a user can possibly change their mind. The
                          engine allows cancel from PLANNING (engine/app.py) and
                          `MEANINGFUL_FROM.cancel` says so too — this button was the
                          only one of the three that disagreed, which is exactly how
                          a permitted action becomes unreachable. `canStopGoal` is
                          the one list all three now read. */}
                            {canStopGoal(msg.goal.status) && (
                              <button
                                type="button"
                                onClick={() =>
                                  onCancelGoal(msg.goal!.id, msg.goal!.version)
                                }
                                className="p-1 text-codify-muted hover:text-codify-danger rounded-lg transition-colors"
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
                                onClick={() =>
                                  onDeleteGoal(msg.goal!.id, msg.goal!.title)
                                }
                                className="p-1 text-codify-muted hover:text-codify-danger rounded-lg transition-colors"
                                title="Delete this goal and its event log (your files are not touched)"
                                aria-label="Delete goal"
                              >
                                <Trash2 className="w-4 h-4" />
                              </button>
                            )}

                            {/* Recording: this run's model calls, kept so it can be
                          replayed without a provider. Shown for a run that was
                          armed, and for one that has not started yet — that second
                          case is the only moment the engine will let a recording
                          begin, and a control that is never offered there is a
                          capability nobody has. A finished unrecorded run still
                          shows nothing, because an empty panel on every one of them
                          would teach people to ignore it. */}
                            {(msg.goal?.trace ||
                              canArmTrace(msg.goal?.status ?? "")) &&
                              traceFor !== msg.goal!.id && (
                              <button
                                type="button"
                                onClick={() => setTraceFor(msg.goal!.id)}
                                className="p-1 text-codify-muted hover:text-codify-warning rounded-lg transition-colors"
                                title="This run was recorded — inspect or delete its model calls"
                                aria-label="Show recording"
                              >
                                <Radio className="w-4 h-4" />
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
                                  const audit = await getGoalAudit(
                                    msg.goal!.id,
                                  );
                                  const blob = new Blob(
                                    [JSON.stringify(audit, null, 2)],
                                    {
                                      type: "application/json",
                                    },
                                  );
                                  const url = URL.createObjectURL(blob);
                                  const a = document.createElement("a");
                                  const stamp = new Date()
                                    .toISOString()
                                    .replace(/[:.]/g, "-")
                                    .slice(0, 19);
                                  const slug =
                                    (msg.goal!.title || msg.goal!.id)
                                      .toLowerCase()
                                      .replace(/[^a-z0-9]+/g, "-")
                                      .replace(/^-+|-+$/g, "")
                                      .slice(0, 40) || msg.goal!.id.slice(0, 8);
                                  a.href = url;
                                  a.download = `audit-${slug}-${stamp}.json`;
                                  a.click();
                                  URL.revokeObjectURL(url);
                                } catch (err: any) {
                                  setExportError(
                                    readRejection(err, "Could not export the audit trail."),
                                  );
                                }
                              }}
                              className="p-1 text-codify-muted hover:text-codify-info rounded-lg transition-colors"
                              title="Export audit trail (plan edits, fallbacks, failures)"
                              aria-label="Export audit trail"
                            >
                              <FileDown className="w-4 h-4" />
                            </button>
                          </div>
                        )}
                      </div>

                      {tracePanel}

                      {/* The conductor's own notes on this goal. Drawn only when it kept some, and apart from
                          the plan below: they are its note to its next run, not something to approve. */}
                      {(() => {
                        const notes = visibleTodos(msg.events);
                        return notes.length > 0 ? <TodoCard items={notes} /> : null;
                      })()}

                      {/* Plan Steps Accordion */}
                      {msg.goal?.steps && msg.goal.steps.length > 0 && (
                        <div className="space-y-2">
                          <div className="flex items-center justify-between gap-2">
                            <div className="text-xs font-semibold text-codify-muted uppercase tracking-wider">
                              Execution Plan ({msg.goal.steps.length} Steps)
                            </div>
                            {/* How many steps are in flight right now. >1 is the
                          parallel-mode proof; the whole point of the flag. */}
                            {msg.goal.status === "RUNNING" &&
                              msg.goal.steps.filter(
                                (s: PlanStep) => s.status === "IN_PROGRESS",
                              ).length > 1 && (
                                <span className="flex items-center gap-1 text-2xs font-mono px-2 py-0.5 rounded-full bg-codify-info/25 border border-codify-info/60 text-codify-info-ink">
                                  <PlayCircle className="w-3 h-3 animate-pulse" />
                                  {
                                    msg.goal.steps.filter(
                                      (s: PlanStep) =>
                                        s.status === "IN_PROGRESS",
                                    ).length
                                  }{" "}
                                  steps in parallel
                                </span>
                              )}
                          </div>
                          <div className="space-y-2">
                            {(() => {
                              // Shared timeline axis: from the first step start to the
                              // last step end (or "now" while anything is running), so
                              // every bar sits on the same scale and overlap is visible
                              // as aligned bars, not just claimed by color.
                              const windowsByStep = msg.events?.length
                                ? buildStepWindows(msg.events)
                                : new Map();
                              let axisStart = Infinity;
                              let axisEnd: number | null = null;
                              for (const ws of windowsByStep.values()) {
                                for (const w of ws) {
                                  axisStart = Math.min(axisStart, w.start);
                                  axisEnd =
                                    w.end === null
                                      ? null
                                      : axisEnd === null
                                        ? Math.max(axisEnd ?? 0, w.end)
                                        : Math.max(axisEnd, w.end);
                                }
                              }
                              const axis = Number.isFinite(axisStart)
                                ? { start: axisStart, end: axisEnd }
                                : null;
                              const anyRunning = msg.goal?.status === "RUNNING";
                              return msg.goal.steps.map((step: PlanStep) => {
                                const windows =
                                  windowsByStep.get(step.id) ?? [];
                                return (
                                  <div
                                    key={step.id}
                                    className="bg-codify-bg border border-codify-border rounded-xl p-3 flex flex-col gap-1.5"
                                  >
                                    {/* The title may shrink and wrap; the status cluster may not. It was the other
                                  way round, so in a narrow column the title kept its width and the status pill
                                  was broken a letter to a line. */}
                                    <div className="flex items-start justify-between gap-2">
                                      <div className="flex min-w-0 flex-1 items-center gap-2">
                                        {/* One icon per tone, chosen from the same table the
                                  badges use, so an icon and a pill about the same
                                  state can never be different colours. The
                                  `animate-pulse` that was on the in-progress icon is
                                  gone: a running step is already reported by its own
                                  pill, and a looping animation in a list of
                                  everything is noise (DESIGN.md §7). */}
                                        {step.status === "COMPLETED" && (
                                          <CheckCircle2
                                            className={`w-4 h-4 flex-shrink-0 ${toneText(stepTone(step.status))}`}
                                          />
                                        )}
                                        {step.status === "IN_PROGRESS" && (
                                          <PlayCircle
                                            className={`w-4 h-4 flex-shrink-0 ${toneText(stepTone(step.status))}`}
                                          />
                                        )}
                                        {step.status === "PENDING" && (
                                          <Clock
                                            className={`w-4 h-4 flex-shrink-0 ${toneText(stepTone(step.status))}`}
                                          />
                                        )}
                                        {step.status === "FAILED" && (
                                          <AlertCircle
                                            className={`w-4 h-4 flex-shrink-0 ${toneText(stepTone(step.status))}`}
                                          />
                                        )}
                                        <span className="min-w-0 break-words text-xs font-semibold text-codify-secondary">
                                          Step {step.ordinal + 1}: {step.title}
                                        </span>
                                        {/* This step is one of several running right now —
                                  the visual proof that parallel mode is real. */}
                                        {step.status === "IN_PROGRESS" &&
                                          msg.goal?.steps &&
                                          msg.goal.steps.filter(
                                            (s: PlanStep) =>
                                              s.status === "IN_PROGRESS",
                                          ).length > 1 && (
                                            <Badge
                                              tone="info"
                                              className="rounded"
                                              title="Running concurrently with the other highlighted steps"
                                            >
                                              <Workflow className="w-2.5 h-2.5 mr-1" />
                                              PARALLEL
                                            </Badge>
                                          )}
                                      </div>

                                      {/* Edit Plan affordance: plan-only goals still awaiting execution */}
                                      {msg.goal?.plan_only &&
                                        msg.goal.status === "PENDING" &&
                                        step.status === "PENDING" &&
                                        !(
                                          editing?.goalId === msg.goal.id &&
                                          editing?.stepId === step.id
                                        ) && (
                                          <button
                                            type="button"
                                            onClick={() =>
                                              setEditing({
                                                goalId: msg.goal!.id,
                                                stepId: step.id,
                                              })
                                            }
                                            className="flex flex-shrink-0 items-center gap-1 px-2 py-0.5 bg-codify-info/20 hover:bg-codify-info/40 text-codify-info-ink border border-codify-info/50 rounded text-xs font-semibold transition-colors"
                                            title="Edit this step's title, description, and target paths"
                                          >
                                            <Pencil className="w-3 h-3" /> Edit
                                          </button>
                                        )}

                                      {/* Step Status or Retry */}
                                      <div className="flex flex-shrink-0 items-center gap-2">
                                        {step.status === "FAILED" ||
                                        (step.status === "IN_PROGRESS" &&
                                          step.review_notes) ? (
                                          <button
                                            type="button"
                                            onClick={() =>
                                              onRetryStep(
                                                msg.goal!.id,
                                                step.id,
                                                msg.goal!.version,
                                              )
                                            }
                                            className="flex items-center gap-1 px-2 py-0.5 bg-codify-warning/30 hover:bg-codify-warning/40 text-codify-warning-ink border border-codify-warning/50 rounded text-xs font-semibold transition-colors"
                                          >
                                            <RotateCcw className="w-3 h-3" />{" "}
                                            Retry
                                          </button>
                                        ) : null}

                                        <span className="whitespace-nowrap text-2xs uppercase font-mono px-1.5 py-0.5 rounded bg-codify-surface text-codify-muted">
                                          {step.status}
                                        </span>
                                      </div>
                                    </div>

                                    {editing?.goalId === msg.goal?.id &&
                                    editing?.stepId === step.id ? (
                                      <PlanStepEditor
                                        step={step}
                                        version={msg.goal!.version}
                                        onSave={(patch) =>
                                          onEditStep(
                                            msg.goal!.id,
                                            step.id,
                                            msg.goal!.version,
                                            patch,
                                          )
                                        }
                                        onCancel={() => setEditing(null)}
                                      />
                                    ) : (
                                      <>
                                        <p className="text-xs text-codify-muted pl-6 leading-relaxed">
                                          {/* A planner writes `code` and **emphasis** here; marks only, because a
                                              step description is a sentence and not a document. */}
                                          <Markdown inline text={step.description} onOpenLink={onOpenLink} />
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
                                            running={
                                              anyRunning &&
                                              step.status === "IN_PROGRESS"
                                            }
                                          />
                                        )}

                                        {step.review_notes && (
                                          <div className="ml-6 mt-1 p-2 rounded-lg bg-codify-warning/15 border border-codify-warning/60 text-xs text-codify-warning-ink flex items-start gap-1.5">
                                            <ShieldCheck className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                                            <div>
                                              <span className="font-semibold">
                                                Critic Notes:{" "}
                                              </span>
                                              {step.review_notes}
                                            </div>
                                          </div>
                                        )}

                                        {step.commit_message && (
                                          <div className="ml-6 mt-1 text-xs font-mono text-codify-muted flex items-center gap-1">
                                            <Check className="w-3 h-3 text-codify-success" />
                                            <span>
                                              Commit: {step.commit_message}
                                            </span>
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
                        <div className="space-y-3 pt-2 border-t border-codify-border/60">
                          <div className="text-xs font-semibold text-codify-muted uppercase tracking-wider flex items-center gap-1.5">
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
                                className={`ml-auto flex items-center gap-1 px-2 py-0.5 rounded-full border text-2xs font-semibold normal-case tracking-normal transition-colors cursor-pointer ${
                                  issueOnly
                                    ? "bg-codify-warning/20 text-codify-warning-ink border-codify-warning/60"
                                    : "bg-codify-bg text-codify-muted border-codify-border hover:text-codify-secondary"
                                }`}
                              >
                                <AlertCircle className="w-3 h-3" />
                                Issues only
                              </button>
                            )}
                          </div>

                          {/* The spine: the pipeline as one line per role, in the order the
   orchestrator walked it. Replaces the repeated "Sub-agent assigned"
   lines further down, which are suppressed so a role is named once,
   not once per call. */}
                          {stages.length > 0 && (
                            <div className="flex flex-col gap-1">
                              {stageStates(stages, activeRole).map((s, i) => {
                                const stage = stages[i];
                                return (
                                  <div
                                    key={s.role}
                                    className="flex items-center gap-2 text-2xs pl-2"
                                  >
                                    <span
                                      aria-hidden
                                      className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                                        s.state === "active"
                                          ? "bg-codify-accent"
                                          : "bg-codify-border-strong"
                                      }`}
                                    />
                                    <span
                                      className={`font-mono uppercase tracking-wider ${
                                        s.state === "active"
                                          ? "text-codify-accent font-semibold"
                                          : "text-codify-muted"
                                      }`}
                                    >
                                      {s.role}
                                    </span>
                                    {stage.calls > 1 && (
                                      <span className="text-codify-muted">
                                        · {callLabel(stage.calls)}
                                      </span>
                                    )}
                                    {stage.model && (
                                      <span className="font-mono text-codify-muted truncate">
                                        · {stage.provider}/{stage.model}
                                      </span>
                                    )}
                                    {stage.steps.length > 0 && (
                                      <span
                                        className="text-codify-muted truncate"
                                        title={stage.steps.join(" · ")}
                                      >
                                        · {stage.steps.join(" · ")}
                                      </span>
                                    )}
                                  </div>
                                );
                              })}
                            </div>
                          )}

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
                                {
                                  text: string;
                                  role: string;
                                  final: boolean;
                                  stepTitle?: string;
                                }
                              > = {};
                              const stepTitleById = new Map<string, string>();
                              (msg.goal?.steps ?? []).forEach((s: PlanStep) =>
                                stepTitleById.set(s.id, s.title),
                              );
                              // "Issues only": the (step, role) streams whose role hit a
                              // real failure this run. Their replies stay — the last words
                              // before a failure are its context — while finished streams
                              // from healthy roles are exactly the noise being filtered.
                              const failedKeys = new Set<string>();
                              if (issueOnly) {
                                msg.events.forEach((ev) => {
                                  if (
                                    (ev.type === "error" ||
                                      ev.type === "agent_call_failed") &&
                                    ev.payload?.role
                                  ) {
                                    failedKeys.add(
                                      `${ev.step_id ?? "goal"}::${ev.payload.role}`,
                                    );
                                  }
                                });
                              }
                              msg.events.forEach((ev) => {
                                if (ev.type === "model_delta") {
                                  const headKey = `${ev.step_id ?? "goal"}::${ev.payload.role}`;
                                  if (issueOnly && !failedKeys.has(headKey))
                                    return;
                                  streamHeads[headKey] = {
                                    text: ev.payload.text,
                                    role: ev.payload.role,
                                    final: !!ev.payload.final,
                                    stepTitle:
                                      (ev.step_id &&
                                        stepTitleById.get(ev.step_id)) ||
                                      undefined,
                                  };
                                  return;
                                }
                                if (issueOnly && !isIssueEvent(ev)) return;
                                // `agent_assigned` is represented by the spine above,
                                // once per role with its call count folded in. Rendering
                                // it again here put four identical "Sub-agent assigned"
                                // lines in the log for a planner that consulted the
                                // librarian four times — which reads as four agents.
                                if (ev.type === "agent_assigned") return;
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
                                      <div className="p-2.5 rounded-lg bg-codify-bg border border-codify-border flex flex-col gap-1.5">
                                        <div className="flex items-center gap-1.5">
                                          <BookOpen className="w-3.5 h-3.5 text-codify-knowledge" />
                                          <span className="font-semibold text-xs uppercase tracking-wider text-codify-muted">
                                            Reconnaissance
                                          </span>
                                          <span className="text-2xs text-codify-muted">
                                            {ev.payload.files?.length ?? 0}{" "}
                                            file(s) cited from{" "}
                                            {ev.payload.counts?.considered ?? 0}{" "}
                                            considered ·{" "}
                                            {ev.payload.rounds ?? 1} round(s)
                                            {ev.payload.counts?.opened !=
                                              null &&
                                              ` · ${ev.payload.counts.opened} opened, ${ev.payload.counts.matched} matched by search`}
                                          </span>
                                        </div>
                                        {ev.payload.summary && (
                                          <p className="text-xs text-codify-secondary leading-relaxed">
                                            {ev.payload.summary}
                                          </p>
                                        )}
                                        {(ev.payload.files?.length ?? 0) >
                                          0 && (
                                          <div className="flex flex-col gap-0.5">
                                            {ev.payload.files.map(
                                              (
                                                f: {
                                                  path: string;
                                                  why?: string;
                                                  evidence?: string;
                                                },
                                                i: number,
                                              ) => (
                                                <div
                                                  key={i}
                                                  className="text-xs flex items-start gap-1.5"
                                                >
                                                  <span className="font-mono text-codify-knowledge">
                                                    {f.path}
                                                  </span>
                                                  {f.evidence && (
                                                    <span className="text-2xs text-codify-muted">
                                                      [{f.evidence}]
                                                    </span>
                                                  )}
                                                  {f.why && (
                                                    <span className="text-codify-muted">
                                                      — {f.why}
                                                    </span>
                                                  )}
                                                </div>
                                              ),
                                            )}
                                          </div>
                                        )}
                                        {ev.payload.test_command && (
                                          <div className="text-xs font-mono text-codify-muted">
                                            test command:{" "}
                                            {ev.payload.test_command.join(" ")}
                                            <span className="text-2xs text-codify-muted ml-1">
                                              (unverified)
                                            </span>
                                          </div>
                                        )}
                                        {ev.payload.conventions &&
                                          ev.payload.conventions.length > 0 && (
                                            <div className="text-xs text-codify-muted">
                                              conventions:{" "}
                                              {ev.payload.conventions.join(
                                                "; ",
                                              )}
                                            </div>
                                          )}
                                        {ev.payload.risks &&
                                          ev.payload.risks.length > 0 && (
                                            <div className="text-xs text-codify-warning">
                                              risks:{" "}
                                              {ev.payload.risks.join("; ")}
                                            </div>
                                          )}
                                        {ev.payload.dropped_paths &&
                                          ev.payload.dropped_paths.length >
                                            0 && (
                                            <div className="flex items-start gap-1.5 text-xs text-codify-warning">
                                              <ShieldAlert className="w-3 h-3 flex-shrink-0 mt-0.5" />
                                              <span className="font-mono">
                                                never opened, dropped:{" "}
                                                {ev.payload.dropped_paths.join(
                                                  ", ",
                                                )}
                                              </span>
                                            </div>
                                          )}
                                      </div>
                                    )}

                                    {/* The body a knowledge goal authored, as the
                              document it is. It reaches the transcript as an
                              event during planning and as a diff once a step has
                              run, and a `+`-prefixed diff is a poor thing to read
                              a file the next run will treat as fact from. */}
                                    {ev.type === "design_contract" &&
                                      ev.payload.mode === "knowledge" && (
                                        <KnowledgeDeliverableCard
                                          goal={msg.goal}
                                          payload={ev.payload}
                                          onOpenLink={onOpenLink}
                                        />
                                      )}

                                    {/* The direction the planner planned against and the fixer
                              was told to obey: locked once, before any step exists,
                              and published so a step can be judged against the
                              contract that shaped it rather than from memory.
                              A knowledge deliverable has its own card above: its
                              body is the artifact, and the design furniture here
                              (tokens, components, acceptance) would describe a
                              contract nothing is bound by. */}
                                    {ev.type === "design_contract" &&
                                      ev.payload.mode !== "knowledge" && (
                                        <DesignDeliverableCard
                                          payload={ev.payload}
                                          goal={msg.goal}
                                          pinOutcome={
                                            pinState?.goalId === msg.goal?.id ? pinState : null
                                          }
                                          pinnedPath={
                                            msg.goal
                                            ? pinnedContracts[msg.goal.workspace_id]
                                            : undefined
                                          }
                                          onPin={handlePinDeliverable}
                                          onOpenLink={onOpenLink}
                                        />
                                      )}

                                    {ev.type === "test_result" && (
                                      <div className="p-2.5 rounded-lg bg-codify-bg border border-codify-border flex flex-col gap-1">
                                        <div className="flex items-center gap-2">
                                          <span
                                            className={`font-semibold uppercase text-xs px-1.5 py-0.5 rounded ${
                                              ev.payload.verdict === "pass"
                                                ? "bg-codify-success/20 text-codify-success-ink border border-codify-success/60"
                                                : ev.payload.verdict === "fail"
                                                  ? "bg-codify-danger/15 text-codify-danger-ink border border-codify-danger/60"
                                                  : "bg-codify-raised text-codify-secondary border border-codify-border"
                                            }`}
                                          >
                                            {ev.payload.verdict}
                                          </span>
                                          {ev.payload.argv && (
                                            <span className="font-mono text-codify-muted">
                                              {ev.payload.argv.join(" ")}
                                            </span>
                                          )}
                                        </div>
                                        {ev.payload.explanation && (
                                          <p className="text-codify-muted pl-1">
                                            {ev.payload.explanation}
                                          </p>
                                        )}
                                        {/* What the engine itself found comparing the written
                                  artifacts against a binding brand contract — advisory,
                                  so it renders as findings, never as the verdict. */}
                                        {ev.payload.brand_drifts &&
                                          ev.payload.brand_drifts.length >
                                            0 && (
                                            <div className="flex flex-col gap-0.5 pt-0.5">
                                              {ev.payload.brand_drifts.map(
                                                (d: string, i: number) => (
                                                  <div
                                                    key={i}
                                                    className="flex items-start gap-1.5 text-xs text-codify-warning"
                                                  >
                                                    <Palette className="w-3 h-3 flex-shrink-0 mt-0.5" />
                                                    <span className="font-mono">
                                                      {d}
                                                    </span>
                                                  </div>
                                                ),
                                              )}
                                            </div>
                                          )}
                                        {/* A verdict with no command behind it must not read like a
                                  run suite: say that the sandbox refused the command. */}
                                        {ev.payload.refused &&
                                          ev.payload.refused.length > 0 && (
                                            <div className="flex flex-col gap-0.5 pt-0.5">
                                              {ev.payload.refused.map(
                                                (r: string, i: number) => (
                                                  <div
                                                    key={i}
                                                    className="flex items-start gap-1.5 text-xs text-codify-warning"
                                                  >
                                                    <ShieldAlert className="w-3 h-3 flex-shrink-0 mt-0.5" />
                                                    <span className="font-mono">
                                                      refused: {r}
                                                    </span>
                                                  </div>
                                                ),
                                              )}
                                              {ev.payload.ran === false && (
                                                <span className="text-xs text-codify-muted pl-4">
                                                  No command ran — this verdict
                                                  is the tester's judgement, not
                                                  a test result.
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
                                      <div className="flex items-start gap-1.5 pl-2 text-codify-info">
                                        <ShieldAlert className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                                        <span className="leading-relaxed">
                                          Fixer asked for another pass
                                          <span className="font-mono text-codify-muted">
                                            {" "}
                                            (pass {ev.payload.attempt},{" "}
                                            {ev.payload.passes_left} of{" "}
                                            {ev.payload.max_passes} left)
                                          </span>
                                        </span>
                                      </div>
                                    )}

                                    {ev.type === "plan_consult" && (
                                      <div className="flex items-start gap-1.5 pl-2 text-codify-knowledge">
                                        <BookOpen className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                                        <span className="leading-relaxed">
                                          Planner asked the librarian for a
                                          follow-up
                                          {ev.payload.refused > 0 && (
                                            <span className="font-mono text-codify-muted">
                                              {" "}
                                              ({ev.payload.refused} request
                                              {ev.payload.refused === 1
                                                ? ""
                                                : "s"}{" "}
                                              refused)
                                            </span>
                                          )}
                                        </span>
                                      </div>
                                    )}

                                    {ev.type === "fix_retry" && (
                                      <div className="flex items-start gap-1.5 pl-2 text-codify-warning">
                                        <ShieldAlert className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                                        <span className="leading-relaxed">
                                          Tests failed — asking the fixer to try
                                          again
                                          <span className="font-mono text-codify-muted">
                                            {" "}
                                            (attempt {ev.payload.attempt}/
                                            {ev.payload.max_attempts})
                                          </span>
                                          {ev.payload.reason && (
                                            <span className="block text-xs text-codify-muted font-mono">
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
                                    {ev.type === "plan_updated" &&
                                      (() => {
                                        const ch = ev.payload?.changes ?? {};
                                        const stepTitle =
                                          (msg.goal?.steps ?? []).find(
                                            (s: PlanStep) =>
                                              s.id === ev.step_id,
                                          )?.title ??
                                          ev.payload?.step_title ??
                                          "step";
                                        const paths = ch.suggested_paths;
                                        const pathChanged =
                                          paths &&
                                          JSON.stringify(paths.before) !==
                                            JSON.stringify(paths.after);
                                        const titleChanged =
                                          ch.title &&
                                          ch.title.before !== ch.title.after;
                                        const fmtPaths = (ps: string[]) =>
                                          ps.length ? ps.join(", ") : "(none)";
                                        // Every changed field gets its own line: a combined
                                        // edit (paths + rename) must be auditable in full,
                                        // not reduced to whichever branch won the if/else.
                                        return (
                                          <div
                                            className="flex items-start gap-1.5 pl-2 text-codify-design rounded"
                                            data-audit-marker="edits"
                                          >
                                            <Pencil className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                                            <span className="leading-relaxed">
                                              Plan edited —{" "}
                                              <span className="font-semibold">
                                                {stepTitle}
                                              </span>
                                              {pathChanged && (
                                                <span className="block font-mono text-xs text-codify-muted">
                                                  paths:{" "}
                                                  <span className="text-codify-danger line-through">
                                                    {fmtPaths(paths.before)}
                                                  </span>{" "}
                                                  →{" "}
                                                  <span className="text-codify-success">
                                                    {fmtPaths(paths.after)}
                                                  </span>
                                                </span>
                                              )}
                                              {titleChanged && (
                                                <span className="block text-codify-muted">
                                                  renamed from “
                                                  {ch.title.before}”
                                                </span>
                                              )}
                                              {ch.description &&
                                                ch.description.before !==
                                                  ch.description.after && (
                                                  <span className="block text-codify-muted">
                                                    description updated
                                                  </span>
                                                )}
                                              {!pathChanged &&
                                                !titleChanged &&
                                                !(
                                                  ch.description &&
                                                  ch.description.before !==
                                                    ch.description.after
                                                ) && (
                                                  <span className="text-codify-muted">
                                                    updated
                                                  </span>
                                                )}
                                            </span>
                                          </div>
                                        );
                                      })()}

                                    {/* The primary target was skipped. Shown as its own line
                              because the reply that follows came from a different
                              model, and the transcript must not credit the wrong one. */}
                                    {ev.type === "provider_fallback" && (
                                      <div
                                        className="flex items-start gap-1.5 pl-2 text-codify-knowledge rounded"
                                        data-audit-marker="fallbacks"
                                      >
                                        <Route className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                                        <span className="leading-relaxed">
                                          <span className="font-semibold">
                                            {ev.payload.role}
                                          </span>{" "}
                                          fell back to{" "}
                                          <span className="font-mono text-codify-secondary">
                                            {ev.payload.to?.provider}/
                                            {ev.payload.to?.model}
                                          </span>{" "}
                                          —{" "}
                                          <span className="font-mono text-codify-muted">
                                            {ev.payload.from?.provider}/
                                            {ev.payload.from?.model ||
                                              "no model"}
                                          </span>{" "}
                                          could not be used
                                          {ev.payload.detail && (
                                            <span className="text-codify-muted">
                                              {" "}
                                              ({ev.payload.detail})
                                            </span>
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
                                        className="flex items-start gap-1.5 pl-2 text-codify-warning rounded"
                                        data-audit-marker="errors"
                                      >
                                        <ShieldAlert className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                                        <span className="leading-relaxed">
                                          <span className="font-semibold">
                                            {ev.payload.role}
                                          </span>{" "}
                                          could not call{" "}
                                          <span className="font-mono text-codify-secondary">
                                            {ev.payload.provider}/
                                            {ev.payload.model}
                                          </span>{" "}
                                          ({ev.payload.target}) —{" "}
                                          <span className="font-mono">
                                            {ev.payload.code}
                                          </span>
                                          {ev.payload.duration_ms != null && (
                                            <span className="text-codify-muted">
                                              {" "}
                                              after{" "}
                                              {ev.payload.duration_ms < 1000
                                                ? `${Math.round(ev.payload.duration_ms)}ms`
                                                : `${(ev.payload.duration_ms / 1000).toFixed(1)}s`}
                                            </span>
                                          )}
                                        </span>
                                      </div>
                                    )}

                                    {ev.type === "file_change_summary" && (
                                      <div className="flex items-start gap-1.5 pl-2 text-codify-info">
                                        <FileCode className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                                        <span>
                                          Files touched:{" "}
                                          {ev.payload.paths?.length
                                            ? ev.payload.paths.join(", ")
                                            : "none"}
                                          {ev.payload.dry_run && (
                                            <span className="ml-1 text-codify-warning">
                                              (dry run — nothing written)
                                            </span>
                                          )}
                                          {/* The fixer proposed these and the contents already
                                    matched, so nothing was written and nothing is
                                    claimed. Named rather than dropped: "none" alone
                                    reads as though the fixer said nothing. */}
                                          {!!ev.payload.unchanged?.length && (
                                            <span className="ml-1 text-codify-muted">
                                              ({ev.payload.unchanged.join(", ")}{" "}
                                              already matched — left alone)
                                            </span>
                                          )}
                                        </span>
                                      </div>
                                    )}

                                    {ev.type === "log" && (
                                      <div
                                        className={`font-mono text-xs pl-2 border-l-2 ${
                                          ev.payload.level === "warn"
                                            ? "border-codify-warning text-codify-warning"
                                            : ev.payload.level === "error"
                                              ? "border-codify-danger text-codify-danger"
                                              : "border-codify-info text-codify-secondary"
                                        }`}
                                      >
                                        {ev.payload.message}
                                      </div>
                                    )}

                                    {ev.type === "error" && (
                                      <div
                                        className="p-2 rounded-lg bg-codify-danger/20 border border-codify-danger/60 text-codify-danger-ink font-semibold flex flex-col gap-2"
                                        data-audit-marker="errors"
                                      >
                                        <span>
                                          Error [{ev.payload.code}]:{" "}
                                          {ev.payload.message}
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
                                          className="flex w-fit items-center gap-1.5 px-2 py-1 bg-codify-raised hover:bg-codify-border border border-codify-border text-codify-secondary text-xs font-semibold rounded transition-colors"
                                        >
                                          <ShieldAlert className="w-3 h-3 text-codify-danger" />
                                          Why did this fail?
                                        </button>
                                      </div>
                                    )}
                                  </div>,
                                );
                              });
                              // The live reply cards, one per (step, role), newest snapshot:
                              const streamKeys = Object.keys(streamHeads);
                              // Filtered down to nothing: say so, rather than rendering a
                              // bare section header over an empty box.
                              if (
                                issueOnly &&
                                rendered.length === 0 &&
                                streamKeys.length === 0
                              ) {
                                rendered.push(
                                  <div
                                    key="issues-empty"
                                    className="text-xs text-codify-muted pl-2 border-l-2 border-codify-warning/60 py-0.5"
                                  >
                                    Nothing else to show — no warnings or errors
                                    in this run.
                                  </div>,
                                );
                              }
                              if (streamKeys.length > 0) {
                                rendered.push(
                                  <div
                                    key="model-stream"
                                    className="space-y-1.5"
                                  >
                                    {streamKeys.map((k) => {
                                      const head = streamHeads[k];
                                      // Finished replies are closed by default; a reply that
                                      // is still arriving is open, because it is the one
                                      // thing on screen that is actively changing.
                                      const open = head.final
                                        ? openReplies[k] === true
                                        : true;
                                      return (
                                        <div
                                          key={k}
                                          className={`rounded-lg border font-mono text-xs leading-relaxed ${
                                            head.final
                                              ? "bg-codify-surface border-codify-border text-codify-muted"
                                              : "bg-codify-bg border-codify-info/60 text-codify-info"
                                          }`}
                                        >
                                          <button
                                            type="button"
                                            aria-expanded={open}
                                            onClick={() =>
                                              head.final &&
                                              setOpenReplies((prev) => ({
                                                ...prev,
                                                [k]: !prev[k],
                                              }))
                                            }
                                            className={`w-full text-left px-2 pt-2 flex items-start gap-1.5 text-2xs uppercase tracking-wider ${
                                              head.final
                                                ? "cursor-pointer hover:text-codify-secondary"
                                                : "cursor-default"
                                            } ${head.final ? "" : "pb-1"}`}
                                          >
                                            <Terminal className="w-3 h-3 mt-px flex-shrink-0" />
                                            {head.role} replied
                                            {head.stepTitle && (
                                              <span className="normal-case font-sans text-codify-muted">
                                                · {head.stepTitle}
                                              </span>
                                            )}
                                            {!head.final && (
                                              <span className="animate-pulse">
                                                ▍
                                              </span>
                                            )}
                                            {head.final && (
                                              <ChevronRight
                                                className={`w-3 h-3 mt-px flex-shrink-0 transition-transform ${
                                                  open ? "rotate-90" : ""
                                                }`}
                                              />
                                            )}
                                          </button>
                                          {open ? (
                                            <div className="whitespace-pre-wrap break-words max-h-48 overflow-y-auto px-2 pb-2">
                                              {head.text || "…"}
                                            </div>
                                          ) : (
                                            // Closed: the summary, not the payload. A
                                            // `title` carries the same line for anyone
                                            // hovering, and the full text is one click away.
                                            <div
                                              className="px-2 pb-2 pt-1 font-sans text-xs text-codify-secondary border-t border-codify-border/60 mt-1.5"
                                              title={replyPreview(head.text)}
                                            >
                                              {replyPreview(head.text)}
                                            </div>
                                          )}
                                        </div>
                                      );
                                    })}
                                  </div>,
                                );
                              }
                              return rendered;
                            })()}
                          </div>
                        </div>
                      )}
                    </>
                  )}
                  {conversational && tracePanel}
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
