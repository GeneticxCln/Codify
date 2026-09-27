import test from "node:test";
import assert from "node:assert/strict";
import { readErrorBody } from "../src/errorBody.ts";

// How the client reads what the engine sends.
//
// `engine/models.py`'s `ErrorBody` is the declared owner of the refusal shape —
// `{code, message}` plus whatever the refusal attaches — and `tests/test_error_contract.py`
// holds the engine to it. This file holds the other end. The two are written
// against each other, which is the only thing that makes a schema useful: a
// declaration nothing reads is as empty as no declaration.
//
// It was not testable before, which is why it drifted. The reading lived inside
// `api.ts`, whose module scope reads `localStorage`, so no test could import it.
// `readErrorBody` is that part, extracted into `./errorBody` — pure, and
// importable here for the same reason `stageMetrics` and `traceSummary` are.
//
// The bug this shape of reader caused: it understood `detail` only as a *string*.
// FastAPI's default body for a rejected request is `{"detail": [...]}`, an
// array, so a validation failure matched nothing and reached the user as a bare
// "HTTP 422" — the one failure shape with no explanation at all.

test("the declared shape is read as declared", () => {
  const read = readErrorBody({
    code: "workspace_not_empty",
    message: "workspace has 3 goals",
  });
  assert.equal(read.code, "workspace_not_empty");
  assert.equal(read.message, "workspace has 3 goals");
});

test("the extras a refusal attaches are kept, not stringified away", () => {
  // The delete routes carry the goal count so a confirm dialog can name the
  // cascade. Losing it is the difference between "cannot delete" and "cannot
  // delete: 3 goals, and the events with them".
  const read = readErrorBody({
    code: "workspace_not_empty",
    message: "workspace has 3 goals",
    goals: 3,
    events: 41,
  });
  assert.deepEqual(read.extra, { goals: 3, events: 41 });
  assert.equal(read.extra.goals, 3);
});

test("code and message are not confused with the extras", () => {
  // A field that happens to be named `detail` is an extra, never the message.
  const read = readErrorBody({
    code: "invalid_request",
    message: "1 field(s) rejected by validation: body.root_path",
    detail: [{ loc: ["body", "root_path"], msg: "field required" }],
  });
  assert.equal(read.code, "invalid_request");
  assert.equal(read.message, "1 field(s) rejected by validation: body.root_path");
  // The field errors survive for a caller that wants them, without being
  // mistaken for the sentence the user reads.
  assert.deepEqual(read.extra.detail, [
    { loc: ["body", "root_path"], msg: "field required" },
  ]);
});

test("a string `detail` is still read, for an engine that sends one", () => {
  // Backwards compatibility, not aspiration: an older engine answers a rejected
  // body with `{"detail": "..."}`. The client should keep understanding it
  // rather than regress to "HTTP 422" on a machine mid-upgrade.
  const read = readErrorBody({ detail: "root_path is required" });
  assert.equal(read.message, "root_path is required");
  assert.equal(read.code, "");
});

test("a body that is not the declared shape reads as empty, not as a crash", () => {
  // The caller falls back to the HTTP status, so a failure is still reported.
  for (const body of [null, undefined, "HTTP 500", 42, [], true]) {
    const read = readErrorBody(body);
    assert.equal(read.code, "", `${JSON.stringify(body)} produced a code`);
    assert.equal(read.message, "", `${JSON.stringify(body)} produced a message`);
    assert.deepEqual(read.extra, {});
  }
});

test("a non-string code is not passed off as one", () => {
  // A body shaped like the contract but wrong inside it is not the contract.
  // Reading `code: 42` as "42" would put a number in front of the message where
  // a caller expects a code to switch on.
  const read = readErrorBody({ code: 42, message: "something went wrong" });
  assert.equal(read.code, "");
  assert.equal(read.message, "something went wrong");
});

test("an empty object is a shape, just an unhelpful one", () => {
  const read = readErrorBody({});
  assert.equal(read.code, "");
  assert.equal(read.message, "");
  assert.deepEqual(read.extra, {});
});
