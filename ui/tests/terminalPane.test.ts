/**
 * What `TerminalPane` puts on screen — which is not much, and that is the point.
 *
 * A terminal is drawn by xterm into a `<div>`, so this component's own markup is
 * the container, the error lines, and what it says when the shell has finished.
 * The model behind it is tested in `terminalModel.test.ts` and the payload
 * readers in `shellEvents.test.ts`; what is here is that a terminal tab's state
 * reaches the DOM, and that the thing a user has to be told — *this shell has
 * exited* — is actually told.
 *
 * Static rendering only, and that limit is sharper here than anywhere else in
 * the UI: `renderToStaticMarkup` runs no effects, so **none of xterm loads and
 * no keystroke is ever sent**. This file checks the markup; `terminalWiring.test.ts`
 * mounts the App and runs the pane for real. The PTY itself is Rust's, and its
 * tests live in `src-tauri/src/terminal.rs`.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { TerminalPane } = await import("../src/components/TerminalPane.tsx");

/** Markup with tags stripped, so an assertion is about words, not nesting. */
const text = (markup: string): string =>
  markup
    .replace(/<[^>]+>/g, " ")
    .replace(/&rsquo;/g, "'")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ")
    .trim();

const render = (over: Record<string, unknown> = {}): string =>
  renderToStaticMarkup(
    React.createElement(TerminalPane, {
      terminalId: "term-1",
      ...over,
    } as any)
  );

const JUNK = ["undefined", "NaN", "[object Object]"];

test("a starting terminal is an empty container, not a void with words in it", () => {
  // xterm owns everything inside the host div. A placeholder string here would
  // be painted over by the renderer on its first frame and would sit in the
  // accessibility tree until then.
  const markup = render();
  assert.ok(markup.length > 0);
  for (const junk of JUNK) {
    assert.ok(!markup.includes(junk), `a fresh pane rendered ${junk}`);
  }
  assert.match(markup, /aria-label="Terminal"/);
  assert.match(markup, /data-terminal-id="term-1"/);
  assert.doesNotMatch(markup, /role="alert"/);
  assert.doesNotMatch(markup, /role="status"/);
});

test("an exited shell says so, and keeps its scrollback", () => {
  // The user pressed Ctrl-D, or a command ended the shell. Throwing the tab away
  // would throw away the scrollback of what just happened, which is the one
  // thing they wanted to read.
  const shown = text(render({ exited: true }));
  assert.match(shown, /This shell has exited/);
  assert.match(shown, /scrollback is still here/);
  // And the container is still there — a banner is not a replacement for it.
  assert.match(render({ exited: true }), /data-terminal-id="term-1"/);
});

test("a terminal pane has no error prop, because nothing could fill it", () => {
  // There *was* an `error?: string | null` here, and the pane rendered it above
  // its scrollback. No caller ever passed it: `pendingTerminal.error` was only
  // read when `pendingTerminal.ptyId` matched the active tab, and a *failed*
  // open leaves that id null — so a shell that would not start was reported to
  // state nothing reads. The user clicked Terminal and got silence.
  //
  // The prop is gone rather than left as a second, permanently-empty channel.
  // A pane cannot have failed before it exists, so the two errors that *can*
  // reach it — a refused write and a renderer that would not import — are both
  // its own `failed` state, and the open failure goes to the app banner. The
  // render below is the half of that which is checkable statically: a healthy
  // pane is not showing an alert it has nothing to say.
  assert.doesNotMatch(
    render(),
    /role="alert"/,
    "a fresh terminal pane is showing an alert with nothing wrong"
  );
  // The channel is still there for the failures that are real: a refused write is
  // shown in the mounted pane, in `terminalWiring.test.ts`.
});

test("two terminal tabs render two panes that can be told apart", () => {
  assert.match(render({ terminalId: "term-1" }), /data-terminal-id="term-1"/);
  assert.match(render({ terminalId: "term-2" }), /data-terminal-id="term-2"/);
});

test("rendering a pane produces no React warning of any kind", () => {
  const warnings: string[] = [];
  const original = console.error;
  console.error = (...args: unknown[]): void => {
    warnings.push(args.map(String).join(" "));
  };
  try {
    render();
    render({ exited: true });
    render({ error: "refused" });
  } finally {
    console.error = original;
  }
  assert.deepEqual(warnings, [], `React complained:\n  ${warnings.join("\n  ")}`);
});

// What the App and the pane do together — the race with xterm's load, claim and
// release, the badge, resize addressing — is checked by mounting the App in
// `terminalWiring.test.ts`.
