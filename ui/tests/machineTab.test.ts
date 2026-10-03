/**
 * The machine tab, as tab arithmetic: what opening one does to the strip, where it is deliberately not remembered, and who it
 * sits beside.
 *
 * A machine tab is a jailed shell (`src-tauri/src/machine.rs`). It is **local, like a terminal**: no key, no engine row, no
 * entry in `CODIFY_TABS`, because the engine's `/shell/tabs` closes `kind` to `chat | browser` and because a live jail is the
 * last thing to write into storage and "restore" on the next launch. Three places read "not terminal, not chat" as a browser
 * and would write a machine down as a broken page; the editor taught that lesson and each is pinned again here.
 *
 * What a machine is *not* is the point of several of these: it is not a terminal (the person's own shell), so a terminal's
 * exit does not touch it and it is never picked as a terminal's partner; and **whether it has a network is on the tab**,
 * fixed when it is opened, because a person must not have to go looking for that.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
import type { Tab, TabState } from "../src/tabs.ts";
registerTsx();

const {
  closeTab,
  isLocalTab,
  markMachineExited,
  markTerminalExited,
  openMachineTab,
  tabsOfKind,
} = await import("../src/tabs.ts");
const { layoutFrom, restoreTabs, parseLayout } = await import("../src/tabPersistence.ts");
const { ensureKeys, planChanges, newMirror } = await import("../src/layoutSync.ts");
const { canPair, pairRefusal, splitPartner, startSplit } = await import("../src/panes.ts");

const chat = (id: string, workspaceId = "w1"): Tab => ({ id, kind: "chat", title: id, workspaceId });
const term = (id: string, over: Partial<Tab> = {}): Tab => ({ id, kind: "terminal", title: id, ...over });
const mach = (id: string, over: Partial<Tab> = {}): Tab => ({ id, kind: "machine", title: "Machine", workspaceId: "w1", network: false, ...over });
const editor = (id: string): Tab => ({ id, kind: "editor", title: id, path: `${id}.py`, workspaceId: "w1" });
const page = (id: string): Tab => ({ id, kind: "browser", title: id, url: "https://example.com/" });
const strip = (tabs: Tab[], activeId: string | null): TabState => ({ tabs, activeId });

// ── opening one ──────────────────────────────────────────────────────────────

test("opening a machine makes a tab named by the shell's id, in its workspace, and shows it", () => {
  const next = openMachineTab(strip([chat("c")], "c"), "mach-3", "w1", false);

  const tab = next.tabs[1];
  assert.equal(tab.kind, "machine");
  assert.equal(tab.id, "mach-3", "the tab is named by the machine's own id: every write, resize and close needs that string");
  assert.equal(tab.workspaceId, "w1");
  assert.equal(next.activeId, "mach-3");
  assert.equal(next.tabs.length, 2);
});

test("whether the jail has a network is on the tab and in its title, and is off unless it was asked for", () => {
  const off = openMachineTab(strip([], null), "mach-1", "w1", false).tabs[0];
  const on = openMachineTab(strip([], null), "mach-2", "w1", true).tabs[0];

  assert.equal(off.network, false);
  assert.equal(off.title, "Machine");
  assert.equal(on.network, true);
  assert.match(on.title, /network/i, "a machine that can reach the network does not say so");
  assert.doesNotMatch(off.title, /network/i);
});

test("two machines are two tabs: each is its own jail", () => {
  const two = openMachineTab(openMachineTab(strip([], null), "mach-1", "w1", false), "mach-2", "w1", false);

  assert.equal(tabsOfKind(two, "machine").length, 2);
});

test("a machine's shell exiting marks that machine and nothing else, and a terminal's exit does not touch a machine", () => {
  const state = strip([term("t"), mach("m1"), mach("m2")], "m1");

  const exited = markMachineExited(state, "m1");

  assert.equal(exited.tabs.find((t) => t.id === "m1")?.exited, true);
  assert.equal(exited.tabs.find((t) => t.id === "m2")?.exited, undefined);
  assert.equal(exited.tabs.find((t) => t.id === "t")?.exited, undefined);
  assert.deepEqual(markTerminalExited(state, "m1"), state, "a terminal's exit marked a machine");
  assert.deepEqual(markMachineExited(state, "t"), state, "a machine's exit marked a terminal");
  assert.deepEqual(markMachineExited(state, "nope"), state);
});

test("closing a machine lands on its left neighbour like any other tab", () => {
  const next = closeTab(strip([chat("c"), term("t"), mach("m")], "m"), "m");

  assert.equal(next.activeId, "t");
});

// ── not remembered: the three places that would have written it down as a browser ──

const machineStrip = (): TabState => openMachineTab(strip([chat("c")], "c"), "mach-1", "w1", true);

test("a machine is a local tab, like a terminal and an editor, and a chat and a page are not", () => {
  assert.equal(isLocalTab(mach("m")), true);
  assert.equal(isLocalTab(term("t")), true);
  assert.equal(isLocalTab(editor("e")), true);
  assert.equal(isLocalTab(chat("c")), false);
  assert.equal(isLocalTab(page("p")), false);
});

test("a machine tab is not written to the remembered layout, and the active tab is re-based onto the rest", () => {
  const layout = layoutFrom(machineStrip());

  assert.equal(layout.tabs.length, 1);
  assert.equal(layout.tabs[0].kind, "chat");
  assert.doesNotMatch(JSON.stringify(layout), /mach-1|machine/);
  assert.ok(layout.activeIndex >= 0 && layout.activeIndex < layout.tabs.length);
});

test("a layout that names a machine kind is refused rather than restored as a page", () => {
  const stored = JSON.stringify({ tabs: [{ key: "k_abcdef12", kind: "machine", network: true }], activeIndex: 0 });

  const restored = restoreTabs({ tabs: [], activeId: null }, parseLayout(stored));

  assert.equal(restored.tabs.length, 0, "a jail was restored from storage");
});

test("a machine tab is never given a key, so it is never synced as a stranger", () => {
  const { state, minted } = ensureKeys(machineStrip());

  assert.equal(minted.length, 1, "only the chat tab was missing a key");
  assert.equal(state.tabs[1].key, undefined);
});

test("a machine tab is never pushed to the engine, even if something gave it a key", () => {
  const state = machineStrip();
  const chatKey = "k_abcdef12";
  const keyed: TabState = {
    ...state,
    tabs: state.tabs.map((t) => ({ ...t, key: t.kind === "machine" ? "k_12345678" : chatKey })),
  };

  const plan = planChanges(keyed, newMirror(), new Set());

  assert.deepEqual(plan.upserts.map((u) => u.key), [chatKey], "only the chat tab may be pushed");
});

// ── in a split ───────────────────────────────────────────────────────────────

test("a machine may sit beside anything: a chat, a terminal, an editor, a page or another machine", () => {
  const m = mach("m");

  for (const other of [chat("c"), term("t"), editor("e"), page("p"), mach("m2")]) {
    assert.equal(canPair(m, other), true, `a machine cannot sit beside a ${other.kind}`);
    assert.equal(canPair(other, m), true);
  }
  assert.equal(pairRefusal(m, m), "same");
});

test("a chat takes whichever of a terminal, machine, editor or page is nearest, and a tie goes right", () => {
  assert.deepEqual(splitPartner(strip([chat("c"), mach("m"), term("t")], "c")), { kind: "tab", id: "m" });
  assert.deepEqual(splitPartner(strip([chat("c"), term("t"), mach("m")], "c")), { kind: "tab", id: "t" });
  assert.deepEqual(splitPartner(strip([term("t"), chat("c"), mach("m")], "c")), { kind: "tab", id: "m" });
  assert.deepEqual(splitPartner(strip([chat("c"), mach("m")], "c")), { kind: "tab", id: "m" }, "a chat with only a machine asked for a new terminal");
});

test("a chat is never offered a machine whose shell has exited", () => {
  assert.deepEqual(splitPartner(strip([chat("c"), mach("m", { exited: true })], "c")), { kind: "new-terminal" });
  assert.deepEqual(splitPartner(strip([chat("c"), mach("m", { exited: true }), term("t")], "c")), { kind: "tab", id: "t" });
});

test("a machine takes the nearest chat first, then an editor, then a page, and never a terminal or another machine", () => {
  assert.deepEqual(splitPartner(strip([editor("e"), mach("m"), chat("c")], "m")), { kind: "tab", id: "c" }, "a chat is the assistant working in it");
  assert.deepEqual(splitPartner(strip([page("p"), mach("m"), editor("e")], "m")), { kind: "tab", id: "e" });
  assert.deepEqual(splitPartner(strip([mach("m"), page("p")], "m")), { kind: "tab", id: "p" });
  assert.deepEqual(splitPartner(strip([term("t"), mach("m")], "m")), { kind: "new-terminal" }, "a jail was put beside the person's own shell for them");
  assert.deepEqual(splitPartner(strip([mach("m1"), mach("m2")], "m1")), { kind: "new-terminal" });
});

test("a terminal never picks a machine for you", () => {
  assert.deepEqual(splitPartner(strip([term("t"), mach("m")], "t")), { kind: "new-terminal" });
  assert.deepEqual(splitPartner(strip([mach("m"), term("t"), chat("c")], "t")), { kind: "tab", id: "c" });
});

test("an editor and a page may be offered a live machine after a chat and a terminal", () => {
  assert.deepEqual(splitPartner(strip([editor("e"), mach("m")], "e")), { kind: "tab", id: "m" });
  assert.deepEqual(splitPartner(strip([page("p"), mach("m")], "p")), { kind: "tab", id: "m" });
  assert.deepEqual(splitPartner(strip([mach("m"), term("t"), editor("e")], "e")), { kind: "tab", id: "t" }, "a terminal comes before a machine");
  assert.deepEqual(splitPartner(strip([editor("e"), mach("m", { exited: true })], "e")), { kind: "new-terminal" });
});

test("showing a machine beside the chat puts the chat left and the machine right, focused", () => {
  const started = startSplit(strip([chat("c"), mach("m")], "c"), "m");

  assert.equal(started.ok, true);
  if (started.ok) {
    assert.deepEqual(started.split, { panes: ["c", "m"], focused: 1 });
    assert.equal(started.state.activeId, "m");
  }
});
