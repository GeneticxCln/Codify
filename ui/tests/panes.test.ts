/**
 * Split panes, the rules: which two tabs may sit side by side, which one a split starts with, when a split is showing, what
 * becomes of it when the active tab moves, and how far its divider may go.
 *
 * Pure like `tabs.ts`, so each rule is a test that needs no renderer. The split is `{ panes, focused }` beside
 * the tab state and never in it, and the active tab is the focused pane's: the rules below keep those two in step.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Tab, TabState } from "../src/tabs.ts";

const {
  DEFAULT_RATIO,
  MIN_PANE_REM,
  PAIR_REFUSALS,
  canPair,
  clampRatio,
  closeInSplit,
  pairRefusal,
  resolveSplit,
  splitPartner,
  startSplit,
} = await import("../src/panes.ts");

const chat = (id: string): Tab => ({ id, kind: "chat", title: id });
const term = (id: string, over: Partial<Tab> = {}): Tab => ({ id, kind: "terminal", title: id, ...over });
const page = (id: string): Tab => ({ id, kind: "browser", title: id });
const tab = (id: string, kind: Tab["kind"]): Tab => ({ id, kind, title: id });
const strip = (tabs: Tab[], activeId: string | null): TabState => ({ tabs, activeId });

// ── which two may sit together ──────────────────────────────────────────────

test("a chat and a terminal, and two terminals, may share the column, in either order", () => {
  assert.equal(canPair(chat("c"), term("t")), true);
  assert.equal(canPair(term("t"), chat("c")), true);
  assert.equal(canPair(term("a"), term("b")), true);
});

test("two conversations may not: they would share one message box", () => {
  assert.equal(pairRefusal(chat("a"), chat("b")), "two-chats");
  assert.equal(canPair(chat("a"), chat("b")), false);
});

test("a browser page may sit beside a chat, a terminal or an editor, in either order", () => {
  for (const other of [chat("c"), term("t"), tab("e", "editor")]) {
    assert.equal(canPair(page("p"), other), true);
    assert.equal(canPair(other, page("p")), true);
  }
});

test("two browser pages may not: each is a native view seated over one rectangle of the window", () => {
  assert.equal(pairRefusal(page("p"), page("p2")), "two-pages");
  assert.equal(canPair(page("p"), page("p2")), false);
});

test("a tab cannot be paired with itself, and a tab that is not there cannot be paired", () => {
  assert.equal(pairRefusal(chat("a"), chat("a")), "same");
  assert.equal(pairRefusal(term("t"), term("t")), "same");
  assert.equal(pairRefusal(undefined, chat("a")), "missing");
  assert.equal(pairRefusal(chat("a"), undefined), "missing");
});

test("the reasons are checked in an order that names the real obstacle", () => {
  // A browser page beside itself is "the same tab", not a two-pages problem; a missing tab beats everything.
  assert.equal(pairRefusal(page("p"), page("p")), "same");
  assert.equal(pairRefusal(undefined, page("p")), "missing");
  assert.equal(pairRefusal(page("p"), page("q")), "two-pages");
  assert.equal(pairRefusal(chat("a"), chat("b")), "two-chats");
});

test("every refusal has a sentence, and it says why", () => {
  assert.match(PAIR_REFUSALS["two-pages"], /two browser pages/i);
  assert.match(PAIR_REFUSALS["two-pages"], /native/i);
  assert.match(PAIR_REFUSALS["two-chats"], /two conversations/i);
  assert.match(PAIR_REFUSALS["two-chats"], /message box/i);
  assert.match(PAIR_REFUSALS.same, /already/i);
  assert.match(PAIR_REFUSALS.missing, /tab/i);
});

// ── starting one ────────────────────────────────────────────────────────────

test("a split starts with the active tab on the left and the other on the right, and the right one is focused", () => {
  const state = strip([chat("c"), term("t"), chat("c2")], "c");
  const out = startSplit(state, "t");
  assert.ok(out.ok);
  assert.deepEqual(out.split, { panes: ["c", "t"], focused: 1 });
  assert.equal(out.state.activeId, "t", "the active tab is the focused pane's");
  assert.deepEqual(out.state.tabs, state.tabs, "starting a split opens and closes nothing");
});

test("starting a split does not change the state it was given", () => {
  const state = strip([chat("c"), term("t")], "c");
  const before = JSON.stringify(state);
  startSplit(state, "t");
  assert.equal(JSON.stringify(state), before);
});

test("a split that cannot be made says why, and changes nothing", () => {
  const state = strip([chat("c"), chat("c2"), page("p"), page("p2"), term("t")], "c");
  assert.deepEqual(startSplit(state, "c2"), { ok: false, why: "two-chats" });
  assert.deepEqual(startSplit(strip(state.tabs, "p"), "p2"), { ok: false, why: "two-pages" });
  assert.deepEqual(startSplit(state, "c"), { ok: false, why: "same" });
  assert.deepEqual(startSplit(state, "nope"), { ok: false, why: "missing" });
  assert.deepEqual(startSplit(strip([term("t")], null), "t"), { ok: false, why: "missing" });
});

test("a browser page can be shown beside the active tab, and a page can be the active tab being split from", () => {
  const beside = startSplit(strip([chat("c"), page("p")], "c"), "p");
  assert.ok(beside.ok);
  assert.deepEqual(beside.split, { panes: ["c", "p"], focused: 1 });
  assert.equal(beside.state.activeId, "p");

  const from = startSplit(strip([page("p"), term("t")], "p"), "t");
  assert.ok(from.ok);
  assert.deepEqual(from.split, { panes: ["p", "t"], focused: 1 });
});

// ── who to split with ───────────────────────────────────────────────────────

test("a chat is split with the nearest terminal, whichever side it is on", () => {
  assert.deepEqual(splitPartner(strip([term("t0"), chat("c"), chat("c2"), term("t1"), term("t2")], "c")), { kind: "tab", id: "t0" });
  assert.deepEqual(splitPartner(strip([chat("c"), chat("c2"), term("t1"), term("t2")], "c")), { kind: "tab", id: "t1" });
  assert.deepEqual(splitPartner(strip([term("t0"), chat("c"), chat("c2")], "c")), { kind: "tab", id: "t0" });
});

test("distance beats direction, and a tie goes to the right", () => {
  assert.deepEqual(splitPartner(strip([term("t0"), chat("x"), chat("c"), chat("y"), term("t1")], "c")), { kind: "tab", id: "t1" }, "equidistant: right wins");
  assert.deepEqual(splitPartner(strip([term("t0"), chat("c"), chat("y"), chat("z"), term("t1")], "c")), { kind: "tab", id: "t0" }, "left is nearer");
});

test("a chat with no terminal to share with asks for a new one, never another chat", () => {
  assert.deepEqual(splitPartner(strip([chat("c"), chat("c2")], "c")), { kind: "new-terminal" });
  assert.deepEqual(splitPartner(strip([chat("c")], "c")), { kind: "new-terminal" });
});

test("a terminal is split with the nearest other terminal, else the nearest chat, else a new terminal", () => {
  assert.deepEqual(splitPartner(strip([chat("c"), term("a"), term("b")], "a")), { kind: "tab", id: "b" });
  assert.deepEqual(splitPartner(strip([term("a"), chat("c"), chat("c2")], "a")), { kind: "tab", id: "c" });
  assert.deepEqual(splitPartner(strip([term("a")], "a")), { kind: "new-terminal" });
});

test("a shell that has exited is never picked for you: it is not what you meant", () => {
  assert.deepEqual(splitPartner(strip([chat("c"), term("dead", { exited: true })], "c")), { kind: "new-terminal" });
  assert.deepEqual(splitPartner(strip([chat("c"), term("dead", { exited: true }), term("live")], "c")), { kind: "tab", id: "live" });
  assert.deepEqual(splitPartner(strip([term("a"), term("dead", { exited: true }), chat("c")], "a")), { kind: "tab", id: "c" });
});

test("a chat, a terminal or an editor may be split with a browser page, whichever is nearest", () => {
  assert.deepEqual(splitPartner(strip([chat("c"), page("p"), term("t")], "c")), { kind: "tab", id: "p" }, "the page is nearer than the terminal");
  assert.deepEqual(splitPartner(strip([chat("c"), term("t"), page("p")], "c")), { kind: "tab", id: "t" }, "the terminal is nearer than the page");
  assert.deepEqual(splitPartner(strip([chat("c"), chat("c2"), page("p")], "c")), { kind: "tab", id: "p" }, "a page is a partner when there is no terminal");
  assert.deepEqual(splitPartner(strip([term("a"), page("p")], "a")), { kind: "tab", id: "p" }, "a terminal with no other terminal or chat takes a page");
  assert.deepEqual(splitPartner(strip([term("a"), page("p"), chat("c")], "a")), { kind: "tab", id: "c" }, "a chat comes before a page for a terminal");
});

test("a browser page is split with the nearest chat, then the nearest live terminal, then an editor, else a new terminal", () => {
  assert.deepEqual(splitPartner(strip([term("t"), page("p"), chat("c")], "p")), { kind: "tab", id: "c" });
  assert.deepEqual(splitPartner(strip([chat("c"), term("t"), page("p")], "p")), { kind: "tab", id: "c" }, "a chat beats a nearer terminal");
  assert.deepEqual(splitPartner(strip([term("dead", { exited: true }), page("p"), term("t")], "p")), { kind: "tab", id: "t" });
  assert.deepEqual(splitPartner(strip([tab("e", "editor"), page("p")], "p")), { kind: "tab", id: "e" });
  assert.deepEqual(splitPartner(strip([page("p"), page("q")], "p")), { kind: "new-terminal" }, "never another page");
  assert.deepEqual(splitPartner(strip([page("p")], "p")), { kind: "new-terminal" });
});

test("nothing open has no partner and says why", () => {
  assert.deepEqual(splitPartner(strip([], null)), { kind: "refused", why: "missing" });
});

// ── when a split is showing ─────────────────────────────────────────────────

test("no split resolves to none", () => {
  assert.deepEqual(resolveSplit(strip([chat("c")], "c"), null), { shown: null, split: null });
});

test("a split is showing while the active tab is one of its two, and the active one is the focused one", () => {
  const tabs = [chat("c"), term("t"), chat("other")];
  const split = { panes: ["c", "t"] as const, focused: 1 as const };

  const onRight = resolveSplit(strip(tabs, "t"), split);
  assert.ok(onRight.shown);
  assert.deepEqual([onRight.shown.left.id, onRight.shown.right.id, onRight.shown.focused], ["c", "t", 1]);

  const onLeft = resolveSplit(strip(tabs, "c"), split);
  assert.ok(onLeft.shown);
  assert.deepEqual([onLeft.shown.left.id, onLeft.shown.right.id, onLeft.shown.focused], ["c", "t", 0]);
  assert.deepEqual(onLeft.split, { panes: ["c", "t"], focused: 0 }, "the stored focus follows the active tab");
});

test("when nothing changed, the very same split comes back, so no effect loops on it", () => {
  const split = { panes: ["c", "t"] as const, focused: 1 as const };
  const out = resolveSplit(strip([chat("c"), term("t")], "t"), split);
  assert.ok(out.split === split, "a new object for an unchanged split would re-render forever");
});

test("a split whose tab has gone is gone", () => {
  assert.deepEqual(resolveSplit(strip([chat("c")], "c"), { panes: ["c", "t"], focused: 0 }), { shown: null, split: null });
  assert.deepEqual(resolveSplit(strip([term("t")], "t"), { panes: ["c", "t"], focused: 1 }), { shown: null, split: null });
});

test("with nothing active a split waits", () => {
  const split = { panes: ["c", "t"] as const, focused: 0 as const };
  assert.deepEqual(resolveSplit(strip([chat("c"), term("t")], null), split), { shown: null, split });
});

test("a new terminal replaces the focused pane when that makes a valid pair", () => {
  const tabs = [chat("c"), term("t"), term("t2")];
  const out = resolveSplit(strip(tabs, "t2"), { panes: ["c", "t"], focused: 1 });
  assert.ok(out.shown);
  assert.deepEqual(out.split, { panes: ["c", "t2"], focused: 1 });
  assert.deepEqual([out.shown.left.id, out.shown.right.id], ["c", "t2"]);
});

test("a chat shown while the focus is on the terminal replaces the other chat, since two chats are not allowed", () => {
  const tabs = [chat("c"), term("t"), chat("c2")];
  const out = resolveSplit(strip(tabs, "c2"), { panes: ["c", "t"], focused: 1 });
  assert.ok(out.shown);
  assert.deepEqual(out.split, { panes: ["c2", "t"], focused: 0 }, "the new chat took the chat's place and the focus");
  assert.deepEqual([out.shown.left.id, out.shown.right.id, out.shown.focused], ["c2", "t", 0]);
});

test("a terminal shown while the focus is on the chat replaces the chat: the focused pane is where you were looking", () => {
  const tabs = [chat("c"), term("t"), term("t2")];
  const out = resolveSplit(strip(tabs, "t2"), { panes: ["c", "t"], focused: 0 });
  assert.ok(out.shown);
  assert.deepEqual(out.split, { panes: ["t2", "t"], focused: 0 });
});

test("a chat shown over two terminals replaces the focused one", () => {
  const out = resolveSplit(strip([term("a"), term("b"), chat("c")], "c"), { panes: ["a", "b"], focused: 0 });
  assert.ok(out.shown);
  assert.deepEqual(out.split, { panes: ["c", "b"], focused: 0 });
});

test("a browser page that arrives from outside never takes a pane, so the split waits, and comes back when you return to a pane's tab", () => {
  const tabs = [chat("c"), term("t"), page("p")];
  const split = { panes: ["c", "t"] as const, focused: 1 as const };

  const away = resolveSplit(strip(tabs, "p"), split);
  assert.equal(away.shown, null);
  assert.ok(away.split === split, "a split that is only waiting is kept as it was");

  const back = resolveSplit(strip(tabs, "c"), away.split);
  assert.ok(back.shown, "the split did not come back");
  assert.deepEqual([back.shown.left.id, back.shown.right.id, back.shown.focused], ["c", "t", 0]);
});

test("a split that holds a page keeps showing it, and the page is the focused pane when it is the active tab", () => {
  const tabs = [chat("c"), page("p")];
  const split = { panes: ["c", "p"] as const, focused: 0 as const };

  const out = resolveSplit(strip(tabs, "p"), split);
  assert.ok(out.shown);
  assert.deepEqual([out.shown.left.id, out.shown.right.id, out.shown.focused], ["c", "p", 1]);
});

test("a chat chosen while a page is beside another chat replaces that chat, and keeps the page", () => {
  const out = resolveSplit(strip([chat("a"), chat("b"), page("p")], "b"), { panes: ["a", "p"], focused: 0 });
  assert.ok(out.shown);
  assert.deepEqual(out.split, { panes: ["b", "p"], focused: 0 });
});

test("a second page arriving while a page is in the split waits, and does not replace it", () => {
  const split = { panes: ["c", "p"] as const, focused: 1 as const };
  const out = resolveSplit(strip([chat("c"), page("p"), page("q")], "q"), split);
  assert.equal(out.shown, null);
  assert.ok(out.split === split);
});

test("two terminals with a browser page between waits the same way", () => {
  const out = resolveSplit(strip([term("a"), term("b"), page("p")], "p"), { panes: ["a", "b"], focused: 0 });
  assert.equal(out.shown, null);
  assert.deepEqual(out.split, { panes: ["a", "b"], focused: 0 });
});

test("resolving never changes what it was given", () => {
  const state = strip([chat("c"), term("t"), chat("c2")], "c2");
  const split = { panes: ["c", "t"] as const, focused: 1 as const };
  const before = JSON.stringify([state, split]);
  resolveSplit(state, split);
  assert.equal(JSON.stringify([state, split]), before);
});

// ── closing a pane's tab ────────────────────────────────────────────────────

test("closing a showing pane's tab ends the split and goes to the other pane's tab", () => {
  const state = strip([chat("c"), term("t")], "c");
  const split = { panes: ["c", "t"] as const, focused: 0 as const };
  assert.deepEqual(closeInSplit(state, split, "c"), { split: null, activate: "t" });
  assert.deepEqual(closeInSplit(state, split, "t"), { split: null, activate: "c" });
});

test("closing a tab that is in neither pane leaves the split alone", () => {
  const state = strip([chat("c"), term("t"), chat("other")], "c");
  assert.equal(closeInSplit(state, { panes: ["c", "t"], focused: 0 }, "other"), null);
  assert.equal(closeInSplit(state, null, "c"), null);
});

test("closing a pane's tab while the split is waiting drops it and moves nothing", () => {
  const state = strip([chat("c"), term("t"), page("p")], "p");
  assert.deepEqual(closeInSplit(state, { panes: ["c", "t"], focused: 0 }, "t"), { split: null, activate: null });
});

// ── the divider ─────────────────────────────────────────────────────────────

test("the defaults are an even split and a pane that is 22rem at the least", () => {
  assert.equal(DEFAULT_RATIO, 0.5);
  assert.equal(MIN_PANE_REM, 22);
});

test("a ratio inside its limits is kept", () => {
  assert.equal(clampRatio(0.4, 1600, 20), 0.4);
  assert.equal(clampRatio(0.5, 1600, 20), 0.5);
});

test("neither pane may be dragged below its minimum width", () => {
  // 1600px at 20px a rem: a pane needs 440px, which is 0.275 of the row.
  assert.equal(clampRatio(0.1, 1600, 20), 440 / 1600);
  assert.equal(clampRatio(0.95, 1600, 20), 1 - 440 / 1600);
});

test("the minimum is in rem, so the same row allows less at a bigger UI scale", () => {
  const atDefault = clampRatio(0.05, 2000, 16);
  const atBig = clampRatio(0.05, 2000, 28);
  assert.ok(atBig > atDefault, `${atBig} should be larger than ${atDefault}`);
  assert.equal(atDefault, (22 * 16) / 2000);
  assert.equal(atBig, (22 * 28) / 2000);
});

test("a row too narrow for two minimum panes is split evenly", () => {
  assert.equal(clampRatio(0.2, 800, 20), 0.5);
  assert.equal(clampRatio(0.9, 880, 20), 0.5, "exactly two minimums leaves no room to move");
});

test("a ratio that is not a number is an even split", () => {
  assert.equal(clampRatio(Number.NaN, 1600, 20), 0.5);
  assert.equal(clampRatio(Number.POSITIVE_INFINITY, 1600, 20), 0.5);
});

test("a row that cannot be measured still keeps the divider off the edges", () => {
  for (const [container, root] of [[0, 20], [Number.NaN, 20], [1600, 0], [-1, -1]] as const) {
    assert.equal(clampRatio(0.01, container, root), 0.2, `${container}/${root}`);
    assert.equal(clampRatio(0.99, container, root), 0.8, `${container}/${root}`);
    assert.equal(clampRatio(0.4, container, root), 0.4, `${container}/${root}`);
  }
});
