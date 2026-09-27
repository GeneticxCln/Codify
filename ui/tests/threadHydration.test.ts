/**
 * Reopening a thread: which turns are worth fetching, and what a hydrated one is.
 *
 * The defect these pin is a pane that opened empty. The engine had the whole
 * transcript — `GET /conversations/{id}/turns`, and each turn's events, the same
 * ones a live stream delivers — and the UI store only held what had streamed into
 * it this session, so every thread was blank after a reload. The two things that
 * make the fix safe are the two things tested here, and both are about the store
 * rather than the network:
 *
 * * **which turns to fetch.** A live turn's message ids are minted from the clock
 *   (`user-${Date.now()}`) and a hydrated one's from the goal id, so message ids
 *   cannot tell the two apart — the goal can, and a fetch that raced a live turn
 *   would otherwise put the turn the user is watching into the pane a second time.
 * * **how the pair lands.** By id, so a thread read twice is replaced rather than
 *   appended to, and a streaming message is never overwritten by a copy fetched
 *   before it existed.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  mergeThreadMessages,
  presentGoalIds,
  shouldHydrateThread,
  turnMessageIds,
  turnMessages,
  turnsNeedingHydration,
} from "../src/threadHydration.ts";
import type {
  ChatMessage,
  ConversationTurn,
  Event,
  Goal,
  GoalStatus,
} from "../src/types.ts";

const status = (s: GoalStatus): Goal => ({
  id: "goal-1",
  workspace_id: "ws-1",
  conversation_id: "thread-1",
  title: "Add a rate limit",
  description: "Add a rate limit to the settings route",
  status: s,
  dry_run: false,
  plan_only: false,
  parallel: false,
  version: 1,
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
});

const turn = (
  goal_id: string,
  prompt: string = "hi",
  created_at: number = 1_700_000_000,
): ConversationTurn => ({
  goal_id,
  conversation_id: "thread-1",
  prompt,
  status: "COMPLETED",
  created_at,
});

const event = (sequence: number, type: Event["type"]): Event => ({
  id: `e${sequence}`,
  goal_id: "goal-1",
  step_id: null,
  sequence,
  type,
  payload: { text: `chunk ${sequence}` },
  timestamp: sequence,
});

/** A turn as the live path leaves it: clock-minted ids and the goal attached. */
const live = (goalId: string, streaming = true): ChatMessage => ({
  id: "assistant-1700000000001",
  role: "assistant",
  content: "Analyzing workspace and planning atomic steps...",
  timestamp: 1_700_000_000_001,
  goal: { ...status("RUNNING"), id: goalId },
  events: [],
  isStreaming: streaming,
  conversationId: "thread-1",
});

test("a hydrated turn is the pair the restore path builds", () => {
  const goal = status("COMPLETED");
  const [user, assistant] = turnMessages(
    turn(goal.id, "Add a rate limit to the settings route"),
    goal,
    [event(2, "log"), event(1, "usage")],
    "thread-1",
  );

  // Same id scheme as `restoreGoal`, so a goal restored from History and the same
  // goal hydrated from its thread are one message in the store, not two.
  assert.deepEqual(turnMessageIds("goal-1"), {
    user: "user-goal-1",
    assistant: "assistant-goal-1",
  });
  assert.equal(user.id, "user-goal-1");
  assert.equal(assistant.id, "assistant-goal-1");
  assert.equal(assistant.goal?.id, goal.id);
  assert.equal(assistant.isStreaming, false);
  // Both stamped with the thread, or the pane would filter them straight out.
  assert.equal(user.conversationId, "thread-1");
  assert.equal(assistant.conversationId, "thread-1");
  // Events in sequence order however the engine returned them: a replayed
  // transcript with a gap reads as a broken one.
  assert.deepEqual(
    assistant.events?.map((e) => e.sequence),
    [1, 2],
  );
  // Timestamps come from the turn, not from when the pane happened to open.
  assert.equal(user.timestamp, 1_700_000_000_000);
});

test("the user bubble reads as the words that were typed", () => {
  const goal = status("COMPLETED");
  goal.title = "Add a rate limit"; // the normalised first line, not the request
  const [user] = turnMessages(
    turn(goal.id, "Add a rate limit to the settings route\n\nwith a 429 body"),
    goal,
    [],
    "thread-1",
  );
  assert.equal(
    user.content,
    "Add a rate limit to the settings route\n\nwith a 429 body",
    "a title the engine normalised must not replace what the user said",
  );
});

test("a turn already in the store is not fetched again", () => {
  const turns = [turn("goal-1"), turn("goal-2")];
  // The store holds goal-2 as a *live* turn, whose message id says nothing about
  // goal-2 — so the discriminator has to be the goal the message carries.
  const messages = [live("goal-2")];
  assert.deepEqual(
    turnsNeedingHydration(turns, messages).map((t) => t.goal_id),
    ["goal-1"],
  );
  assert.deepEqual([...presentGoalIds(messages)], ["goal-2"]);
});

test("a turn that is over is not refetched either", () => {
  const goal = status("COMPLETED");
  const already = turnMessages(turn(goal.id, "hi"), goal, [], "thread-1");
  assert.deepEqual(
    turnsNeedingHydration([turn(goal.id, "hi")], already),
    [],
    "a thread read twice in one session has nothing left to fetch",
  );
});

test("reopening a thread replaces its own messages instead of appending", () => {
  const goal = status("COMPLETED");
  const first = turnMessages(turn(goal.id, "hi"), goal, [event(1, "log")], "thread-1");
  const second = turnMessages(
    turn(goal.id, "hi"),
    goal,
    [event(1, "log"), event(2, "log")],
    "thread-1",
  );
  const merged = mergeThreadMessages(first, second);
  assert.deepEqual(
    merged.map((m) => m.id),
    ["user-goal-1", "assistant-goal-1"],
    "two reads of one turn is one pair of messages",
  );
  assert.equal(merged[1].events?.length, 2, "and it is the newer copy that is kept");
});

test("a live turn is not written a second time by a fetch that raced it", () => {
  // The composition the pane runs: filter the thread's turns against the store,
  // hydrate what is left, merge. The live turn's ids are the clock's, so only the
  // by-goal filter can hold it back — and that is why the filter is re-run inside
  // the write, after the fetches went out.
  const streaming = live("goal-1");
  const turns = [turn("goal-1"), turn("goal-2")];
  const wanted = turnsNeedingHydration(turns, [streaming]);
  assert.deepEqual(
    wanted.map((t) => t.goal_id),
    ["goal-2"],
    "the live turn is not fetched, so it cannot be fetched into a second copy",
  );
  const fetched = wanted.flatMap((t) => {
    const goal = status("COMPLETED");
    goal.id = t.goal_id;
    return turnMessages(t, goal, [event(1, "log")], "thread-1");
  });
  const merged = mergeThreadMessages([streaming], fetched);
  assert.deepEqual(
    merged.map((m) => m.id),
    ["assistant-1700000000001", "user-goal-2", "assistant-goal-2"],
    "the live turn is the one the user watches, and the rest of the thread arrives",
  );
  assert.equal(merged[0].isStreaming, true);
});

test("an untouched store is returned unchanged", () => {
  const messages = [live("goal-1")];
  assert.deepEqual(mergeThreadMessages(messages, []), messages);
});

test("a thread is read once per session, and a read in flight blocks a second", () => {
  assert.equal(shouldHydrateThread("thread-1", new Set(), new Set()), true);
  // No thread open: nothing to read, which is why this is a decision and not a
  // fetch that happens to return nothing.
  assert.equal(shouldHydrateThread(undefined, new Set(), new Set()), false);
  // Read already.
  assert.equal(
    shouldHydrateThread("thread-1", new Set(["thread-1"]), new Set()),
    false,
  );
  // Read in flight: two tabs on one thread must not both read it.
  assert.equal(
    shouldHydrateThread("thread-1", new Set(), new Set(["thread-1"])),
    false,
  );
  // Another thread is irrelevant to this one.
  assert.equal(
    shouldHydrateThread("thread-2", new Set(["thread-1"]), new Set(["thread-1"])),
    true,
  );
});
