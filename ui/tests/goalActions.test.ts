import test from "node:test";
import assert from "node:assert/strict";

// goalActions.ts is the pure policy half of the chat's versioned actions: no
// fetch, no engine, no module-level side effects (the api.ts import is
// type-only and erased). Every rejection below is a duck-typed stand-in for
// api.ts's ApiRequestError — the policy reads `status`, `code` and `message`
// and nothing else, which is itself part of the contract under test.

import {
  RACE_CODES,
  MEANINGFUL_FROM,
  canStopGoal,
  isGoalActive,
  ACTIVE_STATUSES,
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

// ── Can this goal be stopped? ──────────────────────────────────────────────────
//
// The command bar's Stop and the goal card's Cancel both ask this question, and
// they used to answer it differently: the engine permits cancel from PLANNING, and
// `MEANINGFUL_FROM.cancel` says so, but the card's button was rendered from a
// hand-written list that had PLANNING missing. The state that went missing is the
// one worth stopping most — nothing has been written yet. These tests exist so a
// fourth copy of the list cannot appear quietly.

const EVERY_STATUS = [
  "PENDING",
  "PLANNING",
  "RUNNING",
  "PAUSED",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
];

test("canStopGoal is true for exactly the statuses a cancel is meaningful from", () => {
  for (const status of EVERY_STATUS) {
    const expected = MEANINGFUL_FROM.cancel.includes(status);
    assert.equal(
      canStopGoal(status),
      expected,
      `${status}: the button and the policy it must agree with disagree`,
    );
  }
});

test("a planning goal can be stopped, which is when stopping costs least", () => {
  // The regression this pins: PLANNING was absent from the card's button condition
  // even though the engine and the policy both allow it.
  assert.ok(
    canStopGoal("PLANNING"),
    "a planning goal must be stoppable — no fixer has written a file yet, so this " +
      "is the cheapest possible moment for a user to change their mind",
  );
});

test("a settled goal cannot be stopped", () => {
  for (const status of ["COMPLETED", "FAILED", "CANCELLED"]) {
    assert.equal(
      canStopGoal(status),
      false,
      `${status} is settled; offering to stop it would be offering to do nothing`,
    );
  }
});

test("an absent status is not stoppable, and does not throw", () => {
  // A goal id can be tracked before its first status arrives, so the bar asks this
  // question about `undefined` on a real code path. A throw there would blank the
  // command bar during dispatch.
  for (const missing of [null, undefined, ""]) {
    assert.equal(canStopGoal(missing), false);
    assert.equal(isGoalActive(missing), false);
  }
});

test("being stoppable and being worked on are different questions", () => {
  // A paused goal is not being worked on, and can still be ended. Conflating the two
  // is what would make the bar claim "esc to stop" over a goal nobody is running, so
  // the two predicates are pinned apart.
  assert.equal(isGoalActive("PAUSED"), false);
  assert.equal(canStopGoal("PAUSED"), true);
  assert.equal(isGoalActive("PENDING"), false);
  assert.equal(canStopGoal("PENDING"), true);
  for (const status of ACTIVE_STATUSES) {
    assert.equal(isGoalActive(status), true, `${status} should read as in flight`);
  }
});

test("only PLANNING and RUNNING count as in flight", () => {
  assert.deepEqual([...ACTIVE_STATUSES], ["PLANNING", "RUNNING"]);
});
