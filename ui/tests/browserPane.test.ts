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
 * what the App does around the pane is in `browserWiring.test.ts`.
 */
import test from "node:test";
import assert from "node:assert/strict";
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

// The wiring `App.tsx` puts around this pane — closing, seating, focus, popups,
// page events — is checked by mounting the App in `browserWiring.test.ts`.

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
