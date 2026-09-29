/**
 * What `BrowserPane` puts on screen.
 *
 * The model's decisions are tested in `browserHistory.test.ts` and
 * `browserDispatch.test.ts`; this file is the other half of that claim — that
 * a tab's state reaches the markup as words and attributes rather than as a
 * component that happens to return something. A disabled Back button is
 * `disabled` in the DOM, and no amount of correct arithmetic in the model
 * shows up on screen unless the prop is wired to it.
 *
 * Static rendering only: `renderToStaticMarkup` runs no effects and no event
 * handlers, so nothing here clicks Back. The commit/classify cycle is driven
 * for real in `browserPaneInteraction.test.ts` through the DOM harness, and
 * the wiring the render harness cannot reach is frozen by source reads at the
 * bottom — the same "freeze the decision site" move as
 * `browser.rs::open_still_registers_the_embed_wiring`.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { BrowserPane } = await import("../src/components/BrowserPane.tsx");
const { emptyHistory, goBack, visit } = await import("../src/browserHistory.ts");

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

const noop = (): void => {};

const render = (over: Record<string, unknown> = {}): string =>
  renderToStaticMarkup(
    React.createElement(BrowserPane, {
      tabId: "browser-1",
      onNavigate: noop,
      onOpen: noop,
      onBack: noop,
      onForward: noop,
      ...over,
    } as any)
  );

/** The value of the address bar, read back out of the markup. */
const addressBar = (markup: string): string => {
  const match = markup.match(/aria-label="Address"[^>]*value="([^"]*)"/);
  assert.ok(match, "the pane has no address bar");
  return match[1].replace(/&amp;/g, "&");
};

/** Is the control with this accessible name disabled? */
const disabled = (markup: string, name: string): boolean => {
  const button = markup.match(
    new RegExp(`<button[^>]*aria-label="${name}"[^>]*>`)
  );
  assert.ok(button, `the pane has no ${name} control`);
  return /\sdisabled(=""| )/.test(button[0]);
};

const JUNK = ["undefined", "NaN", "[object Object]"];

test("a pane with no address says so instead of showing an empty void", () => {
  const markup = render();
  assert.ok(markup.length > 0);
  for (const junk of JUNK) {
    assert.ok(!markup.includes(junk), `a fresh pane rendered ${junk}`);
  }
  const shown = text(markup);
  assert.match(shown, /No address yet/);
  assert.match(shown, /Type an address above to open one/);
  assert.equal(addressBar(markup), "");
});

test("the page renders in here — a viewport the shell seats a webview on", () => {
  // The one claim this file exists to keep honest: the page is *not* elsewhere.
  // The pane owns a content rectangle the shell places the native view over,
  // and the placeholder for "nothing seated yet" lives inside it.
  const markup = render();
  assert.match(markup, /data-testid="browser-viewport"/);
  assert.doesNotMatch(text(markup), /own window/);
  const withPage = render({
    url: "https://docs.rs/tauri/latest/",
    history: visit(emptyHistory(), "https://docs.rs/tauri/latest/"),
  });
  assert.match(withPage, /data-testid="browser-viewport"/);
  assert.doesNotMatch(text(withPage), /own window/);
  assert.equal(addressBar(withPage), "https://docs.rs/tauri/latest/");
});

test("back and forward are disabled exactly where the stack says", () => {
  const a = "https://a.example";
  const b = "https://b.example";
  const one = visit(emptyHistory(), a);
  const two = visit(one, b);
  const afterBack = goBack(two);
  assert.ok(afterBack, "b should be reachable backwards from two entries");

  // One address, and it is the only one: neither direction goes anywhere.
  const only = render({ url: a, history: one });
  assert.equal(disabled(only, "Back"), true, "nothing before the first entry");
  assert.equal(disabled(only, "Forward"), true, "nothing after the only entry");

  // Back at the first of two: forward is live, back is not.
  const start = render({ url: afterBack.url, history: afterBack.history });
  assert.equal(disabled(start, "Back"), true, "nothing before the first entry");
  assert.equal(disabled(start, "Forward"), false, "b is still ahead");

  // At the newest of two: the other way round.
  const end = render({ url: b, history: two });
  assert.equal(disabled(end, "Back"), false);
  assert.equal(disabled(end, "Forward"), true, "nothing after the newest entry");
});

test("a pane with no page has no reload to offer", () => {
  // Reload is a navigate to the address already shown. With no address there is
  // nothing to navigate to, so the control would be a button that does nothing
  // — the kind of control this app's DESIGN.md §5 rules out.
  const fresh = render();
  assert.equal(disabled(fresh, "Reload"), true);
  const shown = render({
    url: "https://a.example",
    history: visit(emptyHistory(), "https://a.example"),
  });
  assert.equal(disabled(shown, "Reload"), false);
});

test("the shell's own refusal is shown, in the shell's words", () => {
  // Not a rewritten message: `navigation_allowed`'s wording names the rule and
  // the doc, and a paraphrase here would be a second thing to keep true. The
  // mirror delivers the same sentence before the round trip; the prop covers
  // the case where the shell refuses something the mirror passed.
  const refusal =
    'refusing to navigate to "http://localhost:5173": browser webviews load ' +
    "http(s) on non-loopback hosts only (docs/03 §1.5)";
  const markup = render({ error: refusal });
  assert.match(markup, /role="alert"/);
  assert.ok(text(markup).includes(refusal), "the refusal was not shown verbatim");
  // And it is absent when there is nothing wrong, rather than an empty red box.
  assert.doesNotMatch(render(), /role="alert"/);
});

test("the pane names the tab it belongs to", () => {
  // Two browser tabs render two panes over the life of the window, and the
  // attribute is what tells a test — or a screen reader's landmark list — which
  // one is which.
  const markup = render({ tabId: "browser-7" });
  assert.match(markup, /data-tab-id="browser-7"/);
  assert.match(markup, /aria-label="Browser"/);
});

test("rendering a pane produces no React warning of any kind", () => {
  // The claim here is about the markup, and React is the authority on whether
  // it is well-formed.
  const warnings: string[] = [];
  const original = console.error;
  console.error = (...args: unknown[]): void => {
    warnings.push(args.map(String).join(" "));
  };
  try {
    render();
    render({ url: "https://a.example", history: visit(emptyHistory(), "https://a.example") });
    render({ error: "refused" });
  } finally {
    console.error = original;
  }
  assert.deepEqual(warnings, [], `React complained:\n  ${warnings.join("\n  ")}`);
});

// ── the wiring the render harness cannot reach ───────────────────────────
//
// Everything above renders `BrowserPane` directly. What this file cannot see is
// `App.tsx`: nothing in the suite mounts it, so decisions there would fail
// silently. This is the same "freeze the decision site" move as
// `browser.rs::open_still_registers_the_embed_wiring` — a read of the committed
// source, crude on purpose, so that *deleting* the guard fails here instead of
// in a user's window.

test("closing a browser tab with no page does not ask the shell to close one", () => {
  const app = readFileSync(
    new URL("../src/App.tsx", import.meta.url),
    "utf8"
  );
  // The body of `handleCloseTab`, which is the only place a close reaches the
  // shell: the strip's button, ⌘W and the pane all come through it.
  const body = app
    .split("const handleCloseTab = useCallback(")[1]
    ?.split("[tabState.tabs]")[0];
  assert.ok(body, "App.tsx no longer defines handleCloseTab in the expected shape");
  assert.ok(
    body.includes("closeBrowserWebview"),
    "handleCloseTab no longer closes the page — a closed tab would orphan a running webview"
  );
  // `close` in browser.rs refuses a tab that has no page, so calling it
  // unconditionally puts "no browser tab is open" on screen as an error the user
  // caused by closing a tab they never gave an address to. `Tab.url` is what
  // says the tab owns one.
  assert.match(
    body,
    /kind === "browser" && tab\.url/,
    "handleCloseTab calls the shell for a browser tab that never had a page; " +
      "guard it on tab.url, which is what says a page exists"
  );
});

test("the first address seats a page for the tab that is already on screen", () => {
  const app = readFileSync(
    new URL("../src/App.tsx", import.meta.url),
    "utf8"
  );
  // The body of `handleOpenBrowser`, the address bar's first move. A window
  // rather than a split on the dependency list: that list is part of the thing
  // under test, so slicing on it made the test fail the moment the handler
  // learned a second dependency — which is a change to how it works, not to
  // whether it works.
  const marker = "const handleOpenBrowser = useCallback";
  const at = app.indexOf(marker);
  const body = at === -1 ? undefined : app.slice(at, at + 1600);
  assert.ok(
    body,
    "App.tsx no longer defines handleOpenBrowser in the expected shape"
  );
  assert.doesNotMatch(
    body,
    /tabId\("browser"\)/,
    "handleOpenBrowser mints a fresh id again, so the seated page and the tab " +
      "it fills are named differently and the next navigation is refused"
  );
  // One `id` parameter, reaching both the shell and the tab state. Two spellings
  // of the same idea is the bug this freeze exists for.
  assert.match(
    body,
    /openBrowserWebview\(\s*id,/,
    "the page is not seated under the tab's own id"
  );
  // The page is placed where the pane says: the bounds measured by the UI
  // reach the shell, not a guess about the window's chrome.
  assert.match(
    body,
    /browserBoundsRef\.current/,
    "the open call does not carry the pane's measured bounds — the page would " +
      "be placed where nobody measured"
  );
  // The mirror's pre-flight: a refused address never reaches the shell.
  assert.match(
    body,
    /classifyBrowserAddress\(/,
    "handleOpenBrowser does not classify before the round trip — the mirror " +
      "exists so a refusal lands in the frame the address was typed in"
  );
  assert.match(
    body,
    /openBrowserTab\(prev, id, url, selectedWs\?\.id\)/,
    "the tab is not filled in under the id the shell was given, with the " +
      "folder it belongs to"
  );
});

test("a refused first address is reported in the tab the user typed it in", () => {
  const app = readFileSync(
    new URL("../src/App.tsx", import.meta.url),
    "utf8"
  );
  // The pane renders `pendingBrowser.error` only when the pending id equals its
  // own tab id. Filing the refusal under an id no tab has is what made typing an
  // address do nothing at all — no navigation, no error, no alert.
  const handler = app
    .split("const handleOpenBrowser = useCallback")[1]
    ?.split("}, [")[0];
  const render = app.split("{activeBrowserTab ?")[1]?.split("activeTerminalTab ?")[0];
  assert.ok(handler && render, "App.tsx changed shape; re-read these two halves");
  assert.match(
    handler,
    /setPendingBrowser\(\{\s*tabId: id,/,
    "the refusal is not keyed by the tab that exists"
  );
  assert.match(
    render,
    /pendingBrowser\?\.tabId === activeBrowserTab\.id/,
    "the pane no longer matches the pending error against its own tab"
  );
  // And it is that tab's id that the address bar is wired to, not a new one.
  assert.match(
    app,
    /onOpen=\{\(url\) => void handleOpenBrowser\(activeBrowserTab\.id, url\)\}/,
    "the address bar's first move does not carry the tab it is already in"
  );
});

test("a browser tab replaces the transcript rather than sitting under it", () => {
  const app = readFileSync(
    new URL("../src/App.tsx", import.meta.url),
    "utf8"
  );
  assert.match(
    app,
    /\{activeBrowserTab \?/,
    "App.tsx renders no pane conditional — a page tab would show a chat " +
      "transcript under its address bar"
  );
  assert.match(app, /<BrowserPane/, "App.tsx never renders BrowserPane");
});

test("exactly one page is visible at a time, and the pane's geometry is reported", () => {
  const app = readFileSync(
    new URL("../src/App.tsx", import.meta.url),
    "utf8"
  );
  // Visibility is the stacking order for embedded pages: the effect fires on
  // every active-tab change, including to no browser tab at all (the empty id
  // hides everything). Deleting it leaves every page stacked on top of the
  // newest one, which is a browser that shows four pages at once.
  assert.match(
    app,
    /focusBrowserWebview\(activeBrowserId\)/,
    "the focus effect is gone — embedded pages would all show at once"
  );
  // The pane's measurements reach the shell: onBounds is wired, and the
  // coalesced resize carries them.
  assert.match(
    app,
    /onBounds=\{handleBrowserBounds\}/,
    "the pane's content rectangle is not reported to the shell — a page would " +
      "keep the size of the moment it was opened"
  );
  assert.match(
    app,
    /resizeBrowserWebviews\(current\)/,
    "the measured rectangle never reaches the shell's resize"
  );
});

test("the inspector control exists, states its state, and there is no way out", () => {
  // DevTools is offered only when the caller wires it (the shell always has
  // the inspector, so App always passes it), and it says whether the inspector
  // is open. The second escape the pane used to carry is gone, and this is the
  // inverse assertion on purpose: a control that has been removed from the app
  // is one thing, a control that has been removed *everywhere* is the promise.
  const bare = render();
  assert.doesNotMatch(bare, /aria-label="DevTools"/, "DevTools offered without a wiring");
  const withPage = render({
    url: "https://a.example",
    history: visit(emptyHistory(), "https://a.example"),
    onToggleDevtools: noop,
    devtoolsOpen: true,
  });
  const devtools = withPage.match(/<button[^>]*aria-label="DevTools"[^>]*>/);
  assert.ok(devtools, "no DevTools control");
  assert.match(devtools![0], /aria-pressed="true"/, "the control does not state the inspector is open");
  // A page is loaded, its address is known and its own URL is in the bar, and
  // there is still no control that could take that URL anywhere else.
  assert.doesNotMatch(
    withPage,
    /Open in system browser/,
    "the pane offers to hand a page to the operating system's browser again"
  );
  assert.doesNotMatch(
    bare,
    /aria-label="[^"]*(system browser|external)[^"]*"/i,
    "a control that leaves the app is back in the pane"
  );
});

test("the inspector is wired to the shell, and nothing else is", () => {
  const app = readFileSync(
    new URL("../src/App.tsx", import.meta.url),
    "utf8"
  );
  // The toggle is a real open/close pair, not a wish: the shell's commands
  // are called, and the confirmed result — not the press — moves the state.
  assert.match(
    app,
    /openBrowserDevtools\(id\)/,
    "the DevTools toggle does not open the inspector"
  );
  assert.match(
    app,
    /closeBrowserDevtools\(id\)/,
    "the DevTools toggle does not close the inspector"
  );
  assert.match(
    app,
    /devtoolsOpen=\{devtoolsTabId === activeBrowserTab\.id\}/,
    "the control's pressed state is not the inspector's own state"
  );
  // The hand-off to the operating system's own browser is gone from the app,
  // not merely from the pane: no import, no handler, no prop. Every website
  // Codify can open is a tab in Codify, and this is where that is pinned on
  // the UI side — the Rust test `the_only_escape_is_the_inspector_and_the_
  // module_spawns_nothing` pins the shell side, and a removal that only
  // reached one of them would leave a control that dead-ends at the user.
  for (const gone of [/openBrowserExternal/, /onOpenExternal/, /Could not open the system browser/]) {
    assert.doesNotMatch(
      app,
      gone,
      "App still carries the system-browser hand-off — the shell no longer registers it,        so the control would refuse at the user"
    );
  }
  // The control's existence is the shell's answer, not the frontend's guess:
  // the pane asks whether this build has an inspector and hides the control
  // when the answer is no, instead of offering a command that does not exist.
  assert.match(
    app,
    /browserDevtoolsAvailable\(\)/,
    "the UI hardcodes the build shape instead of asking the shell"
  );
  assert.match(
    app,
    /devtoolsAvailable\s*\?\s*\(\) => handleToggleDevtools/,
    "the DevTools control is not gated on the shell's answer"
  );
});

test("the page's life is subscribed: start, finish-with-address, title", () => {
  const app = readFileSync(
    new URL("../src/App.tsx", import.meta.url),
    "utf8"
  );
  // All three events, or the feature is a name without a behaviour: a
  // redirect that never reaches Tab.url is an address bar that lies after
  // every hop, and a title that never lands is a strip of hosts forever.
  for (const event of [
    "BROWSER_PAGE_LOADING",
    "BROWSER_PAGE_LOADED",
    "BROWSER_PAGE_TITLED",
  ]) {
    assert.match(
      app,
      new RegExp("listenShellEvent<unknown>\\s*\\(\\s*" + event),
      event + " is never subscribed — the page reports and nobody listens"
    );
  }
  // The finish carries the live address into the tab, which is what makes a
  // redirect visible; dropping that call keeps the marker logic but breaks
  // the address bar. (Mutation-checked.)
  assert.match(
    app,
    /setBrowserPageUrl\(prev, fact\.tab_id, fact\.url\)/,
    "the load-finish handler does not land the live address — redirects \
     would never reach the address bar"
  );
  // And the strip's loading marker is wired from the page-reported set.
  assert.match(
    app,
    /loadingTabIds=\{\[...loadingBrowserIds\]\}/,
    "the strip is not shown which pages are loading"
  );
});

test("a popup request opens a tab through the guarded path, and never a window", () => {
  const app = readFileSync(
    new URL("../src/App.tsx", import.meta.url),
    "utf8"
  );
  // The shell refuses every popup; the announcement is what the user acts on.
  // The listener must open a real tab (openTab + handleOpenBrowser — the same
  // path as typing), because a popup that opened nothing would make Google
  // sign-in and every target=_blank link dead.
  assert.match(
    app,
    /BROWSER_POPUP_REQUESTED/,
    "App.tsx no longer listens for popup requests — sign-in flows and " +
      "target=_blank links would do nothing"
  );
  assert.match(
    app,
    /readBrowserPopupRequested\(payload\)/,
    "the popup payload is not validated before it opens a tab"
  );
  const listener = app
    .split("BROWSER_POPUP_REQUESTED, (payload) => {")[1]
    ?.split(".then((off)")[0];
  assert.ok(listener, "the popup listener changed shape; re-read it");
  assert.match(listener, /void handleOpenBrowser\(id, popup\.url\)/,
    "a popup does not open through the same guarded path as a typed address");
  // The guard itself, verbatim: an announcement that fails validation must
  // open nothing — and an announcement that *passes* validation must not be
  // swallowable by a weakened condition, which is why the text is pinned and
  // not just its pieces. (Mutation-checked: `if (!popup || true)` fails here.)
  assert.match(
    listener,
    /if \(!popup\) return;/,
    "the popup guard is weakened or reshaped — an announcement must either " +
      "open a tab through the guarded path or be refused by validation, and " +
      "nothing between"
  );
  // The whole app builds no separate window anywhere on this path: the shell
  // refuses them (browser.rs), and the UI's answer is a tab or nothing.
  assert.doesNotMatch(
    app,
    /window\.open\(/,
    "the UI opens a popup with window.open — that is the separate window the " +
      "embedded browser exists to not have"
  );
});

test("the shell's own prose says the page is in the window, not beside it", () => {
  // Comments are load-bearing here. `App.tsx` is 3,000 lines of decisions
  // whose *reasons* live in the comments, and the next change to the browser
  // is written by whoever reads them — so a comment that describes a separate
  // OS window is a defect that behaves like code, while producing no failing
  // test, no build error and no wrong pixels.
  //
  // It happened: two comments in `App.tsx` still described the page as a
  // separate OS window, and `docs/09` §7.2's command list still named the
  // system-browser hand-off that was deleted, after `browser.rs` had stopped
  // building one and a test was failing if it did. Nothing about the running
  // app was wrong. Everything a reader would conclude from it was.
  const app = readFileSync(
    new URL("../src/App.tsx", import.meta.url),
    "utf8"
  );
  const pane = readFileSync(
    new URL("../src/components/BrowserPane.tsx", import.meta.url),
    "utf8"
  );

  // Positive, in both files: the page is a child webview of this window. An
  // absence check alone would pass on a file that stopped talking about the
  // question at all.
  assert.match(
    app,
    /child\s+webview/,
    "App.tsx no longer says the page is a child webview of this window — the \
     shape the shell actually builds (browser.rs: Window::add_child) should be \
     the shape the comment claims"
  );
  assert.match(
    pane,
    /child webview/,
    "BrowserPane.tsx no longer says its page is a child webview of the main \
     window"
  );

  // Negative, in `App.tsx`: the two stale sentences, as they read. Both are
  // refusals of a browser webview being a window of its own — the one place
  // `BrowserPane.tsx` may say it is, in the past tense, is its own history
  // note, so this scan is deliberately scoped to the file that held them.
  // (Mutation-checked: restoring either sentence fails this.)
  assert.doesNotMatch(
    app,
    /separate (?:OS )?window/i,
    "App.tsx describes the browser page as a separate OS window. It is not: \
     codify_browser_open seats a child webview of the main window over \
     BrowserPane's content area (docs/09 §7.3). A comment saying otherwise \
     sends the next change back to a shape the shell refuses to build"
  );
});
