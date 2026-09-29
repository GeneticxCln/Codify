/**
 * What a browser tab does with the facts its page reports.
 *
 * The shell's three page-life events (`browser-page-loading`, `-loaded`,
 * `-titled`) arrive out of order, for tabs that may be gone, about addresses
 * that may be superseded. This file pins the decisions that make that safe:
 * the payload reader refuses by shape, a redirect is an address (the live
 * URL reaches `Tab.url`, the history does not grow), an empty title is no
 * answer, and the loading marker is bounded — the property that stops a
 * page which dies into an interstitial from spinning forever.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

// Dynamic, not static: `src/tabs.ts` imports its siblings extensionlessly,
// which resolves only once the loader above is registered — and a static
// import is hoisted above that registration. (The same gotcha
// tabs.test.ts's header documents.)
const {
  LOADING_TIMEOUT_MS,
  pageLoadFinishedPayload,
  pageLoadStarted,
  pageTitlePayload,
  readBrowserPageState,
} = await import("../src/browserPageState.ts");
const { emptyTabs, openBrowserTab, renameBrowserTab, setBrowserPageUrl } =
  await import("../src/tabs.ts");

// A browser tab with a page already seated — the shape every page-life fact
// lands on. The address matters: `hostOf` derives the title from it.
const tabWith = (id: string) => openBrowserTab(emptyTabs, id, "https://example.com/");

test("the loading bound is generous but finite", () => {
  // Pinned as a property, not just a constant: a value under a few seconds
  // would flash the marker on a slow page; an unbounded one is the forever
  // spinner this module exists to prevent. 15s is "slower than almost every
  // load, shorter than a user's patience for a lie".
  assert.equal(LOADING_TIMEOUT_MS, 15_000);
  assert.ok(Number.isFinite(LOADING_TIMEOUT_MS));
});

test("the payload reader refuses everything that is not a page fact", () => {
  for (const payload of [
    null,
    undefined,
    "tab-1",
    42,
    {},
    [],
    { tab_id: "" },
    { tab_id: "tab-1" },
    { url: "https://a.example" },
    { tab_id: 7, url: "https://a.example" },
    { tab_id: "tab-1", url: "" },
    { tab_id: "tab-1", url: "https://a.example", title: 9 },
  ]) {
    assert.equal(
      readBrowserPageState(payload),
      null,
      `${JSON.stringify(payload)} must not land on a tab`
    );
  }
  assert.deepEqual(readBrowserPageState({ tab_id: "tab-1", url: "https://a.example" }), {
    tab_id: "tab-1",
    url: "https://a.example",
  });
  // Extra fields are a future shell, not a reason to refuse.
  assert.deepEqual(
    readBrowserPageState({ tab_id: "tab-1", url: "https://a.example", extra: 1 }),
    { tab_id: "tab-1", url: "https://a.example" }
  );
});

test("a load start names a browser tab, and only one", () => {
  const state = tabWith("tab-1");
  assert.deepEqual(pageLoadStarted(state.tabs, "tab-1"), ["tab-1"]);
  // Unknown id: no fact to apply. (A browser tab's id is unique by
  // construction now — `tabId("browser")` — so the old shared-id concern is
  // gone with the code that had it.)
  assert.equal(pageLoadStarted(tabWith("tab-1").tabs, "absent"), null);
});

test("a load finish carries the live address — the redirect lands", () => {
  const fact = readBrowserPageState({
    tab_id: "tab-1",
    url: "https://accounts.example/callback?code=x",
  });
  assert.ok(fact);
  assert.deepEqual(pageLoadFinishedPayload(fact!), {
    tabId: "tab-1",
    url: "https://accounts.example/callback?code=x",
  });
  assert.equal(pageLoadFinishedPayload({ tab_id: "", url: "https://a.example" }), null);
});

test("the live address reaches the tab; the history does not grow", () => {
  // The user visited example.com; the page redirected. The address bar says
  // where they are; the history says what they visited. Both true at once.
  let state = tabWith("tab-1");
  state = setBrowserPageUrl(state, "tab-1", "https://accounts.example/after");
  const tab = state.tabs[0];
  assert.equal(tab.url, "https://accounts.example/after");
  assert.equal(tab.title, "example.com", "a redirect does not retitle a tab the page has named");
  // Unknown or non-browser tabs are untouched.
  assert.equal(setBrowserPageUrl(state, "absent", "https://x.example"), state);
});

test("a page's title lands, and an empty one is no answer", () => {
  assert.deepEqual(pageTitlePayload({ tab_id: "tab-1", url: "https://a.example", title: "Example Domain" }), {
    tabId: "tab-1",
    title: "Example Domain",
  });
  assert.equal(pageTitlePayload({ tab_id: "tab-1", url: "https://a.example" }), null);
  assert.equal(pageTitlePayload({ tab_id: "tab-1", url: "https://a.example", title: "" }), null);
  assert.equal(pageTitlePayload({ tab_id: "tab-1", url: "https://a.example", title: "   " }), null);

  let state = renameBrowserTab(tabWith("tab-1"), "tab-1", "Example Domain");
  assert.equal(state.tabs[0].title, "Example Domain");
  // Unknown id: unchanged.
  assert.equal(renameBrowserTab(state, "absent", "x"), state);
});
