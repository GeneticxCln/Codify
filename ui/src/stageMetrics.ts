import type { FailureBreakdown, RoleOutcome, StageCost } from "./types.ts";

/**
 * How the measured pipeline is rendered — the rules, not the pixels.
 *
 * Every function here is one a reader could get wrong in a way that turns a
 * measurement into a claim, which is why they are pure, exported, and tested
 * through `node --test` rather than living inside the component where nothing
 * can assert them:
 *
 *  - a rate with no finished runs is "—", never 0%;
 *  - a stage that has never run is a row of "—", not a 0ms row;
 *  - a window with no failures says so, rather than rendering a table of zeros
 *    that reads as "nothing is wrong" when the truth is "nothing to know";
 *  - no retries is "no evidence", not a perfect recovery record.
 */

/** The stages, in the order they run. A stage stays in the table before it
 * has data: a row that only appears once it has measurements reads as "this is
 * not part of the pipeline", which is how a stage quietly stops being looked
 * at. */
export const STAGE_ORDER = [
  "laya",
  "librarian",
  "design",
  "planner",
  "fixer",
  "verifier",
  "critic",
  "scribe",
] as const;

/** What each stage is for, in the words a person would use. The slugs are the
 * engine's vocabulary; this is the part of it a person can read at a glance. */
export const STAGE_LABELS: Record<string, string> = {
  laya: "Intent gate",
  librarian: "Reconnaissance",
  design: "Design direction",
  planner: "Plan",
  fixer: "Write files",
  verifier: "Verify",
  critic: "Review",
  scribe: "Commit",
};

/** Outcomes that are the role working, not the role failing. Mirrors the
 * engine's `STAGE_SUCCESS_OUTCOMES`; a view that coloured these as errors
 * would report a healthy pipeline as broken. */
export const STAGE_SUCCESS_OUTCOMES: Record<string, string[]> = {
  laya: ["allow", "skipped"],
  librarian: ["pack", "incomplete"],
  design: ["contract", "declined"],
  planner: ["plan", "consult"],
  fixer: ["wrote", "no_change", "replayed"],
  verifier: ["pass", "skip"],
  critic: ["approve"],
  scribe: ["committed", "nothing_to_commit", "not_a_repo", "skipped"],
};

/** "— 12 · 1 pass · 1 fail": the evidence behind a percentage, on one line. */
export function describeOutcomes(outcomes: Record<string, number>): string {
  const entries = Object.entries(outcomes ?? {}).filter(([, n]) => n > 0);
  if (entries.length === 0) return "";
  entries.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return entries.map(([name, n]) => `${n} ${name.replace(/_/g, " ")}`).join(" · ");
}

/** The bar width for a stage's token share. Floored so a stage that spent
 * something still shows a sliver — a bar of width 0 for a stage that ran is
 * indistinguishable from a stage that did not. */
export function shareWidth(share: number): string {
  if (share <= 0) return "0%";
  return `${Math.max(2, share)}%`;
}

/** Has any stage been measured at all? Drives "the engine has not measured
 * stages yet" rather than a table of empty rows. */
export function hasStageData(rows: StageCost[] | undefined): boolean {
  return (rows ?? []).some((r) => r.runs > 0);
}

/** The per-role rate as it should be read: "—" when nothing has finished,
 * because an unrun role has no success rate, and 0% is a claim about it. */
export function roleRateLabel(role: RoleOutcome | undefined): string {
  if (!role || role.runs === 0) return "—";
  if (role.success_rate == null) return "—";
  return `${role.success_rate}%`;
}

/** Tone for a rate: a low one is worth a person's attention, a high one is
 * not, and "no data" is neither. */
export function roleRateTone(role: RoleOutcome | undefined): string {
  if (!role || role.runs === 0 || role.success_rate == null) return "text-gray-500";
  if (role.success_rate >= 90) return "text-green-400";
  if (role.success_rate >= 60) return "text-amber-300";
  return "text-red-300";
}

/** A role with runs but no finished ones (all cancelled) is called out
 * separately: its rate is unknown, not zero. */
export function roleRunSummary(role: RoleOutcome | undefined): string {
  if (!role || role.runs === 0) return "never ran in this window";
  const bits = [`${role.runs} run${role.runs === 1 ? "" : "s"}`];
  if (role.cancelled > 0) bits.push(`${role.cancelled} cancelled`);
  const rest = describeOutcomes(role.outcomes);
  if (rest) bits.push(rest);
  return bits.join(" · ");
}

/** The honest empty state for a window with no failures. */
export function failureEmptyMessage(total: number, retries: number): string {
  if (total === 0 && retries > 0) {
    return `No failures recorded in this window, across ${retries} retried step${retries === 1 ? "" : "s"}.`;
  }
  if (total === 0) return "No failures recorded in this window.";
  return "";
}

/** The recovery sentence. No retries reads as "no evidence", never as 100%. */
export function recoveryLabel(breakdown: FailureBreakdown | undefined): string {
  if (!breakdown || breakdown.retries === 0) {
    return "No retries in this window — nothing to recover from.";
  }
  const rate = breakdown.recovery_rate;
  const pct = rate == null ? "" : ` (${rate}%)`;
  return `${breakdown.recovered} of ${breakdown.retries} retried step${
    breakdown.retries === 1 ? "" : "s"
  } got past the failure${pct}.`;
}

/** The most common cause, for the one line at the top of the view. */
export function topFailure(breakdown: FailureBreakdown | undefined): string {
  if (!breakdown || breakdown.causes.length === 0) return "";
  const first = breakdown.causes[0];
  const rest = breakdown.causes.length - 1;
  const more = rest > 0 ? ` (and ${rest} other cause${rest === 1 ? "" : "s"})` : "";
  return `${first.code} × ${first.count}${more}`;
}
