/**
 * Two windows, one strip: the merge, the cost model, and the offline queue.
 *
 * The interesting cases here are all *two writers*, because with one writer the
 * design could have been a whole-layout `PUT` and this file would have nothing
 * to say. So the tests are arranged by who did what:
 *
 * - **A tab the other window opened appears here**, and one this window opened
 *   appears there. Without a shared strip these are two strips.
 * - **A close is shared**: a tab closed in the other window disappears from this
 *   one, and this is the only rule in `reconcile` that can *remove* anything.
 * - **A close made offline is not undone by the next pull.** This is the case
 *   that decides whether the removal queue is a real design or a comment: the
 *   engine still holds the row, so a window that reads it before pushing its
 *   own close would resurrect the tab the user closed while the engine was down.
 * - **The same tab open in both windows, navigated in one.** The window that is
 *   looking at it wins, and the push tells the engine; this is not a conflict
 *   error, because there is no authority to appeal to.
 * - **A row from another window is not trusted more than one from a stranger.**
 *   Two windows are ordinary, so anything one of them can write the other must be
 *   able to refuse — a key this app did not mint, an address the shell would
 *   refuse, a history whose cursor lies.
 * - **An unchanged strip costs one read.** Without that, a poll would write every
 *   tab every few seconds, which is the difference between sharing a strip and
 *   hammering the engine with it.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { registerTsx } from "./tsxLoader.ts";
// Types statically (erased whole, so they can be hoisted), values dynamically
// (hoisted imports resolve before `registerTsx` runs — the rule `tabs.test.ts`
// states). `type X` inside a destructuring pattern is a syntax error to
// `--experimental-strip-types`, so the two kinds of import cannot share a
// statement.
import type { TabStorage } from "../src/tabPersistence.ts";
import type { TabState } from "../src/tabs.ts";
import type { EngineTab } from "../src/layoutSync.ts";

registerTsx();

const {
  acknowledge,
  activeKeyOf,
  decodeStrip,
  decodeTab,
  emptySharedState,
  encodeTab,
  ensureKeys,
  layoutFrom,
  markPending,
  newMirror,
  planChanges,
  queueRemoval,
  readLayoutMirror,
  reconcile,
  tabKey,
  writeLayoutMirror,
} = await import("../src/layoutSync.ts");
const { closeTab, focusTab, openBrowserTab, openTab } = await import("../src/tabs.ts");

/** A store in memory, so the mirror is testable without a `localStorage`. */
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

const A = "https://a.example/one";
const B = "https://b.example/two";

/** A strip with keys on every tab, as the sync effect leaves it. */
function keyed(state: TabState): TabState {
  return ensureKeys(state).state;
}

/** A strip of browser tabs, keyed, in the given order. */
function strip(...urls: string[]): TabState {
  let state = emptySharedState();
  for (const url of urls) state = openBrowserTab(state, `id-${urls.indexOf(url)}`, url);
  return keyed(state);
}

/** What the engine would answer with for a strip, as rows. */
function rowsFor(state: TabState): EngineTab[] {
  return state.tabs
    .map((tab, position) => ({
      key: tab.key ?? "",
      position,
      kind: tab.kind === "chat" ? ("chat" as const) : ("browser" as const),
      payload: encodeTab(tab),
    }))
    .filter((row) => row.key !== "");
}

// ── keys ────────────────────────────────────────────────────────────────────

test("every remembered tab gets a key, and a terminal does not", () => {
  let state = openBrowserTab(emptySharedState(), "b1", A);
  state = openTab(state, {
    id: "pty-1",
    kind: "terminal",
    title: "Terminal",
    ptyId: "pty-1",
  });
  const { state: next, minted } = ensureKeys(state);
  assert.equal(minted.length, 1, "only the browser tab is remembered, so only it is keyed");
  assert.ok(next.tabs[0].key, "a tab that syncs needs an identity");
  assert.equal(
    next.tabs[1].key,
    undefined,
    "a PTY is a live process and is never shared, so it needs no key",
  );
});

test("keying is idempotent, and a keyed strip is not re-created", () => {
  const once = keyed(strip(A));
  const twice = ensureKeys(once);
  assert.equal(twice.minted.length, 0, "keys are minted once, not on every change");
  assert.equal(
    twice.state,
    once,
    "an unchanged strip comes back as the same object, so a caller can skip a re-render",
  );
});

test("two keys are never equal, and a key is not a tab id", () => {
  const keys = new Set(Array.from({ length: 200 }, () => tabKey()));
  assert.equal(keys.size, 200, "the same millisecond is the ordinary case, not the exotic one");
  for (const key of keys) {
    assert.match(key, /^k_/, "a key must not be mistakable for a per-process Tab.id");
  }
});

// ── the wire shape ──────────────────────────────────────────────────────────

test("a browser tab's payload carries its address and its stack, and no title", () => {
  const state = keyed(strip(A));
  const wire = JSON.parse(encodeTab(state.tabs[0])) as Record<string, unknown>;
  assert.equal(wire.url, A);
  assert.deepEqual(wire.history, { entries: [A], index: 0 });
  assert.equal(
    wire.title,
    undefined,
    "a title is re-derived from the page, and a stored one is stale the moment it loads",
  );
});

test("a chat tab's payload carries its thread and not a URL", () => {
  const state = keyed(
    openTab(emptySharedState(), {
      id: "c1",
      kind: "chat",
      title: "New thread",
      conversationId: "conv-1",
    }),
  );
  const wire = JSON.parse(encodeTab(state.tabs[0])) as Record<string, unknown>;
  assert.equal(wire.kind, "chat");
  assert.equal(wire.conversationId, "conv-1");
  assert.equal(wire.url, undefined, "a chat tab has no address to carry");
});

test("a row round-trips through the wire unchanged", () => {
  const state = keyed(strip(A, B));
  const rows = rowsFor(focusTab(state, state.tabs[1].id));
  const back = reconcile(emptySharedState(), rows, null, []);
  assert.equal(back.tabs.length, 2);
  assert.deepEqual(
    back.tabs.map((t) => t.url),
    [A, B],
    "the strip comes back in the order the engine held it",
  );
  assert.equal(back.tabs[1].url, B, "on the address it was showing");
});

// ── what a row from another window may not do ───────────────────────────────

test("a row this app did not mint is refused", () => {
  // A key is how the engine is told *which* tab, so a key from elsewhere is a
  // row that cannot be matched, updated or closed by name. Two windows are
  // ordinary; a stranger with a database is not, and gets nothing either way.
  for (const key of ["", "tab-1", "k", "x".repeat(65), "K_upper"]) {
    const row: EngineTab = { key, position: 0, kind: "chat", payload: "{}" };
    assert.equal(decodeTab(row), null, `${JSON.stringify(key)} must not be adopted`);
  }
});

test("a kind that is not one of the two is refused", () => {
  for (const kind of ["terminal", "window", ""] as const) {
    const row = { key: tabKey(), position: 0, kind, payload: "{}" } as unknown as EngineTab;
    assert.equal(decodeTab(row), null);
  }
});

test("an address the shell would refuse does not come back as a tab", () => {
  // The same rule the mirror applies, and the reason it is restated here rather
  // than trusted: this is a *second* window's row, and the page it describes
  // would be seated in this one.
  for (const hostile of [
    "javascript:alert(1)",
    "file:///etc/passwd",
    "http://127.0.0.1:7430/health",
    "http://localhost:5173/",
    "http://2130706433/",
  ]) {
    const row: EngineTab = {
      key: tabKey(),
      position: 0,
      kind: "browser",
      payload: JSON.stringify({ kind: "browser", url: hostile }),
    };
    assert.equal(decodeTab(row), null, `${hostile} must not be adopted from the engine`);
  }
});

test("a payload that is not JSON, or not an object, is refused", () => {
  for (const payload of ["not json {", "[]", '"a string"', "null", "7"]) {
    const row: EngineTab = { key: tabKey(), position: 0, kind: "chat", payload };
    assert.equal(decodeTab(row), null, `${payload} must not be adopted`);
  }
});

test("a history that lies about the address bar is dropped, and the tab kept", () => {
  const row: EngineTab = {
    key: tabKey(),
    position: 0,
    kind: "browser",
    payload: JSON.stringify({
      kind: "browser",
      url: A,
      history: { entries: [A, B], index: 1 },
    }),
  };
  const shared = decodeTab(row);
  assert.ok(shared, "the page itself is a page this window could show");
  assert.equal(
    shared.tab.history,
    undefined,
    "the stack is dropped rather than repaired: a back button that disagrees \
     with the address bar is worse than no back button",
  );
});

test("one bad row does not cost the strip its other rows", () => {
  const good = rowsFor(strip(A, B));
  const rows: EngineTab[] = [
    { key: "not-a-key", position: 0, kind: "chat", payload: "{}" },
    ...good,
    { key: tabKey(), position: 9, kind: "browser", payload: "broken" },
  ];
  const shared = decodeStrip(rows);
  assert.equal(shared.length, 2, "a refused row is dropped, not fatal");
  assert.deepEqual(
    shared.map((s) => s.tab.url),
    [A, B],
  );
});

// ── two windows ─────────────────────────────────────────────────────────────

test("a tab the other window opened shows up here", () => {
  // The reason any of this exists. `known` is both windows' keys, which is what
  // a real second window has: the shared keys have been through a response, so
  // this window knows them and a missing one would be a close.
  const here = strip(A);
  const there = keyed(
    openTab(focusTab(here, here.tabs[0].id), {
      id: "id-2",
      key: tabKey(),
      kind: "browser",
      title: "b.example",
      url: B,
      history: { entries: [B], index: 0 },
    }),
  );
  const next = reconcile(here, rowsFor(there), activeKeyOf(here), [], knownKeys(here, there));
  assert.deepEqual(
    next.tabs.map((t) => t.url),
    [A, B],
    "the tab this window never opened is adopted",
  );
  assert.equal(
    next.tabs[1].key,
    there.tabs[1].key,
    "and it keeps the identity the other window gave it, or the next sync would \
     treat one tab as two",
  );
});

test("an adopted tab gets a fresh local id, because it has no webview here", () => {
  const here = strip(A);
  const there = keyed(
    openTab(here, {
      id: "id-2",
      key: tabKey(),
      kind: "browser",
      title: "b.example",
      url: B,
      history: { entries: [B], index: 0 },
    }),
  );
  const next = reconcile(here, rowsFor(there), activeKeyOf(here), [], knownKeys(here, there));
  const adopted = next.tabs[1];
  assert.notEqual(adopted.id, there.tabs[1].id, "`id` names this window's webview");
  assert.ok(adopted.id.length > 0);
});

test("a row whose key this window never issued is somebody else's tab", () => {
  // The flip of the "window wins" rule, and the reason a key is minted once and
  // never on arrival: two rows for one address are two tabs, because a key is
  // the only thing that says "the same tab". An unconfirmed local tab is kept
  // (see the empty-engine test), so the pair ends as two tabs pointing at one
  // page rather than one tab and a lost one — and the next push teaches the
  // engine about the local key, after which the duplicate is visible as the
  // anomaly it is rather than silently merged away.
  const here = strip(A);
  const theirs = strip(A, B);
  const next = reconcile(here, rowsFor(theirs), activeKeyOf(here), [], new Set());
  assert.equal(next.tabs.length, 3, "this window's own tab, plus the other window's two");
  assert.deepEqual(next.tabs.map((t) => t.url), [A, A, B]);
});

test("a pull that changes nothing returns the strip it was given", () => {
  // The contract that keeps the pull from looping. The app reconciles inside
  // `setTabState((prev) => ...)`, so a merge that changes nothing must hand
  // React back the very state it had: a rebuilt-but-equal strip re-renders the
  // app, which re-fires the push effect, which pulls again — the loop that kept
  // `tests/terminalEndToEnd.test.ts` pending forever and would have quietly
  // request-looped against a real engine. Rows mirroring this window's own tabs
  // are exactly the trap: every decode mints a fresh `id`, so a naive rebuild
  // of the strip is *always* unequal to it.
  const mine = strip(A, B);
  const again = reconcile(mine, rowsFor(mine), activeKeyOf(mine), [], knownKeys(mine));
  assert.equal(again, mine, "the engine already says what this window shows — no new state");
  // The empty answer against an engine that has never confirmed anything is
  // also a no-op: nothing to adopt, nothing to retire.
  const untouched = reconcile(mine, [], activeKeyOf(mine), [], new Set());
  assert.equal(untouched, mine, "silence from an engine that knows nothing changes nothing");
});

test("a tab closed in the other window closes here too", () => {
  const before = strip(A, B);
  const after = strip(A); // the other window closed B
  // The engine had confirmed both keys a moment ago, so B's absence is a close
  // and not merely "not pushed yet".
  const next = reconcile(
    before,
    rowsFor(after),
    activeKeyOf(before),
    [],
    new Set(before.tabs.map((t) => t.key!)),
  );
  assert.deepEqual(
    next.tabs.map((t) => t.url),
    [A],
    "a close is shared, which is what shared means",
  );
});

test("an engine that has never heard of this window's tabs does not empty it", () => {
  // The bug this qualification exists for, and it is a data-loss one rather
  // than a cosmetic one: a window with a full mirror against a *fresh* engine
  // (a new `codify.db`, or one nobody has told anything to) would read the
  // empty strip as "every tab was closed" and delete the user's strip. The
  // mirror exists so the strip comes back when the engine does not answer, so it
  // must not be the thing the engine's emptiness takes away.
  const mine = strip(A, B);
  const next = reconcile(mine, [], activeKeyOf(mine), [], new Set());
  assert.deepEqual(
    next.tabs.map((t) => t.url),
    [A, B],
    "an unconfirmed key is kept and pushed, never dropped",
  );
  assert.equal(next.activeId, mine.activeId, "and the tab being read is still the tab");
});

test("a close another window made while this one was shut survives one boot", () => {
  // The honest cost of the rule above, stated as a test so it cannot be
  // "fixed" by accident: a key the engine has never confirmed cannot be retired
  // by an absence, so a tab closed elsewhere while this window was closed stays
  // in the mirror for one boot. The safe direction — the other failure is tabs
  // vanishing — and the way to close the gap is a boot-time retirement against
  // a strip the engine has confirmed, not a rule that trusts emptiness.
  const mine = strip(A, B);
  // The other window's view: the same strip, minus the tab it closed, so the
  // keys that survive are the ones this window also has.
  const after = {
    tabs: [{ ...mine.tabs[0] }],
    activeId: mine.tabs[0].id,
  };
  const next = reconcile(mine, rowsFor(after), activeKeyOf(mine), [], new Set());
  assert.deepEqual(
    next.tabs.map((t) => t.url),
    [A, B],
    "kept until the engine has confirmed this window's own view of the strip",
  );
});

test("the tab this window was showing, closed elsewhere, hands over to its left", () => {
  const before = strip(A, B);
  const showing = before.tabs[1].id;
  const focused = focusTab(before, showing);
  const after = strip(A); // the other window closed the tab we were looking at
  const next = reconcile(
    focused,
    rowsFor(after),
    activeKeyOf(focused),
    [],
    new Set(before.tabs.map((t) => t.key!)),
  );
  assert.equal(next.tabs.length, 1);
  assert.equal(
    next.activeId,
    next.tabs[0].id,
    "focus lands on the neighbour to the left, as a closed tab does in one window",
  );
});

test("the window that is looking at a tab wins it, and the push settles it", () => {
  // Both windows have the *same* tab open — which means the same `key`, because
  // that is what makes it the same tab rather than two that happen to point at
  // one address — at different addresses. There is no authority to appeal to,
  // so the rule is a chosen one: the window looking at it is the authority on
  // what it is looking at, and its push is what the other window converges on.
  const here = strip(A);
  const there = {
    tabs: [{ ...here.tabs[0], url: B, history: { entries: [B], index: 0 } }],
    activeId: here.tabs[0].id,
  };
  const next = reconcile(here, rowsFor(there), activeKeyOf(here), []);
  assert.equal(next.tabs.length, 1, "one tab, not two");
  assert.equal(next.tabs[0].url, A, "this window keeps showing its own address");
  const plan = planChanges(here, newMirror(), new Set([here.tabs[0].key!]));
  assert.deepEqual(
    plan.upserts,
    [],
    "and it had already told the engine, so there is nothing to re-say",
  );
});

/** Every key two windows have been through a response with. */
function knownKeys(...states: TabState[]): Set<string> {
  return new Set(states.flatMap((state) => state.tabs.map((t) => t.key!)));
}

test("an unacknowledged tab is still pushed, because silence is not a yes", () => {
  const here = strip(A);
  const mirror = newMirror();
  const withPending = { ...mirror, pendingWrites: [here.tabs[0].key!] };
  const plan = planChanges(here, withPending, new Set([here.tabs[0].key!]));
  assert.equal(plan.upserts.length, 1, "a write the engine never confirmed is retried");
});

test("a close made offline is not undone by the next read", () => {
  // The case that decides whether the removal queue is real. The engine still
  // holds the row, because the DELETE could not be sent; a window that adopted
  // that row on its next pull would put back the tab the user closed — so its own
  // pending removals are held back through `reconcile`.
  const before = strip(A, B);
  const closing = before.tabs[1];
  const closed = focusTab(before, before.tabs[0].id);
  const mirror = { ...newMirror(), pendingRemovals: [closing.key!] };
  const next = reconcile(
    closed,
    rowsFor(before),
    activeKeyOf(closed),
    mirror.pendingRemovals,
    new Set(before.tabs.map((t) => t.key!)),
  );
  assert.deepEqual(
    next.tabs.map((t) => t.url),
    [A],
    "the tab the user closed offline stays closed, even though the engine still \
     has it",
  );
});

test("a pending removal is dropped once the engine no longer holds it", () => {
  const before = strip(A, B);
  const closed = before.tabs[1].key!;
  const mirror = {
    ...newMirror(),
    pendingRemovals: [closed],
    pendingWrites: [before.tabs[0].key!],
  };
  // The plan asks for the delete (the engine still has the key), and the
  // settled mirror is written from what the engine *answers* — which no longer
  // has it. That is the moment the close stops being owed.
  const plan = planChanges(before, mirror, new Set([closed, before.tabs[0].key!]));
  assert.deepEqual(plan.removals, [closed], "and the delete is the request");
  const settled = acknowledge(mirror, plan, new Set([before.tabs[0].key!]));
  assert.deepEqual(settled.pendingRemovals, [], "the close is no longer pending");
  assert.deepEqual(settled.pendingWrites, [], "and neither is the push");
});

test("a close the engine never had is settled, not retried forever", () => {
  // The leak this shape is for: another window closed that tab, so this window's
  // queued removal names a key the engine does not have. Retrying it forever
  // grows a queue that nothing ever drains, and a mirror that gets slower to
  // write on every change.
  const before = strip(A, B);
  const closed = before.tabs[1].key!;
  const mirror = { ...newMirror(), pendingRemovals: [closed] };
  const state = focusTab(before, before.tabs[0].id);
  const plan = planChanges(state, mirror, new Set([before.tabs[0].key!]));
  assert.deepEqual(plan.removals, [], "there is nothing to delete — it is already gone");
  assert.deepEqual(
    acknowledge(mirror, plan, new Set([before.tabs[0].key!])).pendingRemovals,
    [],
    "and the queue does not keep it forever",
  );
});

test("a pending removal is retried while the engine still has the key", () => {
  const state = strip(A, B);
  const closed = state.tabs[1].key!;
  const mirror = { ...newMirror(), pendingRemovals: [closed] };
  // `known` still holds the key, which is what "the engine has it and we have
  // not told it otherwise" looks like from here.
  const plan = planChanges(
    focusTab(state, state.tabs[0].id),
    mirror,
    new Set([closed, state.tabs[0].key!]),
  );
  assert.deepEqual(plan.removals, [closed], "a close that was not sent is still owed");
});

// ── the cost model ──────────────────────────────────────────────────────────

test("an unchanged strip costs one read and no writes", () => {
  const state = strip(A, B);
  const known = new Set(state.tabs.map((t) => t.key!));
  const plan = planChanges(state, newMirror(), known);
  assert.deepEqual(plan.upserts, [], "nothing changed, so nothing is said");
  assert.deepEqual(plan.removals, []);
});

test("a terminal tab is never pushed, and a tab without a key is not either", () => {
  let state = openBrowserTab(emptySharedState(), "b1", A);
  state = openTab(state, {
    id: "pty-1",
    kind: "terminal",
    title: "Terminal",
    ptyId: "pty-1",
  });
  const unkeyed = state; // deliberately not through ensureKeys
  const plan = planChanges(unkeyed, newMirror(), new Set());
  assert.equal(plan.upserts.length, 0, "a tab with no identity cannot be named to the engine");
  const keyedPlan = planChanges(keyed(unkeyed), newMirror(), new Set());
  assert.equal(keyedPlan.upserts.length, 1, "the browser tab, and only it");
  assert.equal(keyedPlan.upserts[0].position, 0, "at the position it holds in the strip");
});

test("a tab's position is its place in the strip, gaps and all", () => {
  const state = strip(A, B);
  const plan = planChanges(state, newMirror(), new Set());
  assert.deepEqual(
    plan.upserts.map((row) => row.position),
    [0, 1],
  );
});

// ── the mirror ──────────────────────────────────────────────────────────────

test("the mirror survives a store, and an older shape is not believed", () => {
  const store = memoryStore();
  const state = strip(A);
  const mirror = { ...newMirror(), layout: layoutFrom(state) };
  writeLayoutMirror(store, mirror);
  const back = readLayoutMirror(store);
  assert.deepEqual(back.layout, mirror.layout, "the strip round-trips through the mirror");
  // A mirror from a version that meant something else is an empty mirror, not a
  // misread one: the alternative is a window opening tabs nobody asked for.
  const old = memoryStore({ CODIFY_TABS: JSON.stringify({ version: 0, layout: mirror.layout }) });
  assert.deepEqual(readLayoutMirror(old).layout.tabs, []);
  // And a blob that is not a mirror at all.
  assert.deepEqual(readLayoutMirror(memoryStore({ CODIFY_TABS: "{" })).layout.tabs, []);
  assert.deepEqual(
    readLayoutMirror(memoryStore({ CODIFY_TABS: '"a string"' })).layout.tabs,
    [],
  );
});

test("the mirror queues survive it too, which is what makes a close durable", () => {
  const store = memoryStore();
  const state = strip(A);
  const key = state.tabs[0].key!;
  writeLayoutMirror(store, {
    ...newMirror(),
    layout: layoutFrom(state),
    pendingRemovals: [key],
  });
  assert.deepEqual(readLayoutMirror(store).pendingRemovals, [key]);
  // A queue entry that is not a key this app minted is dropped, because it is a
  // `DELETE` that would be sent to the engine.
  writeLayoutMirror(store, { ...newMirror(), pendingRemovals: ["rm -rf /"] });
  assert.deepEqual(readLayoutMirror(store).pendingRemovals, []);
});

test("a store that refuses costs the strip, not the window", () => {
  const hostile: TabStorage = {
    getItem: () => {
      throw new Error("off");
    },
    setItem: () => {
      throw new Error("off");
    },
  };
  assert.deepEqual(readLayoutMirror(hostile).layout.tabs, []);
  assert.doesNotThrow(() =>
    writeLayoutMirror(hostile, { ...newMirror(), pendingRemovals: ["k_x"] }),
  );
});

// ── the wiring in App.tsx ───────────────────────────────────────────────────

function appCode(): string {
  return readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}

test("the sync is wired to the three engine calls, and to nothing else", () => {
  const code = appCode();
  for (const call of ["listShellTabs", "upsertShellTab", "deleteShellTab"]) {
    assert.match(code, new RegExp(`${call}\\(`), `${call} is never called from the window`);
  }
  // The engine is not the mirror: both writes have to happen, and a change that
  // only reached one of them is a strip that is a restart old *or* a strip the
  // other window never sees.
  assert.match(code, /writeLayoutMirror\(layoutStorage\(\),/);
  assert.match(code, /readLayoutMirror\(layoutStorage\(\)\)/);
  // A close reaches the queue from the handler the tab's × calls; the tests
  // below prove what the queue does, this only proves the app feeds it.
  assert.match(code, /queueRemoval\(layoutMirror\.current, tab\.key\)/);
  // And the engine is asked for the strip when the window is next looked at,
  // which is the difference between shared and shared-on-write.
  assert.match(code, /addEventListener\("visibilitychange", onVisible\)/);
  assert.match(code, /addEventListener\("focus", onVisible\)/);
  // A no-op is the point of the cost model, so the plan must be computed against
  // what the engine is known to hold.
  assert.match(
    code,
    /planChanges\(\s*tabState,\s*layoutMirror\.current,\s*engineKnown\.current,\s*engineRows\.current,?\s*\)/,
    "the plan is not computed against the keys the engine has confirmed, so an \
     unchanged strip is re-sent in full",
  );
});

test("the three calls exist in api.ts, each with the boot token", () => {
  const api = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
  for (const name of ["listShellTabs", "upsertShellTab", "deleteShellTab"]) {
    const start = api.indexOf(`export async function ${name}`);
    assert.ok(start >= 0, `api.ts has no ${name}`);
    // To the *next* exported function, not to the first `}`: a body with a
    // nested object (the headers) would be cut in half and the token assertion
    // would pass on a function that sends nothing.
    const next = api.indexOf("\nexport ", start + 1);
    const body = api.slice(start, next < 0 ? undefined : next);
    // docs/00 §6.3: the engine binds loopback and requires the token on
    // everything. A new route is not an exemption, and a new *call* is where that
    // would be quietly dropped.
    assert.match(
      body,
      /Authorization: `Bearer \$\{currentEngine\.token\}`/,
      `${name} does not send the boot token`,
    );
  }
  assert.match(api, /method: "PUT"/, "there is no way to write a tab");
  assert.match(api, /method: "DELETE"/, "and no way to close one");
  // The path is escaped: a key is client-minted, and an unescaped one is a path
  // that can be rewritten by whatever is in it.
  assert.match(api, /encodeURIComponent\(key\)/, "the key is not escaped into the path");
});

// ── the queues are filled by the code that closes and navigates ────────────
//
// Every test above builds `pendingRemovals` / `pendingWrites` by hand, which is
// how a strip could pass all of them while nothing in the app ever put a key in
// either list. These go through `queueRemoval` and `markPending`, the calls the
// app makes, and ask what the *next boot and the next pull* would see.

test("a tab the user closes stays closed through the next pull, once queued", () => {
  const before = strip(A, B);
  const closing = before.tabs[1];
  const after = closeTab(before, closing.id);
  const known = new Set(before.tabs.map((t) => t.key!));

  const mirror = queueRemoval(newMirror(), closing.key);
  assert.deepEqual(mirror.pendingRemovals, [closing.key]);

  // The engine still holds both rows: the DELETE has not been sent yet.
  const pulled = reconcile(after, rowsFor(before), activeKeyOf(after), mirror.pendingRemovals, known);
  assert.deepEqual(pulled.tabs.map((t) => t.url), [A], "the closed tab is not adopted back");

  const plan = planChanges(after, mirror, known);
  assert.deepEqual(plan.removals, [closing.key], "and the engine is told to delete it");
});

test("without queueing the close, the engine's copy brings the tab back", () => {
  // The bug, pinned: a close that never reaches the queue is only an absence, and
  // an absence is what `reconcile` fills from the engine's row.
  const before = strip(A, B);
  const after = closeTab(before, before.tabs[1].id);
  const known = new Set(before.tabs.map((t) => t.key!));
  const pulled = reconcile(after, rowsFor(before), activeKeyOf(after), [], known);
  assert.deepEqual(pulled.tabs.map((t) => t.url), [A, B]);
});

test("queueing a close is idempotent and ignores what is not a remembered tab", () => {
  const key = strip(A).tabs[0].key!;
  const once = queueRemoval(newMirror(), key);
  assert.deepEqual(queueRemoval(once, key).pendingRemovals, [key]);
  assert.equal(queueRemoval(once, undefined), once, "an unkeyed tab owes the engine nothing");
  assert.equal(queueRemoval(once, "pty-1"), once, "a terminal was never the engine's");
});

test("a tab that navigates after its first push is sent again", () => {
  const first = strip(A);
  const key = first.tabs[0].key!;
  const known = new Set([key]);
  const navigated = { tabs: [{ ...first.tabs[0], url: B, history: undefined }], activeId: first.activeId };

  const unchanged = planChanges(first, newMirror(), known, rowsFor(first));
  assert.equal(unchanged.upserts.length, 0, "an unchanged strip still costs nothing");

  const changed = planChanges(navigated, newMirror(), known, rowsFor(first));
  assert.deepEqual(changed.upserts.map((u) => u.key), [key], "a changed address is a write");
});

test("an upsert that was planned but never answered is owed on the next run", () => {
  const state = strip(A);
  const key = state.tabs[0].key!;
  const known = new Set([key]);
  const navigated = { tabs: [{ ...state.tabs[0], url: B, history: undefined }], activeId: state.activeId };

  const plan = planChanges(navigated, newMirror(), known, rowsFor(state));
  const owed = markPending(newMirror(), plan);
  assert.deepEqual(owed.pendingWrites, [key]);

  // The request died. The engine still holds the old payload, but nothing tells
  // the next run *that*, except the pending mark.
  const retry = planChanges(navigated, owed, known);
  assert.deepEqual(retry.upserts.map((u) => u.key), [key]);
  assert.deepEqual(acknowledge(owed, plan, known).pendingWrites, [], "answered, it is settled");
});
