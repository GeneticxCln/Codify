/**
 * The keyboard mapping, as a table.
 *
 * `shortcuts.ts` is pure precisely so this can walk every branch without a
 * DOM: the cases below are the ones a real keypress gets wrong silently —
 * AltGr pretending to be Ctrl, `Ctrl+Shift+T` meaning "reopen" and not "new tab",
 * a `!` keypress that is physically `Digit1`, `Ctrl+9` meaning *last* rather
 * than *ninth*, and the Super key, which belongs to the desktop.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { resolveShortcut, type KeyEventLike } from "../src/shortcuts.ts";

/** A key event with everything off; each case turns on what it is about. */
const key = (over: Partial<KeyEventLike>): KeyEventLike => ({
  key: "",
  code: "",
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  ...over,
});

test("plain typing resolves to nothing", () => {
  for (const e of [
    key({ key: "t", code: "KeyT" }),
    key({ key: "w", code: "KeyW" }),
    key({ key: "k", code: "KeyK" }),
    key({ key: "1", code: "Digit1" }),
    key({ key: "9", code: "Digit9" }),
    key({ key: "Escape", code: "Escape" }),
    key({ key: "ArrowDown", code: "ArrowDown" }),
  ]) {
    assert.equal(resolveShortcut(e), null, `${e.key} alone must not be a shortcut`);
  }
});

test("Ctrl is the modifier", () => {
  assert.deepEqual(
    resolveShortcut(key({ ctrlKey: true, key: "t", code: "KeyT" })),
    { type: "new-tab" },
    "Ctrl+T is new tab"
  );
  assert.deepEqual(
    resolveShortcut(key({ ctrlKey: true, key: "W", code: "KeyW" })),
    { type: "close-active-tab" },
    "Ctrl+W closes the active tab"
  );
  assert.deepEqual(
    resolveShortcut(key({ ctrlKey: true, key: "k", code: "KeyK" })),
    { type: "toggle-palette" },
    "Ctrl+K toggles the palette"
  );
});

test("the Super key is never ours, alone or chorded with Ctrl", () => {
  // `Super` belongs to the desktop (the overview, the launcher, tiling): binding it would steal a
  // key the window manager already owns, so an event that carries it resolves to nothing.
  for (const [k, code] of [["t", "KeyT"], ["w", "KeyW"], ["k", "KeyK"], ["1", "Digit1"], ["9", "Digit9"]] as const) {
    assert.equal(resolveShortcut(key({ metaKey: true, key: k, code })), null, `Super+${k} must not resolve`);
    assert.equal(
      resolveShortcut(key({ metaKey: true, ctrlKey: true, key: k, code })),
      null,
      `Ctrl+Super+${k} must not resolve`
    );
  }
});

test("Alt never fires — AltGr reports Ctrl+Alt and must stay typing", () => {
  // The German/Scandinavian layout shape: AltGr+T arrives as Ctrl+Alt+T.
  assert.equal(
    resolveShortcut(key({ ctrlKey: true, altKey: true, key: "t", code: "KeyT" })),
    null
  );
  assert.equal(
    resolveShortcut(key({ ctrlKey: true, altKey: true, key: "k", code: "KeyK" })),
    null
  );
});

test("letters require no Shift — Ctrl+Shift+T is somebody else's muscle memory", () => {
  assert.equal(
    resolveShortcut(key({ ctrlKey: true, shiftKey: true, key: "T", code: "KeyT" })),
    null
  );
  assert.equal(
    resolveShortcut(key({ ctrlKey: true, shiftKey: true, key: "W", code: "KeyW" })),
    null
  );
});

test("digits jump by strip position, 1-based to 0-based", () => {
  assert.deepEqual(
    resolveShortcut(key({ ctrlKey: true, key: "1", code: "Digit1" })),
    { type: "focus-tab", index: 0 }
  );
  assert.deepEqual(
    resolveShortcut(key({ ctrlKey: true, key: "8", code: "Digit8" })),
    { type: "focus-tab", index: 7 }
  );
});

test("Ctrl+9 is the LAST tab, not the ninth — the strip stays reachable past nine", () => {
  assert.deepEqual(
    resolveShortcut(key({ ctrlKey: true, key: "9", code: "Digit9" })),
    { type: "focus-last-tab" }
  );
});

test("digits are read from code, so a shifted digit key still works", () => {
  // Many layouts reach `1` as Shift+1 and report `key: "!"`.
  assert.deepEqual(
    resolveShortcut(key({ ctrlKey: true, shiftKey: true, key: "!", code: "Digit1" })),
    { type: "focus-tab", index: 0 }
  );
});

test("Digit0 is not a shortcut, and neither are unrelated combos", () => {
  assert.equal(resolveShortcut(key({ ctrlKey: true, key: "0", code: "Digit0" })), null);
  assert.equal(resolveShortcut(key({ ctrlKey: true, key: "p", code: "KeyP" })), null);
  assert.equal(
    resolveShortcut(key({ ctrlKey: true, key: "ArrowLeft", code: "ArrowLeft" })),
    null
  );
  // Shift alone must not promote a digit into a jump.
  assert.equal(resolveShortcut(key({ shiftKey: true, key: "1", code: "Digit1" })), null);
});

test("a held key is one press: new tab, close tab and the palette ignore auto-repeat", () => {
  // Holding Ctrl+T sends `keydown` again at the keyboard's repeat rate. Before this,
  // every repeat opened another tab (a strip full of empty ones), and holding Ctrl+W
  // would have closed every tab in it.
  const held = { repeat: true };
  assert.equal(resolveShortcut(key({ ctrlKey: true, key: "t", code: "KeyT", ...held })), null);
  assert.equal(resolveShortcut(key({ ctrlKey: true, key: "w", code: "KeyW", ...held })), null);
  assert.equal(resolveShortcut(key({ ctrlKey: true, key: "k", code: "KeyK", ...held })), null);
  // The first press of the same chord still works, and so does an explicit false.
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "t", code: "KeyT" })), { type: "new-tab" });
  assert.deepEqual(
    resolveShortcut(key({ ctrlKey: true, key: "w", code: "KeyW", repeat: false })),
    { type: "close-active-tab" },
  );
});

test("focusing a tab by number is harmless to repeat, so it is not suppressed", () => {
  assert.deepEqual(
    resolveShortcut(key({ ctrlKey: true, key: "2", code: "Digit2", repeat: true })),
    { type: "focus-tab", index: 1 },
  );
});
