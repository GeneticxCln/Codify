import test from "node:test";
import assert from "node:assert/strict";

import { statusTone, stepTone } from "../src/statusTone.ts";

// Six hues for one job, and no way to say what a status *meant* without picking a
// colour. These tests pin the meaning, not the colour: change a hue and nothing here
// fails, which is correct — DESIGN.md §2 owns the hues. Change which meaning maps to
// which tone and everything here does.

test("a goal that finished is a success, wherever it is drawn", () => {
  assert.equal(statusTone("COMPLETED"), "success");
});

test("a failed goal is a danger", () => {
  assert.equal(statusTone("FAILED"), "danger");
});

test("both states of in-flight read as in-flight", () => {
  // PLANNING and RUNNING are the same sentence to a reader watching a goal work, and
  // they were separate hand-typed branches before.
  assert.equal(statusTone("PLANNING"), "info");
  assert.equal(statusTone("RUNNING"), "info");
});

test("paused needs attention but is not wrong", () => {
  assert.equal(statusTone("PAUSED"), "warning");
});

test("a goal the reader cancelled, and one not yet started, are both idle", () => {
  // Cancelling is the reader's own decision. Flagging it in a warning colour would
  // report a problem the user caused on purpose.
  assert.equal(statusTone("CANCELLED"), "neutral");
  assert.equal(statusTone("PENDING"), "neutral");
});

test("an unknown status is idle, never a failure", () => {
  // The engine can add a status this build has never heard of, and a provider that
  // stops answering proves nothing. Dressing an unknown state as a failure would
  // invent a diagnosis — the same rule the rest of the app follows about a failed
  // discovery.
  for (const unknown of ["HIBERNATING", "queued", "RETRYING"]) {
    assert.equal(statusTone(unknown), "neutral", `${unknown} should read as idle`);
  }
  assert.equal(statusTone(null), "neutral");
  assert.equal(statusTone(undefined), "neutral");
  assert.equal(statusTone(""), "neutral");
});

test("a skipped step is a warning, where a cancelled goal is not", () => {
  // Steps and goals are different units: "this step did not happen" is worth
  // noticing, "this run was cancelled" is the reader's own act.
  assert.equal(stepTone("SKIPPED"), "warning");
  assert.equal(statusTone("CANCELLED"), "neutral");
  assert.notEqual(stepTone("SKIPPED"), statusTone("CANCELLED"));
});

test("a step's tones cover the states a step is actually in", () => {
  assert.equal(stepTone("PENDING"), "neutral");
  assert.equal(stepTone("IN_PROGRESS"), "info");
  assert.equal(stepTone("PASSED"), "success");
  assert.equal(stepTone("FAILED"), "danger");
  assert.equal(stepTone(null), "neutral");
});

test("every tone the app can emit is one of the five, so no sixth hue appears", () => {
  const allowed = new Set(["neutral", "info", "success", "warning", "danger"]);
  for (const status of [
    "PENDING", "PLANNING", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED",
  ]) {
    assert.ok(allowed.has(statusTone(status)), `${status} produced an unknown tone`);
    assert.ok(allowed.has(stepTone(status)), `${status} produced an unknown step tone`);
  }
});
