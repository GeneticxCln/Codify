/**
 * The scrollback a reopened terminal tab restores.
 *
 * Three things here are easy to get wrong in ways that are invisible until a
 * user is looking at the result, and all three are pinned: the bound, the two
 * trims that make a *cut* string renderable, and the seam that stops a restored
 * session from reading as a live one.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  appendScrollback,
  appendTerminalHistory,
  clearTerminalHistory,
  MAX_SCROLLBACK_BYTES,
  readTerminalHistory,
  replayFor,
  SEAM,
} from "../src/terminalHistory.ts";

test("a workspace that has never had a terminal has nothing to restore", () => {
  assert.equal(readTerminalHistory("w-empty"), "");
  // And nothing is written for it, so a fresh pane's first prompt is the first
  // thing on screen rather than a seam line with nothing after it.
  assert.equal(replayFor(readTerminalHistory("w-empty")), "");
});

test("what a terminal said is what the next pane restores", () => {
  // The record keeps the whole session, **including** the line with no newline
  // in it: that is the prompt, and a shell has not printed one since. Dropping
  // it here (as this used to) did not tidy the record, it deleted the last
  // thing the session said — from the tail *and* from the pane that restores
  // from it, so a pane that went away and came back showed the build and not
  // the prompt under it.
  const tail = appendScrollback("", "$ ls\r\nREADME.md\r\n$ ");
  assert.equal(tail, "$ ls\r\nREADME.md\r\n$ ");
  // And the half-line is still not *replayed*: a read boundary lands mid-line,
  // and half a command above a new prompt reads as corruption, not a boundary.
  // Trimming at display rather than at record is the whole difference.
  assert.equal(replayFor(tail), SEAM + "$ ls\r\nREADME.md\r\n");
});

test("a record that is only a partial line has nothing to replay", () => {
  // The pane is told to write nothing rather than a seam above nothing, which
  // is the case the pane's `restored` branch exists for.
  assert.equal(replayFor("$ "), "");
  assert.equal(replayFor(""), "");
});

test("the seam is what stops a restored session reading as a live one", () => {
  // Without it, a build that failed twenty minutes ago comes back looking like
  // something the shell just printed.
  const replay = replayFor("$ make\r\nerror: no such file\r\n");
  assert.ok(replay.startsWith(SEAM), "the restored session has no marker");
  assert.match(replay, /earlier session in this workspace/);
  // Dim rather than bold, and a rule on its own line, so it reads as chrome.
  assert.match(SEAM, /\x1b\[2m/);
  assert.ok(SEAM.startsWith("\r\n") && SEAM.endsWith("\r\n"));
});

test("the tail is bounded, and the bound holds across many appends", () => {
  // A long build is thousands of appends, and a bound that only worked on the
  // first one is not a bound. The limit is passed in small here so the
  // arithmetic is visible rather than implied by a 64 KiB number.
  let tail = "";
  for (let i = 0; i < 50; i += 1) {
    tail = appendScrollback(tail, `line ${i} of a very long build log\r\n`, 200);
  }
  assert.ok(tail.length <= 200, `tail grew to ${tail.length}`);
  // What survives is the *end* of the log, not the beginning: nobody scrolls
  // back a megabyte to find the line they wanted, and the start is worthless
  // without the rest.
  assert.match(tail, /line 49/);
  assert.doesNotMatch(tail, /line 0 /);
});

test("a cut that lands inside an emoji does not leave half a character", () => {
  // A JS string is UTF-16 and the limit is applied by slicing, so a cut can land
  // between the two halves of a surrogate pair. Only at the *front*: the kept
  // text is a suffix, and a high surrogate is always followed by its low partner,
  // so the tail's last code unit is a whole character. The stranded one is a low
  // surrogate, which renders as a replacement glyph at the top of every restore
  // after, because the stored tail is the cut one.
  //
  // U+1F600, not U+2705. The checkmark is in the BMP and occupies a single code
  // unit, so slicing it can never produce a surrogate at all — the first version
  // of this test used it, passed, and proved nothing. The mutation that removed
  // the trim entirely also passed it.
  const emoji = "\u{1f600}";
  assert.equal(emoji.length, 2, "this character has to be a surrogate pair to test anything");
  const text = `${emoji} done\r\n`;

  // One byte short: the slice starts on the low half.
  const cut = appendScrollback("", text, text.length - 1);
  assert.equal(
    cut.includes("�"),
    false,
    "a stranded low surrogate renders as a replacement character"
  );
  assert.equal(cut.charCodeAt(0) >= 0xdc00 && cut.charCodeAt(0) <= 0xdfff, false);
  // What is left is the readable half of the line, not a mangled one.
  assert.equal(cut, " done\r\n");

  // Wide enough to keep both halves: the emoji survives whole.
  const whole = appendScrollback("", text, text.length);
  assert.equal(whole, text);
  // A limit below the pair still leaves a *complete* line if the line's
  // newline is inside the bound — the CRLF is a line ending, so the tail is
  // "\r\n" and not nothing. An earlier version of this test asserted "" here,
  // which is wrong, and would have been a second thing testing the wrong thing
  // in a file about testing the right thing.
  assert.equal(appendScrollback("", text, 2), "\r\n");
  // Nothing at all is nothing.
  assert.equal(appendScrollback("", text, 0), "");
});

test("a tail with no newline in it is not a partial line to show", () => {
  // The first chunk a terminal emits can be a bare escape sequence or a
  // carriage return with no content yet. Showing it would put a stray control
  // code at the top of the restored pane — so `replayFor` shows nothing, which
  // is where the question of showing belongs.
  //
  // The record keeps the bytes. That is the half this used to get wrong: a
  // shell's prompt is a line with no newline in it, so trimming at append time
  // meant every session's record stopped one line short and those bytes were
  // gone from every store the app has, not merely unshown.
  assert.equal(appendScrollback("", "\x1b[?2004h", 1000), "\x1b[?2004h");
  assert.equal(replayFor(appendScrollback("", "\x1b[?2004h", 1000)), "");
  assert.equal(appendScrollback("", "no newline here", 1000), "no newline here");
  assert.equal(replayFor(appendScrollback("", "no newline here", 1000)), "");
  // Once a line is complete, it replays — the trim is about the *end* of the
  // record, not about lines in general.
  const mixed = appendScrollback("", "no newline here", 1000);
  assert.equal(replayFor(appendScrollback(mixed, "\r\n", 1000)), SEAM + "no newline here\r\n");
});

test("an empty chunk changes nothing", () => {
  // A PTY read can return bytes the lossy decode turns into nothing, and the
  // reader thread emits it regardless.
  const once = appendScrollback("", "kept\r\n");
  assert.equal(appendScrollback(once, ""), once);
});

// ── the store ─────────────────────────────────────────────────────────────

test("each workspace has its own scrollback", () => {
  // The question a user is asking is "what was I doing *here*", and "here" is
  // the workspace. Two workspaces sharing a tail would put another project's
  // build log above this one's prompt.
  clearTerminalHistory("w-a");
  clearTerminalHistory("w-b");
  appendTerminalHistory("w-a", "in a\r\n");
  appendTerminalHistory("w-b", "in b\r\n");
  assert.equal(readTerminalHistory("w-a"), "in a\r\n");
  assert.equal(readTerminalHistory("w-b"), "in b\r\n");
  clearTerminalHistory("w-a");
  assert.equal(readTerminalHistory("w-a"), "");
  assert.equal(readTerminalHistory("w-b"), "in b\r\n", "clearing one is not clearing all");
  clearTerminalHistory("w-b");
});

test("two terminals in one workspace share its scrollback", () => {
  // Which is the feature rather than a leak: opening a second tab in a
  // workspace shows the first one's session, because that is what "what was I
  // doing here" means.
  clearTerminalHistory("w-shared");
  appendTerminalHistory("w-shared", "first tab\r\n");
  assert.equal(readTerminalHistory("w-shared"), "first tab\r\n");
  appendTerminalHistory("w-shared", "second tab\r\n");
  assert.equal(readTerminalHistory("w-shared"), "first tab\r\nsecond tab\r\n");
  clearTerminalHistory("w-shared");
});

test("the default bound is the documented one", () => {
  // Pinned because it is a judgement call with no test to justify it: if
  // somebody changes it, this is where the change is visible.
  assert.equal(MAX_SCROLLBACK_BYTES, 64 * 1024);
});
