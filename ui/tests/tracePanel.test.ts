import test from "node:test";
import assert from "node:assert/strict";

import { traceGoalId } from "../src/tracePanel.ts";

// The crash this pins shipped in `03c1c6f` and was live on master: the transcript
// evaluated `msg.goal!.id` outside the `{msg.goal && …}` guard, so a message with no
// goal — which is *every user message* — threw and took the whole window to a blank
// screen. It survived because this suite has no renderer, so a JSX-only condition was
// never executed by any test. Extracting the decision into a plain module is what
// makes it checkable at all, and the first test is that exact shape.

test("a message with no goal opens nothing, and does not throw", () => {
  // The regression. `undefined` is what a user message carries, and what an
  // assistant message carries between being appended and its goal being fetched.
  assert.equal(traceGoalId(undefined, null), null);
  assert.equal(traceGoalId(undefined, "g1"), null);
  assert.equal(traceGoalId(null, "g1"), null);
  assert.equal(traceGoalId(null, null), null);
});

test("a selected goal opens its own panel", () => {
  assert.equal(traceGoalId({ id: "g1" }, "g1"), "g1");
});

test("nothing selected opens nothing, even for a goal that exists", () => {
  assert.equal(traceGoalId({ id: "g1" }, null), null);
});

test("a selection naming another goal opens nothing", () => {
  // Several goal cards render in one transcript, so "some goal is selected" and
  // "this card's goal is selected" are different questions. Answering the first
  // would open a panel on every card at once.
  assert.equal(traceGoalId({ id: "g1" }, "g2"), null);
});

test("an empty id is treated as a selection, and matches only an empty id", () => {
  // `traceFor` is reset to null, never to "", so "" is not a real state. If it ever
  // becomes one it must not open a panel on a goal whose id happens to be falsy.
  assert.equal(traceGoalId({ id: "g1" }, ""), null);
  assert.equal(traceGoalId({ id: "" }, ""), "");
});

test("the result is a usable id, not a boolean — so the caller needs no assertion", () => {
  // The point of returning `string | null` rather than `boolean` is that the
  // JSX below the decision can be handed a real value. A boolean would have left
  // `msg.goal!.id` in the caller, which is the trap this module was written to close.
  const opened = traceGoalId({ id: "g1" }, "g1");
  assert.equal(typeof opened, "string");
  assert.notEqual(opened, null);
});
