/**
 * The shell-event boundary: what a `browser-window-closed` payload may be, and
 * what subscribing looks like when there is no shell to subscribe to.
 *
 * The event itself crosses a process boundary and cannot be exercised here —
 * that would need the desktop app running, and the browser webview that emits
 * it needs a display. What *can* be pinned is the part that decides: the
 * payload reader (a malformed payload must close no tab at all) and the
 * no-Tauri posture, which is what the UI's effect depends on to unmount
 * cleanly in a plain browser.
 *
 * The event *name* is pinned on the Rust side instead: a test in
 * `src-tauri/src/browser.rs` reads this directory's `shellEvents.ts` and fails
 * if the two ends drift, because no type system spans a Rust constant and a
 * TypeScript string.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  BROWSER_WINDOW_CLOSED,
  listenShellEvent,
  readBrowserWindowClosed,
  readTerminalExit,
  readTerminalOutput,
  TERMINAL_EXIT,
  TERMINAL_OUTPUT,
} from "../src/shellEvents.ts";

test("the event name is the one the shell emits", () => {
  // Not the interesting assertion — `browser.rs`'s test is, which reads this
  // file and fails if the two names differ. Written here so a reader of the UI
  // side sees the wire name rather than having to go looking for it.
  assert.equal(BROWSER_WINDOW_CLOSED, "browser-window-closed");
});

test("a well-formed payload yields its tab id", () => {
  assert.equal(readBrowserWindowClosed({ tab_id: "tab-1" }), "tab-1");
  // Extra fields are a future Rust struct, not a reason to refuse the id.
  assert.equal(readBrowserWindowClosed({ tab_id: "tab-2", label: "x" }), "tab-2");
});

test("anything that is not that payload yields nothing to close", () => {
  // Each of these reaches a handler that closes a *tab*. A payload that is
  // null, a string, or missing the field must close nothing at all.
  for (const payload of [
    null,
    undefined,
    "tab-1",
    42,
    {},
    [],
    { tab_id: "" },
    { tab_id: 7 },
    { tab_id: null },
    { tab: "tab-1" },
  ]) {
    assert.equal(
      readBrowserWindowClosed(payload),
      null,
      `${JSON.stringify(payload)} must not name a tab`,
    );
  }
});

test("subscribing outside the desktop shell is a no-op, not a throw", async () => {
  // The UI also runs in a plain browser (the standalone build), where there is
  // no shell to emit from. The caller holds the result and calls it
  // unconditionally in cleanup, so it has to be a function that does nothing —
  // and calling it twice must not throw either.
  let called = 0;
  const unlisten = await listenShellEvent(BROWSER_WINDOW_CLOSED, () => {
    called += 1;
  });
  assert.equal(typeof unlisten, "function");
  assert.doesNotThrow(() => unlisten());
  assert.doesNotThrow(() => unlisten());
  assert.equal(called, 0, "no shell means no events, and no handler calls");
});

// ── terminal streams ──────────────────────────────────────────────────────
//
// One step stricter than `readBrowserWindowClosed`, because the payload it
// guards does not close a tab — it is written straight into a live terminal.
// A shape that passed here would put the word "undefined" on the user's screen.

test("a terminal-output payload is read only when both halves are strings", () => {
  assert.deepEqual(readTerminalOutput({ id: "term-1", data: "hello" }), {
    id: "term-1",
    data: "hello",
  });
  // Escape sequences are data, not noise: a coloured prompt must survive.
  assert.deepEqual(readTerminalOutput({ id: "term-1", data: "\x1b[32m$ \x1b[0m" }), {
    id: "term-1",
    data: "\x1b[32m$ \x1b[0m",
  });
  for (const bad of [
    null,
    undefined,
    "term-1",
    42,
    [],
    {},
    { id: "term-1" },
    { data: "hello" },
    { id: "", data: "hello" },
    { id: 1, data: "hello" },
    { id: "term-1", data: null },
    { id: "term-1", data: 7 },
    { id: "term-1", data: ["hello"] },
  ]) {
    assert.equal(readTerminalOutput(bad), null, `${JSON.stringify(bad)} is not a chunk`);
  }
});

test("an empty chunk is a chunk", () => {
  // A PTY read can return bytes the lossy decode turns into nothing, and the
  // reader thread emits it regardless. Refusing it here would be a second place
  // deciding what a real chunk looks like.
  assert.deepEqual(readTerminalOutput({ id: "term-1", data: "" }), {
    id: "term-1",
    data: "",
  });
});

test("a terminal-exit payload names a terminal or nothing", () => {
  assert.equal(readTerminalExit({ id: "term-1" }), "term-1");
  for (const bad of [null, undefined, "term-1", 3, [], {}, { id: "" }, { id: 9 }]) {
    assert.equal(readTerminalExit(bad), null, `${JSON.stringify(bad)} is not an exit`);
  }
});

test("the event names are the ones terminal.rs emits", () => {
  // The other end of the same wire, and the same reason the browser's is
  // pinned: nothing in either type system spans the two, so a rename in Rust
  // would leave a terminal that silently never receives a byte.
  assert.equal(TERMINAL_OUTPUT, "terminal-output");
  assert.equal(TERMINAL_EXIT, "terminal-exit");
});
