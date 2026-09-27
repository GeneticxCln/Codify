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

/**
 * The engine connection, as the top bar reads it.
 *
 * `checking` is a state of its own and used to be missing: the pill fell through to
 * whatever the healthy branch said, so the first three seconds after launch claimed
 * a connection nobody had made yet. It is also the only state that is neither good
 * nor bad, which is why it reads as idle rather than as a hue.
 */
export type EngineState = "live" | "checking" | "auth-stale" | "offline";

/**
 * Which state the connection is in.
 *
 * `up === null` is the app still asking, not a failure — the health probe retries
 * for ten attempts before it gives up, and until it does the honest answer is "I
 * don't know yet" rather than either of the two things it might turn out to be.
 * `authenticated` is nullable for the same reason and read the same way: anything
 * other than a confirmed `true` is not a confirmed connection.
 */
export function engineState(up: boolean | null, authenticated: boolean | null): EngineState {
  if (up === null) return "checking";
  if (!up) return "offline";
  return authenticated ? "live" : "auth-stale";
}

/**
 * What the pill says, and what it says on hover, for each state.
 *
 * No port number. It was the healthy label, which made the *number* look like the
 * thing worth reading and the state like decoration: a reader checking whether
 * Codify was working was made to compare four digits, and the digits changed every
 * launch. The port is still where it belongs — the engine's own boot line, the
 * stderr panel when something has gone wrong, and the settings screen.
 */
export const ENGINE_STATE_COPY: Record<EngineState, { label: string; hint: string }> = {
  live: { label: "Live", hint: "Engine connected — click to check settings" },
  checking: { label: "Checking", hint: "Asking the engine whether it is there" },
  "auth-stale": {
    label: "Auth stale",
    hint: "Engine is up but the auth token is not accepted — click to check settings",
  },
  offline: { label: "Offline", hint: "Engine is not responding — click to check settings" },
};

/**
 * The pill's fill and its dot, per state.
 *
 * Full class names, never assembled from a tone: Tailwind finds these by scanning
 * source text, and a `bg-${tone}-500` would compile to nothing.
 */
export const ENGINE_STATE_CLASSES: Record<EngineState, { pill: string; dot: string }> = {
  live: { pill: "bg-codify-bg text-gray-400 border-codify-border", dot: "bg-green-500" },
  checking: { pill: "bg-codify-bg text-gray-400 border-codify-border", dot: "bg-gray-500" },
  "auth-stale": {
    pill: "bg-amber-950/40 text-amber-400 border-amber-800",
    dot: "bg-amber-500",
  },
  offline: { pill: "bg-red-950/40 text-red-400 border-red-800", dot: "bg-red-500" },
};
