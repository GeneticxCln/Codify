import test from "node:test";
import assert from "node:assert/strict";

import {
  describeOutcomes,
  failureEmptyMessage,
  hasStageData,
  recoveryLabel,
  roleRateLabel,
  roleRateTone,
  roleRunSummary,
  shareWidth,
  topFailure,
  STAGE_ORDER,
  STAGE_SUCCESS_OUTCOMES,
} from "../src/stageMetrics.ts";
import type { FailureBreakdown, RoleOutcome, StageCost } from "../src/types.ts";

function role(overrides: Partial<RoleOutcome> = {}): RoleOutcome {
  return {
    role: "planner",
    runs: 0,
    succeeded: 0,
    failed: 0,
    cancelled: 0,
    success_rate: null,
    outcomes: {},
    avg_duration_ms: null,
    p95_duration_ms: null,
    tokens: 0,
    ...overrides,
  };
}

function stage(overrides: Partial<StageCost> = {}): StageCost {
  return {
    stage: "planner",
    role: "planner",
    runs: 0,
    tokens: 0,
    calls: 0,
    avg_duration_ms: null,
    p95_duration_ms: null,
    token_share: 0,
    outcomes: {},
    ...overrides,
  };
}

function failures(overrides: Partial<FailureBreakdown> = {}): FailureBreakdown {
  return {
    window_days: 0,
    generated_at: 0,
    total: 0,
    by_code: {},
    by_role: {},
    by_stage: {},
    causes: [],
    retries: 0,
    recovered: 0,
    recovery_rate: null,
    ...overrides,
  };
}

test("a role that never ran reads as no data, not as zero percent", () => {
  assert.equal(roleRateLabel(role()), "—");
  assert.equal(roleRateLabel(undefined), "—");
  assert.equal(
    roleRateLabel(role({ runs: 3, succeeded: 0, failed: 0, cancelled: 3 })),
    "—",
    "every run cancelled means the rate is unknown, and 0% is a claim",
  );
  assert.equal(roleRateTone(role()), "text-gray-500");
  assert.equal(roleRateTone(role({ runs: 1, success_rate: 95 })), "text-green-400");
  assert.equal(roleRateTone(role({ runs: 1, success_rate: 70 })), "text-amber-300");
  assert.equal(roleRateTone(role({ runs: 1, success_rate: 10 })), "text-red-300");
});

test("a run summary names what the rate was computed from", () => {
  assert.equal(roleRunSummary(role()), "never ran in this window");
  assert.equal(
    roleRunSummary(role({ runs: 4, succeeded: 3, failed: 1, success_rate: 75, outcomes: { plan: 4 } })),
    "4 runs · 4 plan",
  );
  assert.match(
    roleRunSummary(role({ runs: 2, cancelled: 1, succeeded: 1, outcomes: { plan: 1 } })),
    /1 cancelled/,
  );
});

test("outcomes are described biggest first, in plain words", () => {
  assert.equal(describeOutcomes({}), "");
  assert.equal(
    describeOutcomes({ plan: 5, consult: 2 }),
    "5 plan · 2 consult",
  );
  assert.equal(describeOutcomes({ request_changes: 1 }), "1 request changes");
  // Zero-count entries are noise: an outcome that never happened is not a fact.
  assert.equal(describeOutcomes({ plan: 0, consult: 1 }), "1 consult");
});

test("a stage that spent something still shows a sliver", () => {
  assert.equal(shareWidth(0), "0%");
  assert.equal(shareWidth(0.4), "2%", "a bar of width 0 is indistinguishable from unrun");
  assert.equal(shareWidth(37), "37%");
});

test("an unmeasured pipeline is distinguished from an empty one", () => {
  assert.equal(hasStageData(undefined), false);
  assert.equal(hasStageData([stage(), stage({ stage: "fixer" })]), false);
  assert.equal(hasStageData([stage(), stage({ stage: "fixer", runs: 1 })]), true);
});

test("every stage has a label and a success set before it has data", () => {
  for (const name of STAGE_ORDER) {
    assert.ok(STAGE_SUCCESS_OUTCOMES[name], `${name} has no success outcomes`);
  }
});

test("a window with no failures says so instead of showing zeros", () => {
  assert.equal(failureEmptyMessage(0, 0), "No failures recorded in this window.");
  assert.equal(
    failureEmptyMessage(0, 3),
    "No failures recorded in this window, across 3 retried steps.",
  );
  assert.equal(
    failureEmptyMessage(0, 1),
    "No failures recorded in this window, across 1 retried step.",
  );
  assert.equal(failureEmptyMessage(2, 0), "", "with failures there is nothing to say");
});

test("no retries is no evidence, not a perfect record", () => {
  assert.match(recoveryLabel(failures()), /No retries/);
  assert.match(recoveryLabel(undefined), /No retries/);
  assert.equal(
    recoveryLabel(failures({ retries: 4, recovered: 3, recovery_rate: 75, total: 9 })),
    "3 of 4 retried steps got past the failure (75%).",
  );
  assert.equal(
    recoveryLabel(failures({ retries: 1, recovered: 0, recovery_rate: 0 })),
    "0 of 1 retried step got past the failure (0%).",
  );
});

test("the headline names the most common cause and how many others there are", () => {
  assert.equal(topFailure(failures()), "");
  assert.equal(topFailure(undefined), "");
  assert.equal(
    topFailure(failures({
      total: 5,
      causes: [
        { code: "tests_failed", count: 3, message: "", last_seen: 1 },
        { code: "provider_unreachable", count: 2, message: "", last_seen: 1 },
      ],
    })),
    "tests_failed × 3 (and 1 other cause)",
  );
  assert.equal(
    topFailure(failures({
      total: 1,
      causes: [{ code: "path_escape", count: 1, message: "", last_seen: 1 }],
    })),
    "path_escape × 1",
  );
});
