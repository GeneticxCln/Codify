/**
 * Pasting a clip into a terminal: the rule, and the registry that lets the drawer reach a pane.
 *
 * The rule is the part that matters. A terminal that is not in bracketed-paste mode treats every newline in what
 * it is handed as Enter, so pasting "rm -r build\nmake" runs both before the person has read either. The history
 * can hold text from anywhere (a page, a model's answer), so a multi-line clip is only pasted when the shell has
 * said it will hold it back, and a refusal says why.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { pasteInto, registerTerminalPaste, pasteIntoTerminal, PASTE_MESSAGES } = await import("../src/terminalPaste.ts");

function surface(bracketed: boolean) {
  const seen = { pasted: [] as string[], focused: 0 };
  return {
    seen,
    paste: (text: string) => void seen.pasted.push(text),
    focus: () => void (seen.focused += 1),
    modes: { bracketedPasteMode: bracketed },
  };
}

test("one line goes in as it is, whether or not the shell holds pastes back", () => {
  for (const bracketed of [false, true]) {
    const term = surface(bracketed);
    assert.equal(pasteInto(term, "git status -sb", false), "pasted");
    assert.deepEqual(term.seen.pasted, ["git status -sb"]);
    assert.equal(term.seen.focused, 1, "the terminal was left without focus, so the next key went elsewhere");
  }
});

test("several lines are refused while the shell would run each one, and pasted once it will not", () => {
  const text = "cd build\nmake -j4";
  const plain = surface(false);
  assert.equal(pasteInto(plain, text, false), "needs-bracketed-paste");
  assert.deepEqual(plain.seen.pasted, [], "a multi-line paste reached a shell that would have run it");
  assert.equal(plain.seen.focused, 0);

  const guarded = surface(true);
  assert.equal(pasteInto(guarded, text, false), "pasted");
  assert.deepEqual(guarded.seen.pasted, [text], "the text was changed on the way in");
});

test("a trailing newline counts: it is the Enter that runs the command", () => {
  for (const text of ["npm test\n", "npm test\r", "npm test\r\n", "\nnpm test"]) {
    const term = surface(false);
    assert.equal(pasteInto(term, text, false), "needs-bracketed-paste", JSON.stringify(text));
    assert.deepEqual(term.seen.pasted, []);
  }
});

test("a shell that has exited takes nothing, however safe the text", () => {
  const term = surface(true);
  assert.equal(pasteInto(term, "ls", true), "exited");
  assert.deepEqual(term.seen.pasted, []);
});

test("every refusal has a sentence, and the one for several lines says what would have happened", () => {
  assert.match(PASTE_MESSAGES["needs-bracketed-paste"], /each line would run/i);
  assert.match(PASTE_MESSAGES.exited, /exited/i);
  assert.match(PASTE_MESSAGES["no-terminal"], /terminal tab/i);
});

test("the registry reaches the pane registered under that terminal, and no other", () => {
  const a: string[] = [];
  const b: string[] = [];
  const offA = registerTerminalPaste("paste-a", (t) => (a.push(t), "pasted"));
  const offB = registerTerminalPaste("paste-b", (t) => (b.push(t), "pasted"));
  try {
    assert.equal(pasteIntoTerminal("paste-a", "one"), "pasted");
    assert.equal(pasteIntoTerminal("paste-b", "two"), "pasted");
    assert.deepEqual([a, b], [["one"], ["two"]]);
    assert.equal(pasteIntoTerminal("paste-nobody", "x"), "no-terminal");
  } finally {
    offA();
    offB();
  }
});

test("a pane that has gone is no longer reachable, and its outcome is passed through", () => {
  const off = registerTerminalPaste("paste-c", () => "needs-bracketed-paste");
  assert.equal(pasteIntoTerminal("paste-c", "a\nb"), "needs-bracketed-paste");
  off();
  assert.equal(pasteIntoTerminal("paste-c", "ls"), "no-terminal");
});

test("a pane that replaced another under the same id is not unregistered by the old one leaving", () => {
  // React mounts the new pane before the old one's cleanup runs when a tab is re-keyed.
  const calls: string[] = [];
  const offOld = registerTerminalPaste("paste-d", () => (calls.push("old"), "pasted"));
  const offNew = registerTerminalPaste("paste-d", () => (calls.push("new"), "pasted"));
  try {
    offOld();
    assert.equal(pasteIntoTerminal("paste-d", "ls"), "pasted");
    assert.deepEqual(calls, ["new"]);
  } finally {
    offNew();
  }
});
