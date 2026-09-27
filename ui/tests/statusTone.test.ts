import test from "node:test";
import assert from "node:assert/strict";

import {
  ENGINE_STATE_CLASSES,
  ENGINE_STATE_COPY,
  engineState,
  statusTone,
  stepTone,
  type EngineState,
} from "../src/statusTone.ts";

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

// The engine pill: four words, and no port number. These pin the mapping and the
// words, because both used to be a ternary inside JSX — untestable, and free to
// disagree with the colours two lines below it.
test("the engine pill says Live when it answers, and Offline when it does not", () => {
  assert.equal(engineState(true, true), "live");
  assert.equal(engineState(false, true), "offline");
  assert.equal(ENGINE_STATE_COPY.live.label, "Live");
  assert.equal(ENGINE_STATE_COPY.offline.label, "Offline");
});

test("an engine that is up but refuses our token is neither live nor offline", () => {
  // A 401 is a different problem with a different fix, and collapsing it into
  // "offline" would send the reader to restart something that is running.
  assert.equal(engineState(true, false), "auth-stale");
  assert.equal(ENGINE_STATE_COPY["auth-stale"].label, "Auth stale");
});

test("the first three seconds are Checking, not a claim of connection", () => {
  // The pill used to fall through to the healthy branch while the probe was still
  // retrying, so it showed a live connection nobody had made yet.
  assert.equal(engineState(null, null), "checking");
  assert.equal(ENGINE_STATE_COPY.checking.label, "Checking");
});

test("nothing on the pill is a port number any more", () => {
  // The regression this change exists to prevent: a healthy engine answered with
  // `Port 7430`, which made four digits the headline and the state decoration.
  for (const [state, copy] of Object.entries(ENGINE_STATE_COPY)) {
    assert.doesNotMatch(
      copy.label,
      /\d/,
      `${state} still shows a number: ${copy.label}`,
    );
    assert.doesNotMatch(copy.hint, /\bport\b/i, `${state}'s hint still names a port`);
  }
});

test("every state has a label, a hint and a pair of classes", () => {
  // A missing key would render `undefined` into the class attribute, which fails
  // silently — no error, just a pill with no colour.
  const states: EngineState[] = ["live", "checking", "auth-stale", "offline"];
  for (const state of states) {
    assert.ok(ENGINE_STATE_COPY[state]?.label, `${state} has no label`);
    assert.ok(ENGINE_STATE_COPY[state]?.hint, `${state} has no hint`);
    assert.ok(ENGINE_STATE_CLASSES[state]?.pill, `${state} has no pill classes`);
    assert.ok(ENGINE_STATE_CLASSES[state]?.dot, `${state} has no dot class`);
  }
});

test("the classes are whole literals, because Tailwind scans source text", () => {
  // `bg-${tone}-500` compiles to nothing: the scanner never sees a class name it
  // can find in the file. Every colour must be spelled out.
  for (const [state, classes] of Object.entries(ENGINE_STATE_CLASSES)) {
    for (const value of [classes.pill, classes.dot]) {
      assert.doesNotMatch(value, /\$\{/, `${state} builds a class name at runtime: ${value}`);
    }
  }
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
