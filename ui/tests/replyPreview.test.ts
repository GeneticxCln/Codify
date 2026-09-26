import test from "node:test";
import assert from "node:assert/strict";

import { replyPreview } from "../src/replyPreview.ts";

test("an empty reply says so rather than rendering a blank line", () => {
  // A disclosure whose summary is empty is worse than no disclosure: it looks
  // broken, and it is the state the panel sits in while the first token arrives.
  assert.equal(replyPreview(""), "no output yet");
  assert.equal(replyPreview("   \n  "), "no output yet");
  assert.equal(replyPreview(null), "no output yet");
  assert.equal(replyPreview(undefined), "no output yet");
});

test("a JSON reply is summarised by its summary field, not dumped", () => {
  const raw = JSON.stringify({
    summary: "The workspace contains a README.md, a src/app.py, and a test.",
    files: ["README.md", "src/app.py"],
  });
  assert.equal(
    replyPreview(raw),
    "The workspace contains a README.md, a src/app.py, and a test.",
  );
});

test("the summary is found at depth, because roles nest their answers", () => {
  const raw = JSON.stringify({
    plan: { steps: [{ summary: "Add a docstring" }] },
  });
  assert.equal(replyPreview(raw), "Add a docstring");
});

test("a payload with nothing readable falls back to its shape, not to a wall of keys", () => {
  const raw = JSON.stringify({ alpha: 1, beta: 2, gamma: 3, delta: 4, eps: 5 });
  const out = replyPreview(raw);
  assert.match(out, /alpha/);
  // Four keys, not five: a shape summary that is itself a scroll is not a summary.
  assert.ok(!out.includes("eps"), out);
});

test("a truncated stream still produces a readable line", () => {
  // This is the common case, not an edge case: a reply is rendered many times while
  // it is still arriving, and it is invalid JSON every one of those times.
  const partial = '{"summary": "The workspace contains a READ';
  const out = replyPreview(partial);
  assert.ok(out.length > 0);
  assert.ok(!out.startsWith("{"), "a raw brace is not a summary");
  assert.match(out, /The workspace contains a READ/);
});

test("prose keeps its first meaningful line and loses the rest", () => {
  assert.equal(
    replyPreview("\n\nFirst real line.\nsecond line\nthird line"),
    "First real line.",
  );
});

test("a long summary is clamped with an ellipsis, never silently cut", () => {
  const long = "x".repeat(400);
  const out = replyPreview(long);
  assert.equal(out.length, 140);
  assert.ok(out.endsWith("…"));
});

test("a summary at the clamp boundary is not given an ellipsis it does not need", () => {
  const exact = "y".repeat(140);
  assert.equal(replyPreview(exact), exact);
});

test("newlines inside a summary never reach the one-line disclosure", () => {
  const raw = JSON.stringify({ summary: "line one\nline two\nline three" });
  assert.equal(replyPreview(raw), "line one line two line three");
});

test("a non-string preferred key does not win over a usable string", () => {
  const raw = JSON.stringify({ summary: 42, message: "Blocked at the gate" });
  assert.equal(replyPreview(raw), "Blocked at the gate");
});
