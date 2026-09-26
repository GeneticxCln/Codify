/**
 * The chat's versioned goal actions — start, pause, cancel, resume — and the
 * races they can lose.
 *
 * `/pause`, `/cancel` and `/start` are version-protected (`expected_version`),
 * and pause/cancel additionally refuse the wrong status. Both refusals can be
 * *lost races* rather than broken premises: the status a card shows and the
 * version it hands over are two reads no client can make atomic, and the
 * executor legally moves the goal between them (a step finished; an event
 * bumped the version). The engine answers those races with 409 and a body code
 * saying which check refused:
 *
 * - `version_conflict` — the goal moved under you; re-read, re-send.
 * - `illegal_status`   — the goal is no longer in a state where this action
 *   applies; it may have reached it *because* the user acted, or moments
 *   before they did. Either way the intent is already satisfied or moot.
 *
 * `runGoalAction` is the policy the wire tests pin
 * (`tests/test_concurrent_streams_ws.py` retries exactly these two codes; the
 * node tests here pin the client side of the same shape): retry a race a
 * bounded number of times, treat a state that no longer needs the action as
 * *moot* — a refresh, not an error — and surface anything else as a refusal
 * with the engine's own message. A UI that flashed "Failed to pause goal" for
 * a goal that finished a beat too fast was reporting the race as a bug.
 */

import type { Goal } from "./types";

/** The 409 body codes that mean "the goal moved under you", which is legal. */
export const RACE_CODES = ["version_conflict", "illegal_status"] as const;

export type GoalActionKind = "start" | "pause" | "cancel";

/** The statuses each action is meaningful from — anything else is moot. */
export const MEANINGFUL_FROM: Record<GoalActionKind, string[]> = {
  start: ["PENDING", "PAUSED"],
  pause: ["RUNNING"],
  cancel: ["PLANNING", "PENDING", "RUNNING", "PAUSED"],
};

/**
 * Whether a goal in `status` can still be stopped.
 *
 * One question with two honest answers that used to be conflated. "Is the engine
 * working on this?" is `ACTIVE_STATUSES` — planning or running, the states that
 * report progress. "Can the user stop it?" is the cancel set above, and it is
 * strictly wider: a `PAUSED` goal is not being worked on but can still be ended, and
 * a `PLANNING` goal is the *best* moment to stop it, because no fixer has written a
 * file yet.
 *
 * The command bar's Stop asks the second question, so it lives here beside the list
 * it has to agree with. A second, hand-kept copy of these statuses is how the goal
 * card ended up hiding Cancel during `PLANNING` — the engine permitted it, this
 * policy permitted it, and the button that should have offered it was not rendered.
 */
export function canStopGoal(status: string | null | undefined): boolean {
  return !!status && MEANINGFUL_FROM.cancel.includes(status);
}

/**
 * Whether the engine is still doing work on this goal, so a card should show it as
 * in flight rather than settled.
 */
export const ACTIVE_STATUSES: readonly string[] = ["PLANNING", "RUNNING"];

export function isGoalActive(status: string | null | undefined): boolean {
  return !!status && ACTIVE_STATUSES.includes(status);
}

/** What one action run saw. `refused` carries the engine's own words. */
export type GoalActionOutcome<GoalT> =
  | { kind: "applied"; goal: GoalT }
  | { kind: "moot"; status: string }
  | { kind: "refused"; status: number; code: string | null; message: string };

export interface GoalActionArgs<GoalT> {
  action: GoalActionKind;
  /** One wire attempt at the given version (api.ts's start/pause/cancelGoal). */
  attempt: (expected_version: number) => Promise<GoalT>;
  /** Read the goal's current status and version — one GET, re-done per try. */
  observe: () => Promise<{ status: string; version: number }>;
  /**
   * The statuses the action is meaningful from; defaults to the action's own
   * set (MEANINGFUL_FROM). Observing anything else is moot: the goal is
   * already where the action would have put it, or past it.
   */
  meaningfulFrom?: string[];
  maxAttempts?: number;
  /** Milliseconds between attempts; real races resolve in single digits. */
  retryDelayMs?: number;
  /** Progress for the console, not the error banner: retries are not failures. */
  onRetry?: (attempt: number, code: string) => void;
}

function failureOf(err: unknown): {
  status: number; code: string | null; message: string;
} {
  const e = err as { status?: unknown; code?: unknown; message?: unknown };
  return {
    status: typeof e?.status === "number" ? e.status : 0,
    code: typeof e?.code === "string" ? e.code : null,
    message: typeof e?.message === "string" ? e.message : "the request failed",
  };
}

export async function runGoalAction<GoalT>(
  args: GoalActionArgs<GoalT>,
): Promise<GoalActionOutcome<GoalT>> {
  const maxAttempts = args.maxAttempts ?? 3;
  const retryDelayMs = args.retryDelayMs ?? 150;
  const meaningfulFrom = args.meaningfulFrom ?? MEANINGFUL_FROM[args.action];
  let last: { status: number; code: string | null; message: string } | null = null;

  for (let n = 1; n <= maxAttempts; n++) {
    let observed: { status: string; version: number };
    try {
      observed = await args.observe();
    } catch (err) {
      // The goal cannot even be read — an engine that is down answers nothing,
      // and retrying a read that just failed would only widen the outage.
      const f = failureOf(err);
      return { kind: "refused", ...f };
    }
    if (!meaningfulFrom.includes(observed.status)) {
      // The moment passed while the user was looking at it. The card refresh
      // tells the story; an error banner would be lying about a race the
      // engine adjudicated correctly.
      return { kind: "moot", status: observed.status };
    }

    try {
      const goal = await args.attempt(observed.version);
      return { kind: "applied", goal };
    } catch (err) {
      const f = failureOf(err);
      const raced = f.status === 409 && f.code !== null
        && (RACE_CODES as readonly string[]).includes(f.code);
      if (!raced) {
        return { kind: "refused", ...f };
      }
      last = f;
      if (n < maxAttempts) {
        // A retry is what follows, so progress is reported only when there is
        // one — the terminal attempt's failure is the outcome, not progress.
        args.onRetry?.(n, f.code ?? "");
        await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
      }
    }
  }
  return {
    kind: "refused",
    status: last?.status ?? 0,
    code: last?.code ?? null,
    message: last?.message ?? "the engine kept moving the goal",
  };
}

/** The api.ts shape the handlers already expect, applied through the policy. */
export type GoalActionResult = GoalActionOutcome<Goal>;
