import React, { useEffect, useState } from "react";
import {
  UsageLane,
  StatsOverview,
  StatsHistoryDay,
  StageCost,
  RoleOutcome,
  FailureBreakdown,
} from "../types";
import {
  clearStatsImport,
  fetchFailureBreakdown,
  fetchStatsHistory,
  fetchStatsImport,
  fetchStatsOverview,
  saveStatsImport,
} from "../api";
import {
  mergeDays,
  mergeStatsHistory,
  summarizeStatsHistoryImport,
  validateStatsHistoryDocument,
  type MergedDay,
} from "../statsHistory";
import {
  describeOutcomes,
  failureEmptyMessage,
  hasStageData,
  recoveryLabel,
  roleRateLabel,
  roleRateTone,
  roleRunSummary,
  shareWidth,
  topFailure,
  STAGE_LABELS,
} from "../stageMetrics";
import {
  CheckCircle2,
  Coins,
  XCircle,
  Ban,
  Zap,
  TrendingUp,
  FileDown,
  FileUp,
} from "lucide-react";

/**
 * Cross-goal statistics: what the whole history adds up to.
 *
 * Everything shown is derived by the engine from the stores that already exist
 * (goals + the usage/agent_call_failed events), so the numbers here can never
 * disagree with the per-goal audit cards — they are the same log, swept wider.
 *
 * Honesty carries over from the engine's aggregation: "success" is COMPLETED
 * only, an unmeasured duration renders as "—" rather than 0ms, and the daily
 * trend shows only days with activity. A stats view that invented roundness
 * would be exactly the kind of claim this codebase keeps refusing to make.
 */

const WINDOWS: { label: string; days: number }[] = [
  { label: "24h", days: 1 },
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "All", days: 0 },
];

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function fmtDuration(ms: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function downloadJson(payload: unknown, filename: string): void {
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = window.document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

/** One summary card: an icon, a big number, and the sentence that says what it
 * counts — a number without its denominator is decoration. */
const StatCard: React.FC<{
  icon: React.ReactNode;
  value: string;
  sub: string;
  tone?: string;
}> = ({ icon, value, sub, tone = "text-gray-100" }) => (
  <div className="bg-codify-bg border border-codify-border rounded-xl p-3.5 flex flex-col gap-1 min-w-0">
    <div className="flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-wider text-gray-500">
      {icon}
      {sub}
    </div>
    <div className={`text-xl font-bold font-mono ${tone}`}>{value}</div>
  </div>
);

/** The per-role / per-model table: one row per lane, spend and calls. */
const UsageTable: React.FC<{
  title: string;
  lanes: Record<string, UsageLane>;
}> = ({ title, lanes }) => {
  const entries = Object.entries(lanes).sort(
    (a, b) => b[1].total_tokens - a[1].total_tokens,
  );
  if (entries.length === 0) return null;
  return (
    <div className="bg-codify-bg border border-codify-border rounded-xl p-3">
      <div className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-2">
        {title}
      </div>
      <div className="flex flex-col gap-1.5">
        {entries.map(([name, lane]) => (
          <div key={name} className="flex items-center gap-2 text-xs">
            <span className="font-mono text-gray-300 truncate min-w-0 flex-1">
              {name}
            </span>
            <div className="h-1.5 w-24 bg-codify-surface rounded-full overflow-hidden flex-shrink-0">
              <div
                className="h-full bg-blue-600 rounded-full"
                style={{
                  width: `${Math.max(
                    2,
                    (lane.total_tokens /
                      Math.max(...entries.map(([, l]) => l.total_tokens))) *
                      100,
                  )}%`,
                }}
              />
            </div>
            <span className="font-mono text-gray-400 w-14 text-right flex-shrink-0">
              {fmtTokens(lane.total_tokens)}
            </span>
            <span className="text-gray-500 w-20 text-right flex-shrink-0">
              {lane.calls} call{lane.calls === 1 ? "" : "s"} ·{" "}
              {fmtDuration(lane.avg_duration_ms)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
};

/**
 * The one continuous day-by-day chart: frozen days first, the live tail after,
 * no seam a viewer can see. Bars encode the day's success rate (amber under
 * 50%, green at/above, grey when the day had no terminal goals); the caption
 * carries the detail. A dot marks today — the one bar that is still moving.
 */
const SuccessRateChart: React.FC<{ days: MergedDay[] }> = ({ days }) => {
  if (days.length === 0) return null;
  const today = new Date().toISOString().slice(0, 10);
  return (
    <div className="bg-codify-bg border border-codify-border rounded-xl p-3">
      <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400 mb-2">
        <TrendingUp className="w-3.5 h-3.5 text-blue-400" />
        Day by day
        {days.some((d) => d.source === "snapshot") && (
          <span className="normal-case font-normal text-gray-500">
            · saved days survive restarts
          </span>
        )}
      </div>
      <div className="flex flex-col gap-1">
        {days.map((d) => {
          const terminal = d.succeeded + d.failed + d.cancelled;
          const rate =
            terminal > 0 ? Math.round((100 * d.succeeded) / terminal) : null;
          const isToday = d.date === today;
          return (
            <div key={d.date} className="flex items-center gap-2 text-xs">
              <span className="font-mono text-gray-500 w-20 flex-shrink-0">
                {isToday ? "today" : d.date.slice(5)}
              </span>
              <div className="flex-1 h-2 bg-codify-surface rounded-full overflow-hidden min-w-0">
                <div
                  className={`h-full rounded-full ${
                    rate == null
                      ? "bg-gray-600/60"
                      : rate < 50
                        ? "bg-amber-600/80"
                        : "bg-green-600/70"
                  }`}
                  style={{ width: `${rate == null ? 2 : Math.max(4, rate)}%` }}
                />
              </div>
              <span className="text-gray-400 w-56 text-right flex-shrink-0 truncate">
                {isToday && <span className="text-blue-300 mr-1">●</span>}
                {rate == null ? "no terminal goals" : `${rate}% ok`}
                {" · "}
                {d.created} started
                {d.failed > 0 && (
                  <span className="text-red-400/80"> · {d.failed} failed</span>
                )}
                {d.cancelled > 0 && (
                  <span className="text-amber-400/80">
                    {" "}
                    · {d.cancelled} cancelled
                  </span>
                )}
                {d.total_tokens > 0 && (
                  <span className="text-gray-500">
                    {" "}
                    · {fmtTokens(d.total_tokens)} tok · {d.calls} call
                    {d.calls === 1 ? "" : "s"}
                  </span>
                )}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
};

/**
 * Per stage: runs, tokens, its share of the window's spend, and the stage's own
 * wall clock. A *stage* is not a call — it is the model call plus the engine
 * work around it (diff rendering, `fs.apply`, a sandboxed command) — so these
 * latencies are larger than the per-call averages above, and both are labelled
 * for what they measure.
 */
const StageTable: React.FC<{ rows: StageCost[] }> = ({ rows }) => {
  if (!hasStageData(rows)) {
    return (
      <div className="bg-codify-bg border border-codify-border rounded-xl p-3">
        <div className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-1">
          By stage
        </div>
        <p className="text-xs text-codify-muted">
          No stage has been measured in this window. A goal has to run first.
        </p>
      </div>
    );
  }
  return (
    <div className="bg-codify-bg border border-codify-border rounded-xl p-3">
      <div className="text-xs font-semibold uppercase tracking-wider text-codify-secondary mb-2">
        By stage
        <span className="normal-case font-normal text-codify-muted">
          {" "}
          · wall clock per stage, not per model call
        </span>
      </div>
      {/* Two lines per row, not one wide one.

          This was a single flex line whose last child — "3 runs · avg 2.6s · p95
          3.1s" — had no width and no `shrink`, so it set the row's minimum. Measured:
          499px of content inside a 384px drawer, which meant the drawer scrolled
          sideways and the p95 column was unreachable without scrolling to find it.

          The fix is not a smaller font or a wider drawer. The detail line moves under
          the row it describes, so every number stays readable and the row's width is
          bounded by the four fixed columns instead of by a sentence. */}
      <div className="flex flex-col gap-2.5">
        {rows.map((row) => (
          <div key={row.stage} className="flex flex-col gap-0.5">
            <div className="flex items-center gap-2 text-xs">
              <span
                className="text-codify-primary w-28 flex-shrink-0 truncate"
                title={describeOutcomes(row.outcomes)}
              >
                {STAGE_LABELS[row.stage] ?? row.stage}
              </span>
              <div className="h-1.5 w-20 bg-codify-surface rounded-full overflow-hidden flex-shrink-0">
                <div
                  className="h-full bg-emerald-600 rounded-full"
                  style={{ width: shareWidth(row.token_share) }}
                />
              </div>
              <span className="font-mono text-codify-muted w-14 text-right flex-shrink-0">
                {row.token_share}%
              </span>
              <span className="font-mono text-codify-muted w-16 text-right flex-shrink-0">
                {fmtTokens(row.tokens)}
              </span>
            </div>
            <div className="text-2xs text-codify-muted pl-[7.5rem]">
              {row.runs} run{row.runs === 1 ? "" : "s"} · avg{" "}
              {fmtDuration(row.avg_duration_ms)}
              {row.p95_duration_ms != null &&
                ` · p95 ${fmtDuration(row.p95_duration_ms)}`}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

/**
 * Per role: did it do its job. A verifier that reported `fail` and a critic
 * that asked for changes have both worked exactly as specified, so neither is
 * counted against its role — the goal-level rate above answers the other
 * question. The run summary under each rate is the evidence for it, and a role
 * that never ran says so instead of showing 0%.
 */
const RoleOutcomeTable: React.FC<{ roles: Record<string, RoleOutcome> }> = ({
  roles,
}) => {
  const entries = Object.values(roles ?? {});
  if (entries.length === 0) return null;
  return (
    <div className="bg-codify-bg border border-codify-border rounded-xl p-3">
      <div className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-1">
        Per role
        <span className="normal-case font-normal text-gray-500">
          {" "}
          · did the role do its job, not did the goal succeed
        </span>
      </div>
      <div className="grid grid-cols-2 xl:grid-cols-4 gap-x-4 gap-y-1.5">
        {entries.map((role) => (
          <div key={role.role} className="flex flex-col gap-0.5 min-w-0">
            <div className="flex items-baseline gap-1.5">
              <span className="text-xs text-gray-300 truncate">
                {role.role}
              </span>
              <span className={`text-xs font-mono ${roleRateTone(role)}`}>
                {roleRateLabel(role)}
              </span>
              <span className="text-2xs text-gray-600 font-mono ml-auto">
                {fmtTokens(role.tokens)}
              </span>
            </div>
            <span
              className="text-2xs text-gray-600 truncate"
              title={roleRunSummary(role)}
            >
              {roleRunSummary(role)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
};

/**
 * What went wrong, and how much of it the retry loops got back. The empty
 * state is a sentence, not a table of zeros: an install with no failures has
 * no evidence about whether failures are handled, and rendering "0 failures"
 * would claim otherwise.
 */
const FailureView: React.FC<{ breakdown: FailureBreakdown | null }> = ({
  breakdown,
}) => {
  if (!breakdown) return null;
  const empty = failureEmptyMessage(breakdown.total, breakdown.retries);
  return (
    <div className="bg-codify-bg border border-codify-border rounded-xl p-3">
      <div className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-2">
        Failures
        {topFailure(breakdown) && (
          <span className="normal-case font-normal text-gray-400">
            {" "}
            · {topFailure(breakdown)}
          </span>
        )}
      </div>
      {empty ? (
        <p className="text-xs text-gray-500">{empty}</p>
      ) : (
        <div className="flex flex-col gap-1.5">
          {breakdown.causes.slice(0, 6).map((cause) => (
            <div key={cause.code} className="flex items-center gap-2 text-xs">
              <span className="font-mono text-red-300 w-44 flex-shrink-0 truncate">
                {cause.code}
              </span>
              <span className="font-mono text-gray-400 w-8 text-right flex-shrink-0">
                {cause.count}
              </span>
              <span
                className="text-gray-500 truncate min-w-0"
                title={cause.message}
              >
                {cause.message}
              </span>
            </div>
          ))}
        </div>
      )}
      <p className="text-2xs text-gray-600 mt-2">{recoveryLabel(breakdown)}</p>
    </div>
  );
};

export const StatsPanel: React.FC = () => {
  const [windowDays, setWindowDays] = useState(0);
  const [stats, setStats] = useState<StatsOverview | null>(null);
  const [history, setHistory] = useState<StatsHistoryDay[]>([]);
  const [importedHistory, setImportedHistory] = useState<StatsHistoryDay[]>([]);
  const [importedFileName, setImportedFileName] = useState<string | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [failures, setFailures] = useState<FailureBreakdown | null>(null);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    fetchStatsOverview(windowDays)
      .then((s) => {
        if (!cancelled) setStats(s);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [windowDays]);

  // The failure view is a separate read from a separate endpoint. It is
  // allowed to be missing: an engine too old to serve it, or one whose metrics
  // read failed, must not take the rest of this panel down with it.
  useEffect(() => {
    let cancelled = false;
    fetchFailureBreakdown(windowDays)
      .then((f) => {
        if (!cancelled) setFailures(f);
      })
      .catch(() => {
        if (!cancelled) setFailures(null);
      });
    return () => {
      cancelled = true;
    };
  }, [windowDays]);

  // The frozen days: fetched once per mount (each reading also nudges the
  // engine's freeze-forward, so a refetch on window change is unnecessary —
  // the strip covers past days, which a window change cannot alter).
  useEffect(() => {
    let cancelled = false;
    fetchStatsHistory(60)
      .then((days) => {
        if (!cancelled) setHistory(days);
      })
      .catch(() => {
        // A failed history fetch never hides the live view; the strip just
        // stays out of the way.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // The imported document lives in the engine, not in this component's state,
  // so reopening the panel (or the whole app) brings it back. Hydrated on
  // mount; a failure here only means "no import to restore", which is the
  // normal state, so it must not raise the panel's error banner.
  useEffect(() => {
    let cancelled = false;
    fetchStatsImport()
      .then((stored) => {
        if (cancelled || !stored?.imported || stored.days.length === 0) return;
        setImportedHistory(stored.days);
        setImportedFileName(stored.source || "imported history");
      })
      .catch(() => {
        // No engine, or nothing stored. Either way there is nothing to restore.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // One continuous series: frozen history under the live tail. Computed once
  // per render of both feeds; the merge rule (snapshot wins a shared day)
  // lives with the data, in mergeDays.
  const mergedDays = mergeDays(
    [...importedHistory, ...history],
    stats?.daily ?? [],
  );
  const importSummary = summarizeStatsHistoryImport(importedHistory, history);
  const matchedLocalDays = importSummary.matchedLocal;
  const addedHistoryDays = importSummary.added;

  const importHistory = () => {
    const input = window.document.createElement("input");
    input.type = "file";
    input.accept = "application/json,.json";
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      try {
        const document: unknown = JSON.parse(await file.text());
        // The client check is for a fast, specific message. The engine
        // re-validates the same rules and is the one that decides — a file that
        // passes here could still be refused there, and that refusal wins.
        const validation = validateStatsHistoryDocument(document);
        if (!validation.ok) {
          setImportError(`"${file.name}" ${validation.error}`);
          return;
        }
        setImportBusy(true);
        try {
          await saveStatsImport(validation.document, file.name);
        } catch (err) {
          setImportError(
            `"${file.name}" could not be saved: ${err instanceof Error ? err.message : String(err)}`,
          );
          return;
        } finally {
          setImportBusy(false);
        }
        setImportedHistory(validation.document.days);
        setImportedFileName(file.name);
        setImportError(null);
      } catch (err) {
        setImportError(
          `Could not read "${file.name}": ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    };
    input.click();
  };

  const clearImportedHistory = async () => {
    setImportError(null);
    try {
      // Clear the engine's copy too, or the next mount would restore what the
      // user just removed — "clear" that comes back is worse than no clear.
      await clearStatsImport();
    } catch (err) {
      setImportError(
        `Could not clear the stored import: ${err instanceof Error ? err.message : String(err)}`,
      );
      return;
    }
    setImportedHistory([]);
    setImportedFileName(null);
  };

  const exportHistory = async () => {
    if (history.length === 0 && importedHistory.length === 0) return;
    setExporting(true);
    try {
      // The chart intentionally loads a short tail; the export asks the engine
      // for every retained frozen day, including days outside that tail.
      const serverDays = await fetchStatsHistory(0);
      const days = mergeStatsHistory(importedHistory, history, serverDays);
      downloadJson(
        { exported_at: new Date().toISOString(), days },
        `codify-stats-history-${new Date().toISOString().slice(0, 10)}.json`,
      );
    } catch (err) {
      console.error("stats history export failed", err);
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      {/* Window selector */}
      <div className="flex items-center gap-1.5">
        {WINDOWS.map((w) => (
          <button
            key={w.days}
            type="button"
            onClick={() => setWindowDays(w.days)}
            className={`px-2.5 py-1 text-xs font-semibold rounded-lg border transition-colors cursor-pointer ${
              windowDays === w.days
                ? "bg-blue-950/40 text-blue-300 border-blue-800"
                : "bg-codify-bg text-gray-400 border-codify-border hover:text-gray-200"
            }`}
          >
            {w.label}
          </button>
        ))}
        {stats && (
          <span className="ml-auto text-2xs text-gray-500">
            {stats.goals.goals} goal{stats.goals.goals === 1 ? "" : "s"} in view
          </span>
        )}
      </div>

      {failed && (
        <p className="text-xs text-red-300 bg-red-950/40 border border-red-800 rounded-lg p-2.5">
          Could not load statistics from the engine.
        </p>
      )}

      {!failed && !stats && (
        <p className="text-xs text-gray-500 px-1 py-2">Loading statistics…</p>
      )}

      <div className="flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={importHistory}
          disabled={importBusy}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-codify-border bg-codify-bg text-xs text-gray-400 hover:text-blue-400 hover:border-blue-500/40 transition-colors"
          title="Open a Codify stats-history JSON export and add its frozen days to this view"
        >
          <FileUp className="w-3.5 h-3.5" />
          Import history…
        </button>
        {importedFileName && (
          <button
            type="button"
            onClick={clearImportedHistory}
            className="px-2.5 py-1.5 rounded-lg border border-codify-border bg-codify-bg text-xs text-gray-400 hover:text-red-300 hover:border-red-500/40 transition-colors"
            title="Remove the stored imported history from the engine and this view"
          >
            Clear imported
          </button>
        )}
        <button
          type="button"
          onClick={exportHistory}
          disabled={
            (history.length === 0 && importedHistory.length === 0) || exporting
          }
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-codify-border bg-codify-bg text-xs text-gray-400 hover:text-blue-400 hover:border-blue-500/40 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          title="Export every frozen statistics day as one JSON file"
        >
          <FileDown className="w-3.5 h-3.5" />
          {exporting ? "Exporting…" : "Export frozen history"}
        </button>
      </div>
      {importedFileName && (
        <p className="text-2xs text-blue-300/80 text-right leading-relaxed">
          Imported {importedHistory.length} frozen day
          {importedHistory.length === 1 ? "" : "s"} from {importedFileName} and
          stored it in the engine, so it survives a restart. Merge:{" "}
          {addedHistoryDays} added to the loaded history, {matchedLocalDays}{" "}
          matching local snapshot{matchedLocalDays === 1 ? "" : "s"}
          {matchedLocalDays > 0 ? " (local wins)" : ""}.
        </p>
      )}
      {importError && (
        <p className="text-xs text-red-300 bg-red-950/40 border border-red-800 rounded-lg p-2.5">
          {importError}
        </p>
      )}
      <SuccessRateChart days={mergedDays} />

      {stats && (
        <>
          {/* Headline numbers */}
          <div className="grid grid-cols-2 xl:grid-cols-4 gap-2.5">
            <StatCard
              icon={<CheckCircle2 className="w-3 h-3 text-green-400" />}
              sub="Success rate"
              value={
                stats.goals.success_rate == null
                  ? "—"
                  : `${stats.goals.success_rate}%`
              }
              tone="text-green-400"
            />
            <StatCard
              icon={<XCircle className="w-3 h-3 text-red-400" />}
              sub="Failed"
              value={String(stats.goals.failed)}
              tone="text-red-300"
            />
            <StatCard
              icon={<Ban className="w-3 h-3 text-amber-400" />}
              sub="Cancelled"
              value={String(stats.goals.cancelled)}
              tone="text-amber-300"
            />
            <StatCard
              icon={<Coins className="w-3 h-3 text-blue-400" />}
              sub="Tokens"
              value={fmtTokens(stats.usage.total_tokens)}
              tone="text-blue-300"
            />
          </div>

          <p className="text-xs text-gray-500 leading-relaxed px-0.5">
            {stats.goals.goals === 0
              ? "No goals in this window yet."
              : `${stats.goals.active} still active · ${stats.usage.calls} model call${
                  stats.usage.calls === 1 ? "" : "s"
                }${stats.usage.failures > 0 ? ` · ${stats.usage.failures} failed` : ""} · avg ${fmtDuration(
                  stats.usage.avg_duration_ms,
                )}`}
            {". "}
            <span className="text-gray-600">
              Success counts completed goals only — cancelled and failed are
              kept apart.
            </span>
          </p>

          {/* Where the tokens went */}
          <UsageTable title="By role" lanes={stats.usage.by_role} />
          <UsageTable title="By model" lanes={stats.usage.by_model} />
          {stats.by_stage && <StageTable rows={stats.by_stage} />}
          {stats.by_role_outcome && (
            <RoleOutcomeTable roles={stats.by_role_outcome} />
          )}
          <FailureView breakdown={failures} />

          {stats.usage.calls === 0 &&
            stats.usage.failures === 0 &&
            stats.goals.goals > 0 && (
              <p className="text-xs text-gray-500 px-0.5 flex items-center gap-1.5">
                <Zap className="w-3 h-3" />
                No model calls recorded in this window (plan-only, or an engine
                run before usage was logged).
              </p>
            )}
        </>
      )}
    </div>
  );
};
