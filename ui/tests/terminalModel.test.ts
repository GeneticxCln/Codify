/**
 * The terminal's non-rendering decisions: what size a PTY may be told it is,
 * when to say so, and what to do with output that arrives before the renderer
 * exists.
 *
 * Every one of these is invisible in markup and produces the same symptom when
 * it is wrong — a terminal that opens blank, or one that stutters when a window
 * is dragged — which is exactly the class of bug that gets "fixed" by nudging a
 * sleep somewhere else. `terminalPane.test.ts` covers the pane's markup;
 * nothing else would catch any of this.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  bufferOutput,
  bufferOwnOutput,
  DEFAULT_GRID,
  drainOutput,
  emptyOutputBuffer,
  gridChanged,
  pendingFor,
  usableGrid,
  type OutputBuffer,
} from "../src/terminalModel.ts";

test("the size a PTY is opened at is a guess the pane corrects", () => {
  // 80x24 is what a shell assumes before it has been told otherwise. It has to
  // be a constant because the pane measures itself *after* it exists, and the
  // pane is what tells the PTY the truth a moment later.
  assert.deepEqual(DEFAULT_GRID, { cols: 80, rows: 24 });
});

test("a real grid passes through unchanged", () => {
  assert.deepEqual(usableGrid(120, 40), { cols: 120, rows: 40 });
});

test("a pane that has no cells produces no grid at all", () => {
  // The failure this replaces: a hidden or not-yet-laid-out pane reports zero,
  // and forwarding that hands the PTY a zero-cell screen — the shell redraws
  // its entire layout into it and the user gets a column of wrapped text until
  // the next real measurement. Holding the last good size costs nothing.
  assert.equal(usableGrid(0, 0), null);
  assert.equal(usableGrid(0, 24), null);
  assert.equal(usableGrid(80, 0), null);
  assert.equal(usableGrid(-1, 24), null);
});

test("counts that are not numbers produce no grid", () => {
  // `Terminal.cols` is typed `number` in xterm but this is a value that crossed
  // into a command parameter, and the whole point of the check is that it is
  // not trusted. `NaN` is the shape a division by an unmeasured cell takes.
  assert.equal(usableGrid(NaN, 24), null);
  assert.equal(usableGrid(80, Infinity), null);
  assert.equal(usableGrid("80", 24), null);
  assert.equal(usableGrid(80, null), null);
  assert.equal(usableGrid(undefined, undefined), null);
});

test("a fractional count is floored, not rounded", () => {
  assert.deepEqual(usableGrid(80.9, 24.2), { cols: 80, rows: 24 });
});

test("a grid beyond a u16 is capped, because the command's parameter is one", () => {
  // `codify_terminal_resize` takes `u16`. A grid past that is not a value
  // anyone can send, and clamping here is where it stops being one.
  assert.deepEqual(usableGrid(1e9, 1e9), { cols: 0xffff, rows: 0xffff });
});

test("a resize is only worth sending when the grid actually moved", () => {
  // A window drag fires a resize observer per frame, and each one is an IPC
  // round trip to a process that re-wraps its scrollback.
  const grid = { cols: 80, rows: 24 };
  assert.equal(gridChanged(grid, { cols: 80, rows: 24 }), false);
  assert.equal(gridChanged(grid, { cols: 81, rows: 24 }), true);
  assert.equal(gridChanged(grid, { cols: 80, rows: 25 }), true);
  // And a pane that has no grid yet must be told, because the last size it was
  // given is the default rather than anything measured.
  assert.equal(gridChanged(null, grid), true);
  assert.equal(gridChanged(grid, null), true);
  assert.equal(gridChanged(null, null), false);
});

// ── the output buffer ─────────────────────────────────────────────────────

const buffered = (...chunks: [string, string][]): OutputBuffer =>
  chunks.reduce((b, [id, data]) => bufferOutput(b, id, data), emptyOutputBuffer());

test("output that arrives before the renderer exists is kept, in order", () => {
  // The race this exists for: the shell starts printing the moment
  // `codify_terminal_open` returns, and xterm is a dynamic import away. Drop
  // this and every terminal opens blank, which reads as broken rather than slow.
  const buffer = buffered(
    ["term-1", "$ "],
    ["term-1", "hello\r\n"],
    ["term-1", "$ "]
  );
  assert.equal(pendingFor(buffer, "term-1"), 3);
  assert.equal(drainOutput(buffer, "term-1"), "$ hello\r\n$ ");
});

test("a drained buffer is empty, so a flush cannot replay it", () => {
  // xterm holds its own write queue, so a second write of the same bytes is not
  // a no-op — it is the prompt appearing twice.
  const buffer = buffered(["term-1", "once"]);
  assert.equal(drainOutput(buffer, "term-1"), "once");
  assert.equal(pendingFor(buffer, "term-1"), 0);
  assert.equal(drainOutput(buffer, "term-1"), "");
});

test("one terminal's output is never handed to another", () => {
  // Two terminal tabs are two PTYs, and a prompt from one landing in the other
  // is the sort of thing a user reports as the app being haunted.
  const buffer = buffered(
    ["term-1", "first"],
    ["term-2", "second"]
  );
  assert.equal(drainOutput(buffer, "term-2"), "second");
  assert.equal(pendingFor(buffer, "term-1"), 1, "the other terminal is untouched");
  assert.equal(drainOutput(buffer, "term-1"), "first");
});

test("a terminal that has said nothing drains as nothing", () => {
  // So a caller can `write(drain(id, buffer))` unconditionally — an empty write
  // is a no-op, and a branch here would only be a place to forget one.
  assert.equal(drainOutput(emptyOutputBuffer(), "term-9"), "");
  assert.equal(pendingFor(emptyOutputBuffer(), "term-9"), 0);
});

test("an empty chunk is buffered rather than treated as nothing to say", () => {
  // A PTY read can return bytes that the lossy decode turns into nothing, and
  // the reader thread emits it regardless. Dropping it here would be a second
  // place deciding what a real chunk looks like.
  const buffer = buffered(["term-1", "a"], ["term-1", ""], ["term-1", "b"]);
  assert.equal(drainOutput(buffer, "term-1"), "ab");
});

// ── a pane holds its own terminal's output and nobody else's ────────────────

test("a pane holds its own terminal's chunks, and says so", () => {
  const buffer = emptyOutputBuffer();
  assert.equal(bufferOwnOutput(buffer, "term-1", "term-1", "a"), true);
  assert.equal(bufferOwnOutput(buffer, "term-1", "term-1", "b"), true);
  assert.equal(drainOutput(buffer, "term-1"), "ab");
});

test("another terminal's chunks are not held, so a pane cannot hoard what it will never show", () => {
  // A build running in a second shell writes for as long as the first pane is mounted. Nothing ever drains it there.
  const buffer = emptyOutputBuffer();
  for (let n = 0; n < 1000; n += 1) {
    assert.equal(bufferOwnOutput(buffer, "term-1", "term-2", "x".repeat(100)), false);
  }
  assert.equal(buffer.chunks.size, 0, "a chunk for another terminal was kept");
  assert.equal(pendingFor(buffer, "term-2"), 0);
  assert.equal(drainOutput(buffer, "term-2"), "");
});

test("with two panes each keeps only its own", () => {
  const a = emptyOutputBuffer();
  const b = emptyOutputBuffer();
  for (const [id, data] of [["term-a", "1"], ["term-b", "2"], ["term-a", "3"], ["term-b", "4"]] as const) {
    bufferOwnOutput(a, "term-a", id, data);
    bufferOwnOutput(b, "term-b", id, data);
  }
  assert.equal(drainOutput(a, "term-a"), "13");
  assert.equal(drainOutput(b, "term-b"), "24");
  assert.equal(a.chunks.size + b.chunks.size, 0, "a pane was left holding the other's output");
});
