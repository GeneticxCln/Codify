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

import { resolveShortcut, SHORTCUT_HELP, type KeyEventLike } from "../src/shortcuts.ts";

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

test("unrelated combos are not shortcuts", () => {
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

test("Ctrl+B hides and shows the left panel, and is not repeated by a held key", () => {
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "b", code: "KeyB" })), { type: "toggle-sidebar" });
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "B", code: "KeyB" })), { type: "toggle-sidebar" });
  assert.equal(resolveShortcut(key({ ctrlKey: true, key: "b", code: "KeyB", repeat: true })), null, "held Ctrl+B flickered the panel");
  // Letters take no Shift (Ctrl+Shift+B is somebody else's bookmarks bar), no Alt, no Super.
  assert.equal(resolveShortcut(key({ ctrlKey: true, shiftKey: true, key: "B", code: "KeyB" })), null);
  assert.equal(resolveShortcut(key({ ctrlKey: true, altKey: true, key: "b", code: "KeyB" })), null);
  assert.equal(resolveShortcut(key({ ctrlKey: true, metaKey: true, key: "b", code: "KeyB" })), null);
  assert.equal(resolveShortcut(key({ key: "b", code: "KeyB" })), null, "typing a b is not a shortcut");
});

test("Ctrl + / Ctrl - / Ctrl 0 scale the window, the way a browser's zoom does", () => {
  const up = { type: "scale-up" };
  const down = { type: "scale-down" };
  const reset = { type: "scale-reset" };
  // Bigger: `=` is where `+` lives without Shift on most layouts, and Ctrl++ (with Shift) must work too.
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "=", code: "Equal" })), up);
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, shiftKey: true, key: "+", code: "Equal" })), up);
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "+", code: "NumpadAdd" })), up);
  // `+` is its own key on some layouts (German), so `key` counts as much as `code`.
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "+", code: "BracketRight" })), up);
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "-", code: "Minus" })), down);
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "-", code: "NumpadSubtract" })), down);
  // Reset is read from the physical key, so AZERTY's shifted `0` still resets.
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "0", code: "Digit0" })), reset);
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "à", code: "Digit0" })), reset);
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "0", code: "Numpad0" })), reset);
});

test("scaling needs Ctrl alone, and typing = - 0 is never a shortcut", () => {
  for (const e of [
    key({ key: "=", code: "Equal" }),
    key({ key: "-", code: "Minus" }),
    key({ key: "0", code: "Digit0" }),
    key({ ctrlKey: true, altKey: true, key: "-", code: "Minus" }),
    key({ ctrlKey: true, altKey: true, key: "0", code: "Digit0" }),
    key({ ctrlKey: true, metaKey: true, key: "=", code: "Equal" }),
  ]) {
    assert.equal(resolveShortcut(e), null, `${e.key} / ${e.code} must not scale the window`);
  }
});

test("a held scale key is one step, not a run to the end of the scale", () => {
  assert.equal(resolveShortcut(key({ ctrlKey: true, key: "=", code: "Equal", repeat: true })), null);
  assert.equal(resolveShortcut(key({ ctrlKey: true, key: "-", code: "Minus", repeat: true })), null);
  assert.equal(resolveShortcut(key({ ctrlKey: true, key: "0", code: "Digit0", repeat: true })), null);
  assert.deepEqual(resolveShortcut(key({ ctrlKey: true, key: "=", code: "Equal", repeat: false })), { type: "scale-up" });
});

test("the shortcut list the About tab shows is what the keyboard layer really does", () => {
  // Each line carries a real key event, and it must resolve to the action the line claims. A binding
  // that changes, or a key the list names wrongly, fails here instead of misleading a reader.
  for (const line of SHORTCUT_HELP) {
    assert.equal(
      resolveShortcut(line.probe)?.type,
      line.action,
      `${line.keys} (${line.does}) does not resolve to ${line.action}`,
    );
  }
  // And nothing the layer can do goes unlisted: every action type has a line. This is the pin that
  // makes adding a shortcut without documenting it a failure.
  assert.deepEqual(
    [...new Set(SHORTCUT_HELP.map((line) => line.action))].sort(),
    [
      "close-active-tab", "focus-last-tab", "focus-tab", "new-tab", "scale-down", "scale-reset",
      "scale-up", "toggle-palette", "toggle-sidebar",
    ],
  );
  assert.equal(new Set(SHORTCUT_HELP.map((line) => line.keys)).size, SHORTCUT_HELP.length, "a chord is listed twice");
});
