import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { canArmTrace } from "../src/traceSummary.ts";

// `canArmTrace` was written, given a rule, given a test — and never called by
// anything. `setGoalTrace` in `api.ts` was the same: a client for
// `PUT /goals/{id}/trace`, which the engine implements and refuses past the start
// of a run with `trace_locked`. Between them, a capability the engine fully
// supports had no path to the user: the only way to record a run was the command
// bar's toggle, applied when the goal was created.
//
// That matters because arming is a decision people make late. You write the
// prompt, the plan comes back, and *then* you decide you want the receipt. The
// window the engine leaves open is exactly that window — the goal is PLANNING —
// and nothing in the UI offered it.
//
// The rule itself is already tested in traceSummary.test.ts, so what is asserted
// here is only the wiring: that the control exists, that it is offered exactly
// when the engine would accept it, and that it is not offered on a run that has
// already started (where it would earn a `trace_locked` refusal).

const HERE = dirname(fileURLToPath(import.meta.url));
const COMPONENTS = join(HERE, "..", "src", "components");

test("the arming rule is still the one the engine enforces", () => {
  // Guard the wiring assertions below against a rule change that leaves them
  // testing nothing: a goal that has started must never be armable.
  assert.equal(canArmTrace("PLANNING"), true);
  for (const status of [
    "PENDING",
    "RUNNING",
    "PAUSED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
  ]) {
    assert.equal(
      canArmTrace(status),
      false,
      `${status} must not be armable — the engine refuses with trace_locked`,
    );
  }
});

test("the recording panel can arm and disarm a run", () => {
  const panel = readFileSync(join(COMPONENTS, "TracePanel.tsx"), "utf8");
  assert.match(
    panel,
    /onSetTrace/,
    "TracePanel must expose a way to change a recording's armed state",
  );
  // Both directions: arming is the missing half, disarming is the one that has to
  // stay available after the run begins.
  assert.match(panel, /handleToggleRecording\(true\)/);
  assert.match(panel, /handleToggleRecording\(false\)/);
  assert.match(
    panel,
    /canArmTrace\(goal\.status\)/,
    "arming must be offered from the engine's own rule, not a second guess at it",
  );
});

test("the control is reachable on a goal that has not started", () => {
  // The button is what makes the panel openable, and the panel is the only place
  // the control lives — so a button gated on "already recording" hides the
  // feature from exactly the goals it is for.
  const timeline = readFileSync(join(COMPONENTS, "ChatTimeline.tsx"), "utf8");
  assert.match(
    timeline,
    /msg\.goal\?\.trace\s*\|\|\s*\n?\s*canArmTrace\(/,
    "the recording control must appear for a goal that can still be armed",
  );
  assert.match(timeline, /onSetGoalTrace/);
});

test("a finished unrecorded run still shows no recording control", () => {
  // The reason the control is not on every card: an empty panel on every goal
  // teaches people to ignore it. This is the other half of the same rule, and
  // the fix above must not have cost it.
  const timeline = readFileSync(join(COMPONENTS, "ChatTimeline.tsx"), "utf8");
  const gated = timeline.slice(
    timeline.indexOf("canArmTrace(msg.goal?.status"),
    timeline.indexOf("canArmTrace(msg.goal?.status") + 400,
  );
  assert.match(gated, /traceFor !== msg\.goal!\.id/);
});

test("the engine's refusal is surfaced rather than swallowed", () => {
  // `setGoalTrace` throws `ApiRequestError` with `trace_locked` intact. A control
  // that caught it and did nothing would be a lie about what happened.
  const app = readFileSync(join(HERE, "..", "src", "App.tsx"), "utf8");
  const handler = app.slice(
    app.indexOf("const handleSetGoalTrace"),
    app.indexOf("const handleSetGoalTrace") + 600,
  );
  assert.match(handler, /await setGoalTrace\(goalId, enabled\)/);
  assert.doesNotMatch(
    handler,
    /catch\s*\(\)\s*\{\s*\}/,
    "a refused arming must not be swallowed",
  );
});
