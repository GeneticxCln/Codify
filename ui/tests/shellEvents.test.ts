/**
 * The shell-event boundary: what a `browser-popup-requested` payload may be,
 * and what subscribing looks like when there is no shell to subscribe to.
 *
 * The event itself crosses a process boundary and cannot be exercised here —
 * that would need the desktop app running, and the page that asks for a popup
 * needs a display. What *can* be pinned is the part that decides: the payload
 * reader (a malformed payload must open no tab at all) and the no-Tauri
 * posture, which is what the UI's effect depends on to unmount cleanly in a
 * plain browser.
 *
 * The event *name* is pinned on the Rust side instead: a test in
 * `src-tauri/src/browser/` reads this directory's `shellEvents.ts` and fails
 * if the two ends drift, because no type system spans a Rust constant and a
 * TypeScript string.
 *
 * The event this file used to pin — `browser-window-closed` — is gone with
 * the separate window it announced: pages are child webviews of the main
 * window now, a child cannot close itself, and the tab strip is the only
 * closer. The Rust test that reads this file also fails if that name comes
 * back, which is why there is no `BROWSER_WINDOW_CLOSED` export to import
 * here.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  BROWSER_PAGE_FOCUSED,
  BROWSER_POPUP_REQUESTED,
  listenShellEvent,
  MACHINE_EXIT,
  MACHINE_OUTPUT,
  readBrowserPageFocused,
  readMachineExit,
  readMachineOutput,
  readBrowserPopupRequested,
  readTerminalNotification,
  TERMINAL_NOTIFICATION,
} from "../src/shellEvents.ts";

test("the event name is the one the shell emits", () => {
  // Not the interesting assertion — the browser module's test is, which reads this
  // file and fails if the two names differ. Written here so a reader of the UI
  // side sees the wire name rather than having to go looking for it.
  assert.equal(BROWSER_POPUP_REQUESTED, "browser-popup-requested");
});

test("a well-formed payload names the tab that asked and the address it asked for", () => {
  assert.deepEqual(readBrowserPopupRequested({ tab_id: "tab-1", url: "https://a.example" }), {
    tab_id: "tab-1",
    url: "https://a.example",
  });
  // Extra fields are a future Rust struct, not a reason to refuse the payload.
  assert.deepEqual(
    readBrowserPopupRequested({ tab_id: "tab-2", url: "https://b.example", label: "x" }),
    { tab_id: "tab-2", url: "https://b.example" },
  );
});

test("anything that is not that payload opens no tab at all", () => {
  // Each of these reaches a handler that *opens a tab*. A payload that is
  // null, a string, or missing either field must open nothing rather than a
  // tab pointed nowhere.
  for (const payload of [
    null,
    undefined,
    "tab-1",
    42,
    {},
    [],
    { tab_id: "" },
    { tab_id: "tab-1" },
    { tab_id: 7, url: "https://a.example" },
    { tab_id: null, url: "https://a.example" },
    { tab_id: "tab-1", url: "" },
    { tab_id: "tab-1", url: 7 },
    { tab: "tab-1", url: "https://a.example" },
  ]) {
    assert.equal(
      readBrowserPopupRequested(payload),
      null,
      `${JSON.stringify(payload)} must not open anything`,
    );
  }
});

test("a page taking the keyboard names the tab, and only a tab", () => {
  assert.equal(BROWSER_PAGE_FOCUSED, "browser-page-focused");
  assert.equal(readBrowserPageFocused({ tab_id: "tab-1" }), "tab-1");
  // Extra fields are a future Rust struct, not a reason to refuse the payload.
  assert.equal(readBrowserPageFocused({ tab_id: "tab-2", url: "https://a.example" }), "tab-2");
  // Each of these reaches a handler that moves the split's focus: a payload that
  // is not a tab id must move it nowhere.
  for (const payload of [null, undefined, "tab-1", 42, [], {}, { tab_id: "" }, { tab_id: 5 }, { tab_id: null }]) {
    assert.equal(readBrowserPageFocused(payload), null, `${JSON.stringify(payload)} was read as a tab`);
  }
});

test("a machine's output and exit are their own events, and are read as strictly as a terminal's", () => {
  assert.equal(MACHINE_OUTPUT, "machine-output");
  assert.equal(MACHINE_EXIT, "machine-exit");
  assert.deepEqual(readMachineOutput({ id: "mach-1", data: "ls\r\n" }), { id: "mach-1", data: "ls\r\n" });
  // An empty chunk is a real thing the reader thread can emit, and is not refused.
  assert.deepEqual(readMachineOutput({ id: "mach-1", data: "" }), { id: "mach-1", data: "" });
  for (const payload of [null, undefined, "x", 4, [], {}, { id: "", data: "x" }, { id: 1, data: "x" }, { id: "mach-1" }, { id: "mach-1", data: 5 }]) {
    assert.equal(readMachineOutput(payload), null, `${JSON.stringify(payload)} was read as output`);
  }
  assert.equal(readMachineExit({ id: "mach-1" }), "mach-1");
  for (const payload of [null, undefined, "mach-1", 4, [], {}, { id: "" }, { id: 7 }]) {
    assert.equal(readMachineExit(payload), null, `${JSON.stringify(payload)} was read as an exit`);
  }
});

test("a terminal's notification is read as its two halves, and nothing else", () => {
  assert.equal(TERMINAL_NOTIFICATION, "terminal-notification");
  assert.deepEqual(
    readTerminalNotification({ id: "term-3", seq: 7, title: "Build", body: "finished", when: 1 }),
    { id: "term-3", seq: 7, title: "Build", body: "finished" },
  );
  // OSC 9 carries only a body and OSC 99's title half only a title; both are real, and the window
  // decides which one becomes the entry's heading.
  assert.deepEqual(readTerminalNotification({ id: "term-1", seq: 0, title: "", body: "done" }), {
    id: "term-1",
    seq: 0,
    title: "",
    body: "done",
  });
  assert.deepEqual(readTerminalNotification({ id: "term-1", seq: 1, title: "Deploy", body: "" }), {
    id: "term-1",
    seq: 1,
    title: "Deploy",
    body: "",
  });
  // Each of these reaches an inbox entry. A payload with the right shape and the wrong types must
  // make none rather than one that says "undefined" — and `seq` is the entry's identity.
  for (const payload of [
    null,
    undefined,
    "term-1",
    42,
    [],
    {},
    { id: "", seq: 1, title: "x", body: "y" },
    { id: 7, seq: 1, title: "x", body: "y" },
    { id: "term-1", title: "x", body: "y" },
    { id: "term-1", seq: "1", title: "x", body: "y" },
    { id: "term-1", seq: -1, title: "x", body: "y" },
    { id: "term-1", seq: 1.5, title: "x", body: "y" },
    { id: "term-1", seq: 1, body: "y" },
    { id: "term-1", seq: 1, title: "x", body: null },
    { id: "term-1", seq: 1, title: "   ", body: "  " },
  ]) {
    assert.equal(
      readTerminalNotification(payload),
      null,
      `${JSON.stringify(payload)} was read as a notification`,
    );
  }
});

test("subscribing outside the desktop shell is a no-op, not a throw", async () => {
  // The UI also runs in a plain browser (the standalone build), where there is
  // no shell to emit from. The caller holds the result and calls it
  // unconditionally in cleanup, so it has to be a function that does nothing —
  // and calling it twice must not throw either.
  let called = 0;
  const unlisten = await listenShellEvent(BROWSER_POPUP_REQUESTED, () => {
    called += 1;
  });
  assert.equal(typeof unlisten, "function");
  assert.doesNotThrow(() => unlisten());
  assert.doesNotThrow(() => unlisten());
  assert.equal(called, 0, "no shell means no events, and no handler calls");
});
