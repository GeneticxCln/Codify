/**
 * The tab strip, remembered and shared, through the mounted App.
 *
 * `tabPersistence.test.ts` and `layoutSync.test.ts` cover the pure modules. What
 * they cannot see is `App.tsx` putting them to work: that the strip comes back
 * before the engine answers, that it is written down before the engine is asked
 * to share it, that a window which is only being looked at catches up, and that
 * a restored page keeps its history when it is opened. These tests replace ones
 * that read `App.tsx` for those decisions; they run the app and look at what it
 * did to storage, to the engine and to the shell.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp } = await import("./appHarness.ts");

const mirror = (storage: Storage): { layout: { tabs: Array<Record<string, unknown>>; activeIndex: number } } => {
  const raw = storage.getItem("CODIFY_TABS");
  assert.ok(raw, "nothing was written to the tab mirror");
  return JSON.parse(raw);
};

const stored = (tabs: Array<Record<string, unknown>>, activeIndex = 0) => ({
  version: 1,
  layout: { tabs, activeIndex },
  pendingRemovals: [],
  pendingWrites: [],
});

const CHATS = stored([
  { key: "k_test_1", kind: "chat", conversationId: "c1", workspaceId: "ws-a" },
  { key: "k_test_2", kind: "chat", conversationId: "c2", workspaceId: "ws-a" },
]);

import type { EngineCall } from "./appHarness.ts";

const requests = (engine: EngineCall[], method: string, path: string) =>
  engine.filter((c) => c.method === method && c.path === path);

const newTab = (root: HTMLElement): HTMLElement =>
  root.querySelector('button[title^="A new, empty tab"]') as HTMLElement;

test("the strip comes back with no engine at all", async () => {
  // The pages are webviews of this process, so a window that waits for the engine
  // to draw its tabs shows nothing when the engine is down even though the pages
  // would have painted.
  await withApp({ health: "down", storedTabs: CHATS }, async ({ tabs }) => {
    assert.equal(tabs().length, 2, "the remembered strip needed the engine to come back");
  });
});

test("the strip is never written to the engine a stale cache names", async () => {
  // The leak this pins: `localStorage` names a previous process's engine, which can
  // be a real, authenticating engine that belongs to somebody else. The strip was
  // written there — 137 rows of one smoke run's URL in a developer's real database.
  await withApp(
    { storedTabs: CHATS, localStorage: { CODIFY_PORT: "7999", CODIFY_TOKEN: "cached-token" } },
    async ({ engine, settle }) => {
      await settle();
      // The startup loads (workspaces, models) do go to the cached address for a
      // moment, before the shell has answered; that is a read of stale data that is
      // replaced a tick later. What must never happen is the *strip* — a write —
      // reaching an engine that belongs to somebody else.
      const strays = engine.filter((c) => c.path.startsWith("/shell/tabs") && c.url.includes(":7999"));
      assert.deepEqual(
        strays.map((c) => `${c.method} ${c.path}`),
        [],
        "the strip was sent to the engine the cache named, not the one the shell reported",
      );
      assert.ok(
        requests(engine, "PUT", "/shell/tabs").every((c) => c.url.includes(":51820")),
        "the strip was not sent to the engine the shell reported",
      );
      assert.ok(requests(engine, "PUT", "/shell/tabs").length > 0, "nothing was shared at all");
    },
  );
});

test("a tab opened while the engine refuses the strip is still remembered", async () => {
  // The mirror is what makes the *next* boot work, including one with no engine, so
  // it must be written before the round trip and must not depend on it succeeding.
  await withApp({ storedTabs: CHATS, failRoutes: ["/shell/tabs"] }, async ({ dom, settle, tabs }) => {
    await dom.click(newTab(dom.container));
    await settle();
    assert.equal(tabs().length, 3);
    assert.equal(mirror(dom.window.localStorage).layout.tabs.length, 3, "the new tab did not reach the mirror");
  });
});

test("the strip is shared once per change: known tabs are not re-sent, a new one is", async () => {
  // `planChanges` is the whole cost model. Asking the engine to store every tab on
  // every change is how a "free" sync becomes a write per tab.
  await withApp({ storedTabs: CHATS }, async ({ dom, engine, settle, engineTabs }) => {
    await settle();
    assert.equal(requests(engine, "PUT", "/shell/tabs").length, 2, "the two remembered tabs were not shared exactly once");
    await dom.click(newTab(dom.container));
    await settle();
    assert.equal(requests(engine, "PUT", "/shell/tabs").length, 3, "an unchanged tab was sent again");
    assert.equal(engineTabs().length, 3);
  });
});

test("a tab the user closes is deleted on the engine and stays closed", async () => {
  await withApp({ storedTabs: CHATS }, async ({ dom, engine, settle, tabs, engineTabs, act }) => {
    await settle();
    const closers = [...dom.container.querySelectorAll('button[aria-label^="Close"]')] as HTMLElement[];
    await dom.click(closers[1]);
    await settle();
    assert.equal(requests(engine, "DELETE", "/shell/tabs/k_test_2").length, 1, "the close never reached the engine");
    assert.deepEqual(engineTabs().map((r) => r.key), ["k_test_1"]);
    // A later look at the engine must not bring it back.
    await act(() => {
      dom.window.dispatchEvent(new dom.window.Event("focus"));
    });
    await settle();
    assert.equal(tabs().length, 1, "the closed tab came back");
  });
});

test("an idle window catches up on what another window did, when it is next looked at", async () => {
  await withApp({ storedTabs: CHATS }, async ({ dom, engine, settle, tabs, putEngineTab, act }) => {
    await settle();
    assert.equal(tabs().length, 2);
    const before = requests(engine, "GET", "/shell/tabs").length;
    // Another window opens a page. Nothing tells this one.
    putEngineTab({
      key: "k_other_1",
      position: 2,
      kind: "browser",
      payload: JSON.stringify({ kind: "browser", url: "https://example.com/other" }),
    });
    await settle();
    assert.equal(tabs().length, 2, "the window changed without being looked at");

    await act(() => {
      dom.window.dispatchEvent(new dom.window.Event("focus"));
    });
    await settle();
    assert.ok(requests(engine, "GET", "/shell/tabs").length > before, "focusing the window did not read the strip");
    assert.equal(tabs().length, 3, "the other window's tab did not appear");
  });
});

test("a hidden window is not read", async () => {
  await withApp({ storedTabs: CHATS }, async ({ dom, engine, settle, act }) => {
    await settle();
    const before = requests(engine, "GET", "/shell/tabs").length;
    Object.defineProperty(dom.window.document, "visibilityState", { value: "hidden", configurable: true });
    await act(() => {
      dom.window.dispatchEvent(new dom.window.Event("focus"));
    });
    await settle();
    assert.equal(requests(engine, "GET", "/shell/tabs").length, before, "a window nobody is looking at asked the engine");
  });
});

// ── seating a restored page ───────────────────────────────────────────────

const HISTORY = ["https://example.com/a", "https://example.com/b", "https://example.com/c"];
const PAGES = stored(
  [
    { key: "k_page_1", kind: "browser", url: HISTORY[2], history: { entries: HISTORY, index: 2 } },
    { key: "k_page_2", kind: "browser", url: "https://example.com/other" },
  ],
  0,
);

const opened = (shell: { calls: string[]; args: Array<Record<string, unknown>> }) =>
  shell.calls.flatMap((c, i) => (c === "codify_browser_open" ? [shell.args[i]] : []));

test("a restored page is seated once the pane has a rectangle, only for the tab on screen, with its stack intact", async () => {
  await withApp({ storedTabs: PAGES, viewport: { width: 900, height: 600 } }, async ({ dom, shell, settle }) => {
    await settle();
    const seats = opened(shell);
    assert.equal(seats.length, 1, `expected the one tab on screen to be seated, saw ${seats.length}`);
    assert.equal(seats[0].url, HISTORY[2], "the page opened was not the tab's own address");
    const rect = seats[0].bounds as { width: number; height: number };
    assert.ok(rect.width > 0 && rect.height > 0, "the page was seated without the pane's measured rectangle");
    // The trap: opening it through the address-bar path starts a fresh one-entry
    // history and throws the restored stack away.
    const kept = mirror(dom.window.localStorage).layout.tabs[0].history as { entries: string[]; index: number };
    assert.deepEqual(kept.entries, HISTORY, "seating the page dropped its back/forward stack");
    assert.equal(kept.index, 2);
  });
});

test("a page opened from the address bar is not opened a second time by the restore path", async () => {
  await withApp({ viewport: { width: 900, height: 600 } }, async ({ dom, shell, settle }) => {
    await dom.click(dom.container.querySelector('button[title^="Browser"]') as HTMLElement);
    await settle();
    const address = dom.container.querySelector('input[aria-label="Address"]') as HTMLInputElement;
    await dom.fill(address, "https://example.com/typed");
    await dom.press(address, "Enter");
    await settle();
    assert.equal(opened(shell).length, 1, "both paths that open a page ran for the same tab");
  });
});
