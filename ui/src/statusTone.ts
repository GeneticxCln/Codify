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
    hint: "Engine is up but the auth token is not accepted — the banner below has the fix",
  },
  offline: { label: "Offline", hint: "Engine is not responding — click to check settings" },
};

/**
 * What the auth-stale state tells the user to *do*, and how it says it.
 *
 * **Why this is here and not in the component.** `statusTone.ts` is a plain module
 * because the UI suite runs through `node --test` with no renderer; the copy for
 * the one state that has a fix attached is exactly the kind of decision that used
 * to live in JSX and become untestable. `ui/tests/statusTone.test.ts` holds these
 * strings, and it does so *loosely on purpose*: a test that pins the whole sentence
 * forces every wording tweak through the test first, which is the wrong direction
 * for prose — but a test that pins the *shape* (which keys exist, that the token
 * reaches the browser console, that the make target is named) fails the day someone
 * deletes the fix without replacing it.
 *
 * **Why the fix is a paste and not a button.** The token is held by the engine's
 * spawner — the Tauri shell, which self-heals via IPC, or `make run-engine-preview`,
 * which prints it. A browser tab cannot read either; only the developer standing in
 * front of the console can bridge that gap. So the banner shows the two statements
 * that bridge it, already filled in, and a copy button — it does not pretend to
 * apply anything itself, because it cannot reach the value it would need.
 */
export const STALE_AUTH_FIX: {
  heading: string;
  body: string;
  /** The make target that prints a fresh handshake, named so a rename shows up here. */
  command: string;
  /** How the printed values reach the app, as one sentence. */
  pasteHint: string;
} = {
  heading: "The engine is up, but this tab's token is not accepted",
  body:
    "The engine rotates its boot token on every restart, and this browser tab is still " +
    "holding the previous one. The desktop app fixes itself; a browser tab has no way to " +
    "read the new token, so it has to be pasted once.",
  command: "make run-engine-preview",
  pasteHint:
    "Run it and paste the two localStorage lines it prints into this console, then reload.",
};

/**
 * The pill's fill and its dot, per state.
 *
 * Full class names, never assembled from a tone: Tailwind finds these by scanning
 * source text, and a `bg-${tone}-500` would compile to nothing.
 *
 * They name the theme's status tokens rather than `bg-codify-success/20` / `bg-codify-danger/20`,
 * which is the same change `components/ui/Badge.tsx` made: these were literals, so
 * the Offline dot was Tailwind red in every theme — including the two that draw
 * rain behind it. The class names are still written out, because that is what
 * makes them findable; `ui/src/index.css` is what paints them.
 */
export const ENGINE_STATE_CLASSES: Record<EngineState, { pill: string; dot: string }> = {
  live: { pill: "bg-codify-bg text-codify-muted border-codify-border", dot: "bg-codify-success" },
  checking: {
    pill: "bg-codify-bg text-codify-muted border-codify-border",
    dot: "bg-codify-neutral",
  },
  "auth-stale": {
    pill: "bg-codify-warning/40 text-codify-warning border-codify-warning",
    dot: "bg-codify-warning",
  },
  offline: {
    pill: "bg-codify-danger/40 text-codify-danger border-codify-danger",
    dot: "bg-codify-danger",
  },
};
