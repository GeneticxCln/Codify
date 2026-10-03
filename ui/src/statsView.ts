import type { StatsCoverage, UsageLane } from "./types";

/**
 * What the Statistics drawer shows of a long history, and what it says about the rest.
 *
 * The drawer is 28rem wide and was drawing every day the engine knew about (up to sixty frozen days and thirty
 * live ones) and every model that had ever answered, one row each: with 40 models and a year of history that
 * is a hundred and thirty rows in a column that has room for twenty. The newest rows and the biggest spenders
 * are what a person opens it for, so those come first and the rest is one click away, with the count and the
 * tokens that are in it, so a collapsed list never hides a number silently.
 *
 * Pure, so the arithmetic is tested without a panel.
 */

/** Days of the day-by-day chart shown before "show earlier". */
export const DAYS_SHOWN = 14;
/** Rows of the by-model table shown before "show more". */
export const LANES_SHOWN = 8;

/** The newest `limit` of a list that runs oldest first, and how many were left out. */
export function newestDays<T>(days: readonly T[], expanded: boolean, limit: number = DAYS_SHOWN): { shown: T[]; hidden: number } {
  if (expanded || days.length <= limit) return { shown: [...days], hidden: 0 };
  return { shown: days.slice(days.length - limit), hidden: days.length - limit };
}

/** The biggest spenders by tokens, and how many were left out and what they spent between them. */
export function topLanes(
  lanes: Record<string, UsageLane>,
  expanded: boolean,
  limit: number = LANES_SHOWN,
): { shown: [string, UsageLane][]; hidden: number; hiddenTokens: number } {
  const all = Object.entries(lanes).sort((a, b) => b[1].total_tokens - a[1].total_tokens);
  if (expanded || all.length <= limit) return { shown: all, hidden: 0, hiddenTokens: 0 };
  const rest = all.slice(limit);
  return {
    shown: all.slice(0, limit),
    hidden: rest.length,
    hiddenTokens: rest.reduce((sum, [, lane]) => sum + lane.total_tokens, 0),
  };
}

/**
 * The sentence for a window the engine could not read all of, or null when the numbers are complete.
 *
 * The engine counts the newest 20,000 model calls and the newest 5,000 goals. "All" over a longer history
 * used to show those as if they were everything (63% of the true total in a measured install), so when a cap
 * cut something off and the window reaches back past where the read begins, the panel says from when it counts.
 * A window that starts inside what was read is complete and says nothing.
 */
export function coverageNotice(coverage: StatsCoverage | undefined, windowDays: number, nowMs: number): string | null {
  if (!coverage || !coverage.truncated || coverage.since == null) return null;
  const windowStart = windowDays > 0 ? nowMs / 1000 - windowDays * 86400 : -Infinity;
  if (windowStart >= coverage.since) return null;
  const capped: string[] = [];
  if (coverage.events >= coverage.event_cap) capped.push(`the newest ${coverage.event_cap.toLocaleString("en-US")} model calls`);
  if (coverage.goals >= coverage.goal_cap) capped.push(`the newest ${coverage.goal_cap.toLocaleString("en-US")} goals`);
  const what = capped.length > 0 ? capped.join(" and ") : "the newest history";
  return `These totals count ${what}, since ${new Date(coverage.since * 1000).toISOString().slice(0, 10)}. Earlier activity is not in them.`;
}
