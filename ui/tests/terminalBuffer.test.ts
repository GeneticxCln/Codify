/**
 * The backlog a terminal earns while no pane displays it.
 *
 * One property decides every test here: **each byte is recorded exactly
 * once, by exactly one layer.** The app-level recorder holds what nobody is
 * displaying; a pane that claims its terminal takes that backlog over and is,
 * from that moment, the only writer for what it shows. The tests walk the
 * whole life — background, claimed, released, retired — because the failures
 * that matter are all sequencing failures: an exit before the first chunk, a
 * tab closed with its pane unmounted, a claim that does not stop the
 * recorder.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  appendTerminalHistory,
  readTerminalHistory,
} from "../src/terminalHistory.ts";
import {
  claimTerminal,
  EXIT_MARKER,
  MAX_BACKLOG_BYTES,
  ownsTerminal,
  readBacklog,
  recordExit,
  recordOutput,
  releaseTerminal,
  retireTerminal,
} from "../src/terminalBuffer.ts";

test("a background terminal's output is held and replayed in order", () => {
  recordOutput("t-bg", "building…\r\n");
  recordOutput("t-bg", "done\r\n");
  assert.equal(readBacklog("t-bg"), "building…\r\ndone\r\n");

  // The claim is the handover: the pane takes everything the shell said while
  // it was away, and the store forgets it — replayed twice would be a
  // duplicate, not a restoration.
  const backlogged = claimTerminal("t-bg");
  assert.equal(backlogged, "building…\r\ndone\r\n");
  assert.equal(readBacklog("t-bg"), "");
  assert.ok(ownsTerminal("t-bg"), "a claimed terminal must be owned");
});

test("an owned terminal's output is left to its pane", () => {
  claimTerminal("t-owned");
  const kept = recordOutput("t-owned", "pane is up\r\n");
  assert.ok(!kept, "the recorder must leave an owned terminal's chunk to its pane");
  assert.equal(readBacklog("t-owned"), "");

  // And claiming again on a re-mount is a clean no-op on the store side: the
  // pane never gave the terminal back, so there is nothing new to take.
  assert.equal(claimTerminal("t-owned"), "");
  releaseTerminal("t-owned");
});

test("a released terminal is recorded again", () => {
  claimTerminal("t-cycle");
  releaseTerminal("t-cycle");
  assert.ok(!ownsTerminal("t-cycle"));

  recordOutput("t-cycle", "background again\r\n");
  assert.equal(readBacklog("t-cycle"), "background again\r\n");
});

test("an exit in the background is filed once, with its marker", () => {
  recordOutput("t-exit", "last words\r\n");
  recordExit("t-exit");
  retireTerminal("t-exit", "w-exit");

  assert.equal(readTerminalHistory("w-exit"), "last words\r\n" + EXIT_MARKER);
  assert.equal(readBacklog("t-exit"), "", "a retired terminal must be forgotten");
});

test("an exit that arrives before any output is still recorded", () => {
  // The shell died before saying anything — the record is the marker alone.
  // `recordExit` creates the entry precisely so this ordering cannot lose it.
  recordExit("t-silent");
  assert.ok(readBacklog("t-silent") === "", "no bytes were said");
  retireTerminal("t-silent", "w-silent");
  assert.equal(readTerminalHistory("w-silent"), EXIT_MARKER);
});

test("an owned terminal's exit is its pane's business, not the store's", () => {
  claimTerminal("t-live");
  recordExit("t-live");
  // Nothing is filed and nothing is held: the pane wrote the marker into
  // xterm itself, and a store entry here would sit unclaimed forever.
  assert.equal(readBacklog("t-live"), "");
  retireTerminal("t-live", "w-live");
  assert.equal(readTerminalHistory("w-live"), "");
  releaseTerminal("t-live");
});

test("a tab closed in the background files what the shell said since", () => {
  appendTerminalHistory("w-closed", "earlier session\r\n");
  recordOutput("t-closed", "printed while unmounted\r\n");
  const filed = retireTerminal("t-closed", "w-closed");
  assert.equal(filed, "printed while unmounted\r\n");
  assert.equal(
    readTerminalHistory("w-closed"),
    "earlier session\r\nprinted while unmounted\r\n",
    "the backlog continues the workspace's record, it does not replace it"
  );
});

test("retiring without a workspace has nothing to file into", () => {
  recordOutput("t-nows", "somewhere\r\n");
  const filed = retireTerminal("t-nows");
  assert.equal(filed, "somewhere\r\n");
  // And the store is still clean — the record had its one chance to matter.
  assert.equal(readBacklog("t-nows"), "");
});

test("retiring twice files once", () => {
  recordOutput("t-twice", "once\r\n");
  retireTerminal("t-twice", "w-twice");
  retireTerminal("t-twice", "w-twice");
  assert.equal(readTerminalHistory("w-twice"), "once\r\n");
});

test("a background build is bounded, whole characters, prompts intact", () => {
  // Two properties, both deliberate: the bound is the same 64 KiB the
  // workspace scrollback uses, and there is **no line trim** — a shell's
  // warm-up is a prompt with no newline, and trimming to whole lines would
  // swallow exactly the bytes the returning pane is waiting for.
  assert.equal(MAX_BACKLOG_BYTES, 64 * 1024);

  // The second record forces the bound to cut exactly between the halves of
  // the emoji, which is the cut `trimTail` exists to repair.
  recordOutput("t-bound", "🎉");
  recordOutput("t-bound", "a".repeat(MAX_BACKLOG_BYTES - 1));
  recordOutput("t-bound", "$ ");
  const held = claimTerminal("t-bound");
  assert.ok(held.length <= MAX_BACKLOG_BYTES);
  assert.ok(held.endsWith("$ "), "the prompt survived the bound");
  assert.ok(!held.includes("🎉"), "a cut surrogate is dropped, not replayed");
});
