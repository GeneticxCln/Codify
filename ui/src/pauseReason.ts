import type { Event } from "./types.ts";

/**
 * Why a goal is paused, from the engine's own words.
 *
 * `PAUSED` has several causes and the status alone names none of them: the person pressed Pause, the critic
 * asked for changes, or the conductor could not finish a step (it ran out of calls, lost its model, or stopped).
 * An engine pause carries a `reason_code` from a closed set and a `reason` sentence the engine wrote on the
 * `goal_status` event (`docs/04` §1.4; `engine/models.py` `PAUSE_CODES`). The person's own Pause carries
 * neither, and shows nothing here: they know.
 *
 * The newest `goal_status` event is the whole state. A Start publishes a `RUNNING` status with no reason, so a
 * resumed goal stops showing one with no bookkeeping, and a goal record that has not caught up with the event
 * yet cannot keep a stale banner alive. A code this build does not know is not drawn: a half-drawn pause
 * ("paused: ???") is worse than the plain `PAUSED` badge the card already has.
 *
 * Pure and DOM-free, so the rules have tests that need no renderer.
 */
export const PAUSE_CODES = ["conductor_budget", "conductor_provider", "conductor_stopped", "critic_rejected"] as const;
export type PauseCode = (typeof PAUSE_CODES)[number];

/** The one-line name of each cause. The engine's sentence says what to do about it. */
export const PAUSE_HEADINGS: Record<PauseCode, string> = {
  conductor_budget: "The conductor ran out of calls",
  conductor_provider: "The conductor's model could not be reached",
  conductor_stopped: "The conductor stopped before finishing",
  critic_rejected: "The critic asked for changes",
};

export interface PauseReason {
  code: PauseCode;
  heading: string;
  /** The engine's sentence, cut to a length one event cannot use to take over the card. */
  reason: string;
  /** The step the pause names, when it names one. */
  stepId: string | null;
  /** The critic's own reasons for a `critic_rejected` pause, from the step's notes; otherwise null. */
  notes: string | null;
}

const MAX_REASON = 400;
const MAX_NOTES = 1200;

const clip = (text: string, max: number): string => (text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`);

export function isPauseCode(value: unknown): value is PauseCode {
  return typeof value === "string" && (PAUSE_CODES as readonly string[]).includes(value);
}

/** The newest notes a step was given, or null. They are the critic's opinion of a change, shown as text. */
function latestNotes(events: readonly Event[], stepId: string | null | undefined): string | null {
  if (!stepId) return null;
  let found: { sequence: number; notes: string } | null = null;
  for (const e of events) {
    if (e.type !== "step_status" || e.step_id !== stepId) continue;
    const notes = e.payload?.review_notes;
    if (typeof notes !== "string" || notes.trim().length === 0) continue;
    if (!found || e.sequence > found.sequence) found = { sequence: e.sequence, notes };
  }
  return found ? clip(found.notes.trim(), MAX_NOTES) : null;
}

export function pauseReasonOf(status: string | undefined, events: readonly Event[] | undefined): PauseReason | null {
  if (status !== "PAUSED" || !events || events.length === 0) return null;
  let newest: Event | null = null;
  for (const e of events) {
    if (e.type === "goal_status" && (!newest || e.sequence > newest.sequence)) newest = e;
  }
  if (!newest) return null;
  const payload = newest.payload ?? {};
  if (payload.status !== "PAUSED") return null;
  if (!isPauseCode(payload.reason_code)) return null;
  if (typeof payload.reason !== "string" || payload.reason.trim().length === 0) return null;
  const code = payload.reason_code;
  return {
    code,
    heading: PAUSE_HEADINGS[code],
    reason: clip(payload.reason.trim(), MAX_REASON),
    stepId: newest.step_id ?? null,
    notes: code === "critic_rejected" ? latestNotes(events, newest.step_id) : null,
  };
}
