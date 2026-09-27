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
 * no keystroke is ever sent**. The file below checks the markup and freezes the
 * decisions the harness cannot reach. The PTY itself is Rust's, and its tests
 * live in `src-tauri/src/terminal.rs`.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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
  // The channel is still there for the failures that are real.
  assert.match(
    source,
    /\{failed && \(\s*<div\s*role="alert"/,
    "the pane no longer renders the failures it detects itself"
  );
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

// ── the decisions the render harness cannot reach ────────────────────────

const source = readFileSync(
  new URL("../src/components/TerminalPane.tsx", import.meta.url),
  "utf8"
);

test("xterm is loaded inside the effect, so this file stays importable", () => {
  // The whole reason the dynamic import exists: a module-scope one needs a real
  // DOM and a measured font, and it would put this component out of the reach of
  // `node --test` — so none of the assertions above could run. The CSS import
  // is in `main.tsx` for the same reason, one step further out.
  const [beforeEffect] = source.split("useEffect(");
  assert.ok(beforeEffect, "this file has no effect to import inside");
  assert.doesNotMatch(
    beforeEffect,
    /@xterm/,
    "xterm is imported at module scope — the pane is no longer importable by " +
      "node --test, and the render tests above cannot run"
  );
  assert.match(source, /await Promise\.all\(\[.*@xterm\/xterm.*@xterm\/addon-fit/s);
  // The type annotation on the `let term` line is a *type* position and pulls
  // nothing in at runtime, so it is allowed above the effect. What is not
  // allowed is a value import, which is what `from "@xterm` would mean.
  assert.doesNotMatch(beforeEffect, /from "@xterm/);
});

test("output is subscribed before the renderer exists", () => {
  // The order is the fix. `codify_terminal_open` starts printing the moment it
  // answers, so a listener that goes up after the dynamic import has already
  // missed the first prompt, and every terminal opens blank.
  //
  // Both needles are deliberately specific, because both of their obvious
  // versions are wrong and the test passed anyway:
  //
  // - `TERMINAL_OUTPUT` alone finds the *import* at the top of the file, which is
  //   always before the dynamic import — so it passed against a pane that
  //   subscribed afterwards, and only moving the subscription caught it. The
  //   call site is what is wanted.
  // - `import("@xterm/xterm")` alone finds the *type annotation* on the `let term`
  //   line, which sits above the subscription. The expression position is the
  //   array element, and the comma is what tells the two apart.
  const outputAt = source.indexOf("listenShellEvent<unknown>(TERMINAL_OUTPUT");
  const importAt = source.indexOf('import("@xterm/xterm"),');
  assert.ok(outputAt > 0, "the pane never subscribes to terminal-output");
  assert.ok(importAt > 0, "the pane never dynamically imports xterm");
  assert.ok(
    outputAt < importAt,
    "the output listener is set up after xterm is imported — the first prompt " +
      "is lost and the terminal opens blank"
  );
});

test("what was buffered before xterm existed is flushed into it", () => {
  // The two halves of the race fix, in order: subscribe early, then drain what
  // the early subscription caught. Either alone is broken — subscribing late
  // loses the prompt, and buffering without a flush loses it just as surely,
  // with a test suite that is still green because `drainOutput` is itself
  // perfectly correct and nobody called it.
  const openAt = source.indexOf("term.open(hostRef.current)");
  const flushAt = source.indexOf("drainOutput(buffer, terminalIdRef.current)");
  assert.ok(openAt > 0, "the pane never opens the terminal");
  assert.ok(flushAt > 0, "the pane never flushes what it buffered");
  assert.ok(
    openAt < flushAt,
    "the buffered output is drained before the terminal exists to receive it"
  );
  const [, afterFlush] = source.split("drainOutput(buffer, terminalIdRef.current)");
  assert.match(
    afterFlush.slice(0, 200),
    /term\.write\(held\)/,
    "the buffer is drained and thrown away — the shell's first prompt is " +
      "discarded and the terminal opens blank"
  );
});

test("a restored session is written before the live one, and recorded as it arrives", () => {
  // The order is the feature. The restored bytes are older than this shell's,
  // so writing the live prompt *above* them would read as the new shell quoting
  // the old one. And the append has to be on the output path rather than at
  // close, or a terminal that was never closed — the common case — files
  // nothing.
  const replayAt = source.indexOf("replayFor(");
  const drainAt = source.indexOf("drainOutput(buffer, terminalIdRef.current)");
  assert.ok(replayAt > 0, "the pane never restores a previous session");
  assert.ok(replayAt < drainAt, "the live prompt is written above the restored one");
  assert.match(source, /readTerminalHistory\(workspaceIdRef\.current\)/);

  const start = source.indexOf("listenShellEvent<unknown>(TERMINAL_OUTPUT");
  const body = source.slice(start, source.indexOf("TERMINAL_EXIT", start));
  assert.match(
    body,
    /appendTerminalHistory\(/,
    "output is not filed into the workspace's scrollback, so a reopened pane " +
      "has nothing to restore"
  );
  // And filed before the pane check, so a chunk belonging to a tab the user has
  // since closed still counts as something this workspace said.
  assert.ok(
    body.indexOf("appendTerminalHistory(") < body.indexOf("if (chunk.id !== terminalIdRef.current)"),
    "only this tab's own output is filed; another terminal's in the same " +
      "workspace is part of the same answer"
  );
});

test("a terminal tab replaces the transcript too, and App says so", () => {
  // The browser's own freeze covers `{activeBrowserTab ?`; nothing covered the
  // terminal's branch, so dropping it would have left a terminal tab showing a
  // chat transcript with no way to type into it — and the pane test above would
  // still be green, because it renders the pane directly.
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(app, /<TerminalPane/, "App.tsx never renders TerminalPane");
  assert.match(
    app,
    /activeTerminalTab \? \(\s*<TerminalPane/,
    "App.tsx renders no terminal branch — a terminal tab would show a chat " +
      "transcript under no terminal at all"
  );
  // And it has to be reached from the same conditional the browser's is, or one
  // of the two kinds of tab silently falls through to the chat column.
  assert.match(app, /activeBrowserTab \?([\s\S]{0,2000}?)activeTerminalTab \?/);
  // The tab is named by the shell, so the pane is handed the tab's own id.
  assert.match(app, /terminalId=\{activeTerminalTab\.id\}/);
});

test("a resize is sent to the terminal the pane is drawing, not the last one opened", () => {
  // The pane measures itself and is the only party that knows which PTY it
  // belongs to. `App.handleTerminalResize` used to read the id out of
  // `pendingTerminal`, which held the *most recently opened* terminal — so with
  // two shells up, dragging the window resized the other one, and a terminal
  // restored from scrollback was never resized at all.
  assert.match(
    source,
    /onResizeRef\.current\?\.\(terminalIdRef\.current, grid\)/,
    "the pane no longer sends its own id with the grid it measured"
  );
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const body = app
    .split("const handleTerminalResize = useCallback")[1]
    ?.split("[],")[0];
  assert.ok(
    body,
    "App.tsx no longer defines handleTerminalResize in the expected shape"
  );
  assert.doesNotMatch(
    body,
    /pendingTerminal/,
    "handleTerminalResize still reads the last-opened terminal instead of the " +
      "id it was handed"
  );
  assert.match(
    body,
    /resizeTerminal\(terminalId, grid\.cols, grid\.rows\)/,
    "the resize is not addressed to the terminal the pane named"
  );
});

test("a shell that will not start is reported where a user will see it", () => {
  // A terminal that never opened has no pane, so there is nothing local to
  // report into. The app-wide banner is the surface that is always on screen.
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const body = app
    .split("const handleOpenTerminal = useCallback")[1]
    ?.split("[selectedWs]")[0];
  assert.ok(
    body,
    "App.tsx no longer defines handleOpenTerminal in the expected shape"
  );
  assert.match(
    body,
    /catch \(err: any\) \{[\s\S]{0,200}?setError\(readRejection\(err, "Could not start a shell"\)\)/,
    "a refused shell start is no longer reported to the app banner, or is " +
      "reported through something that cannot read a Rust `Err(String)`"
  );
  assert.doesNotMatch(
    body,
    /pendingTerminal[^;]*error/,
    "handleOpenTerminal still files its failure into pending state; that error " +
      "was never rendered anywhere"
  );
});

test("the output path writes to the terminal and never to React state", () => {
  // A PTY emits faster than a frame. A chunk that called `setState` would
  // re-render this pane and everything above it, once per chunk, so `ls` in a
  // large directory would take the app with it.
  //
  // Scoped to the output handler's own body, because `setFailed` is legitimate
  // twice elsewhere — a failed keystroke write, and xterm failing to import.
  // Checking the whole file for it was the first version of this test and it
  // could not have passed; checking for `setFailed(chunk…)` specifically could
  // be defeated by renaming the variable, which is not a real bar.
  const start = source.indexOf("listenShellEvent<unknown>(TERMINAL_OUTPUT");
  assert.ok(start > 0, "the pane never subscribes to terminal-output");
  const body = source.slice(start, source.indexOf("TERMINAL_EXIT", start));
  assert.match(
    body,
    /\.write\(/,
    "the output handler never writes to the terminal"
  );
  assert.doesNotMatch(
    body,
    /setFailed|useState/,
    "the output handler is reaching React state — a PTY emits faster than a " +
      "frame, so every chunk would re-render the pane and everything above it"
  );
});
