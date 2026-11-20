import React, { useState } from "react";
import {
  Pencil,
  Route,
  AlertCircle,
  RotateCcw,
  CheckCircle2,
  XCircle,
  Clock,
  Workflow,
  Coins,
  BellOff,
  ChevronDown,
  ChevronRight,
  Eye,
} from "lucide-react";
import { ReviewReport, looksLikeReviewReport, type ReviewReportPayload } from "./ReviewReport";

/**
 * A downloaded audit document, rendered back into a readable report.
 *
 * The shape is the engine's GET /goals/{id}/audit response — validated
 * structurally before import (see `looksLikeAudit`), then displayed lane by
 * lane. Nothing here talks to the engine: an import is a closed artifact, so
 * it reads identically whether the run's engine is online or long gone.
 */

export interface AuditDoc {
  goal_id?: string;
  prompt?: string;
  final_status?: string;
  created_at?: number;
  mode?: { plan_only?: boolean; dry_run?: boolean; parallel?: boolean };
  status_timeline?: { status?: string; version?: number; at?: number }[];
  plan_edits?: {
    step?: string | null;
    fields?: string[];
    changes?: Record<string, { from?: unknown; to?: unknown }>;
    at?: number;
  }[];
  fallbacks?: {
    role?: string;
    step?: string | null;
    from?: { provider?: string; model?: string };
    to?: { provider?: string; model?: string };
    code?: string;
    detail?: string;
    at?: number;
  }[];
  fix_retries?: {
    step?: string | null;
    attempt?: number;
    max_attempts?: number;
    reason?: string;
    at?: number;
  }[];
  /** One per reviewed step when `review_deep` was on: the lenses that ran, and what they found (docs/21 P1). */
  review_reports?: (Partial<ReviewReportPayload> & { step?: string | null; at?: number })[];
  errors?: {
    step?: string | null;
    role?: string;
    code?: string;
    message?: string;
    at?: number;
  }[];
  step_outcomes?: {
    step_id?: string;
    step?: string | null;
    status?: string;
    attempts?: number;
    started_at?: number;
    ended_at?: number;
  }[];
  parallel_peak?: number;
  parallel_waves?: number;
  usage?: {
    calls?: number;
    totals?: { input_tokens?: number; output_tokens?: number; total_tokens?: number };
    by_role?: Record<string, { input_tokens?: number; output_tokens?: number; total_tokens?: number; calls?: number }>;
    by_model?: Record<string, { input_tokens?: number; output_tokens?: number; total_tokens?: number; calls?: number }>;
  };
  silent_roles?: { role?: string; assigned_model?: string }[];
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function asArray<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

function asRecords<T extends Record<string, unknown>>(v: unknown): T[] {
  return Array.isArray(v) ? v.filter((item): item is T => isRecord(item)) : [];
}

function fmtText(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

function fmtNum(v: unknown): string {
  return typeof v === "number" && Number.isFinite(v) ? v.toLocaleString() : "0";
}

const OPTIONAL_ARRAY_KEYS = [
  "plan_edits",
  "fallbacks",
  "fix_retries",
  "review_reports",
  "errors",
  "step_outcomes",
  "status_timeline",
  "silent_roles",
] as const;

/** Structural check, not a schema parser: enough to reject wrong or malformed files with a clear message. */
export function looksLikeAudit(doc: unknown): doc is AuditDoc {
  if (!isRecord(doc)) return false;
  const hasLane =
    Array.isArray(doc.plan_edits) ||
    Array.isArray(doc.fallbacks) ||
    Array.isArray(doc.step_outcomes);
  if (!hasLane) return false;
  if (!("goal_id" in doc || "final_status" in doc)) return false;
  for (const key of OPTIONAL_ARRAY_KEYS) {
    if (doc[key] !== undefined && doc[key] !== null && !Array.isArray(doc[key])) {
      return false;
    }
  }
  if (doc.mode !== undefined && doc.mode !== null && !isRecord(doc.mode)) {
    return false;
  }
  if (doc.usage !== undefined && doc.usage !== null && !isRecord(doc.usage)) {
    return false;
  }
  return true;
}

const STATUS_STYLES: Record<string, string> = {
  COMPLETED: "text-codify-success-ink border-codify-success/30 bg-codify-success/10",
  FAILED: "text-codify-danger-ink border-codify-danger/30 bg-codify-danger/10",
  CANCELLED: "text-codify-secondary border-codify-border bg-codify-neutral/10",
  RUNNING: "text-codify-info-ink border-codify-info/30 bg-codify-info/10",
  IN_PROGRESS: "text-codify-info-ink border-codify-info/30 bg-codify-info/10",
  PENDING: "text-codify-muted border-codify-border bg-codify-neutral/10",
  PAUSED: "text-codify-warning-ink border-codify-warning/30 bg-codify-warning/10",
};

function StatusPill({ status }: { status?: string }) {
  if (!status) return null;
  return (
    <span
      className={`px-2 py-0.5 rounded-full border text-2xs font-semibold uppercase tracking-wide ${
        STATUS_STYLES[status] ?? "text-codify-muted border-codify-border bg-codify-neutral/10"
      }`}
    >
      {status}
    </span>
  );
}

function fmtTime(ts?: unknown): string {
  if (typeof ts !== "number" || !Number.isFinite(ts) || ts === 0) return "";
  return new Date(ts * 1000).toLocaleString();
}

function fmtPaths(ps: unknown): string {
  if (!Array.isArray(ps) || ps.length === 0) return "(none)";
  return ps.join(", ");
}

/** Collapsible section: collapsed by default so an uneventful lane costs one line. */
function Section({
  icon,
  title,
  count,
  tone,
  children,
  defaultOpen = false,
}: {
  icon: React.ReactNode;
  title: string;
  count: number;
  tone: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  if (count === 0) return null;
  return (
    <div className="rounded-xl border border-codify-border bg-codify-bg overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-3 py-2 text-xs font-semibold text-codify-secondary hover:bg-codify-surface transition-colors"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        {icon}
        <span>{title}</span>
        <span
          className={`ml-auto px-1.5 py-0.5 rounded-full text-2xs font-bold ${tone}`}
        >
          {count}
        </span>
      </button>
      {open && <div className="px-3 pb-3 pt-1 space-y-2">{children}</div>}
    </div>
  );
}

const fmtValue = (v: unknown): string =>
  Array.isArray(v) ? fmtPaths(v) : typeof v === "string" ? v : JSON.stringify(v);

export function AuditReport({ doc }: { doc: AuditDoc }) {
  const safeDoc: AuditDoc = isRecord(doc) ? doc : {};
  const planEdits = asRecords<NonNullable<AuditDoc["plan_edits"]>[number]>(safeDoc.plan_edits);
  const fallbacks = asRecords<NonNullable<AuditDoc["fallbacks"]>[number]>(safeDoc.fallbacks);
  const fixRetries = asRecords<NonNullable<AuditDoc["fix_retries"]>[number]>(safeDoc.fix_retries);
  const reviews = asArray<unknown>(safeDoc.review_reports).filter(
    (r): r is ReviewReportPayload & { step?: string | null; at?: number } => looksLikeReviewReport(r),
  );
  const errors = asRecords<NonNullable<AuditDoc["errors"]>[number]>(safeDoc.errors);
  const steps = asRecords<NonNullable<AuditDoc["step_outcomes"]>[number]>(safeDoc.step_outcomes);
  const timeline = asRecords<NonNullable<AuditDoc["status_timeline"]>[number]>(safeDoc.status_timeline);
  const silentRoles = asRecords<NonNullable<AuditDoc["silent_roles"]>[number]>(safeDoc.silent_roles);
  const mode = isRecord(safeDoc.mode) ? safeDoc.mode : undefined;
  const usage = isRecord(safeDoc.usage) ? safeDoc.usage : undefined;
  const usageCalls = typeof usage?.calls === "number" && Number.isFinite(usage.calls) ? usage.calls : 0;
  const usageTotals = isRecord(usage?.totals) ? usage.totals : undefined;
  const usageByRole = isRecord(usage?.by_role)
    ? Object.entries(usage.by_role).filter(([, b]) => isRecord(b))
    : [];
  const usageByModel = isRecord(usage?.by_model)
    ? Object.entries(usage.by_model).filter(([, b]) => isRecord(b))
    : [];
  const parallelPeak =
    typeof safeDoc.parallel_peak === "number" && Number.isFinite(safeDoc.parallel_peak)
      ? safeDoc.parallel_peak
      : 1;
  const parallelVisible = parallelPeak > 1;

  const stepName = (label?: unknown, fallback = "step") => {
    if (typeof label === "string" && label) return label.replace(/\s*\([^)]*\)\s*$/, "");
    if (typeof label === "number") return String(label);
    return fallback;
  };

  return (
    <div className="mt-2 space-y-2 text-left">
      {/* Header: the outcome, the prompt, the mode */}
      <div className="rounded-xl border border-codify-border bg-codify-surface p-3 space-y-1.5">
        <div className="flex items-center gap-2 flex-wrap">
          <StatusPill status={typeof safeDoc.final_status === "string" ? safeDoc.final_status : undefined} />
          {mode?.plan_only && <StatusPill status="PLAN ONLY" />}
          {mode?.dry_run && <StatusPill status="DRY RUN" />}
          {mode?.parallel && <StatusPill status="PARALLEL" />}
          {parallelVisible && (
            <span className="text-2xs text-codify-info font-medium">
              peak {parallelPeak} in parallel · {fmtText(safeDoc.parallel_waves ?? 0)} wave(s)
            </span>
          )}
          {typeof safeDoc.created_at === "number" && safeDoc.created_at !== 0 && (
            <span className="ml-auto text-2xs text-codify-muted">{fmtTime(safeDoc.created_at)}</span>
          )}
        </div>
        {safeDoc.prompt && (
          <div className="text-xs text-codify-secondary font-medium">“{fmtText(safeDoc.prompt)}”</div>
        )}
        {safeDoc.goal_id && (
          <div className="font-mono text-2xs text-codify-muted break-all">{fmtText(safeDoc.goal_id)}</div>
        )}
      </div>

      {/* Plan edits: the drift lane — what was planned vs what ran */}
      <Section
        icon={<Pencil className="w-3.5 h-3.5 text-codify-design" />}
        title="Plan edits"
        count={planEdits.length}
        tone="bg-codify-design/15 text-codify-design-ink"
        defaultOpen
      >
        {planEdits.map((edit, i) => {
          const changes = isRecord(edit.changes) ? Object.entries(edit.changes) : [];
          return (
            <div key={i} className="rounded-lg border border-codify-border p-2.5 space-y-1.5">
              <div className="text-xs font-semibold text-codify-design">
                {stepName(edit.step, "step")}
              </div>
              {changes.map(([field, ch]) => {
                const diff = isRecord(ch) ? ch : { from: undefined, to: ch };
                return (
                  <div key={field} className="font-mono text-xs text-codify-muted">
                    {field}:{" "}
                    <span className="text-codify-danger line-through">{fmtValue(diff.from)}</span>
                    {" → "}
                    <span className="text-codify-success">{fmtValue(diff.to)}</span>
                  </div>
                );
              })}
              <div className="text-2xs text-codify-muted">{fmtTime(edit.at)}</div>
            </div>
          );
        })}
      </Section>

      {/* Fallbacks: who was skipped, why, who answered */}
      <Section
        icon={<Route className="w-3.5 h-3.5 text-codify-knowledge" />}
        title="Provider fallbacks"
        count={fallbacks.length}
        tone="bg-codify-knowledge/15 text-codify-knowledge-ink"
      >
        {fallbacks.map((fb, i) => {
          const from = isRecord(fb.from) ? fb.from : undefined;
          const to = isRecord(fb.to) ? fb.to : undefined;
          return (
            <div key={i} className="rounded-lg border border-codify-border p-2.5 space-y-1">
              <div className="text-xs">
                <span className="font-semibold text-codify-knowledge">{fmtText(fb.role)}</span>
                {fb.step && (
                  <span className="text-codify-muted"> · {stepName(fb.step)}</span>
                )}
              </div>
              <div className="font-mono text-xs text-codify-muted">
                <span className="text-codify-danger line-through">
                  {fmtText(from?.provider)}/{fmtText(from?.model)}
                </span>
                {" → "}
                <span className="text-codify-success">
                  {fmtText(to?.provider)}/{fmtText(to?.model)}
                </span>
              </div>
              {fb.code && <div className="font-mono text-2xs text-codify-warning">{fmtText(fb.code)}</div>}
              {fb.detail && <div className="text-xs text-codify-muted">{fmtText(fb.detail)}</div>}
              <div className="text-2xs text-codify-muted">{fmtTime(fb.at)}</div>
            </div>
          );
        })}
      </Section>

      {/* Errors: what failed and which role owned it */}
      <Section
        icon={<AlertCircle className="w-3.5 h-3.5 text-codify-danger" />}
        title="Errors"
        count={errors.length}
        tone="bg-codify-danger/15 text-codify-danger-ink"
        defaultOpen
      >
        {errors.map((err, i) => (
          <div key={i} className="rounded-lg border border-codify-danger/20 bg-codify-danger/5 p-2.5 space-y-1">
            <div className="text-xs">
              {err.role && <span className="font-semibold text-codify-danger">{fmtText(err.role)}</span>}
              {err.step && <span className="text-codify-muted"> · {stepName(err.step)}</span>}
              {err.code && (
                <span className="ml-2 font-mono text-2xs text-codify-warning">{fmtText(err.code)}</span>
              )}
            </div>
            {err.message && <div className="text-xs text-codify-secondary">{fmtText(err.message)}</div>}
            <div className="text-2xs text-codify-muted">{fmtTime(err.at)}</div>
          </div>
        ))}
      </Section>

      {/* Fix retries: the bounded repair loop */}
      <Section
        icon={<RotateCcw className="w-3.5 h-3.5 text-codify-warning" />}
        title="Fix retries"
        count={fixRetries.length}
        tone="bg-codify-warning/15 text-codify-warning-ink"
      >
        {fixRetries.map((retry, i) => (
          <div key={i} className="rounded-lg border border-codify-border p-2.5 space-y-1">
            <div className="text-xs text-codify-warning">
              attempt {fmtText(retry.attempt)}
              {retry.max_attempts ? ` of ${fmtText(retry.max_attempts)}` : ""}
              {retry.step && <span className="text-codify-muted"> · {stepName(retry.step)}</span>}
            </div>
            {retry.reason && <div className="text-xs text-codify-muted">{fmtText(retry.reason)}</div>}
            <div className="text-2xs text-codify-muted">{fmtTime(retry.at)}</div>
          </div>
        ))}
      </Section>

      {/* Deep review: what the fresh-context readers found, and which of them ran */}
      <Section
        icon={<Eye className="w-3.5 h-3.5 text-codify-info" />}
        title="Deep review"
        count={reviews.length}
        tone="bg-codify-info/15 text-codify-info-ink"
        defaultOpen
      >
        {reviews.map((r, i) => (
          <div key={i} className="space-y-1">
            <div className="text-2xs text-codify-muted">{stepName(r.step, "step")}</div>
            <ReviewReport report={r} />
          </div>
        ))}
      </Section>

      {/* Step outcomes: what each step actually did */}
      <Section
        icon={<Workflow className="w-3.5 h-3.5 text-codify-info" />}
        title="Step outcomes"
        count={steps.length}
        tone="bg-codify-info/15 text-codify-info-ink"
        defaultOpen
      >
        {steps.map((s, i) => (
          <div
            key={typeof s.step_id === "string" ? s.step_id : i}
            className="flex items-center gap-2 rounded-lg border border-codify-border p-2.5"
          >
            {s.status === "COMPLETED" ? (
              <CheckCircle2 className="w-3.5 h-3.5 text-codify-success shrink-0" />
            ) : s.status === "FAILED" ? (
              <XCircle className="w-3.5 h-3.5 text-codify-danger shrink-0" />
            ) : (
              <Clock className="w-3.5 h-3.5 text-codify-info shrink-0" />
            )}
            <span className="text-xs text-codify-secondary truncate">{stepName(s.step)}</span>
            {typeof s.attempts === "number" && s.attempts > 1 && (
              <span className="text-2xs text-codify-warning">×{s.attempts} attempts</span>
            )}
            <span className="ml-auto shrink-0">
              <StatusPill status={typeof s.status === "string" ? s.status : undefined} />
            </span>
          </div>
        ))}
      </Section>

      {/* Token usage: who spent what, per role and per model. A role that
          fell back shows under both models — the audit shows the real cost,
          not just the primary's bill. */}
      {usage && usageCalls > 0 && (
        <div className="rounded-xl border border-codify-border bg-codify-bg overflow-hidden">
          <div className="w-full flex items-center gap-2 px-3 py-2 text-xs font-semibold text-codify-secondary">
            <Coins className="w-3.5 h-3.5 text-codify-warning" />
            <span>Token usage</span>
            <span className="ml-auto font-normal text-2xs text-codify-muted font-mono">
              {fmtNum(usageTotals?.total_tokens)} tokens · {usageCalls} call{usageCalls === 1 ? "" : "s"}
            </span>
          </div>
          <div className="px-3 pb-3 space-y-1">
            {usageByRole.map(([role, b]) => (
              <div key={role} className="flex items-center gap-2 text-xs">
                <span className="font-semibold text-codify-secondary w-20 shrink-0">{role}</span>
                <span className="font-mono text-codify-muted">
                  {fmtNum(b.total_tokens)} tokens
                </span>
                <span className="text-codify-muted">
                  ({fmtNum(b.input_tokens)} in / {fmtNum(b.output_tokens)} out)
                </span>
                {typeof b.calls === "number" && (
                  <span className="ml-auto text-codify-muted">{b.calls} call{b.calls === 1 ? "" : "s"}</span>
                )}
              </div>
            ))}
            {usageByModel.length > 1 && (
              <div className="pt-1 mt-1 border-t border-codify-border/60 space-y-1">
                {usageByModel.map(([model, b]) => (
                  <div key={model} className="flex items-center gap-2 text-2xs">
                    <span className="font-mono text-codify-muted flex-1 truncate">{model}</span>
                    <span className="font-mono text-codify-muted">{fmtNum(b.total_tokens)}</span>
                    <span className="text-codify-muted w-10 text-right">{fmtText(b.calls ?? 0)}×</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Silent roles: assigned by the engine but never completed a model
          call. Worth noticing — a guard skip, a silent failure, or another
          role absorbing the work. Deliberate skips (never assigned) don't
          appear, so an empty run shows nothing here. */}
      {silentRoles.length > 0 && (
        <div className="rounded-xl border border-codify-warning/30 bg-codify-warning/5 p-3 space-y-1.5">
          <div className="flex items-center gap-2 text-xs font-semibold text-codify-warning">
            <BellOff className="w-3.5 h-3.5" />
            <span>
              Silent role{silentRoles.length === 1 ? "" : "s"} — assigned but never ran
            </span>
          </div>
          {silentRoles.map((s, i) => (
            <div key={i} className="flex items-center gap-2 text-xs">
              <span className="font-semibold text-codify-warning">{fmtText(s.role)}</span>
              {s.assigned_model && (
                <span className="font-mono text-codify-muted">{fmtText(s.assigned_model)}</span>
              )}
              <span className="text-codify-muted">no completed model call</span>
            </div>
          ))}
        </div>
      )}

      {/* Status timeline: the goal's life in order */}
      {timeline.length > 1 && (
        <div className="flex items-center gap-1.5 px-1 py-0.5 text-2xs text-codify-muted flex-wrap">
          {timeline.map((t, i) => (
            <React.Fragment key={i}>
              {i > 0 && <span className="text-codify-muted">→</span>}
              <span className="font-mono">{fmtText(t.status)}</span>
            </React.Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
