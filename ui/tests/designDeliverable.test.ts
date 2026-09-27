import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_DESIGN_MD,
  DEFAULT_KNOWLEDGE_MD,
  deliverableHeading,
  deliverablePath,
  deliverableReviewed,
  knowledgeReadiness,
  pinReadiness,
  revises,
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

// ── the knowledge deliverable ─────────────────────────────────────────────
//
// The same union field, a different file, and a different question. A design
// card answers "may I pin this?" with a button; a knowledge card has no button
// and one sentence to say what makes the file real instead. These are the facts
// that sentence is built from, and the mirror of the engine's
// `DELIVERABLE_FILES` that keeps the two files apart.

const knowledgeGoal = (steps: PlanStep[], overrides: Partial<Goal> = {}): Goal => ({
  ...goal(steps),
  title: "Record what this repository is",
  mode: "knowledge",
  ...overrides,
});

const knowledgeStep = (overrides: Partial<PlanStep> = {}): PlanStep =>
  step({
    title: "Write CODIFY.md",
    suggested_paths: ["CODIFY.md"],
    ...overrides,
  });

test("a knowledge goal resolves CODIFY.md, not the design convention", () => {
  const found = deliverablePath(knowledgeGoal([knowledgeStep()]));
  assert.equal(found, "CODIFY.md");
});

test("the knowledge file is matched case-insensitively, as the engine does", () => {
  assert.equal(
    deliverablePath(knowledgeGoal([knowledgeStep({ suggested_paths: ["codify.MD"] })])),
    "codify.MD"
  );
});

test("a knowledge plan naming no knowledge file falls back to the convention", () => {
  // A design.md step in a knowledge goal is a plan the engine will not hand a
  // knowledge body to, so the card must not answer with that file either.
  const found = deliverablePath(
    knowledgeGoal([knowledgeStep({ suggested_paths: ["DESIGN.md"] })])
  );
  assert.equal(found, DEFAULT_KNOWLEDGE_MD);
});

test("a nested CODIFY.md is still the knowledge file", () => {
  assert.equal(
    deliverablePath(knowledgeGoal([knowledgeStep({ suggested_paths: ["docs/CODIFY.md"] })])),
    "docs/CODIFY.md"
  );
});

test("a knowledge goal with no plan yet still resolves", () => {
  // The body is published during planning, before any step exists.
  assert.equal(deliverablePath(knowledgeGoal([])), DEFAULT_KNOWLEDGE_MD);
});

test("a DESIGN.md step does not count as a knowledge review", () => {
  // The step has to name the file the *mode* is allowed to write, or the card
  // would report a knowledge file as reviewed on the strength of a design run.
  assert.equal(
    deliverableReviewed(
      knowledgeGoal([
        knowledgeStep({ suggested_paths: ["DESIGN.md"], status: "COMPLETED" }),
      ])
    ),
    false
  );
  assert.equal(
    deliverableReviewed(
      knowledgeGoal([knowledgeStep({ status: "COMPLETED" })])
    ),
    true
  );
});

test("a normal goal is not holding a knowledge file", () => {
  // The fallback answer is DESIGN.md for a normal goal, so a CODIFY.md step in
  // one must not read as a deliverable either.
  const normal = goalWith([knowledgeStep({ status: "COMPLETED" })], {
    mode: "normal",
  });
  assert.equal(deliverableReviewed(normal), false);
  assert.equal(deliverablePath(normal), DEFAULT_DESIGN_MD);
});

// ── what the knowledge card says, having no button to press ───────────────

test("an unreviewed knowledge draft says what makes the file real", () => {
  const readiness = knowledgeReadiness(knowledgeGoal([]), "CODIFY.md");
  assert.equal(readiness.reviewed, false);
  assert.equal(readiness.bodyLabel, "the draft, not yet reviewed");
  assert.match(readiness.note, /takes effect when a step writes it/);
  assert.match(readiness.note, /prior/);
});

test("a written, reviewed knowledge file says the next run reads it", () => {
  const readiness = knowledgeReadiness(
    knowledgeGoal([knowledgeStep({ status: "COMPLETED" })]),
    "CODIFY.md"
  );
  assert.equal(readiness.reviewed, true);
  assert.equal(readiness.bodyLabel, "the deliverable, as written");
  assert.match(readiness.note, /every later run's librarian now reads CODIFY\.md/);
  assert.doesNotMatch(
    readiness.note,
    /apply/i,
    "an applied run has nothing left to do, and saying so would be advice to re-apply"
  );
});

test("a reviewed dry run is a proposal, and says the next run is unaffected", () => {
  // The knowledge-specific trap: the critic approved a document that was never
  // written, so the next run is still holding whatever an earlier run left. A
  // card that said "the deliverable, as written" here would be claiming a file
  // that does not exist.
  const readiness = knowledgeReadiness(
    knowledgeGoal([knowledgeStep({ status: "COMPLETED" })], { dry_run: true }),
    "CODIFY.md"
  );
  assert.equal(readiness.reviewed, true);
  assert.equal(readiness.bodyLabel, "the proposed draft — nothing written to disk yet");
  assert.match(readiness.note, /apply this goal to write CODIFY\.md/);
  assert.match(readiness.note, /the next run still reads what an earlier run left/);
});

test("the note names the file the plan actually chose", () => {
  const nested = knowledgeGoal([
    knowledgeStep({ suggested_paths: ["docs/CODIFY.md"], status: "COMPLETED" }),
  ]);
  assert.match(
    knowledgeReadiness(nested, "docs/CODIFY.md").note,
    /docs\/CODIFY\.md/
  );
});

test("there is nothing to click, so the three notes must not be one string", () => {
  // With no button to fall back on, the sentence is the only thing on the card
  // saying what state the file is in. Three states that read alike would leave
  // the user unable to tell a draft from a file the next run will read.
  const notes = [
    knowledgeReadiness(knowledgeGoal([]), "CODIFY.md").note,
    knowledgeReadiness(
      knowledgeGoal([knowledgeStep({ status: "COMPLETED" })], { dry_run: true }),
      "CODIFY.md"
    ).note,
    knowledgeReadiness(
      knowledgeGoal([knowledgeStep({ status: "COMPLETED" })]),
      "CODIFY.md"
    ).note,
  ];
  assert.equal(new Set(notes).size, 3);
});

test("a goal with no plan is still described, not thrown on", () => {
  // The card is rendered from an event that arrives during planning, before any
  // step exists, and a transcript message can carry no goal at all.
  const orphan = knowledgeReadiness(undefined, DEFAULT_KNOWLEDGE_MD);
  assert.equal(orphan.reviewed, false);
  assert.match(orphan.note, /CODIFY\.md/);
});

// ── the heading, which is a function of the mode and not of taste ─────────

test("each mode is named for what it produces", () => {
  assert.equal(deliverableHeading("knowledge"), "Knowledge deliverable");
  assert.equal(deliverableHeading("design"), "Design deliverable");
  // A normal goal's contract is an input: it is obeyed, not produced.
  assert.equal(deliverableHeading("normal"), "Design direction");
  assert.equal(deliverableHeading(undefined), "Design direction");
});

// ── what the run is rewriting ─────────────────────────────────────────────
//
// The engine publishes the copy the drafter's prompt quoted, so the transcript
// can show the new body next to the old one. These read that block defensively:
// it is optional (a workspace with no CODIFY.md has nothing to revise), and the
// fields the card leans on — the text, and the list of paths the engine had
// already distrusted — are exactly the ones that must not be guessed at.

const revisesPayload = (over: Record<string, any> = {}) => ({
  revises: {
    path: "CODIFY.md",
    text: "# Old\n\nThe old renderer is `ui/src/legacy/render.js`.\n",
    chars: 45,
    truncated: false,
    stale_paths: ["ui/src/legacy/render.js"],
    ...over,
  },
});

test("the published copy of the file is read as the engine published it", () => {
  const block = revises(revisesPayload());
  assert.equal(block?.path, "CODIFY.md");
  assert.match(block!.text, /old renderer/);
  assert.equal(block?.chars, 45);
  assert.equal(block?.truncated, false);
  assert.deepEqual(block?.stale_paths, ["ui/src/legacy/render.js"]);
});

test("a goal with no file to revise has no block", () => {
  // Nothing on the other side of the comparison: rendering an empty block would
  // be a claim about a file the workspace does not have.
  assert.equal(revises(undefined), undefined);
  assert.equal(revises({}), undefined);
  assert.equal(revises({ design_md: "x" }), undefined);
  assert.equal(revises({ revises: null }), undefined);
  assert.equal(revises(revisesPayload({ text: "" })), undefined);
  assert.equal(revises(revisesPayload({ text: "   \n\t " })), undefined);
});

test("a block missing a field falls back rather than rendering undefined", () => {
  // An older engine, or a partial payload, must not put `undefined` on screen
  // where a file name or a length belongs.
  const bare = revises({ revises: { text: "# Old" } });
  assert.equal(bare?.path, DEFAULT_KNOWLEDGE_MD, "the name falls back to the convention");
  assert.equal(bare?.chars, 5, "the length is countable when the engine did not send it");
  assert.deepEqual(bare?.stale_paths, []);
  assert.equal(bare?.truncated, false);
});

test("a non-array stale list is no list, not a crash", () => {
  const odd = revises(revisesPayload({ stale_paths: "ui/src/legacy/render.js" }));
  assert.deepEqual(odd?.stale_paths, [], "one string is not a list of paths");
  assert.match(odd!.text, /old renderer/, "and the text is still there to read");
});

test("truncation is stated rather than implied", () => {
  // A half-read prior shown in full would read as a complete file, which is the
  // failure docs/04 §4.9.1 exists to prevent.
  const cut = revises(revisesPayload({ truncated: true }));
  assert.equal(cut?.truncated, true);
  assert.equal(revises(revisesPayload({ truncated: "yes" }))?.truncated, false);
});
