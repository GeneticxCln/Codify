/**
 * The suggestions inbox in the window (docs/09 §10.22): the rules that decide what a person is offered and when it
 * can be sent. That nothing here starts a turn by itself is held by `suggestionsApp.test.ts`, which mounts the App.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { acceptState, bellCount, bellLabel, idsOf, kindLabel, POLL_MS, SNOOZE_HOURS } from "../src/suggestions.ts";

test("every kind the engine can send has its own words, and an unknown one is still shown", () => {
  assert.equal(kindLabel("failing_command"), "Failing command");
  assert.equal(kindLabel("paused_goal"), "Paused goal");
  assert.equal(kindLabel("stale_health_review"), "Health review");
  assert.equal(kindLabel("red_ci"), "CI is red");
  assert.equal(kindLabel("something_from_a_newer_engine"), "Suggestion");
});

test("an offer can be sent only into the project it is about", () => {
  const offer = { workspace_id: "w1" };
  assert.deepEqual(acceptState(offer, "w1", false), { allowed: true, reason: null });
  assert.equal(acceptState(offer, "w2", false).allowed, false);
  assert.match(acceptState(offer, "w2", false).reason ?? "", /different project/);
  assert.equal(acceptState(offer, undefined, false).allowed, false);
  assert.match(acceptState(offer, undefined, false).reason ?? "", /Select a project/);
});

test("while one is being sent its button waits, so it cannot be sent twice", () => {
  const state = acceptState({ workspace_id: "w1" }, "w1", true);
  assert.equal(state.allowed, false);
  assert.match(state.reason ?? "", /Sending/);
});

test("the bell adds the inbox to what is unread, and says what the number is made of", () => {
  assert.equal(bellCount(0, 0), 0);
  assert.equal(bellCount(2, 3), 5);
  assert.equal(bellCount(-1, 2), 2, "a negative count is not subtracted from the other");
  assert.equal(bellLabel(0, 0), "Notifications");
  assert.equal(bellLabel(2, 0), "Notifications, 2 unread", "unchanged where there is no inbox");
  assert.equal(bellLabel(0, 1), "Notifications, 1 suggestion");
  assert.equal(bellLabel(2, 3), "Notifications, 2 unread, 3 suggestions");
});

test("the constants are the ones the docs promise", () => {
  assert.equal(SNOOZE_HOURS, 24);
  assert.equal(POLL_MS, 60_000);
  assert.deepEqual([...idsOf([{ id: "a" }, { id: "b" }, { id: "a" }])].sort(), ["a", "b"]);
});
