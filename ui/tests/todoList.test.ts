/**
 * The conductor's notes, as the window reads them: the rules.
 *
 * The engine publishes the whole todo list as a `todo_updated` event every time the conductor changes it
 * (`engine/todo.py`, `docs/04` §1.4), so the newest event *is* the list. The text is the model's, and can echo
 * anything it read, so it is shown as plain text, one line each, bounded; an item or a snapshot this build
 * cannot read is left out rather than half-drawn; and an abandoned item is not shown as open work.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Event } from "../src/types.ts";

const { MAX_TODO_ITEMS, MAX_TODO_TEXT, visibleTodos } = await import("../src/todoList.ts");

const ev = (sequence: number, payload: unknown, type = "todo_updated"): Event =>
  ({ id: `e${sequence}`, goal_id: "g1", step_id: null, type, payload, timestamp: sequence, sequence }) as Event;

const item = (id: string, text: string, status = "pending") => ({ id, text, status });

test("the newest snapshot is the list, whatever order the events were stored in", () => {
  const events = [
    ev(9, { items: [item("t1", "newest"), item("t2", "also newest")] }),
    ev(2, { items: [item("t1", "oldest")] }),
  ];
  assert.deepEqual(
    visibleTodos(events).map((i) => i.text),
    ["newest", "also newest"],
  );
});

test("no events, no todo events, and no list are all an empty list", () => {
  assert.deepEqual(visibleTodos(undefined), []);
  assert.deepEqual(visibleTodos([]), []);
  assert.deepEqual(visibleTodos([ev(1, { status: "RUNNING" }, "goal_status")]), []);
  assert.deepEqual(visibleTodos([ev(1, { items: [] })]), []);
});

test("a snapshot that is not one is an empty list, and does not fall back to an older one", () => {
  // The newest event is the list. A damaged newest event costs the notes; it does not resurrect stale ones.
  const events = [ev(1, { items: [item("t1", "old")] }), ev(2, "garbage")];
  assert.deepEqual(visibleTodos(events), []);
  assert.deepEqual(visibleTodos([ev(1, null)]), []);
  assert.deepEqual(visibleTodos([ev(1, { items: "no" })]), []);
});

test("an item this build cannot read is left out and its neighbours are kept", () => {
  const events = [
    ev(1, {
      items: [
        item("t1", "keep"),
        item("t2", "unknown status", "exploding"),
        { id: 3, text: "id is not text", status: "pending" },
        item("t4", "   "),
        { id: "t5", status: "pending" },
        "nonsense",
        null,
        item("t6", "keep too", "done"),
      ],
    }),
  ];
  assert.deepEqual(
    visibleTodos(events).map((i) => i.id),
    ["t1", "t6"],
  );
});

test("a dropped item is not shown as open work", () => {
  const events = [ev(1, { items: [item("t1", "kept"), item("t2", "gave up on it", "dropped"), item("t3", "finished", "done")] })];
  assert.deepEqual(
    visibleTodos(events).map((i) => [i.id, i.status]),
    [
      ["t1", "pending"],
      ["t3", "done"],
    ],
  );
});

test("an item is one line of bounded text, so one event cannot take over the card", () => {
  const long = "x".repeat(5000);
  const events = [ev(1, { items: [item("t1", long), item("t2", "a\n\nb\tc")] })];
  const [first, second] = visibleTodos(events);
  assert.ok(first.text.length <= MAX_TODO_TEXT, String(first.text.length));
  assert.ok(first.text.endsWith("…"));
  assert.equal(second.text, "a b c", "newlines and tabs would let an item break out of its row");
});

test("a snapshot longer than the engine allows is cut to the limit", () => {
  const many = Array.from({ length: 60 }, (_, n) => item(`t${n + 1}`, `item ${n + 1}`));
  assert.equal(visibleTodos([ev(1, { items: many })]).length, MAX_TODO_ITEMS);
});

test("the limits are the engine's", () => {
  assert.equal(MAX_TODO_ITEMS, 20);
  assert.equal(MAX_TODO_TEXT, 160);
});
