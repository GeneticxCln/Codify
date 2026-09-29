/**
 * The remembered tab strip: what survives a restart, and what is refused.
 *
 * These are decisions no markup shows. A tab strip that comes back with the
 * wrong tab focused, a history whose cursor points at a page the address bar
 * does not claim, or an address the shell would have refused — none of those
 * are visible in a screenshot, and all of them are the feature. The round trip
 * is therefore the headline test: a live strip goes in, the same strip comes
 * out, and the stack is still a stack afterwards.
 *
 * The refusal half matters as much as the round trip, and for the reason
 * `tabPersistence.ts` states: `localStorage` is not a type, so every shape
 * arriving from it is checked, and the degradation is always *less*. A test
 * that only proved the happy path would pass against a module that trusted
 * whatever it read.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
// Types come in statically and values dynamically, and the difference is not
// taste. `node --experimental-strip-types` *erases* types rather than checking
// them, and it can only erase a type import it can see as one: `type X` inside
// a destructuring pattern is a syntax error there, so the two kinds of import
// cannot share a statement. An `import type` is erased whole, which is also why
// it is safe to hoist above the registration below.
import type { PersistedLayout, TabStorage } from "../src/tabPersistence.ts";
import type { BrowserHistory } from "../src/browserHistory.ts";
import type { Tab, TabState } from "../src/tabs.ts";
// Not for JSX — none of the three modules has any. For the hook's `resolve`
// step: `tabPersistence.ts` reaches two siblings (`./browserDispatch`,
// `./tabs`) and `src/` imports are extensionless (vite and tsc both resolve them
// that way), which node cannot follow on its own.
registerTsx();

// Dynamic, not static, because a static import is hoisted and resolved *before*
// `registerTsx()` runs — the same two-line rule `tabs.test.ts` states. Top-level
// await keeps the names in module scope for the tests below.
const {
  MAX_PERSISTED_HISTORY,
  MAX_PERSISTED_TABS,
  TABS_KEY,
  emptyLayout,
  layoutFrom,
  parseLayout,
  persistLayout,
  readLayout,
  restoreTabs,
} = await import("../src/tabPersistence.ts");
const { emptyHistory, visit } = await import("../src/browserHistory.ts");
const {
  closeTab,
  emptyTabs,
  focusTab,
  openBrowserTab,
  openConversation,
  openTab,
  setBrowserPageUrl,
  setBrowserUrl,
  tabId,
} = await import("../src/tabs.ts");

/** A store in memory, so the tests never need a `localStorage`. */
function memoryStore(seed: Record<string, string> = {}): TabStorage & {
  written: Record<string, string>;
} {
  const map: Record<string, string> = { ...seed };
  return {
    written: map,
    getItem: (key) => (key in map ? map[key] : null),
    setItem: (key, value) => {
      map[key] = value;
    },
  };
}

/** Three entries deep, with the cursor in the middle. */
function stack(...urls: string[]): BrowserHistory {
  let history = emptyHistory();
  for (const url of urls) history = visit(history, url);
  return history;
}

const A = "https://a.example/one";
const B = "https://b.example/two";
const C = "https://c.example/three";

// ── the round trip ────────────────────────────────────────────────────────

test("a live strip survives a restart with its stacks and its focus", () => {
  // Built through the same functions the app uses, so the state is one the app
  // can actually be in: `setBrowserUrl` moves the address and the stack
  // together, which is what keeps the two from disagreeing.
  let live = openBrowserTab(emptyTabs, tabId("browser"), A, "ws-1");
  const browserId = live.tabs[0].id;
  live = setBrowserUrl(live, browserId, B, stack(A, B));
  live = setBrowserUrl(live, browserId, C, stack(A, B, C));
  live = openConversation(live, "conv-1", "Thread one", "ws-1");
  live = focusTab(live, live.tabs[1].id);

  const store = memoryStore();
  persistLayout(live, store);
  const restored = restoreTabs(emptyTabs, readLayout(store));

  assert.equal(restored.tabs.length, 2, "both tabs come back");
  assert.deepEqual(
    restored.tabs.map((t) => t.kind),
    ["browser", "chat"],
    "in the order they were left",
  );
  assert.equal(restored.activeId, restored.tabs[1].id, "the tab that was showing shows");
  const restoredBrowser = restored.tabs[0];
  assert.equal(restoredBrowser.kind, "browser");
  assert.equal(restoredBrowser.url, C, "the address it was on, not the first it visited");
  assert.deepEqual(restoredBrowser.history, stack(A, B, C), "the whole stack, cursor and all");
  assert.equal(restoredBrowser.history?.index, 2, "so back still goes to B, then A");
  assert.equal(restoredBrowser.workspaceId, "ws-1", "the folder it belonged to");
  assert.equal(restored.tabs[1].conversationId, "conv-1");
  assert.equal(restored.tabs[1].workspaceId, "ws-1");
});

test("a remembered tab keeps the key the engine knows it by", () => {
  // **The bug this test exists to hold.** `PersistedTab` carried no key, so a
  // boot restored a strip in which every tab was keyless; `ensureKeys` then
  // minted a fresh key for each one, `planChanges` read them as tabs the engine
  // had never been told about, and the push wrote a *second* row beside every
  // row that was already there. `PUT /shell/tabs` only ever upserts and nothing
  // but an explicit delete removes a row, so the strip grew by its own size on
  // every boot: measured in the developer's real database as 172 rows where one
  // session's worth was intended, in bursts of up to `MAX_PERSISTED_TABS` per
  // boot, with the same URL repeated under a dozen different keys.
  const live = openBrowserTab(emptyTabs, tabId("browser"), A, "ws-1");
  const keyed = { ...live, tabs: live.tabs.map((t) => ({ ...t, key: "k_aaaaaa_1" })) };

  const store = memoryStore();
  persistLayout(keyed, store);
  const remembered = readLayout(store);
  assert.equal(
    remembered.tabs[0].key,
    "k_aaaaaa_1",
    "the mirror forgot the tab's key, so the next boot renames a tab the engine \
     already holds and the strip accumulates a copy of itself per boot",
  );
  const restored = restoreTabs(emptyTabs, remembered);
  assert.equal(
    restored.tabs[0].key,
    "k_aaaaaa_1",
    "a restored tab came back without its key — the engine's row would be \
     abandoned rather than adopted",
  );
});

test("a tab that has never been keyed is still restored without one", () => {
  // The other half, and the reason `key` is optional: a tab that has not been
  // through `ensureKeys` yet (the very first session) must not be remembered
  // with an invented key, or it would be pushed as a stranger under a name the
  // engine has never confirmed.
  const live = openBrowserTab(emptyTabs, tabId("browser"), A, "ws-1");
  const store = memoryStore();
  persistLayout(live, store);
  const remembered = readLayout(store);
  assert.equal(remembered.tabs[0].key, undefined);
  assert.equal(restoreTabs(emptyTabs, remembered).tabs[0].key, undefined);
});

test("a redirect moves the current entry instead of costing the stack", () => {
  // The case the module's redirect rule exists for. `setBrowserPageUrl` is what
  // the shell's `browser-page-loaded` event drives, and after a redirect it
  // records the *live* address on the tab while the stack's cursor still names
  // the address the redirect came from. Persisting that as-is would drop the
  // whole stack on every site that redirects — which is nearly all of them —
  // and the user would lose their back button without being told.
  const redirected = "https://b.example/redirected";
  let live = openBrowserTab(emptyTabs, tabId("browser"), A);
  const id = live.tabs[0].id;
  live = setBrowserUrl(live, id, B, stack(A, B));
  live = setBrowserPageUrl(live, id, redirected);

  const projected = layoutFrom(live);
  const persisted = projected.tabs[0];
  assert.equal(persisted.url, redirected, "the address bar is the live one");
  assert.deepEqual(
    persisted.history,
    { entries: [A, redirected], index: 1 },
    "one entry per visit, and the current one now points at where it landed",
  );
  const restored = restoreTabs(emptyTabs, projected);
  assert.deepEqual(
    restored.tabs[0].history,
    { entries: [A, redirected], index: 1 },
    "so back still goes to A after a restart",
  );
});

test("the focused tab is the one that comes back focused", () => {
  let live = openBrowserTab(emptyTabs, tabId("browser"), A);
  live = openBrowserTab(live, tabId("browser"), B);
  live = focusTab(live, live.tabs[0].id);
  const projected = layoutFrom(live);
  assert.equal(projected.activeIndex, 0, "the first tab was showing");
  const restored = restoreTabs(emptyTabs, projected);
  // By position, not by the id the tab had before: ids are process-local and
  // restore mints fresh ones, so the only thing that can be compared across a
  // restart is where the tab sits.
  assert.equal(restored.activeId, restored.tabs[0].id, "and it is the one showing");
  assert.equal(restored.tabs[0].url, A, "which is the tab that was showing");
});

test("restored tabs get fresh ids, because ids are process-local", () => {
  const live = openBrowserTab(emptyTabs, "tab-1", A);
  const restored = restoreTabs(emptyTabs, layoutFrom(live));
  // The id in storage is never trusted back: `tabId()` counts from one in every
  // process, so a restored "tab-1" would collide with the next one minted.
  assert.equal(restored.tabs.length, 1);
  assert.notEqual(restored.tabs[0].id, "tab-1");
  const second = openBrowserTab(restored, tabId("browser"), B);
  assert.equal(
    new Set(second.tabs.map((t) => t.id)).size,
    second.tabs.length,
    "no id collision between a restored tab and a new one",
  );
});

test("a terminal tab is not remembered, and the tab to its left takes the focus", () => {
  let live = openBrowserTab(emptyTabs, tabId("browser"), A);
  live = openBrowserTab(live, tabId("browser"), B);
  // The terminal is the third tab, so its left neighbour is the *second* — the
  // strip, not "the first page".
  live = openTab(live, {
    id: "pty-1",
    kind: "terminal",
    title: "Terminal",
    ptyId: "pty-1",
    workspaceId: "ws-1",
  });
  live = focusTab(live, "pty-1");
  assert.equal(live.activeId, "pty-1", "a terminal was showing");

  const projected = layoutFrom(live);
  assert.equal(
    projected.tabs.length,
    2,
    "the PTY and its scrollback are gone with the process, so the tab is not remembered",
  );
  const restored = restoreTabs(emptyTabs, projected);
  assert.equal(
    restored.activeId,
    restored.tabs[1].id,
    "focus lands on the tab to the vanished one's left, where the eye was",
  );
  assert.equal(restored.tabs[1].url, B);
});

test("a closed tab stays closed across a restart", () => {
  let live = openBrowserTab(emptyTabs, tabId("browser"), A);
  const doomed = live.tabs[0].id;
  live = openBrowserTab(live, tabId("browser"), B);
  live = closeTab(live, doomed);
  const restored = restoreTabs(emptyTabs, layoutFrom(live));
  assert.deepEqual(
    restored.tabs.map((t) => t.url),
    [B],
    "the layout is written from the live strip, so a close is remembered as a close",
  );
});

test("a chat tab with no thread is a clean slate, and is remembered as one", () => {
  const live: TabState = openTab(emptyTabs, {
    id: "chat-1",
    kind: "chat",
    title: "New thread",
  });
  const restored = restoreTabs(emptyTabs, layoutFrom(live));
  assert.equal(restored.tabs.length, 1);
  assert.equal(restored.tabs[0].kind, "chat");
  assert.equal(
    restored.tabs[0].conversationId,
    undefined,
    "an unnamed composer is a real state, not a broken one",
  );
});

test("an address bar that never went anywhere comes back empty, not as a page", () => {
  const live: TabState = openTab(emptyTabs, {
    id: "browser-1",
    kind: "browser",
    title: "",
  });
  const projected = layoutFrom(live);
  assert.equal(projected.tabs[0].url, undefined, "no address, nothing to remember");
  const restored = restoreTabs(emptyTabs, projected);
  assert.equal(restored.tabs[0].url, undefined);
  assert.equal(restored.tabs[0].history, undefined, "and no stack pretending otherwise");
});

// ── titles are derived, not stored ─────────────────────────────────────────

test("titles are not remembered, because the page and the thread re-name the tab", () => {
  let live = openBrowserTab(emptyTabs, tabId("browser"), A);
  live = {
    ...live,
    tabs: live.tabs.map((t) => ({ ...t, title: "Example Domain" })),
  };
  const projected = layoutFrom(live);
  assert.equal(
    "title" in projected.tabs[0],
    false,
    "a stored title is stale the moment the page loads again, so the key is not \
     written at all (and `PersistedTab` has no field for it)",
  );
  const restored = restoreTabs(emptyTabs, projected);
  assert.equal(restored.tabs[0].title, "a.example", "the host stands in until the page speaks");
});

// ── what is refused, and the shape of the refusal ──────────────────────────

test("nothing remembered is an empty strip, not an error", () => {
  assert.deepEqual(readLayout(memoryStore()), emptyLayout());
  assert.deepEqual(readLayout(undefined), emptyLayout());
  assert.deepEqual(parseLayout(null), emptyLayout());
  assert.deepEqual(parseLayout(""), emptyLayout());
  assert.deepEqual(parseLayout("{"), emptyLayout());
  assert.deepEqual(parseLayout("[]"), emptyLayout());
  assert.deepEqual(parseLayout("null"), emptyLayout());
  assert.deepEqual(parseLayout('"a string"'), emptyLayout());
  assert.deepEqual(parseLayout('{"tabs":"not a list"}'), emptyLayout());
  assert.deepEqual(parseLayout('{"tabs":[{"kind":"terminal","ptyId":"pty-1"}]}').tabs, []);
});

test("an address the shell would refuse does not come back as a tab", () => {
  for (const hostile of [
    "javascript:alert(1)",
    "data:text/html,<h1>hi</h1>",
    "file:///etc/passwd",
    "tauri://localhost/",
    // Loopback is the guard's core rule, and this is the one that would matter
    // most: a stored `http://127.0.0.1:7430/` is the engine, and a restored tab
    // must never be the one that reaches it.
    "http://127.0.0.1:7430/health",
    "http://localhost:5173/",
    "http://2130706433/",
    "http://[::1]:7430/",
  ]) {
    const raw = JSON.stringify({ tabs: [{ kind: "browser", url: hostile }], activeIndex: 0 });
    const layout = parseLayout(raw);
    assert.equal(
      layout.tabs.length,
      0,
      `${hostile} must not be restored — the whole tab goes, because an address bar showing a refused address is the thing §7.2 refuses to seat`,
    );
  }
});

test("a refused address anywhere in a stack costs the stack, not the tab", () => {
  const raw = JSON.stringify({
    tabs: [
      {
        kind: "browser",
        url: A,
        history: { entries: [A, "javascript:alert(1)"], index: 0 },
      },
    ],
    activeIndex: 0,
  });
  const layout = parseLayout(raw);
  assert.equal(layout.tabs.length, 1, "the page is still a page the user left open");
  assert.equal(
    layout.tabs[0].history,
    undefined,
    "the stack is dropped rather than repaired: a hole in it moves the cursor",
  );
});

test("a stack that disagrees with the address bar is dropped", () => {
  const cases: Array<Record<string, unknown>> = [
    // The cursor points at a page the tab is not showing.
    { entries: [A, B], index: 1 },
    // The cursor is out of the stack.
    { entries: [A, B], index: 2 },
    { entries: [A, B], index: -2 },
    // An empty stack with a cursor that claims otherwise.
    { entries: [], index: 0 },
    // A non-integer cursor, which is what a hand-edited file leaves behind.
    { entries: [A, B], index: 0.5 },
    // A hole in the entries.
    { entries: [A, 7], index: 0 },
    { entries: [A, ""], index: 0 },
  ];
  for (const history of cases) {
    const raw = JSON.stringify({ tabs: [{ kind: "browser", url: A, history }], activeIndex: 0 });
    assert.equal(
      parseLayout(raw).tabs[0]?.history,
      undefined,
      `${JSON.stringify(history)} must not be believed as a stack`,
    );
  }
});

test("an empty stack with the one honest cursor is kept", () => {
  const raw = JSON.stringify({
    tabs: [{ kind: "browser", history: { entries: [], index: -1 } }],
    activeIndex: 0,
  });
  const layout = parseLayout(raw);
  assert.deepEqual(layout.tabs[0].history, { entries: [], index: -1 });
});

test("entries that are not strings, and a history that is not an object, are refused", () => {
  for (const history of [null, "entries", 7, { entries: "nope", index: 0 }]) {
    const raw = JSON.stringify({ tabs: [{ kind: "browser", url: A, history }] });
    assert.equal(parseLayout(raw).tabs[0]?.history, undefined);
  }
});

test("a stored address is canonicalised the way a typed one is", () => {
  // `example.com` is what the address bar holds when the user types it, and
  // the pane sends `https://example.com/`. Restoring must not re-decide that.
  const raw = JSON.stringify({ tabs: [{ kind: "browser", url: "example.com" }] });
  const layout = parseLayout(raw);
  assert.equal(layout.tabs[0].url, "https://example.com/");
  const restored = restoreTabs(emptyTabs, layout);
  assert.equal(restored.tabs[0].url, "https://example.com/");
});

test("a tab that is not an object, or has no kind, is dropped", () => {
  const raw = JSON.stringify({
    tabs: [null, "browser", 7, [], { url: A }, { kind: "chat" }, { kind: "browser", url: A }],
    activeIndex: 0,
  });
  const layout = parseLayout(raw);
  // Two survive: the browser tab, and the chat tab with no thread — which is a
  // clean slate, a real state, not a malformed one. The four entries that said
  // nothing about what they are are gone.
  assert.equal(layout.tabs.length, 2);
  assert.equal(layout.tabs[0].kind, "chat");
  assert.equal(layout.tabs[0].conversationId, undefined);
  assert.equal(layout.tabs[1].url, A);
});

test("a layout with nothing in it restores to nothing, and never points at tab zero", () => {
  const projected = layoutFrom(emptyTabs);
  assert.equal(projected.tabs.length, 0);
  assert.equal(projected.activeIndex, -1, "no tabs, so no position to remember");
  const restored = restoreTabs(emptyTabs, projected);
  assert.deepEqual(restored.tabs, []);
  assert.equal(restored.activeId, null, "and nothing is focused, which is a real state");
});

test("an out-of-range focus position does not focus a tab that is not there", () => {
  const layout: PersistedLayout = {
    tabs: [{ kind: "browser", url: A }],
    activeIndex: 7,
  };
  const restored = restoreTabs(emptyTabs, layout);
  assert.equal(restored.tabs.length, 1);
  assert.ok(restored.activeId, "the tab that is there is the tab that shows");
});

// ── the caps ───────────────────────────────────────────────────────────────

test("a deep history is remembered to its most recent entries, cursor and all", () => {
  const urls = Array.from({ length: MAX_PERSISTED_HISTORY + 40 }, (_, i) =>
    `https://site${i}.example/`,
  );
  let history = emptyHistory();
  for (const url of urls) history = visit(history, url);
  const live: TabState = openTab(emptyTabs, {
    id: "browser-1",
    kind: "browser",
    title: "site",
    url: urls[urls.length - 1],
    history,
  });
  const projected = layoutFrom(live);
  const kept = projected.tabs[0].history;
  assert.ok(kept);
  assert.equal(kept.entries.length, MAX_PERSISTED_HISTORY, "the cap holds");
  assert.equal(
    kept.entries[kept.entries.length - 1],
    urls[urls.length - 1],
    "and the page it was showing is still the last entry",
  );
  assert.equal(kept.index, kept.entries.length - 1, "so the cursor still points at it");
});

test("a long strip is remembered without losing the tab that was showing", () => {
  let live = emptyTabs;
  const ids: string[] = [];
  for (let i = 0; i < MAX_PERSISTED_TABS + 6; i++) {
    live = openBrowserTab(live, tabId("browser"), `https://s${i}.example/`);
    ids.push(live.tabs[i].id);
  }
  // Focus the last one — the one a cap that dropped the tail would lose.
  const focused = ids[ids.length - 1];
  live = focusTab(live, focused);
  const projected = layoutFrom(live);
  assert.equal(projected.tabs.length, MAX_PERSISTED_TABS, "the cap holds");
  const restored = restoreTabs(emptyTabs, projected);
  assert.equal(
    restored.tabs[restored.tabs.length - 1].url,
    "https://s29.example/",
    "the tab that was showing is the last one remembered",
  );
  assert.equal(restored.activeId, restored.tabs[restored.tabs.length - 1].id);
});

// ── the store itself ───────────────────────────────────────────────────────

test("the layout is written under one namespaced key", () => {
  const store = memoryStore();
  persistLayout(openBrowserTab(emptyTabs, tabId("browser"), A), store);
  assert.equal(TABS_KEY, "CODIFY_TABS");
  assert.ok(store.written[TABS_KEY], "and only that key is written");
  const round = JSON.parse(store.written[TABS_KEY]) as PersistedLayout;
  assert.equal(round.tabs.length, 1);
});

test("a store that refuses costs the strip, not the render", () => {
  const hostile: TabStorage = {
    getItem: () => {
      throw new Error("storage is off");
    },
    setItem: () => {
      throw new Error("storage is off");
    },
  };
  assert.deepEqual(readLayout(hostile), emptyLayout());
  // The write is the one that must not throw, and it is called from an effect.
  assert.doesNotThrow(() => persistLayout(openBrowserTab(emptyTabs, tabId("browser"), A), hostile));
});

test("a quota-exceeded write is swallowed, as a full store should be", () => {
  const full: TabStorage = {
    getItem: () => null,
    setItem: () => {
      throw new DOMException("quota", "QuotaExceededError");
    },
  };
  assert.doesNotThrow(() => persistLayout(openBrowserTab(emptyTabs, tabId("browser"), A), full));
});

test("appending to a non-empty state keeps what was already open", () => {
  // The signature does not require an empty base, so the behaviour is pinned:
  // a caller that opened something first must not lose it to a restore.
  const existing = openConversation(emptyTabs, "conv-9", "Existing", "ws-9");
  const restored = restoreTabs(existing, parseLayout(JSON.stringify({
    tabs: [{ kind: "browser", url: A }],
    activeIndex: 0,
  })));
  assert.equal(restored.tabs.length, 2);
  assert.equal(restored.tabs[0].conversationId, "conv-9", "the caller's tab is still first");
  assert.equal(restored.tabs[1].url, A);
});

test("a restored tab is a Tab the strip can use: kind, id and title are all there", () => {
  // The shape assertion, because `restoreTabs` builds `Tab` literals by hand
  // and a missing field is a tab that renders blank rather than one that fails
  // a test somewhere else.
  const restored = restoreTabs(
    emptyTabs,
    parseLayout(JSON.stringify({ tabs: [{ kind: "chat", conversationId: "c", workspaceId: "w" }] })),
  );
  const tab = restored.tabs[0] as Tab;
  assert.equal(tab.kind, "chat");
  assert.equal(typeof tab.id, "string");
  assert.ok(tab.id.length > 0);
  assert.equal(typeof tab.title, "string", "a title is required, and empty is allowed");
  assert.equal(tab.conversationId, "c");
  assert.equal(tab.workspaceId, "w");
});

// The wiring that puts this module to work — the strip restored before the first
// render, written to the mirror before the engine is asked to share it, caught up
// on focus, a restored page seated once with its stack intact — is checked by
// running the app, in `tabRestoreEndToEnd.test.ts`.
