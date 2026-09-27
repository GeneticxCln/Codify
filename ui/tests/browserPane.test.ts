/**
 * What `BrowserPane` puts on screen.
 *
 * The model's decisions are tested in `browserHistory.test.ts`; this file is the
 * other half of that claim — that a tab's state reaches the markup as words and
 * attributes rather than as a component that happens to return something. A
 * disabled Back button is `disabled` in the DOM, and no amount of correct
 * arithmetic in the model shows up on screen unless the prop is wired to it.
 *
 * Static rendering only: `renderToStaticMarkup` runs no effects and no event
 * handlers, so nothing here clicks Back. That is the harness's limit and it is
 * why the interesting assertions are about *state* — what the controls say when
 * there is nothing behind them, and what the pane tells the user about where the
 * page is.
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

test("a pane that has a page discloses that the page is elsewhere", () => {
  // The one claim that must not be implied away: the page is a separate OS
  // window, so chrome with no document under it is the design and not a bug.
  const url = "https://docs.rs/tauri/latest/";
  const markup = render({ url, history: visit(emptyHistory(), url) });
  assert.match(text(markup), /The page is open in its own window/);
  assert.equal(addressBar(markup), url);
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
  // the doc, and a paraphrase here would be a second thing to keep true.
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
// `App.tsx`: nothing in the suite mounts it, so two decisions there would fail
// silently. This is the same "freeze the decision site" move as
// `browser.rs::open_still_registers_the_close_listener` — a read of the
// committed source, crude on purpose, so that *deleting* the guard fails here
// instead of in a user's window.

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
    "handleCloseTab no longer closes the webview — a closed tab would orphan a running page"
  );
  // `close` in browser.rs refuses a tab that has no window, so calling it
  // unconditionally puts "no browser tab is open" on screen as an error the user
  // caused by closing a tab they never gave an address to. `Tab.url` is what
  // says the tab owns one.
  assert.match(
    body,
    /kind === "browser" && tab\.url/,
    "handleCloseTab calls the shell for a browser tab that never had a page; " +
      "guard it on tab.url, which is what says a webview exists"
  );
});

test("the first address opens a webview for the tab that is already on screen", () => {
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
  const body = at === -1 ? undefined : app.slice(at, at + 1200);
  assert.ok(
    body,
    "App.tsx no longer defines handleOpenBrowser in the expected shape"
  );
  assert.doesNotMatch(
    body,
    /tabId\("browser"\)/,
    "handleOpenBrowser mints a fresh id again, so the webview window and the tab " +
      "it fills are named differently and the next navigation is refused"
  );
  // One `id` parameter, reaching both the shell and the tab state. Two spellings
  // of the same idea is the bug this freeze exists for.
  assert.match(
    body,
    /openBrowserWebview\(id, url\)/,
    "the webview is not opened under the tab's own id"
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
  // address do nothing at all — no navigation, no error, no alert — because the
  // first address is always refused before there is a page behind the pane.
  const handler = app
    .split("const handleOpenBrowser = useCallback")[1]
    ?.split("}, []);")[0];
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
