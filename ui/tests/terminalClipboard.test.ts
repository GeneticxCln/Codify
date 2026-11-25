import test from "node:test";
import assert from "node:assert/strict";

import {
  attachTerminalClipboardKeys,
  type TerminalClipboardSurface,
} from "../src/terminalClipboard.ts";

function fakeKeyEvent(over: Partial<KeyboardEvent> = {}): KeyboardEvent & { prevented: boolean } {
  const ev = {
    type: "keydown",
    key: "",
    code: "",
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    metaKey: false,
    prevented: false,
    preventDefault() {
      ev.prevented = true;
    },
    ...over,
  };
  return ev as unknown as KeyboardEvent & { prevented: boolean };
}

function makeSurface(selection = ""): {
  surface: TerminalClipboardSurface;
  pasted: string[];
  fire: (ev: KeyboardEvent) => boolean;
} {
  let handler: ((ev: KeyboardEvent) => boolean) | undefined;
  const pasted: string[] = [];
  const surface: TerminalClipboardSurface = {
    attachCustomKeyEventHandler(fn) {
      handler = fn;
    },
    hasSelection: () => selection.length > 0,
    getSelection: () => selection,
    paste(text) {
      pasted.push(text);
    },
  };
  return {
    surface,
    pasted,
    fire(ev) {
      assert.ok(handler, "custom key event handler was not attached");
      return handler(ev);
    },
  };
}

test("Ctrl+Shift+C copies the terminal selection and never sends SIGINT to xterm", async () => {
  const copied: string[] = [];
  const { surface, fire } = makeSurface("npm run build");
  attachTerminalClipboardKeys(surface, {
    copyTarget: {
      writeText: async (text) => {
        copied.push(text);
      },
    },
  });

  const ev = fakeKeyEvent({ key: "C", code: "KeyC", ctrlKey: true, shiftKey: true });
  const passToXterm = fire(ev);
  await Promise.resolve();

  assert.equal(passToXterm, false, "Ctrl+Shift+C must not pass through as SIGINT");
  assert.equal(ev.prevented, true);
  assert.deepEqual(copied, ["npm run build"]);
});

test("Ctrl+Shift+C with no selection still does not send SIGINT", async () => {
  const copied: string[] = [];
  const { surface, fire } = makeSurface("");
  attachTerminalClipboardKeys(surface, {
    copyTarget: {
      writeText: async (text) => {
        copied.push(text);
      },
    },
  });

  const ev = fakeKeyEvent({ key: "C", code: "KeyC", ctrlKey: true, shiftKey: true });
  assert.equal(fire(ev), false);
  await Promise.resolve();
  assert.deepEqual(copied, []);
});

test("Ctrl+Shift+V pastes sanitised clipboard text into xterm and suppresses ^V", async () => {
  const { surface, pasted, fire } = makeSurface();
  attachTerminalClipboardKeys(surface, {
    readText: async () => "echo \x1b[201~safe\x07",
  });

  const ev = fakeKeyEvent({ key: "V", code: "KeyV", ctrlKey: true, shiftKey: true });
  const passToXterm = fire(ev);
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(passToXterm, false, "Ctrl+Shift+V must not pass through as ^V");
  assert.equal(ev.prevented, true);
  assert.deepEqual(pasted, ["echo safe"]);
});

test("Ctrl+Shift+V does not paste into an exited terminal or machine", async () => {
  const { surface, pasted, fire } = makeSurface();
  let exited = true;
  attachTerminalClipboardKeys(surface, {
    isExited: () => exited,
    readText: async () => "should not paste",
  });

  const ev = fakeKeyEvent({ key: "V", code: "KeyV", ctrlKey: true, shiftKey: true });
  assert.equal(fire(ev), false);
  await Promise.resolve();
  assert.deepEqual(pasted, []);

  // Once revived (e.g. after Machine reset), Ctrl+Shift+V pastes again.
  exited = false;
  assert.equal(fire(fakeKeyEvent({ key: "V", code: "KeyV", ctrlKey: true, shiftKey: true })), false);
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(pasted, ["should not paste"]);
});

test("plain Ctrl+C, Ctrl+V, keyup, and Alt/Meta chords pass through untouched to xterm", () => {
  const { surface, fire } = makeSurface("selected");
  attachTerminalClipboardKeys(surface, {
    copyTarget: { writeText: async () => assert.fail("must not copy on plain Ctrl+C") },
    readText: async () => assert.fail("must not paste on plain Ctrl+V"),
  });

  assert.equal(fire(fakeKeyEvent({ key: "c", code: "KeyC", ctrlKey: true })), true);
  assert.equal(fire(fakeKeyEvent({ key: "v", code: "KeyV", ctrlKey: true })), true);
  assert.equal(fire(fakeKeyEvent({ type: "keyup", key: "C", code: "KeyC", ctrlKey: true, shiftKey: true })), true);
  assert.equal(fire(fakeKeyEvent({ key: "C", code: "KeyC", ctrlKey: true, shiftKey: true, altKey: true })), true);
  assert.equal(fire(fakeKeyEvent({ key: "V", code: "KeyV", ctrlKey: true, shiftKey: true, metaKey: true })), true);
});
