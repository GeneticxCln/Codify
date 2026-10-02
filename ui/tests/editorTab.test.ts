/**
 * The editor tab, as tab arithmetic: what opening a file does to the strip, and the places the editor is deliberately not
 * remembered.
 *
 * An editor tab is a *file in a workspace*. Opening one that is already open focuses it rather than making a second:
 * two tabs on one file would be two buffers that disagree about what the file says, and only one of them could be right
 * when it was saved (the same reason `openConversation` is idempotent, `docs/09` §4).
 *
 * It is also **local, like a terminal**: no key, no engine row, no entry in `CODIFY_TABS`. The engine's `/shell/tabs`
 * closes `kind` to `chat | browser`, so a row of any other kind is a 422 on an older engine and a 500 reading a newer
 * database; and what an editor tab holds that matters (unsaved text) is exactly what must never be written to a
 * `localStorage` entry or a table. Three places treat "not terminal, not chat" as a browser and would write an editor
 * tab down as a broken one; each is pinned here.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
import type { Tab, TabState } from "../src/tabs.ts";
registerTsx();

const {
  closeTab,
  focusTab,
  markTerminalExited,
  openBlankTab,
  openEditorTab,
  tabForFile,
  tabsOfKind,
} = await import("../src/tabs.ts");
const { layoutFrom, restoreTabs, parseLayout } = await import("../src/tabPersistence.ts");
const { ensureKeys, planChanges, newMirror } = await import("../src/layoutSync.ts");
const { canPair, pairRefusal, splitPartner, startSplit } = await import("../src/panes.ts");

const chat = (id: string, workspaceId = "w1"): Tab => ({ id, kind: "chat", title: id, workspaceId });
const term = (id: string, over: Partial<Tab> = {}): Tab => ({ id, kind: "terminal", title: id, ...over });
const strip = (tabs: Tab[], activeId: string | null): TabState => ({ tabs, activeId });

// ── opening a file ───────────────────────────────────────────────────────────

test("opening a file makes an editor tab named for the file, in its workspace, and shows it", () => {
  const next = openEditorTab(strip([chat("c")], "c"), { workspaceId: "w1", path: "src/app/main.py" });

  const tab = next.tabs[1];
  assert.equal(tab.kind, "editor");
  assert.equal(tab.title, "main.py");
  assert.equal(tab.path, "src/app/main.py");
  assert.equal(tab.workspaceId, "w1");
  assert.equal(next.activeId, tab.id);
  assert.equal(next.tabs.length, 2);
});

test("a file already open is focused, not opened again, and the strip is not reshuffled", () => {
  const once = openEditorTab(strip([chat("c")], "c"), { workspaceId: "w1", path: "a.py" });
  const editorId = once.tabs[1].id;
  const elsewhere = focusTab(once, "c");

  const again = openEditorTab(elsewhere, { workspaceId: "w1", path: "a.py" });

  assert.equal(again.tabs.length, 2);
  assert.equal(again.activeId, editorId);
  assert.deepEqual(again.tabs.map((t) => t.id), elsewhere.tabs.map((t) => t.id));
});

test("the same path in another workspace is another file", () => {
  const a = openEditorTab(strip([], null), { workspaceId: "w1", path: "README.md" });
  const b = openEditorTab(a, { workspaceId: "w2", path: "README.md" });

  assert.equal(tabsOfKind(b, "editor").length, 2);
  assert.notEqual(b.tabs[0].id, b.tabs[1].id);
});

test("two files with one name are two tabs, told apart by their folder in the title", () => {
  const a = openEditorTab(strip([], null), { workspaceId: "w1", path: "src/a/index.ts" });
  const b = openEditorTab(a, { workspaceId: "w1", path: "src/b/index.ts" });

  assert.equal(b.tabs.length, 2);
  assert.deepEqual(b.tabs.map((t) => t.title), ["a/index.ts", "b/index.ts"]);
});

test("closing one of two files told apart by folder gives the other its bare name back", () => {
  const a = openEditorTab(strip([], null), { workspaceId: "w1", path: "src/a/index.ts" });
  const both = openEditorTab(a, { workspaceId: "w1", path: "src/b/index.ts" });
  assert.deepEqual(both.tabs.map((t) => t.title), ["a/index.ts", "b/index.ts"]);

  const after = closeTab(both, both.tabs[1].id);

  assert.deepEqual(after.tabs.map((t) => t.title), ["index.ts"]);
});

test("closing something that is not an editor leaves the editors' titles alone", () => {
  const a = openEditorTab(strip([chat("c")], "c"), { workspaceId: "w1", path: "src/a/index.ts" });
  const both = openEditorTab(a, { workspaceId: "w1", path: "src/b/index.ts" });

  const after = closeTab(both, "c");

  assert.deepEqual(after.tabs.map((t) => t.title), ["a/index.ts", "b/index.ts"]);
});

test("the same file in two workspaces cannot be told apart by folder, so both keep their name", () => {
  const a = openEditorTab(strip([], null), { workspaceId: "w1", path: "src/util.py" });
  const both = openEditorTab(a, { workspaceId: "w2", path: "src/util.py" });

  assert.deepEqual(both.tabs.map((t) => t.title), ["util.py", "util.py"]);
});

test("three files with one name go as deep as it takes", () => {
  let state = strip([], null);
  for (const path of ["a/x/index.ts", "b/x/index.ts", "c/index.ts"]) {
    state = openEditorTab(state, { workspaceId: "w1", path });
  }

  const titles = state.tabs.map((t) => t.title);

  assert.equal(new Set(titles).size, 3, titles.join(" | "));
  assert.deepEqual(titles, ["a/x/index.ts", "b/x/index.ts", "c/index.ts"]);
});

test("a file with no folder is titled by its name alone, and a lone index file keeps its bare name", () => {
  const next = openEditorTab(strip([], null), { workspaceId: "w1", path: "index.ts" });

  assert.equal(next.tabs[0].title, "index.ts");
});

test("opening without showing leaves the active tab alone", () => {
  const before = strip([chat("c")], "c");

  const next = openEditorTab(before, { workspaceId: "w1", path: "a.py" }, { show: false });

  assert.equal(next.tabs.length, 2);
  assert.equal(next.activeId, "c");
});

test("re-opening without showing neither focuses nor duplicates", () => {
  const once = openEditorTab(strip([chat("c")], "c"), { workspaceId: "w1", path: "a.py" }, { show: false });

  const twice = openEditorTab(once, { workspaceId: "w1", path: "a.py" }, { show: false });

  assert.equal(twice, once, "an open that changes nothing must return the very state it was given");
});

test("a caller may name the tab it opens, and an open that finds the file already there ignores the name", () => {
  const named = openEditorTab(strip([], null), { workspaceId: "w1", path: "a.py" }, { id: "editor-fixed" });
  assert.equal(named.tabs[0].id, "editor-fixed");

  const again = openEditorTab(named, { workspaceId: "w1", path: "a.py" }, { id: "editor-other" });
  assert.equal(again.tabs.length, 1);
  assert.equal(again.tabs[0].id, "editor-fixed");
});

test("tabForFile finds an open file by workspace and path, and only that", () => {
  const state = openEditorTab(strip([chat("c")], "c"), { workspaceId: "w1", path: "a.py" });

  assert.equal(tabForFile(state, "w1", "a.py")?.path, "a.py");
  assert.equal(tabForFile(state, "w2", "a.py"), undefined);
  assert.equal(tabForFile(state, "w1", "b.py"), undefined);
});

test("closing an editor lands on its left neighbour like any other tab", () => {
  const state = openEditorTab(strip([chat("c"), term("t")], "t"), { workspaceId: "w1", path: "a.py" });

  const next = closeTab(state, state.tabs[2].id);

  assert.equal(next.activeId, "t");
});

test("a terminal's exit does not touch an editor", () => {
  const state = openEditorTab(strip([term("t")], "t"), { workspaceId: "w1", path: "a.py" });

  assert.deepEqual(markTerminalExited(state, state.tabs[1].id), state);
});

test("a blank chat is still found for a workspace when an editor is the active tab", () => {
  const state = openEditorTab(strip([chat("c")], "c"), { workspaceId: "w1", path: "a.py" });

  const next = openBlankTab(state, "w1");

  assert.equal(next.tabs.filter((t) => t.kind === "chat").length, 2);
});

// ── not remembered: the three places that would have written it down as a browser ──

const editorStrip = (): TabState =>
  openEditorTab(strip([chat("c")], "c"), { workspaceId: "w1", path: "src/a.py" });

test("an editor tab is not written to the remembered layout, and the active tab is re-based onto the rest", () => {
  const layout = layoutFrom(editorStrip());

  assert.equal(layout.tabs.length, 1);
  assert.equal(layout.tabs[0].kind, "chat");
  assert.doesNotMatch(JSON.stringify(layout), /src\/a\.py|editor/);
});

test("an editor tab active at shutdown remembers the nearest tab that is remembered", () => {
  // Re-based, not dropped on the floor: `activeIndex` counts remembered tabs only.
  const state = editorStrip();
  const layout = layoutFrom(state);

  assert.ok(layout.activeIndex >= 0 && layout.activeIndex < layout.tabs.length);
});

test("a layout that names an editor kind is refused rather than restored as a page", () => {
  const stored = JSON.stringify({ tabs: [{ key: "k_abcdef12", kind: "editor", path: "a.py" }], activeIndex: 0 });

  const restored = restoreTabs({ tabs: [], activeId: null }, parseLayout(stored));

  assert.equal(restored.tabs.length, 0);
});

test("an editor tab is never given a key, so it is never synced as a stranger", () => {
  const { state, minted } = ensureKeys(editorStrip());

  assert.equal(minted.length, 1, "only the chat tab was missing a key");
  assert.equal(state.tabs[1].key, undefined);
});

test("an editor tab is never pushed to the engine, even if something gave it a key", () => {
  const state = editorStrip();
  const chatKey = "k_abcdef12";
  const keyed: TabState = {
    ...state,
    tabs: state.tabs.map((t) => ({ ...t, key: t.kind === "editor" ? "k_12345678" : chatKey })),
  };

  const plan = planChanges(keyed, newMirror(), new Set());

  assert.deepEqual(plan.upserts.map((u) => u.key), [chatKey], "only the chat tab may be pushed");
});

test("a terminal is still local: not remembered, not keyed, not pushed", () => {
  const state = strip([chat("c"), term("t")], "c");
  const keyed: TabState = { ...state, tabs: state.tabs.map((t) => ({ ...t, key: t.kind === "terminal" ? "k_12345678" : "k_abcdef12" })) };

  assert.equal(layoutFrom(state).tabs.length, 1);
  assert.equal(ensureKeys(state).state.tabs[1].key, undefined);
  assert.deepEqual(planChanges(keyed, newMirror(), new Set()).upserts.map((u) => u.key), ["k_abcdef12"]);
});

// ── in a split ───────────────────────────────────────────────────────────────

test("an editor may sit beside a chat, a terminal or another editor", () => {
  const e1: Tab = { id: "e1", kind: "editor", title: "a.py", path: "a.py", workspaceId: "w1" };
  const e2: Tab = { id: "e2", kind: "editor", title: "b.py", path: "b.py", workspaceId: "w1" };

  assert.equal(canPair(e1, chat("c")), true);
  assert.equal(canPair(chat("c"), e1), true);
  assert.equal(canPair(e1, term("t")), true);
  assert.equal(canPair(e1, e2), true);
  assert.equal(pairRefusal(e1, e1), "same");
});

test("a chat with an editor and a terminal open takes whichever is nearer, and a tie goes right", () => {
  const e: Tab = { id: "e", kind: "editor", title: "a.py", path: "a.py", workspaceId: "w1" };
  const nearEditor = strip([chat("c"), e, term("t")], "c");
  const nearTerminal = strip([chat("c"), term("t"), e], "c");
  const tie = strip([term("t"), chat("c"), e], "c");

  assert.deepEqual(splitPartner(nearEditor), { kind: "tab", id: "e" });
  assert.deepEqual(splitPartner(nearTerminal), { kind: "tab", id: "t" });
  assert.deepEqual(splitPartner(tie), { kind: "tab", id: "e" });
});

test("a chat with only an editor open takes it, instead of asking for a new terminal", () => {
  const e: Tab = { id: "e", kind: "editor", title: "a.py", path: "a.py", workspaceId: "w1" };

  assert.deepEqual(splitPartner(strip([chat("c"), e], "c")), { kind: "tab", id: "e" });
});

test("an editor takes the nearest chat first, then the nearest live terminal", () => {
  const e: Tab = { id: "e", kind: "editor", title: "a.py", path: "a.py", workspaceId: "w1" };

  assert.deepEqual(splitPartner(strip([term("t"), e, chat("c")], "e")), { kind: "tab", id: "c" });
  assert.deepEqual(splitPartner(strip([term("t"), e], "e")), { kind: "tab", id: "t" });
  assert.deepEqual(splitPartner(strip([term("t", { exited: true }), e], "e")), { kind: "new-terminal" });
});

test("an editor never picks another editor for you", () => {
  const e1: Tab = { id: "e1", kind: "editor", title: "a.py", path: "a.py", workspaceId: "w1" };
  const e2: Tab = { id: "e2", kind: "editor", title: "b.py", path: "b.py", workspaceId: "w1" };

  assert.deepEqual(splitPartner(strip([e1, e2], "e1")), { kind: "new-terminal" });
});

test("showing an editor beside the chat puts the chat left and the editor right, focused", () => {
  const e: Tab = { id: "e", kind: "editor", title: "a.py", path: "a.py", workspaceId: "w1" };

  const started = startSplit(strip([chat("c"), e], "c"), "e");

  assert.equal(started.ok, true);
  if (started.ok) {
    assert.deepEqual(started.split, { panes: ["c", "e"], focused: 1 });
    assert.equal(started.state.activeId, "e");
  }
});
