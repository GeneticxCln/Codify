/**
 * The gate card's checkpoint field, without a renderer.
 *
 * The engine refuses a non-Hub value with a 422; the field should say so before the round
 * trip. The rules are the engine's own (`engine/laya.py` `checkpoint_problem`), mirrored here
 * so the two cannot drift in the one direction a user would notice.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { CHECKPOINT_MAX, checkpointProblem } from "../src/layaCheckpoint.ts";

test("empty means the built-in Router", () => {
  assert.equal(checkpointProblem(""), null);
  assert.equal(checkpointProblem("   "), null);
});

test("an owner/name Hugging Face repo id is accepted", () => {
  for (const value of [
    "me/laya-codify",
    "convaiinnovations/laya",
    "some-org/laya_codify.v2",
    "  me/laya-codify  ",
  ]) {
    assert.equal(checkpointProblem(value), null, value);
  }
});

test("what is not a Hub repo id is refused", () => {
  for (const value of [
    "laya-codify",
    "https://huggingface.co/me/laya-codify",
    "me/laya-codify/extra",
    "../laya",
    "me\\laya",
    "me/laya codify",
    "/me/laya",
    "me/",
  ]) {
    assert.ok(checkpointProblem(value), `accepted ${JSON.stringify(value)}`);
  }
});

test("the length cap is the engine's", () => {
  assert.equal(CHECKPOINT_MAX, 200);
  const long = `${"a".repeat(100)}/${"b".repeat(100)}`;
  assert.equal(long.length, 201);
  assert.match(checkpointProblem(long) ?? "", /200/);
});
