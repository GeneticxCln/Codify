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
  const flushAt = source.indexOf("drainOutput(outputBuffer, terminalIdRef.current)");
  assert.ok(openAt > 0, "the pane never opens the terminal");
  assert.ok(flushAt > 0, "the pane never flushes what it buffered");
  assert.ok(
    openAt < flushAt,
    "the buffered output is drained before the terminal exists to receive it"
  );
  const [, afterFlush] = source.split("drainOutput(outputBuffer, terminalIdRef.current)");
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
  const drainAt = source.indexOf("drainOutput(outputBuffer, terminalIdRef.current)");
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
  // …and filed **after** the pane check, because the app-level recorder
  // (`terminalBuffer.ts`) is what owns a chunk no pane is displaying. Filing
  // another terminal's bytes here as well is how they come to be on screen
  // twice: the sibling pane that happens to be mounted files this terminal's
  // background build into the same workspace record the backlog replays from,
  // so every line of that build is written to xterm a second time when its pane
  // comes back. The other half of the promise — a tab closed in the background
  // is still part of what this workspace restores — is the store's, and
  // `terminalBuffer.test.ts` pins it.
  const guardAt = body.indexOf("if (chunk.id !== terminalIdRef.current)");
  const fileAt = body.indexOf("appendTerminalHistory(");
  assert.ok(guardAt > 0, "the pane never checks whose chunk this is");
  assert.ok(
    fileAt > guardAt,
    "a pane files chunks that are not its own terminal's, and the store " +
      "files them too — so a background build is written to the workspace " +
      "scrollback twice and a returning pane shows every line of it twice"
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

test("a pane claims its terminal on mount and hands it back on unmount", () => {
  // The background-output guarantee is a relay, not a duplication: the store
  // records only what nobody displays, so the pane must take ownership when it
  // arrives (taking the backlog earned in its absence) and release it when it
  // goes. Drop the claim and the recorder buffers a live tab's output forever;
  // drop the release and a backgrounded tab's output is never caught again.
  const claimAt = source.indexOf("claimTerminal(terminalIdRef.current)");
  assert.ok(claimAt > 0, "the pane never claims its terminal from the store");
  // Claimed in the effect body — before the async half subscribes — so no
  // chunk can slip between mounting and owning.
  const asyncAt = source.indexOf("void (async () => {");
  assert.ok(asyncAt > claimAt, "the claim happens after the subscription went up");
  const cleanupAt = source.indexOf("return () => {", claimAt);
  const cleanup = source.slice(cleanupAt, source.indexOf("}, []);", cleanupAt));
  assert.match(
    cleanup,
    /releaseTerminal\(terminalIdRef\.current\)/,
    "the pane never hands the terminal back — background output after this " +
      "unmount is dropped on the floor"
  );
  // The backlog earned while the pane was away is replayed into xterm, not
  // just taken and dropped — and behind the resume seam, because the tail it
  // continues was already on screen before the pane went away.
  assert.match(
    source,
    /term\.write\(\(restored \? RESUME_SEAM : ""\) \+ backlogged\)/,
    "the claimed backlog is never written, or written without the resume " +
      "seam — switching tabs ate the output the recorder was built to keep, " +
      "or replayed it as if the shell had never had a gap"
  );
});

test("the pane is keyed by terminal id, so two shells are two terminals", () => {
  // Without a key, React reuses one pane — one xterm, one subscription, one
  // claim — across different PTYs when the user switches tabs: the second
  // shell's prompt lands in the first shell's scrollback and neither claim
  // nor release ever runs for the terminal that was switched away from.
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const branch = app.slice(
    app.indexOf("activeTerminalTab ? ("),
    app.indexOf(") : (", app.indexOf("activeTerminalTab ? ("))
  );
  assert.match(branch, /<TerminalPane/, "the terminal branch no longer renders the pane");
  assert.match(
    branch,
    /key=\{activeTerminalTab\.id\}/,
    "the terminal pane has no key, so switching terminal tabs reuses one " +
      "xterm across different shells"
  );
});

test("App records what no pane is there to catch", () => {
  // The app-level recorder is the other half of the relay: one subscription
  // at app scope, keyed to the app's mount, feeding the store. The needles
  // name the store's functions because that is the contract — a recorder that
  // calls anything else is a second filer, and two filers is how bytes land
  // on screen twice.
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(app, /recordOutput\(/, "no app-level recorder: background output is still dropped");
  assert.match(app, /recordExit\(/, "background exits are never recorded");
  assert.match(
    app,
    /retireTerminal\(ptyId, ws\)/,
    "an unowned shell that finishes is never filed into its workspace"
  );
  assert.match(
    app,
    /ownsTerminal\(ptyId\)/,
    "the recorder does not ask who owns the terminal before filing"
  );
  // And closing a background tab files what its shell said since, before the
  // PTY is closed — the pane that would have claimed it will never exist.
  const closeBody = app
    .split("const handleCloseTab = useCallback")[1]
    ?.split("[tabState.tabs],")[0];
  assert.ok(closeBody, "App.tsx no longer defines handleCloseTab in the expected shape");
  assert.match(
    closeBody,
    /retireTerminal\(id, tab\.workspaceId\)/,
    "a background tab closed with its shell still talking loses everything it " +
      "said while unmounted"
  );
});

test("the badge is driven by kept chunks, never by the active tab's own output", () => {
  // `recordOutput`'s boolean is the whole event source: true means the store
  // kept the chunk, which means no pane displayed it. The active-tab guard is
  // still needed because of the claim window — chunks that arrive between a
  // pane mounting and its claim landing are kept by the store even though the
  // user is looking at that very tab — and badging the tab the user is
  // reading would make the badge lie about attention.
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const handlerAt = app.indexOf("const kept = recordOutput(chunk.id, chunk.data)");
  assert.ok(handlerAt > 0, "the recorder ignores whether the store kept the chunk");
  const handlerBody = app.slice(handlerAt, handlerAt + 800);
  assert.match(
    handlerBody,
    /if \(active === chunk\.id\) return;/,
    "the badge can land on the tab the user is reading — the claim window's " +
      "kept chunks are not unread"
  );
  assert.match(
    handlerBody,
    /setUnreadTerminalIds/,
    "kept chunks never reach the badge state"
  );
});

test("the badge clears when the tab is shown, and goes with it when closed", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const focusBody = app
    .split("onFocus={(id) => {")[1]
    ?.split("unreadTerminalIds={[")[0];
  assert.ok(focusBody, "App.tsx no longer defines the tab-strip onFocus inline");
  assert.match(
    focusBody,
    /setUnreadTerminalIds[\s\S]{0,200}?next\.delete\(id\)/,
    "focusing a badged tab leaves the badge up — the strip keeps announcing " +
      "output the user is now looking at"
  );
  const closeBody = app
    .split("const handleCloseTab = useCallback")[1]
    ?.split("[tabState.tabs],")[0];
  assert.ok(closeBody, "App.tsx no longer defines handleCloseTab in the expected shape");
  assert.match(
    closeBody,
    /setUnreadTerminalIds[\s\S]{0,200}?next\.delete\(id\)/,
    "a closed tab keeps its badge — a dot for a tab that no longer exists"
  );
  // And the strip is handed the ids at all.
  assert.match(
    app,
    /unreadTerminalIds=\{\[\.\.\.unreadTerminalIds\]\}/,
    "TabBar is never told which terminal tabs are unread"
  );
});

test("an exit clears the badge only where the user is watching", () => {
  // A background exit is the moment the badge matters most — the shell
  // finished (or failed) while the user was elsewhere, and the output is
  // still unseen. Clearing there would silence the one notification the
  // feature exists to deliver; clearing on an *owned* exit is right, because
  // the user just watched it happen.
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const exitStart = app.indexOf("listenShellEvent<unknown>(TERMINAL_EXIT");
  assert.ok(exitStart > 0, "the app recorder never listens for terminal-exit");
  const exitBody = app.slice(exitStart, app.indexOf("}, []);", exitStart));
  assert.match(
    exitBody,
    /if \(ownsTerminal\(ptyId\)\) \{[\s\S]{0,300}?next\.delete\(ptyId\)/,
    "the badge is cleared on the wrong side of the ownership line — either a " +
      "watched exit keeps its badge, or a background one loses it"
  );
});
