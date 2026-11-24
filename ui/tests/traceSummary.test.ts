import { test } from "node:test";
import assert from "node:assert/strict";
import {
  traceHeadline,
  traceRoles,
  promptStorageNote,
  formatTracePayload,
  canArmTrace,
} from "../src/traceSummary.ts";
import type { TraceSummary } from "../src/types.ts";

function summary(over: Partial<TraceSummary> = {}): TraceSummary {
  return {
    goal_id: "g1",
    calls: 0,
    by_role: {},
    prompts_kept: false,
    recorded: [],
    ...over,
  };
}

test("the headline names a count, and does not dress an empty recording as data", () => {
  assert.equal(traceHeadline(summary()), "No calls recorded");
  assert.equal(traceHeadline(summary({ calls: 1 })), "1 call recorded");
  assert.equal(traceHeadline(summary({ calls: 7 })), "7 calls recorded");
});

test("roles are ordered by count, and ties do not reshuffle between renders", () => {
  const roles = traceRoles(
    summary({ by_role: { planner: 2, fixer: 5, critic: 2, scribe: 5 } }),
  );
  assert.deepEqual(roles, [
    ["fixer", 5],
    ["scribe", 5],
    ["critic", 2],
    ["planner", 2],
  ]);
  assert.deepEqual(traceRoles(summary({ by_role: {} })), []);
});

test("digests and kept text are different documents, and the note says which", () => {
  assert.match(promptStorageNote(false), /Digests only/);
  assert.match(promptStorageNote(true), /Full prompts and replies kept/);
  assert.notEqual(promptStorageNote(true), promptStorageNote(false));
});

test("formatTracePayload pretty-prints JSON messages and leaves prose untouched", () => {
  assert.equal(formatTracePayload(null), "");
  assert.equal(formatTracePayload("plain prose"), "plain prose");
  assert.equal(
    formatTracePayload('[{"role":"user","content":"hello"}]'),
    '[\n  {\n    "role": "user",\n    "content": "hello"\n  }\n]',
  );
  assert.equal(
    formatTracePayload("[not valid json]"),
    "[not valid json]",
  );
});

test("only a goal that has not started can still be armed", () => {
  assert.equal(canArmTrace("PLANNING"), true);
  for (const status of [
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "PAUSED",
  ]) {
    assert.equal(canArmTrace(status), false, status);
  }
});
