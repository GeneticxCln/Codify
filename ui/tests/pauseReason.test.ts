/**
 * A paused goal says why: the rules.
 *
 * `PAUSED` has three causes (the person's Pause, the critic asking for changes, and a conductor that could not
 * finish a step) and the status alone names none of them. The engine now puts a code from a closed set and a
 * sentence of its own on the `goal_status` event of an engine pause (`docs/04` §1.4). These are the rules for
 * what the window makes of that, with no renderer: the newest status event decides, so a Start clears it with
 * no bookkeeping; the person's own Pause has no reason and shows none; a code this build does not know is not
 * drawn rather than half-drawn; and a notification is raised only for a pause that is the goal's *current*
 * state, so the engine replaying history on a reconnect never announces an old one.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Event } from "../src/types.ts";

const { PAUSE_CODES, PAUSE_HEADINGS, isPauseCode, pauseReasonOf } = await import("../src/pauseReason.ts");
const { pausedNotification } = await import("../src/notifications.ts");

const ev = (sequence: number, payload: Record<string, unknown>, type = "goal_status", stepId: string | null = null): Event =>
  ({ id: `e${sequence}`, goal_id: "g1", step_id: stepId, type, payload, timestamp: sequence, sequence }) as Event;

const paused = (sequence: number, code: string, version = sequence, stepId: string | null = "s1"): Event =>
  ev(sequence, { status: "PAUSED", version, reason_code: code, reason: `engine words for ${code}` }, "goal_status", stepId);

test("the codes are the engine's four, each with a heading", () => {
  assert.deepEqual([...PAUSE_CODES], ["conductor_budget", "conductor_provider", "conductor_stopped", "critic_rejected"]);
  for (const code of PAUSE_CODES) assert.ok(PAUSE_HEADINGS[code].length > 0, code);
  assert.equal(isPauseCode("conductor_budget"), true);
  assert.equal(isPauseCode("because"), false);
  assert.equal(isPauseCode(undefined), false);
});

test("a paused goal's newest status event gives the reason", () => {
  const found = pauseReasonOf("PAUSED", [ev(1, { status: "RUNNING", version: 2 }), paused(2, "conductor_budget")]);
  assert.ok(found);
  assert.equal(found.code, "conductor_budget");
  assert.equal(found.heading, PAUSE_HEADINGS.conductor_budget);
  assert.equal(found.reason, "engine words for conductor_budget");
  assert.equal(found.stepId, "s1");
});

test("the person's own pause has no reason, and neither does a goal that is not paused", () => {
  assert.equal(pauseReasonOf("PAUSED", [ev(1, { status: "PAUSED", version: 2 })]), null, "a Pause button press carries no engine reason");
  assert.equal(pauseReasonOf("RUNNING", [paused(1, "conductor_budget")]), null, "a goal that is running again is not paused");
  assert.equal(pauseReasonOf("PAUSED", []), null);
  assert.equal(pauseReasonOf("PAUSED", undefined), null);
});

test("a Start clears it with no bookkeeping: the newest status event is not a pause", () => {
  const events = [paused(2, "conductor_stopped"), ev(3, { status: "RUNNING", version: 4 })];
  assert.equal(pauseReasonOf("PAUSED", events), null, "even if the goal record has not caught up yet");
});

test("an event that is not a pause is not drawn as one, even if it carries a code", () => {
  // The engine only puts a code on a PAUSED event. A malformed `RUNNING` event that has one, arriving while
  // the goal record still says PAUSED, must not keep a banner alive.
  const malformed = ev(4, { status: "RUNNING", version: 4, reason_code: "conductor_budget", reason: "stale words" });
  assert.equal(pauseReasonOf("PAUSED", [paused(2, "conductor_budget"), malformed]), null);
});

test("a later pause replaces an earlier one, whatever order the events were stored in", () => {
  const events = [paused(9, "conductor_provider"), paused(2, "conductor_budget")];
  assert.equal(pauseReasonOf("PAUSED", events)?.code, "conductor_provider");
});

test("a code this build does not know, or a reason that is not text, is not drawn", () => {
  assert.equal(pauseReasonOf("PAUSED", [paused(1, "something_new")]), null);
  assert.equal(pauseReasonOf("PAUSED", [ev(1, { status: "PAUSED", version: 1, reason_code: "conductor_budget", reason: "  " })]), null);
  assert.equal(pauseReasonOf("PAUSED", [ev(1, { status: "PAUSED", version: 1, reason_code: "conductor_budget", reason: 7 })]), null);
});

test("a very long reason is cut, so one event cannot take over the card", () => {
  const long = "x".repeat(5000);
  const found = pauseReasonOf("PAUSED", [ev(1, { status: "PAUSED", version: 1, reason_code: "conductor_stopped", reason: long })]);
  assert.ok(found && found.reason.length <= 400, String(found?.reason.length));
  assert.ok(found.reason.endsWith("…"));
});

test("the critic's reasons come from the step's own notes, and only for a critic's pause", () => {
  const notes = ev(5, { status: "IN_PROGRESS", review_notes: "a.py: no docstring\nb.py: bad name" }, "step_status", "s1");
  const otherStep = ev(6, { status: "IN_PROGRESS", review_notes: "someone else's notes" }, "step_status", "s2");
  const critic = pauseReasonOf("PAUSED", [notes, otherStep, paused(7, "critic_rejected")]);
  assert.equal(critic?.notes, "a.py: no docstring\nb.py: bad name");

  const budget = pauseReasonOf("PAUSED", [notes, paused(7, "conductor_budget")]);
  assert.equal(budget?.notes, null, "notes are the critic's, and only a critic's pause shows them");
});

test("a step's newest notes win, and a step with none shows none", () => {
  const first = ev(1, { status: "IN_PROGRESS", review_notes: "old" }, "step_status", "s1");
  const second = ev(2, { status: "IN_PROGRESS", review_notes: "new" }, "step_status", "s1");
  assert.equal(pauseReasonOf("PAUSED", [first, second, paused(3, "critic_rejected")])?.notes, "new");
  assert.equal(pauseReasonOf("PAUSED", [paused(3, "critic_rejected")])?.notes, null);
});

// ── the notification ───────────────────────────────────────────────────────

const goal = (over: Record<string, unknown> = {}) =>
  ({ id: "g1", title: "Cache the build", status: "PAUSED", version: 7, updated_at: 100, ...over }) as never;

test("a pause that is the goal's current state is a warning notification that opens the goal", () => {
  const n = pausedNotification(paused(5, "conductor_budget", 7), goal(), 1000);
  assert.ok(n);
  assert.equal(n.kind, "goal");
  assert.equal(n.tone, "warning");
  assert.equal(n.title, PAUSE_HEADINGS.conductor_budget);
  assert.match(n.detail ?? "", /Cache the build/);
  assert.match(n.detail ?? "", /engine words for conductor_budget/);
  assert.deepEqual(n.target, { kind: "goal", goalId: "g1" });
  assert.equal(n.read, false);
});

test("the engine replaying an old pause on a reconnect is not news", () => {
  // The goal has moved on (version 9) since the pause the stream just replayed (version 7).
  assert.equal(pausedNotification(paused(5, "conductor_budget", 7), goal({ version: 9 }), 1000), null);
  // The goal is running again.
  assert.equal(pausedNotification(paused(5, "conductor_budget", 7), goal({ status: "RUNNING" }), 1000), null);
});

test("only an engine pause with a known code is announced", () => {
  assert.equal(pausedNotification(ev(5, { status: "PAUSED", version: 7 }), goal(), 1000), null, "the person's own Pause");
  assert.equal(pausedNotification(paused(5, "something_new", 7), goal(), 1000), null);
  assert.equal(pausedNotification(ev(5, { status: "RUNNING", version: 7 }), goal({ status: "RUNNING" }), 1000), null);
  assert.equal(pausedNotification(ev(5, { status: "PAUSED", version: 7, reason_code: "conductor_budget", reason: "x" }, "step_status"), goal(), 1000), null);
});

test("the same pause is the same notification, and a second pause of the same goal is another", () => {
  const a = pausedNotification(paused(5, "conductor_budget", 7), goal(), 1000);
  const again = pausedNotification(paused(5, "conductor_budget", 7), goal(), 2000);
  const later = pausedNotification(paused(9, "conductor_budget", 11), goal({ version: 11 }), 3000);
  assert.equal(a?.id, again?.id);
  assert.notEqual(a?.id, later?.id);
});

test("a long reason is cut in the notification too", () => {
  const long = ev(5, { status: "PAUSED", version: 7, reason_code: "conductor_stopped", reason: "y".repeat(3000) });
  const n = pausedNotification(long, goal({ title: "T".repeat(500) }), 1000);
  assert.ok(n && (n.detail ?? "").length <= 300, String(n?.detail?.length));
});
