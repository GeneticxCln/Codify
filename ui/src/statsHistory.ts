import type { StatsHistoryDay, StatsOverview } from "./types.ts";

/** One day as the merged chart shows it — the same fields whichever source
 * supplied it, so a frozen day and a live day render identically. */
export interface MergedDay {
  date: string;
  created: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  total_tokens: number;
  calls: number;
  source: "snapshot" | "live";
}

export interface StatsHistoryDocument {
  exported_at: string;
  days: StatsHistoryDay[];
}

export type StatsHistoryValidation =
  | { ok: true; document: StatsHistoryDocument }
  | { ok: false; error: string };

export interface StatsHistoryImportSummary {
  imported: number;
  matchedLocal: number;
  added: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isFinite(value));
}

function isDayStats(value: unknown): value is StatsHistoryDay["day_stats"] {
  if (!isRecord(value)) return false;
  return (
    typeof value.date === "string" &&
    isCount(value.created) &&
    isCount(value.succeeded) &&
    isCount(value.failed) &&
    isCount(value.cancelled) &&
    isCount(value.total_tokens) &&
    isCount(value.calls)
  );
}

function isStatsGoals(value: unknown): value is StatsHistoryDay["goals"] {
  if (!isRecord(value)) return false;
  return (
    isCount(value.goals) &&
    isCount(value.active) &&
    isCount(value.succeeded) &&
    isCount(value.failed) &&
    isCount(value.cancelled) &&
    isNullableNumber(value.success_rate)
  );
}

function isUsage(value: unknown): value is StatsHistoryDay["usage"] {
  if (!isRecord(value)) return false;
  return (
    isCount(value.input_tokens) &&
    isCount(value.output_tokens) &&
    isCount(value.total_tokens) &&
    isCount(value.calls) &&
    isNullableNumber(value.avg_duration_ms)
  );
}

function hasValidDays(value: Record<string, unknown>): value is Record<string, unknown> & { days: StatsHistoryDay[] } {
  return (
    Array.isArray(value.days) &&
    value.days.every(
      (day) =>
        isRecord(day) &&
        typeof day.day === "string" &&
        isDayStats(day.day_stats) &&
        day.day_stats.date === day.day &&
        isStatsGoals(day.goals) &&
        isUsage(day.usage),
    )
  );
}

/** Reject ambiguity in the artifact instead of silently repairing it. Exports
 * are oldest-first and one row per UTC day; both are part of the contract. */
function validateSequence(days: StatsHistoryDay[]): string | null {
  if (days.length === 0) return "contains no frozen days.";

  const seen = new Set<string>();
  for (const day of days) {
    if (seen.has(day.day)) return `contains duplicate day ${day.day}.`;
    seen.add(day.day);
  }
  for (let index = 1; index < days.length; index += 1) {
    const previous = days[index - 1].day;
    const current = days[index].day;
    if (current < previous) {
      return `is not chronological: ${current} appears after ${previous}. Days must be oldest first.`;
    }
  }
  return null;
}

/** Validate the closed JSON artifact before any of its numbers reach the chart. */
export function validateStatsHistoryDocument(value: unknown): StatsHistoryValidation {
  if (!isRecord(value) || typeof value.exported_at !== "string" || !hasValidDays(value)) {
    return {
      ok: false,
      error: "is not a Codify stats-history export — expected exported_at and validated frozen days.",
    };
  }
  const sequenceError = validateSequence(value.days);
  if (sequenceError) return { ok: false, error: sequenceError };
  return { ok: true, document: { exported_at: value.exported_at, days: value.days } };
}

/**
 * Merge frozen history with the live trend. Sources later in `history` win a
 * shared date; the chart then lets any frozen row win over the live tail.
 */
export function mergeDays(history: StatsHistoryDay[], daily: StatsOverview["daily"]): MergedDay[] {
  const byDate = new Map<string, MergedDay>();
  for (const day of history) {
    // day_stats, deliberately: the document's top-level blocks are cumulative
    // through the end of that day, and charting them would redraw every earlier
    // day with the total-so-far.
    const stats = day.day_stats;
    byDate.set(day.day, {
      date: day.day,
      created: stats.created,
      succeeded: stats.succeeded,
      failed: stats.failed,
      cancelled: stats.cancelled,
      total_tokens: stats.total_tokens,
      calls: stats.calls,
      source: "snapshot",
    });
  }
  for (const day of daily) {
    if (byDate.has(day.date)) continue;
    byDate.set(day.date, { ...day, source: "live" });
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
}

/** Combine history sources for export. Later sources win duplicate dates. */
export function mergeStatsHistory(...sources: StatsHistoryDay[][]): StatsHistoryDay[] {
  const byDate = new Map<string, StatsHistoryDay>();
  for (const source of sources) {
    for (const day of source) byDate.set(day.day, day);
  }
  return [...byDate.values()].sort((a, b) => a.day.localeCompare(b.day));
}

/** Describe exactly what an imported file contributes to the loaded history. */
export function summarizeStatsHistoryImport(
  imported: StatsHistoryDay[],
  local: StatsHistoryDay[],
): StatsHistoryImportSummary {
  const localDates = new Set(local.map((day) => day.day));
  const matchedLocal = imported.filter((day) => localDates.has(day.day)).length;
  return { imported: imported.length, matchedLocal, added: imported.length - matchedLocal };
}
