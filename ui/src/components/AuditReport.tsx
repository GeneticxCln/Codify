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
} from "lucide-react";

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

/** Structural check, not a schema parser: enough to reject wrong files with a clear message. */
export function looksLikeAudit(doc: unknown): doc is AuditDoc {
  return (
    typeof doc === "object" &&
    doc !== null &&
    (("plan_edits" in doc && Array.isArray((doc as AuditDoc).plan_edits)) ||
      ("fallbacks" in doc && Array.isArray((doc as AuditDoc).fallbacks)) ||
      ("step_outcomes" in doc && Array.isArray((doc as AuditDoc).step_outcomes))) &&
    ("goal_id" in doc || "final_status" in doc)
  );
}

const STATUS_STYLES: Record<string, string> = {
  COMPLETED: "text-green-400 border-green-500/30 bg-green-500/10",
  FAILED: "text-red-400 border-red-500/30 bg-red-500/10",
  CANCELLED: "text-gray-300 border-gray-500/30 bg-gray-500/10",
  RUNNING: "text-blue-400 border-blue-500/30 bg-blue-500/10",
  IN_PROGRESS: "text-blue-400 border-blue-500/30 bg-blue-500/10",
  PENDING: "text-gray-400 border-gray-500/30 bg-gray-500/10",
  PAUSED: "text-amber-400 border-amber-500/30 bg-amber-500/10",
};

function StatusPill({ status }: { status?: string }) {
  if (!status) return null;
  return (
    <span
      className={`px-2 py-0.5 rounded-full border text-[10px] font-semibold uppercase tracking-wide ${
        STATUS_STYLES[status] ?? "text-gray-400 border-gray-500/30 bg-gray-500/10"
      }`}
    >
      {status}
    </span>
  );
}

function fmtTime(ts?: number): string {
  if (!ts) return "";
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
    <div className="rounded-xl border border-[#30363d] bg-[#0d1117] overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-3 py-2 text-xs font-semibold text-gray-300 hover:bg-[#161b22] transition-colors"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        {icon}
        <span>{title}</span>
        <span
          className={`ml-auto px-1.5 py-0.5 rounded-full text-[10px] font-bold ${tone}`}
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
  const planEdits = doc.plan_edits ?? [];
  const fallbacks = doc.fallbacks ?? [];
  const fixRetries = doc.fix_retries ?? [];
  const errors = doc.errors ?? [];
  const steps = doc.step_outcomes ?? [];
  const timeline = doc.status_timeline ?? [];
  const parallelVisible = (doc.parallel_peak ?? 1) > 1;

  const stepName = (label?: string | null, fallback = "step") =>
    label ? label.replace(/\s*\([^)]*\)\s*$/, "") : fallback;

  return (
    <div className="mt-2 space-y-2 text-left">
      {/* Header: the outcome, the prompt, the mode */}
      <div className="rounded-xl border border-[#30363d] bg-[#161b22] p-3 space-y-1.5">
        <div className="flex items-center gap-2 flex-wrap">
          <StatusPill status={doc.final_status} />
          {doc.mode?.plan_only && <StatusPill status="PLAN ONLY" />}
          {doc.mode?.dry_run && <StatusPill status="DRY RUN" />}
          {doc.mode?.parallel && <StatusPill status="PARALLEL" />}
          {parallelVisible && (
            <span className="text-[10px] text-blue-300 font-medium">
              peak {doc.parallel_peak} in parallel · {doc.parallel_waves} wave(s)
            </span>
          )}
          {doc.created_at && (
            <span className="ml-auto text-[10px] text-gray-500">{fmtTime(doc.created_at)}</span>
          )}
        </div>
        {doc.prompt && (
          <div className="text-xs text-gray-200 font-medium">“{doc.prompt}”</div>
        )}
        {doc.goal_id && (
          <div className="font-mono text-[10px] text-gray-500 break-all">{doc.goal_id}</div>
        )}
      </div>

      {/* Plan edits: the drift lane — what was planned vs what ran */}
      <Section
        icon={<Pencil className="w-3.5 h-3.5 text-violet-400" />}
        title="Plan edits"
        count={planEdits.length}
        tone="bg-violet-500/15 text-violet-300"
        defaultOpen
      >
        {planEdits.map((edit, i) => (
          <div key={i} className="rounded-lg border border-[#30363d] p-2.5 space-y-1.5">
            <div className="text-xs font-semibold text-violet-300">
              {stepName(edit.step, "step")}
            </div>
            {Object.entries(edit.changes ?? {}).map(([field, ch]) => (
              <div key={field} className="font-mono text-[11px] text-gray-400">
                {field}:{" "}
                <span className="text-red-400/80 line-through">{fmtValue(ch.from)}</span>
                {" → "}
                <span className="text-green-400">{fmtValue(ch.to)}</span>
              </div>
            ))}
            <div className="text-[10px] text-gray-500">{fmtTime(edit.at)}</div>
          </div>
        ))}
      </Section>

      {/* Fallbacks: who was skipped, why, who answered */}
      <Section
        icon={<Route className="w-3.5 h-3.5 text-teal-400" />}
        title="Provider fallbacks"
        count={fallbacks.length}
        tone="bg-teal-500/15 text-teal-300"
      >
        {fallbacks.map((fb, i) => (
          <div key={i} className="rounded-lg border border-[#30363d] p-2.5 space-y-1">
            <div className="text-xs">
              <span className="font-semibold text-teal-300">{fb.role}</span>
              {fb.step && (
                <span className="text-gray-400"> · {stepName(fb.step)}</span>
              )}
            </div>
            <div className="font-mono text-[11px] text-gray-400">
              <span className="text-red-400/80 line-through">
                {fb.from?.provider}/{fb.from?.model}
              </span>
              {" → "}
              <span className="text-green-400">
                {fb.to?.provider}/{fb.to?.model}
              </span>
            </div>
            {fb.code && <div className="font-mono text-[10px] text-amber-400/80">{fb.code}</div>}
            {fb.detail && <div className="text-[11px] text-gray-500">{fb.detail}</div>}
            <div className="text-[10px] text-gray-500">{fmtTime(fb.at)}</div>
          </div>
        ))}
      </Section>

      {/* Errors: what failed and which role owned it */}
      <Section
        icon={<AlertCircle className="w-3.5 h-3.5 text-red-400" />}
        title="Errors"
        count={errors.length}
        tone="bg-red-500/15 text-red-300"
        defaultOpen
      >
        {errors.map((err, i) => (
          <div key={i} className="rounded-lg border border-red-500/20 bg-red-500/5 p-2.5 space-y-1">
            <div className="text-xs">
              {err.role && <span className="font-semibold text-red-300">{err.role}</span>}
              {err.step && <span className="text-gray-400"> · {stepName(err.step)}</span>}
              {err.code && (
                <span className="ml-2 font-mono text-[10px] text-amber-400/80">{err.code}</span>
              )}
            </div>
            {err.message && <div className="text-[11px] text-gray-300">{err.message}</div>}
            <div className="text-[10px] text-gray-500">{fmtTime(err.at)}</div>
          </div>
        ))}
      </Section>

      {/* Fix retries: the bounded repair loop */}
      <Section
        icon={<RotateCcw className="w-3.5 h-3.5 text-amber-400" />}
        title="Fix retries"
        count={fixRetries.length}
        tone="bg-amber-500/15 text-amber-300"
      >
        {fixRetries.map((retry, i) => (
          <div key={i} className="rounded-lg border border-[#30363d] p-2.5 space-y-1">
            <div className="text-xs text-amber-300">
              attempt {retry.attempt}
              {retry.max_attempts ? ` of ${retry.max_attempts}` : ""}
              {retry.step && <span className="text-gray-400"> · {stepName(retry.step)}</span>}
            </div>
            {retry.reason && <div className="text-[11px] text-gray-400">{retry.reason}</div>}
            <div className="text-[10px] text-gray-500">{fmtTime(retry.at)}</div>
          </div>
        ))}
      </Section>

      {/* Step outcomes: what each step actually did */}
      <Section
        icon={<Workflow className="w-3.5 h-3.5 text-blue-400" />}
        title="Step outcomes"
        count={steps.length}
        tone="bg-blue-500/15 text-blue-300"
        defaultOpen
      >
        {steps.map((s, i) => (
          <div
            key={s.step_id ?? i}
            className="flex items-center gap-2 rounded-lg border border-[#30363d] p-2.5"
          >
            {s.status === "COMPLETED" ? (
              <CheckCircle2 className="w-3.5 h-3.5 text-green-400 flex-shrink-0" />
            ) : s.status === "FAILED" ? (
              <XCircle className="w-3.5 h-3.5 text-red-400 flex-shrink-0" />
            ) : (
              <Clock className="w-3.5 h-3.5 text-blue-400 flex-shrink-0" />
            )}
            <span className="text-xs text-gray-200 truncate">{stepName(s.step)}</span>
            {typeof s.attempts === "number" && s.attempts > 1 && (
              <span className="text-[10px] text-amber-400/80">×{s.attempts} attempts</span>
            )}
            <span className="ml-auto flex-shrink-0">
              <StatusPill status={s.status} />
            </span>
          </div>
        ))}
      </Section>

      {/* Token usage: who spent what, per role and per model. A role that
          fell back shows under both models — the audit shows the real cost,
          not just the primary's bill. */}
      {doc.usage && (doc.usage.calls ?? 0) > 0 && (
        <div className="rounded-xl border border-[#30363d] bg-[#0d1117] overflow-hidden">
          <div className="w-full flex items-center gap-2 px-3 py-2 text-xs font-semibold text-gray-300">
            <Coins className="w-3.5 h-3.5 text-amber-400" />
            <span>Token usage</span>
            <span className="ml-auto font-normal text-[10px] text-gray-500 font-mono">
              {(doc.usage.totals?.total_tokens ?? 0).toLocaleString()} tokens · {doc.usage.calls} call{(doc.usage.calls ?? 0) === 1 ? "" : "s"}
            </span>
          </div>
          <div className="px-3 pb-3 space-y-1">
            {Object.entries(doc.usage.by_role ?? {}).map(([role, b]) => (
              <div key={role} className="flex items-center gap-2 text-[11px]">
                <span className="font-semibold text-gray-300 w-20 flex-shrink-0">{role}</span>
                <span className="font-mono text-gray-400">
                  {((b.total_tokens ?? 0)).toLocaleString()} tokens
                </span>
                <span className="text-gray-500">
                  ({(b.input_tokens ?? 0).toLocaleString()} in / {(b.output_tokens ?? 0).toLocaleString()} out)
                </span>
                {typeof b.calls === "number" && (
                  <span className="ml-auto text-gray-500">{b.calls} call{b.calls === 1 ? "" : "s"}</span>
                )}
              </div>
            ))}
            {Object.entries(doc.usage.by_model ?? {}).length > 1 && (
              <div className="pt-1 mt-1 border-t border-[#30363d]/60 space-y-1">
                {Object.entries(doc.usage.by_model ?? {}).map(([model, b]) => (
                  <div key={model} className="flex items-center gap-2 text-[10px]">
                    <span className="font-mono text-gray-500 flex-1 truncate">{model}</span>
                    <span className="font-mono text-gray-400">{(b.total_tokens ?? 0).toLocaleString()}</span>
                    <span className="text-gray-600 w-10 text-right">{b.calls}×</span>
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
      {(doc.silent_roles?.length ?? 0) > 0 && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-3 space-y-1.5">
          <div className="flex items-center gap-2 text-xs font-semibold text-amber-300">
            <BellOff className="w-3.5 h-3.5" />
            <span>
              Silent role{(doc.silent_roles ?? []).length === 1 ? "" : "s"} — assigned but never ran
            </span>
          </div>
          {(doc.silent_roles ?? []).map((s, i) => (
            <div key={i} className="flex items-center gap-2 text-[11px]">
              <span className="font-semibold text-amber-200/90">{s.role}</span>
              {s.assigned_model && (
                <span className="font-mono text-gray-500">{s.assigned_model}</span>
              )}
              <span className="text-gray-500">no completed model call</span>
            </div>
          ))}
        </div>
      )}

      {/* Status timeline: the goal's life in order */}
      {timeline.length > 1 && (
        <div className="flex items-center gap-1.5 px-1 py-0.5 text-[10px] text-gray-500 flex-wrap">
          {timeline.map((t, i) => (
            <React.Fragment key={i}>
              {i > 0 && <span className="text-gray-600">→</span>}
              <span className="font-mono">{t.status}</span>
            </React.Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
