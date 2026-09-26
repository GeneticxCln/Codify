import type { BadgeTone } from "./components/ui/Badge";

/**
 * One status, one colour, everywhere.
 *
 * The audit found six hues doing one job. "Success" was green on the history row,
 * emerald somewhere in the settings, and teal in the audit-flash CSS; "in flight" was
 * blue on one card and violet on another; and a status was drawn by hand at every
 * call site, so a seventh shade was always one copy-paste away.
 *
 * This is the table that makes that impossible. It is a plain module on purpose —
 * the UI suite runs through `node --test` with no renderer, so a decision that lives
 * in JSX is a decision no test can reach. The one status-coloured ternary this
 * replaces sat in `ChatTimeline` and in the history drawer, and both had been free to
 * disagree.
 *
 * The mapping is also *narrower* than the status list, deliberately. A tone is a claim
 * about meaning, so a status this table has never heard of falls back to `neutral`
 * rather than guessing a hue — an unknown state is "nothing to report", and colouring
 * it as a failure would be inventing a diagnosis the engine never made. The same
 * reasoning the engine applies when a provider fails to answer.
 */
const GOAL_TONES: Record<string, BadgeTone> = {
  PLANNING: "info",
  RUNNING: "info",
  PENDING: "neutral",
  PAUSED: "warning",
  COMPLETED: "success",
  FAILED: "danger",
  CANCELLED: "neutral",
};

/** The tone for a goal's status. Unknown and absent statuses read as idle. */
export function statusTone(status: string | null | undefined): BadgeTone {
  if (!status) return "neutral";
  return GOAL_TONES[status] ?? "neutral";
}

const STEP_TONES: Record<string, BadgeTone> = {
  PENDING: "neutral",
  IN_PROGRESS: "info",
  RUNNING: "info",
  PASSED: "success",
  FAILED: "danger",
  CANCELLED: "neutral",
  SKIPPED: "warning",
};

/**
 * The tone for a plan step's status.
 *
 * Separate from the goal mapping on purpose: a step that was skipped is a *warning*,
 * where a goal that was cancelled is merely *neutral*. A step is one unit of work
 * inside a run, so "this one did not happen" is a thing the reader wants noticed — a
 * cancelled goal is the reader's own decision and needs no flag.
 */
export function stepTone(status: string | null | undefined): BadgeTone {
  if (!status) return "neutral";
  return STEP_TONES[status] ?? "neutral";
}
