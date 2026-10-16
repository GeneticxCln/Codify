/**
 * Answers about a goal can arrive in a different order than they were asked for. Only the newest read asked for may
 * be written into the transcript.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { createGoalReadOrder } = await import("../src/goalReadOrder.ts");

test("answers that arrive in the order they were asked are all applied", () => {
  const order = createGoalReadOrder();
  const a = order.begin("g1");
  const b = order.begin("g1");
  assert.equal(order.apply("g1", a), true);
  assert.equal(order.apply("g1", b), true);
});

test("an older read answered after a newer one is not applied", () => {
  const order = createGoalReadOrder();
  const poll = order.begin("g1");
  const event = order.begin("g1");
  assert.equal(order.apply("g1", event), true);
  assert.equal(order.apply("g1", poll), false, "the poll's older answer replaced the newer goal");
});

test("a ticket is applied once", () => {
  const order = createGoalReadOrder();
  const t = order.begin("g1");
  assert.equal(order.apply("g1", t), true);
  assert.equal(order.apply("g1", t), false);
});

test("goals are ordered separately", () => {
  const order = createGoalReadOrder();
  const a = order.begin("g1");
  const b = order.begin("g2");
  assert.equal(order.apply("g2", b), true);
  assert.equal(order.apply("g1", a), true);
});
