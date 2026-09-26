import test from "node:test";
import assert from "node:assert/strict";

import {
  foldStages,
  stageStates,
  callLabel,
  type AssignmentEvent,
} from "../src/pipeline.ts";

const assigned = (
  role: string,
  extra: Record<string, unknown> = {},
  stepId?: string
): AssignmentEvent => ({
  type: "agent_assigned",
  step_id: stepId ?? null,
  payload: { role, provider: "ollama", model: "qwen2.5-coder:7b", ...extra },
});

test("four dispatches of one role fold into one stage, not four", () => {
  // The bug this exists for: a planner that consults the librarian four times emitted
  // four identical lines, which read as four different agents.
  const events = [
    assigned("librarian"),
    assigned("planner"),
    assigned("planner"),
    assigned("planner"),
    assigned("planner"),
  ];
  const stages = foldStages(events);
  assert.equal(stages.length, 2);
  assert.equal(stages[0].role, "librarian");
  assert.equal(stages[0].calls, 1);
  assert.equal(stages[1].role, "planner");
  assert.equal(stages[1].calls, 4);
});

test("stages come back in first-dispatch order, which is the pipeline's own order", () => {
  const stages = foldStages([
    assigned("laya"),
    assigned("librarian"),
    assigned("planner"),
    assigned("fixer"),
  ]);
  assert.deepEqual(
    stages.map((s) => s.role),
    ["laya", "librarian", "planner", "fixer"]
  );
});

test("non-assignment events are ignored, so a caller can pass the whole log", () => {
  const events: AssignmentEvent[] = [
    { type: "step_started", payload: { role: "not-a-dispatch" } },
    assigned("librarian"),
    { type: "error", payload: { role: "neither-is-this" } },
  ];
  const stages = foldStages(events);
  assert.equal(stages.length, 1);
  assert.equal(stages[0].role, "librarian");
});

test("no role list is hardcoded: a role the UI has never heard of still folds", () => {
  // engine/models.py owns ROLES. If this test ever needs editing because a role was
  // added, the duplication has leaked back in.
  const stages = foldStages([assigned("some_future_role"), assigned("some_future_role")]);
  assert.equal(stages.length, 1);
  assert.equal(stages[0].role, "some_future_role");
  assert.equal(stages[0].calls, 2);
});

test("an event with no role is skipped rather than rendered as a blank stage", () => {
  const stages = foldStages([
    { type: "agent_assigned", payload: {} },
    { type: "agent_assigned", payload: { role: "   " } },
    { type: "agent_assigned" },
    assigned("fixer"),
  ]);
  assert.equal(stages.length, 1);
  assert.equal(stages[0].role, "fixer");
});

test("a fallback's target wins, because that is the model that actually ran", () => {
  const stages = foldStages([
    assigned("design", { provider: "openai", model: "gpt-x" }),
    assigned("design", { provider: "ollama", model: "qwen2.5-coder:7b" }),
  ]);
  assert.equal(stages[0].provider, "ollama");
  assert.equal(stages[0].model, "qwen2.5-coder:7b");
});

test("a step the role worked on is listed once, however many times it was dispatched", () => {
  const stages = foldStages(
    [assigned("fixer", {}, "s1"), assigned("fixer", {}, "s1"), assigned("fixer", {}, "s2")],
    (id) => (id === "s1" ? "Write the docstring" : "Update the README")
  );
  assert.equal(stages[0].calls, 3);
  assert.deepEqual(stages[0].steps, ["Write the docstring", "Update the README"]);
});

test("an unresolvable step id is left off the list rather than shown as a blank", () => {
  const stages = foldStages([assigned("fixer", {}, "gone")], () => undefined);
  assert.deepEqual(stages[0].steps, []);
});

test("an empty log produces an empty spine, not a placeholder row", () => {
  assert.deepEqual(foldStages([]), []);
});

test("a finished goal marks every observed stage done", () => {
  const stages = foldStages([assigned("librarian"), assigned("fixer")]);
  assert.deepEqual(stageStates(stages, null), [
    { role: "librarian", state: "done" },
    { role: "fixer", state: "done" },
  ]);
});

test("while the goal is live, only the most recently dispatched role is active", () => {
  const stages = foldStages([assigned("librarian"), assigned("fixer")]);
  assert.deepEqual(stageStates(stages, "fixer"), [
    { role: "librarian", state: "done" },
    { role: "fixer", state: "active" },
  ]);
});

test("a role dispatched again after another becomes active again", () => {
  // The planner consulting the librarian is the common case: a stage can re-enter.
  const stages = foldStages([
    assigned("librarian"),
    assigned("planner"),
    assigned("librarian"),
  ]);
  assert.deepEqual(stageStates(stages, "librarian"), [
    { role: "librarian", state: "active" },
    { role: "planner", state: "done" },
  ]);
});

test("an active role nobody has dispatched is not invented as a stage", () => {
  const stages = foldStages([assigned("librarian")]);
  const states = stageStates(stages, "verifier");
  assert.equal(states.length, 1);
  assert.equal(states[0].state, "done");
});

test("call count is pluralised, because '1 calls' is a typo the eye catches", () => {
  assert.equal(callLabel(1), "1 call");
  assert.equal(callLabel(2), "2 calls");
  assert.equal(callLabel(0), "0 calls");
});
