import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_DESIGN_MD,
  deliverablePath,
  deliverableReviewed,
  pinReadiness,
} from "../src/designDeliverable.ts";
import type { Goal, PlanStep } from "../src/types.ts";

function step(overrides: Partial<PlanStep> = {}): PlanStep {
  return {
    id: "s1",
    goal_id: "g1",
    ordinal: 0,
    title: "Write DESIGN.md",
    description: "publish the brand contract",
    suggested_paths: [],
    status: "PENDING",
    ...overrides,
  };
}

function goalWith(steps: PlanStep[], overrides: Partial<Goal> = {}): Goal {
  const base = goal(steps);
  return { ...base, ...overrides };
}

function goal(steps: PlanStep[]): Goal {
  return {
    id: "g1",
    workspace_id: "w1",
    title: "Draft the brand",
    description: "",
    status: "PENDING",
    dry_run: false,
    plan_only: false,
    parallel: false,
    mode: "design",
    version: 0,
    created_at: 0,
    updated_at: 0,
    steps,
  };
}

test("the write step's own path wins over the convention", () => {
  const found = deliverablePath(
    goal([step({ suggested_paths: ["docs/BRAND-DESIGN.md"] })])
  );
  assert.equal(found, "docs/BRAND-DESIGN.md");
});

test("the match is case-insensitive, as it is in the engine", () => {
  // The engine lowercases before matching, so a plan that capitalized the name
  // still gets pinned at the file it named.
  const found = deliverablePath(goal([step({ suggested_paths: ["Design.MD"] })]));
  assert.equal(found, "Design.MD");
});

test("a plan that names no design file falls back to the convention", () => {
  const found = deliverablePath(
    goal([
      step({ suggested_paths: ["src/app.tsx"] }),
      step({ id: "s2", ordinal: 1, suggested_paths: [] }),
    ])
  );
  assert.equal(found, DEFAULT_DESIGN_MD);
});

test("a step with no suggested paths does not throw", () => {
  const bare = goal([step()]);
  bare.steps![0].suggested_paths = undefined as unknown as string[];
  assert.equal(deliverablePath(bare), DEFAULT_DESIGN_MD);
});

test("a goal with no steps at all still resolves", () => {
  // The design card is rendered from an event that arrives during planning,
  // before any step exists — the pin must be clickable then too, and the
  // engine's own validation is what refuses a file that is not there.
  assert.equal(deliverablePath(goal([])), DEFAULT_DESIGN_MD);
  assert.equal(deliverablePath(undefined), DEFAULT_DESIGN_MD);
});

test("the first design path in the plan wins", () => {
  const found = deliverablePath(
    goal([
      step({ id: "s1", ordinal: 0, suggested_paths: ["first/DESIGN.md"] }),
      step({ id: "s2", ordinal: 1, suggested_paths: ["second/DESIGN.md"] }),
    ])
  );
  assert.equal(found, "first/DESIGN.md");
});

test("a draft no step has finished writing is not reviewable", () => {
  // The body is published during planning, long before the write step runs.
  assert.equal(deliverableReviewed(goal([])), false);
  assert.equal(
    deliverableReviewed(goal([step({ suggested_paths: ["DESIGN.md"], status: "PENDING" })])),
    false
  );
  assert.equal(
    deliverableReviewed(goal([step({ suggested_paths: ["DESIGN.md"], status: "IN_PROGRESS" })])),
    false,
    "request-changes leaves the step IN_PROGRESS — a review that did not approve"
  );
  assert.equal(
    deliverableReviewed(goal([step({ suggested_paths: ["DESIGN.md"], status: "FAILED" })])),
    false
  );
});

test("only the completed step that named the file counts as reviewed", () => {
  assert.equal(
    deliverableReviewed(goal([step({ suggested_paths: ["DESIGN.md"], status: "COMPLETED" })])),
    true
  );
  // A completed step that touched no design file proves nothing about the
  // deliverable, however finished it is.
  assert.equal(
    deliverableReviewed(
      goal([step({ suggested_paths: ["src/app.tsx"], status: "COMPLETED" })])
    ),
    false
  );
  // One reviewed after another — the plan's order does not matter, the review does.
  assert.equal(
    deliverableReviewed(
      goal([
        step({ id: "s1", ordinal: 0, suggested_paths: ["DESIGN.md"], status: "PENDING" }),
        step({ id: "s2", ordinal: 1, suggested_paths: ["DESIGN.md"], status: "COMPLETED" }),
      ])
    ),
    true
  );
});

test("a goal with no plan yet is not reviewable", () => {
  assert.equal(deliverableReviewed(undefined), false);
  assert.equal(deliverableReviewed(goalWith([], { steps: undefined })), false);
});

// ── what the card may offer, and why not ─────────────────────────────────

const written = () =>
  goalWith([step({ suggested_paths: ["DESIGN.md"], status: "COMPLETED" })]);

const approvedDryRun = () =>
  goalWith([step({ suggested_paths: ["DESIGN.md"], status: "COMPLETED" })], {
    dry_run: true,
  });

test("a written, reviewed deliverable can be pinned", () => {
  const readiness = pinReadiness(written(), "DESIGN.md");
  assert.equal(readiness.ready, true);
  assert.equal(readiness.reason, "");
  assert.equal(readiness.bodyLabel, "the deliverable, as written");
});

test("a draft the critic has not approved cannot be pinned", () => {
  const readiness = pinReadiness(goal([]), "DESIGN.md");
  assert.equal(readiness.ready, false);
  // The reason names what is being waited on, not that something is missing.
  assert.match(readiness.reason, /waiting on the review/);
  assert.equal(readiness.bodyLabel, "the draft, not yet reviewed");
});

test("a reviewed dry run cannot be pinned either, and says what to do", () => {
  // The case a single "is it reviewed" gate gets wrong: the critic approved a
  // proposal, the disk is untouched, and the engine will refuse the pin.
  const readiness = pinReadiness(approvedDryRun(), "DESIGN.md");
  assert.equal(readiness.ready, false);
  assert.match(readiness.reason, /apply the goal, then pin it/);
  assert.ok(
    readiness.reason.includes("DESIGN.md"),
    "the reason names the file the run did not write"
  );
  assert.equal(readiness.bodyLabel, "the proposed draft — nothing written to disk yet");
});

test("the dry-run reason names the path the plan actually chose", () => {
  const readiness = pinReadiness(approvedDryRun(), "docs/BRAND-DESIGN.md");
  assert.match(readiness.reason, /docs\/BRAND-DESIGN\.md/);
});

test("the two blocked reasons are distinct, not one vague message", () => {
  const unreviewed = pinReadiness(goal([]), "DESIGN.md");
  const dryRun = pinReadiness(approvedDryRun(), "DESIGN.md");
  assert.notEqual(unreviewed.reason, dryRun.reason);
  assert.equal(unreviewed.bodyLabel === dryRun.bodyLabel, false);
});

// ── the pin the workspace already holds ───────────────────────────────────

test("a workspace that already obeys this file is reported as pinned", () => {
  // The card is read long after the click, in a tab that never saw it, or after
  // the pin was set from the workspace picker instead. Offering the pin again
  // there asks the user to do something they have already done.
  const readiness = pinReadiness(written(), "DESIGN.md", "DESIGN.md");
  assert.equal(readiness.pinned, true);
  assert.equal(readiness.ready, false, "there is nothing left to pin");
  assert.equal(readiness.reason, "", "a pin is a state, not a reason");
  assert.equal(readiness.bodyLabel, "the deliverable, as written");
});

test("the pinned path is the same file however it is capitalised", () => {
  // A pin stores the path it was given and the plan capitalises freely, so
  // `design.md` and `DESIGN.md` are one file. Reading that as "not pinned"
  // would offer to pin a contract already in force.
  assert.equal(pinReadiness(written(), "DESIGN.md", "design.md").pinned, true);
  assert.equal(pinReadiness(written(), "Design.MD", "DESIGN.md").pinned, true);
  assert.equal(pinReadiness(written(), "DESIGN.md", "  DESIGN.md  ").pinned, true);
});

test("a different pinned file leaves the pin on offer", () => {
  // The workspace obeys some other contract. This deliverable is still
  // unpinned, and whether it can be is the question this card answers.
  const readiness = pinReadiness(written(), "DESIGN.md", "BRAND.md");
  assert.equal(readiness.pinned, false);
  assert.equal(readiness.ready, true);
});

test("a cleared pin is not a pin", () => {
  // Removing a pin stores the empty string, and the workspace list drops the
  // key entirely. Neither may read as "already pinned".
  assert.equal(pinReadiness(written(), "DESIGN.md", "").pinned, false);
  assert.equal(pinReadiness(written(), "DESIGN.md", "   ").pinned, false);
  assert.equal(pinReadiness(written(), "DESIGN.md", undefined).pinned, false);
});

test("the review gate outranks the pin", () => {
  // "This is the contract" is a claim about the reviewed draft. A goal that has
  // not been reviewed is proposing a revision of a file that happens to be
  // pinned, and crediting it with the pin would be claiming a decision it has
  // not earned.
  const readiness = pinReadiness(goal([]), "DESIGN.md", "DESIGN.md");
  assert.equal(readiness.pinned, false);
  assert.equal(readiness.ready, false);
  assert.match(readiness.reason, /waiting on the review/);
});

test("the dry-run gate outranks the pin too", () => {
  // The file on disk belongs to an earlier run; this one proposed a draft and
  // wrote nothing, so the pin says nothing about what this run is offering.
  const readiness = pinReadiness(approvedDryRun(), "DESIGN.md", "DESIGN.md");
  assert.equal(readiness.pinned, false);
  assert.match(readiness.reason, /apply the goal, then pin it/);
});

test("the ordinary cases report no pin at all", () => {
  assert.equal(pinReadiness(written(), "DESIGN.md").pinned, false);
  assert.equal(pinReadiness(goal([]), "DESIGN.md").pinned, false);
  assert.equal(pinReadiness(approvedDryRun(), "DESIGN.md").pinned, false);
});
