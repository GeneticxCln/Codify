import test from "node:test";
import assert from "node:assert/strict";

// goalActions.ts is the pure policy half of the chat's versioned actions: no
// fetch, no engine, no module-level side effects (the api.ts import is
// type-only and erased). Every rejection below is a duck-typed stand-in for
// api.ts's ApiRequestError — the policy reads `status`, `code` and `message`
// and nothing else, which is itself part of the contract under test.

import {
  RACE_CODES,
  runGoalAction,
  type GoalActionArgs,
} from "../src/goalActions.ts";

let now = 0;
const clock: number[] = [];

// Deterministic "setTimeout": record the requested delay, resolve on the spot.
// Waiting in real time would make these tests as flaky as the code they pin.
globalThis.setTimeout = ((fn: () => void, ms?: number) => {
  clock.push(ms ?? 0);
  fn();
  return 0 as unknown as ReturnType<typeof setTimeout>;
}) as typeof setTimeout;

interface Rejection extends Error {
  status: number;
  code: string | null;
}

function refuse(status: number, code: string | null, message: string): Rejection {
  return Object.assign(new Error(message), { status, code });
}

function goalOf(version: number) {
  return { id: "g1", version } as unknown as Parameters<
    NonNullable<GoalActionArgs<unknown>["attempt"]>
  > extends never ? never : object;
}

function args(overrides: Partial<GoalActionArgs<object>>): GoalActionArgs<object> {
  return {
    action: "pause",
    attempt: async () => goalOf(9),
    observe: async () => ({ status: "RUNNING", version: 5 }),
    ...overrides,
  };
}

test("a race refusal is retried with a fresh read, then applies", async () => {
  clock.length = 0; // module-global: every clock assertion resets it first
  const versions: number[] = [];
  const reads = [5, 6, 7];
  const outcome = await runGoalAction(
    args({
      attempt: async (v) => {
        versions.push(v);
        if (versions.length < 3) throw refuse(409, "version_conflict", "version mismatch");
        return goalOf(v);
      },
      observe: async () => ({ status: "RUNNING", version: reads[versions.length] ?? 7 }),
    }),
  );
  assert.equal(outcome.kind, "applied");
  assert.deepEqual(versions, [5, 6, 7]);
  assert.deepEqual(clock, [150, 150]);
});

test("illegal_status is a race too, and retried like one", async () => {
  const outcome = await runGoalAction(
    args({
      attempt: async () => {
        throw refuse(409, "illegal_status", "cannot pause from COMPLETED");
      },
    }),
  );
  // Exhausted retries surface the refusal — a pause that keeps answering
  // illegal_status on a goal that still reads RUNNING is a real problem.
  assert.equal(outcome.kind, "refused");
  assert.equal(outcome.code, "illegal_status");
});

test("a non-race refusal is never retried", async () => {
  clock.length = 0;
  const attempts: number[] = [];
  const outcome = await runGoalAction(
    args({
      attempt: async () => {
        attempts.push(1);
        throw refuse(404, "unknown_goal", "goal not found");
      },
    }),
  );
  assert.equal(outcome.kind, "refused");
  assert.equal(outcome.code, "unknown_goal");
  assert.equal(attempts.length, 1);
  assert.deepEqual(clock, []);
});

test("a moot state refreshes instead of acting or erroring", async () => {
  const outcome = await runGoalAction(
    args({ observe: async () => ({ status: "COMPLETED", version: 9 }) }),
  );
  assert.deepEqual(outcome, { kind: "moot", status: "COMPLETED" });
});

test("each attempt observes fresh: no version is reused after a race", async () => {
  const observations: string[] = [];
  const outcome = await runGoalAction(
    args({
      maxAttempts: 2,
      attempt: async () => {
        throw refuse(409, "version_conflict", "version mismatch");
      },
      observe: async () => {
        observations.push("read");
        return { status: "RUNNING", version: observations.length };
      },
    }),
  );
  assert.equal(outcome.kind, "refused");
  assert.equal(observations.length, 2);
});

test("a failing observe refuses immediately — retrying a dead engine helps nobody", async () => {
  clock.length = 0;
  const attempts: number[] = [];
  const outcome = await runGoalAction(
    args({
      attempt: async () => {
        attempts.push(1);
        return goalOf(1);
      },
      observe: async () => {
        throw refuse(0, null, "the engine is down");
      },
    }),
  );
  assert.equal(outcome.kind, "refused");
  assert.equal(attempts.length, 0);
  assert.deepEqual(clock, []);
});

test("retry progress reaches onRetry, not the error path", async () => {
  const retries: Array<[number, string]> = [];
  await runGoalAction(
    args({
      maxAttempts: 3,
      attempt: async () => {
        throw refuse(409, "version_conflict", "version mismatch");
      },
      onRetry: (attempt, code) => retries.push([attempt, code]),
    }),
  );
  assert.deepEqual(retries, [[1, "version_conflict"], [2, "version_conflict"]]);
});

test("the race codes are exactly the two the wire tests pin", () => {
  assert.deepEqual([...RACE_CODES], ["version_conflict", "illegal_status"]);
});

test("defaults: three attempts, 150 ms apart", async () => {
  clock.length = 0;
  const outcome = await runGoalAction(
    args({
      attempt: async () => {
        throw refuse(409, "version_conflict", "version mismatch");
      },
    }),
  );
  assert.equal(outcome.kind, "refused");
  assert.deepEqual(clock, [150, 150]);
});
