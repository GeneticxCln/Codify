/**
 * Tab arithmetic, which is the part of a shell that is impossible to see in
 * markup and easy to get subtly wrong.
 *
 * `componentLoader.test.ts` and the two card tests prove what lands on screen.
 * None of them can click. Where you land when a tab closes, that opening the
 * same conversation twice is one tab rather than two, that a drag past the last
 * tab clamps instead of dropping it — those are pure functions here so
 * `node --test` can reach them without a DOM.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
// Not for the JSX — `tabs.ts` has none. For the hook's `resolve` step: `src/`
// imports are extensionless (vite and tsc both resolve them that way), and node
// cannot follow `./browserHistory` without it. A module that used to be
// self-contained stops being reachable the moment it reaches a sibling.
registerTsx();

// Dynamic, not static, and the reason is the two lines above: a static import is
// hoisted and resolved *before* `registerTsx()` runs, so `tabs.ts` would be
// resolved before the `resolve` hook existed and its extensionless
// `./browserHistory` import would fail. Top-level await is what puts the import
// after the registration while keeping the names in module scope for the tests
// below.
const {
  activeTab,
  closeTab,
  emptyTabs,
  focusTab,
  openConversation,
  openBlankTab,
  closeConversation,
  withWorkspace,
  UNTITLED_THREAD_TITLE,
  openTab,
  reorderTabs,
  renameTab,
  tabIndex,
  markTerminalExited,
  openBrowserTab,
  openTerminalTab,
  setBrowserUrl,
  tabForConversation,
  tabsOfKind,
} = await import("../src/tabs.ts");
const { canGoBack, currentUrl, emptyHistory, visit } = await import(
  "../src/browserHistory.ts"
);

import type { Tab, TabState } from "../src/tabs.ts";

// The tab id is the conversation id in these fixtures — not what
// `openConversation` generates, which is a counter. These tests are about tab
// arithmetic, and naming the tab after its thread keeps the assertions readable.
const chat = (conversationId: string, title = ""): Tab => ({
  id: conversationId,
  kind: "chat",
  title,
  conversationId,
});

const shell = (id: string): Tab => ({
  id,
  kind: "terminal",
  title: "zsh",
});

const stateWith = (...tabs: Tab[]): TabState => ({
  tabs,
  activeId: tabs.length ? tabs[0].id : null,
});

// ── opening ──────────────────────────────────────────────────────────────

test("opening a conversation twice is one tab, not two", () => {
  // The defect this prevents: two tabs pointed at one transcript both streaming
  // the same events. A duplicated view is a duplicated mistake.
  let s = openConversation(emptyTabs, "c1", "Fix the flaky test");
  s = openConversation(s, "c1", "Fix the flaky test");
  assert.equal(s.tabs.length, 1);
  assert.equal(s.tabs[0].conversationId, "c1");
  assert.equal(s.activeId, s.tabs[0].id);
});

test("reopening a renamed thread picks up the new name", () => {
  // A tab opened as "New chat" should not keep saying that after the thread is
  // named. The sidebar passes the thread's current title, and it wins.
  let s = openConversation(emptyTabs, "c1", "New chat");
  s = openConversation(s, "c1", "Refactor the parser");
  assert.equal(s.tabs.length, 1);
  assert.equal(s.tabs[0].title, "Refactor the parser");
});

test("reopening with no title keeps the name the tab already had", () => {
  // An empty title is not a request to unname anything.
  let s = openConversation(emptyTabs, "c1", "Refactor the parser");
  s = openConversation(s, "c1", "");
  assert.equal(s.tabs[0].title, "Refactor the parser");
});

test("reopening a thread focuses the tab already showing it", () => {
  let s = openConversation(emptyTabs, "c1", "one");
  s = openTab(s, shell("term-1"));
  assert.equal(s.activeId, "term-1");
  s = openConversation(s, "c1", "one");
  assert.equal(s.tabs.length, 2, "the terminal stays");
  assert.equal(
    activeTab(s)?.conversationId,
    "c1",
    "and the chat tab comes forward"
  );
});

// ── one tab per thread ───────────────────────────────────────────────────
//
// The strip holds a tab for each thread you have open, and *that* is the
// difference from a single shared "Chats" tab: starting a new thread adds a tab
// rather than replacing the one you were reading. What has not changed is that
// one thread never gets two tabs.

test("a thread in a project you are not looking at gets its own tab", () => {
  // The rule that the single-tab model broke, in the form that still holds: a
  // thread you open from somewhere else must not take over the tab you are
  // reading. The ⌘K case — the palette can name a thread in another project,
  // and that project has to become visible to be read.
  let s = openConversation(emptyTabs, "c1", "Fix the flaky test", "w1");
  s = openConversation(s, "c2", "Refactor the parser", "w2");
  assert.equal(s.tabs.length, 2, "two projects, two tabs");
  assert.equal(s.tabs[0].conversationId, "c1", "and the first is untouched");
  assert.equal(s.tabs[0].title, "Fix the flaky test");
  assert.equal(s.tabs[1].conversationId, "c2");
  assert.equal(s.activeId, s.tabs[1].id, "the new one is showing");
});

// ─────────────────────────────────────────────────────────────────────────────
// One project per tab
//
// A tab is a project's window and the threads inside it are what it shows in
// turn. These are the four cases `openConversation` has to tell apart, and each
// of them was one of the answers being wrong: a thread that hijacks a window, a
// clean slate that spawns a second window, a thread moved out of the tab that
// already had it, or a thread landing in a project it does not belong to.
// ─────────────────────────────────────────────────────────────────────────────

test("a clean slate is a project with a place to type, and no thread", () => {
  // The New Tab button's whole effect. It creates nothing on the engine, so
  // there is nothing here but a tab that is waiting: no conversation, and the
  // placeholder name a tab with nothing in it is called by.
  const s = openBlankTab(emptyTabs, "w1");
  assert.equal(s.tabs.length, 1);
  assert.equal(s.tabs[0].kind, "chat");
  assert.equal(s.tabs[0].workspaceId, "w1", "a tab is a project, so it records one");
  assert.equal(s.tabs[0].conversationId, undefined, "a clean slate has no thread to show");
  assert.equal(s.tabs[0].title, UNTITLED_THREAD_TITLE);
  assert.equal(s.activeId, s.tabs[0].id, "and it is the tab you are looking at");
});

test("the first prompt's thread lands in the clean slate rather than a new tab", () => {
  // What the send path does after it creates the conversation. If this opened a
  // tab instead, the clean slate would sit there empty next to the thread that
  // was just typed into it — which is the one artefact a clean slate exists to
  // avoid.
  const s = openConversation(openBlankTab(emptyTabs, "w1"), "c1", "Fix the flaky test", "w1");
  assert.equal(s.tabs.length, 1, "the thread took a second tab");
  assert.equal(s.tabs[0].conversationId, "c1");
  assert.equal(s.tabs[0].title, "Fix the flaky test", "the tab kept the placeholder name");
  assert.equal(s.tabs[0].workspaceId, "w1");
});

test("choosing a thread in the panel shows it in that project's tab", () => {
  // The side panel's whole job now. Two threads of one project are two things
  // one tab shows in turn, not two tabs, and the thread you were reading is
  // still in the panel to come back to.
  let s = openConversation(emptyTabs, "c1", "Fix the flaky test", "w1");
  s = openConversation(s, "c2", "Refactor the parser", "w1");
  assert.equal(s.tabs.length, 1, "one project, one tab");
  assert.equal(activeTab(s)?.conversationId, "c2");
  assert.equal(activeTab(s)?.title, "Refactor the parser");
  assert.equal(activeTab(s)?.workspaceId, "w1", "and the tab is still the project's");

  // And back again, which is the case a strip could not have made obvious.
  s = openConversation(s, "c1", "Fix the flaky test", "w1");
  assert.equal(s.tabs.length, 1);
  assert.equal(activeTab(s)?.conversationId, "c1");
});

test("a thread already open in a background tab is focused, not moved", () => {
  // Rule 1 before rule 2, and the order is the whole point: two tabs of one
  // project are possible (New Tab twice), so a thread can be open in a tab that
  // is not on screen. Choosing it in the panel must bring that tab forward. If
  // rule 2 ran first it would show the thread in the tab you are already in and
  // leave the other one pointing at a thread that is now on screen twice.
  let s = openConversation(emptyTabs, "c1", "Fix the flaky test", "w1");
  const first = s.tabs[0].id;
  s = openBlankTab(s, "w1"); // a second window onto the same project, now on screen
  const second = s.tabs[1].id;
  s = openConversation(s, "c1", "Fix the flaky test", "w1");
  assert.equal(s.tabs.length, 2, "no third tab");
  assert.equal(s.activeId, first, "the tab that had the thread came forward");
  assert.notEqual(s.activeId, second);
  assert.equal(s.tabs[1].conversationId, undefined, "and the other tab is still a clean slate");
});

test("an empty tab in the project is filled before a new one is made", () => {
  // The ⌘K case within one project: a terminal is on screen, and the project has
  // a clean slate in the strip. The thread goes in the slate, because opening
  // another tab when one is free is how a strip fills with near-identical rows.
  let s = openTerminalTab(openBlankTab(emptyTabs, "w1"), "term-1", "w1");
  assert.equal(activeTab(s)?.kind, "terminal");
  s = openConversation(s, "c1", "Fix the flaky test", "w1");
  assert.equal(s.tabs.length, 2, "a third tab for a project that already had one free");
  assert.equal(activeTab(s)?.kind, "chat");
  assert.equal(activeTab(s)?.conversationId, "c1");
});

test("two clean slates in one project are two tabs, because that is what New Tab means", () => {
  // The other direction: a second window onto the same project is a thing the
  // button offers, so quietly focusing the existing tab would make the control a
  // no-op that looks like it worked.
  let s = openBlankTab(emptyTabs, "w1");
  s = openBlankTab(s, "w1");
  assert.equal(s.tabs.length, 2);
  assert.equal(new Set(s.tabs.map((t) => t.workspaceId)).size, 1, "one project, two windows");
});

test("a thread whose project is not known does not take over a tab", () => {
  // The safety direction. A tab with no recorded folder is not evidence of a
  // shared project, so a thread we cannot place gets a tab of its own rather
  // than being shown in a window that may belong to somewhere else.
  const s = openConversation(openBlankTab(emptyTabs, "w1"), "c1", "Fix the flaky test");
  assert.equal(s.tabs.length, 2);
  assert.equal(s.tabs[0].conversationId, undefined, "the clean slate was filled by a thread it cannot host");
  assert.equal(activeTab(s)?.conversationId, "c1");
});

test("a terminal on screen is not a tab a thread can be shown in", () => {
  // Rule 2 is about chat tabs. A live shell is a window onto a cwd, and a
  // transcript shown in it would replace the scrollback.
  let s = openTerminalTab(emptyTabs, "term-1", "w1");
  s = openConversation(s, "c1", "Fix the flaky test", "w1");
  assert.equal(s.tabs.length, 2);
  assert.equal(activeTab(s)?.kind, "chat");
  assert.equal(tabsOfKind(s, "terminal").length, 1);
});

test("re-opening a thread focuses its tab instead of adding a second", () => {
  // The idempotence, which is a deduplication and not a cap: two tabs on one
  // conversation would be two live event streams over the same goal.
  let s = openConversation(emptyTabs, "c1", "one");
  s = openConversation(s, "c2", "two");
  const c1 = s.tabs[0].id;
  s = openConversation(s, "c1", "one");
  assert.equal(s.tabs.length, 2, "still two tabs, not three");
  assert.equal(s.activeId, c1, "and the thread's own tab comes forward");
  assert.equal(activeTab(s)?.conversationId, "c1");
});

test("a thread with no name yet gets a placeholder, not a blank tab", () => {
  // The engine stores an empty title until a turn gives it one. A tab with no
  // label is an unlabelled control in the strip, and this is the only place that
  // can decide what to call it.
  const s = openConversation(emptyTabs, "c1", "");
  assert.equal(s.tabs[0].title, UNTITLED_THREAD_TITLE);
  assert.equal(UNTITLED_THREAD_TITLE, "New chat");
});

test("a thread named after its tab opened picks up the name", () => {
  // Opened as "New chat", named by a turn, clicked again: the tab must take the
  // engine's name rather than keep the placeholder forever.
  let s = openConversation(emptyTabs, "c1", "");
  s = openConversation(s, "c1", "Refactor the parser");
  assert.equal(s.tabs.length, 1);
  assert.equal(s.tabs[0].title, "Refactor the parser");
});

test("re-opening a renamed thread keeps the name it already had", () => {
  // The panel does not always have a title to hand — an empty one must not
  // unname a tab that is already named.
  let s = openConversation(emptyTabs, "c1", "Refactor the parser");
  s = openConversation(s, "c1", "");
  assert.equal(s.tabs[0].title, "Refactor the parser");
});

test("a new thread tab joins the shell tabs rather than replacing them", () => {
  // Only chat is per-thread. A terminal is a live shell and a browser tab is a
  // live webview window, so starting a thread adds to the strip and leaves both
  // of them running.
  let s = openConversation(emptyTabs, "c1", "one");
  s = openTab(s, shell("term-1"));
  s = openBrowserTab(s, "browser-1", "https://example.com");
  s = openConversation(s, "c2", "two");
  assert.equal(s.tabs.length, 4);
  assert.equal(tabsOfKind(s, "chat").length, 2);
  assert.equal(tabsOfKind(s, "terminal").length, 1);
  assert.equal(tabsOfKind(s, "browser").length, 1);
  assert.equal(activeTab(s)?.conversationId, "c2");
});

test("a browser tab records its folder too", () => {
  // The strip says which folder a tab is in. A browser tab that never recorded
  // one made that true of chat and terminal tabs and not the third kind — an
  // inconsistency in the feature rather than a bug in any one tab.
  const s = openBrowserTab(emptyTabs, "browser-1", "https://example.com", "w1");
  assert.equal(s.tabs[0].workspaceId, "w1");
  assert.equal(s.tabs[0].title, "example.com", "the folder displaced the host");
});

test("the first address fills in the empty tab it was typed in, rather than adding a second with the same id", () => {
  // The New Tab control opens an empty browser tab; typing an address into it
  // must complete *that* tab. Appending a second tab under the same id left a
  // ghost "New tab" on the strip and two children with one React key.
  let s = openConversation(emptyTabs, "c1", "one");
  s = openTab(s, { id: "browser-1", kind: "browser", title: "New tab", key: "k_stable" });
  s = openConversation(s, "c2", "two");
  s = openBrowserTab(s, "browser-1", "https://example.com/a", "w1");
  assert.equal(tabsOfKind(s, "browser").length, 1, "the empty tab stayed behind");
  assert.equal(new Set(s.tabs.map((t) => t.id)).size, s.tabs.length, "two tabs share an id");
  const filled = s.tabs.find((t) => t.id === "browser-1");
  assert.equal(filled?.url, "https://example.com/a");
  assert.equal(filled?.key, "k_stable", "filling a tab in minted it a new identity");
  assert.equal(s.tabs.indexOf(filled!), 1, "the tab moved instead of being filled where it stood");
  assert.equal(s.activeId, "browser-1");
});

test("a browser tab with no folder records none rather than a guess", () => {
  // A caller that has not loaded the workspace list has nothing to record, and
  // printing a folder it cannot see is worse than printing none.
  const s = openBrowserTab(emptyTabs, "browser-1", "https://example.com");
  assert.equal(s.tabs[0].workspaceId, undefined);
});

test("a chat tab remembers the folder it was opened in", () => {
  // The folder used to be one pill in the header naming the composer's
  // workspace, which is wrong for every tab but the one the composer happens to
  // point at. The tab carries its own, so the strip cannot mislabel it.
  const s = openConversation(emptyTabs, "c1", "Refactor the parser", "w1");
  assert.equal(s.tabs[0].workspaceId, "w1");
});

test("a tab opened with no folder records no folder", () => {
  // A guessed folder is worse than none: it would name a directory the thread
  // is not in, and the strip would print it as fact.
  const s = openConversation(emptyTabs, "c1", "Refactor the parser");
  assert.equal(s.tabs[0].workspaceId, undefined);
});

test("re-opening a thread picks up its folder if it was missing", () => {
  // A tab opened from a goal, before the conversation had been read, has no
  // folder yet. Opening the thread again is where it gets one.
  let s = openConversation(emptyTabs, "c1", "Refactor the parser");
  assert.equal(s.tabs[0].workspaceId, undefined);
  s = openConversation(s, "c1", "", "w1");
  assert.equal(s.tabs.length, 1);
  assert.equal(s.tabs[0].workspaceId, "w1");
});

test("a folder cannot be stamped onto a tab that is not open", () => {
  const s = withWorkspace(emptyTabs, "no-such-tab", "w1");
  assert.equal(s, emptyTabs);
});

test("archiving a thread blanks its tab and leaves the others", () => {
  // The tab is the project's window now, so it outlives the thread that was in
  // it: closing it would destroy a window the person is still working in, and
  // archiving one thread should not be a way to lose a project. What is left is
  // a clean slate — the same state New Tab opens, and a state a tab is allowed
  // to be in.
  let s = openConversation(emptyTabs, "c1", "one", "w1");
  s = openTerminalTab(s, "term-1", "w1");
  const chat = s.tabs[0].id;
  s = closeConversation(s, "c1");
  assert.equal(s.tabs.length, 2, "archiving a thread did not close a tab");
  assert.equal(s.tabs[0].id, chat, "and the chat tab is the one that went blank");
  assert.equal(s.tabs[0].conversationId, undefined, "the tab still points at the archived thread");
  assert.equal(s.tabs[0].title, UNTITLED_THREAD_TITLE, "and still calls it by the old name");
  assert.equal(s.tabs[0].workspaceId, "w1", "the project is the tab, so it stays");
  assert.equal(tabForConversation(s, "c1"), undefined);
  // The shell tab is untouched: archiving a thread says nothing about shells.
  assert.equal(tabsOfKind(s, "terminal").length, 1);
});

test("archiving a thread you never opened changes nothing", () => {
  // The ordinary case: archived from the panel without ever being read. It must
  // not close some other thread's tab by falling through to "close something".
  const s = openConversation(emptyTabs, "c1", "one");
  assert.deepEqual(closeConversation(s, "c2"), s);
});

test("a terminal and a chat may both be open", () => {
  let s = openConversation(emptyTabs, "c1", "one");
  s = openTab(s, shell("term-1"));
  assert.equal(s.tabs.length, 2);
  assert.equal(tabsOfKind(s, "terminal").length, 1);
  assert.equal(tabsOfKind(s, "chat").length, 1);
});

test("focusing an id that is not open changes nothing", () => {
  const s = stateWith(chat("c1"));
  const after = focusTab(s, "ghost");
  assert.equal(after.activeId, s.activeId);
  assert.deepEqual(after.tabs, s.tabs);
});

test("the active tab is the one the strip is showing", () => {
  const s = stateWith(chat("c1"), shell("term-1"));
  assert.equal(activeTab(s)?.id, "c1");
  assert.equal(activeTab(focusTab(s, "term-1"))?.id, "term-1");
});

// ── closing, and where you land ─────────────────────────────────────────

test("closing lands on the tab to the left", () => {
  // Not the one to the right, and not tab zero. A transcript read to the bottom
  // leaves you wanting the tab you were on before.
  const s = { ...stateWith(chat("c1"), chat("c2"), chat("c3")), activeId: "c3" };
  const after = closeTab(s, "c3");
  assert.equal(after.activeId, "c2");
  assert.deepEqual(after.tabs.map((t) => t.id), ["c1", "c2"]);
});

test("closing the leftmost tab lands on the one that took its place", () => {
  const s = { ...stateWith(chat("c1"), chat("c2"), chat("c3")), activeId: "c1" };
  const after = closeTab(s, "c1");
  assert.equal(after.activeId, "c2");
});

test("closing the last tab leaves the shell open on nothing", () => {
  // An empty shell is the honest state. Inventing a tab to land on would mean
  // closing everything and still looking at a transcript.
  const after = closeTab(stateWith(chat("c1")), "c1");
  assert.deepEqual(after, emptyTabs);
  assert.equal(after.activeId, null);
});

test("closing a background tab leaves the active one alone", () => {
  const s = { ...stateWith(chat("c1"), chat("c2"), chat("c3")), activeId: "c3" };
  const after = closeTab(s, "c1");
  assert.equal(after.activeId, "c3");
  assert.deepEqual(after.tabs.map((t) => t.id), ["c2", "c3"]);
});

test("closing an id that is not open changes nothing", () => {
  const s = stateWith(chat("c1"));
  assert.deepEqual(closeTab(s, "ghost"), s);
});

test("closing one of two lands on the survivor whichever side it was", () => {
  const left = { ...stateWith(chat("c1"), chat("c2")), activeId: "c2" };
  assert.equal(closeTab(left, "c2").activeId, "c1");
  const right = { ...stateWith(chat("c1"), chat("c2")), activeId: "c1" };
  assert.equal(closeTab(right, "c1").activeId, "c2");
});

// ── reordering ──────────────────────────────────────────────────────────

test("a drag past the end clamps instead of dropping the tab", () => {
  // A drag that lands one pixel past the last tab is a move to the end.
  const s = stateWith(chat("c1"), chat("c2"), chat("c3"));
  const after = reorderTabs(s, 0, 99);
  assert.deepEqual(after.tabs.map((t) => t.id), ["c2", "c3", "c1"]);
});

test("a drag before the start clamps to the front", () => {
  const s = stateWith(chat("c1"), chat("c2"), chat("c3"));
  const after = reorderTabs(s, 2, -5);
  assert.deepEqual(after.tabs.map((t) => t.id), ["c3", "c1", "c2"]);
});

test("reordering keeps the active tab active", () => {
  const s = { ...stateWith(chat("c1"), chat("c2"), chat("c3")), activeId: "c2" };
  const after = reorderTabs(s, 1, 2);
  assert.equal(after.activeId, "c2");
  assert.deepEqual(after.tabs.map((t) => t.id), ["c1", "c3", "c2"]);
});

test("moving a tab to its own place changes nothing", () => {
  const s = stateWith(chat("c1"), chat("c2"));
  assert.deepEqual(reorderTabs(s, 1, 1), s);
});

test("moving a tab that is not there changes nothing", () => {
  const s = stateWith(chat("c1"));
  assert.deepEqual(reorderTabs(s, 5, 0), s);
});

// ── renaming ────────────────────────────────────────────────────────────

test("a rename reaches the tab it was aimed at", () => {
  const s = stateWith(chat("c1", "old"), chat("c2", "other"));
  const after = renameTab(s, "c1", "new");
  assert.equal(after.tabs[0].title, "new");
  assert.equal(after.tabs[1].title, "other");
});

test("a rename aimed at nothing changes nothing", () => {
  const s = stateWith(chat("c1", "old"));
  assert.deepEqual(renameTab(s, "ghost", "new"), s);
});

// ── the module's own helpers ────────────────────────────────────────────

test("the thread a chat tab shows is findable by conversation", () => {
  const s = openConversation(openConversation(emptyTabs, "c1", "one"), "c2", "two");
  assert.equal(tabForConversation(s, "c2")?.conversationId, "c2");
  assert.equal(tabForConversation(s, "c2")?.title, "two");
  // Both threads have their own tab, so both are findable — and that is the
  // assertion that would have failed under the single shared chat tab, where
  // "c1" vanished from the strip the moment "c2" was opened.
  assert.equal(tabForConversation(s, "c1")?.title, "one");
  assert.equal(tabForConversation(s, "c3"), undefined);
  assert.equal(tabIndex(s, s.tabs[0].id), 0);
  assert.equal(tabIndex(s, "nope"), -1);
});

test("a terminal tab is never mistaken for a thread", () => {
  // `conversationId` is the key that makes opening idempotent, so a tab that
  // happens to share an id must not be found by conversation.
  const s = openTab(emptyTabs, {
    id: "c1",
    kind: "terminal",
    title: "zsh",
  });
  assert.equal(tabForConversation(s, "c1"), undefined);
});

// ── browser tabs ──────────────────────────────────────────────────────────
//
// A browser tab owns its state rather than pointing at a thread, and the two
// rules that fall out of that are here: its history is seeded at creation, and
// it is *not* deduplicated the way a chat tab is.

test("a browser tab starts with its first address already in its history", () => {
  // A stack that starts empty while the tab already shows a page would make Back
  // a no-op on page one — not what a browser does, and a bug nobody notices
  // until it is fixed by accident.
  const s = openBrowserTab(emptyTabs, "browser-1", "https://example.com/docs");
  const tab = s.tabs[0];
  assert.equal(tab.kind, "browser");
  assert.equal(tab.url, "https://example.com/docs");
  assert.equal(tab.history?.index, 0);
  assert.deepEqual(tab.history?.entries, ["https://example.com/docs"]);
  assert.equal(currentUrl(tab.history!), "https://example.com/docs");
  assert.equal(canGoBack(tab.history!), false, "nothing before the first entry");
});

test("a browser tab is named by the id its caller already gave it", () => {
  // The one thing this function must not decide for itself. `browser::open`
  // names the webview window `browser-<tab_id>` and `browser::navigate` looks
  // that label up again, so the tab id *is* the shell's handle on the page.
  // This used to mint its own id while the caller asked the shell to open a
  // window under a different one: the first address worked, and every address
  // after it was refused as "no such browser tab" — in the app, not just here.
  const s = openBrowserTab(emptyTabs, "browser-7", "https://example.com");
  assert.equal(s.tabs[0].id, "browser-7");
  assert.equal(s.activeId, "browser-7");
});

test("a browser tab is titled by its host, not by its whole address", () => {
  // The strip is narrow, and `example.com` tells a user which page they are
  // looking at where a 90-character URL with a query string tells them nothing.
  const s = openBrowserTab(emptyTabs, "browser-1", "https://docs.rs/tauri/latest/?q=1#x");
  assert.equal(s.tabs[0].title, "docs.rs");
});

test("two browser tabs on one address are two tabs", () => {
  // The opposite of `openConversation`, on purpose. A duplicate chat tab is a
  // duplicated transcript streaming the same events twice; two browser tabs are
  // two webview windows with two histories, and collapsing them would make the
  // second window unreachable.
  const one = openBrowserTab(emptyTabs, "browser-1", "https://example.com");
  const two = openBrowserTab(one, "browser-2", "https://example.com");
  assert.equal(two.tabs.length, 2);
  assert.notEqual(two.tabs[0].id, two.tabs[1].id);
  assert.equal(two.activeId, two.tabs[1].id);
});

test("recording where a browser tab is moves its address, title and history together", () => {
  // One seam, so the address bar and the stack cannot disagree about which page
  // is showing. A tab whose title said one host and whose bar said another is
  // exactly the bug a single writer prevents.
  let s = openBrowserTab(emptyTabs, "browser-1", "https://a.example");
  const id = s.tabs[0].id;
  const next = visit(s.tabs[0].history!, "https://b.example");
  s = setBrowserUrl(s, id, "https://b.example", next);
  assert.equal(s.tabs[0].url, "https://b.example");
  assert.equal(s.tabs[0].title, "b.example");
  assert.equal(s.tabs[0].history, next);
  assert.equal(canGoBack(s.tabs[0].history!), true);
});

test("recording a browser address reaches no tab but the browser one", () => {
  // The updater walks every tab, so a matching id is not enough: a chat or
  // terminal tab sharing an id must keep its title.
  const s: TabState = {
    tabs: [
      { id: "same", kind: "chat", title: "Design the brand", conversationId: "c1" },
    ],
    activeId: "same",
  };
  const untouched = setBrowserUrl(
    s,
    "same",
    "https://a.example",
    visit(emptyHistory(), "https://a.example")
  );
  assert.equal(untouched.tabs[0].title, "Design the brand");
  assert.equal(untouched.tabs[0].url, undefined);
  // And an id that is not there at all is not an error.
  assert.deepEqual(
    setBrowserUrl(s, "absent", "https://a.example", emptyHistory()).tabs,
    s.tabs
  );
});

// ── terminal tabs ─────────────────────────────────────────────────────────
//
// A terminal tab is named by the shell — `terminal.rs` numbers its own sessions
// `term-1`, `term-2` — so the id is the PTY's rather than ours, and the pane
// needs that exact string for every write, resize and close.

test("a terminal tab is named by the PTY it was given", () => {
  // One string, not two. A tab id of our own beside the PTY's would be a
  // mapping with no payoff: nothing else in the shell cares what a terminal tab
  // is called.
  const s = openTerminalTab(emptyTabs, "term-7", "w1");
  assert.equal(s.tabs.length, 1);
  assert.equal(s.tabs[0].id, "term-7");
  assert.equal(s.tabs[0].kind, "terminal");
  assert.equal(s.tabs[0].ptyId, "term-7");
  assert.equal(s.tabs[0].title, "Terminal");
  assert.equal(s.tabs[0].conversationId, undefined, "a terminal is not a thread");
});

test("a terminal tab remembers the workspace its shell was pinned to", () => {
  // The tab's own workspace, not the composer's current selection. The shell's
  // cwd was decided by `pin_cwd` at open time, and switching workspaces
  // afterwards must not change what that shell is in — or which earlier session
  // a reopened pane restores.
  const s = openTerminalTab(emptyTabs, "term-1", "w-old");
  assert.equal(s.tabs[0].workspaceId, "w-old");
});

test("the app pins a terminal to the workspace it was opened in, and files its output there", async () => {
  // The claim above, through the mounted app: the shell is asked to open the
  // terminal in the selected workspace, and what the terminal prints is filed
  // under *that* workspace's scrollback — the key a reopened pane restores from.
  const { withApp } = await import("./appHarness.ts");
  const { readTerminalHistory } = await import("../src/terminalHistory.ts");
  await withApp(
    {
      workspaces: [
        { id: "ws-a", name: "Alpha", root_path: "/tmp/e2e-alpha" },
        { id: "ws-b", name: "Beta", root_path: "/tmp/e2e-beta" },
      ],
    },
    async ({ dom, shell, emit, settle }) => {
      await dom.click(dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
      await settle();
      const at = shell.calls.indexOf("codify_terminal_open");
      assert.ok(at >= 0, "Terminal never asked the shell for a terminal");
      assert.equal(shell.args[at].workspaceId, "ws-a", "the shell was not asked to pin the shell to the selected workspace");

      await emit("terminal-output", { id: "term-1", data: "quinton@alpha:~$ make build\r\n" });
      await settle();
      assert.match(readTerminalHistory("ws-a"), /make build/, "the output was not filed under the tab's workspace");
      assert.equal(readTerminalHistory("ws-b"), "", "the output leaked into another workspace's scrollback");
    },
  );
});

test("an exited shell is marked without losing the tab", () => {
  // Ctrl-D, or a command that ended the shell. The scrollback is the record of
  // what just happened; throwing the tab away is not what the user meant.
  const s = markTerminalExited(openTerminalTab(emptyTabs, "term-1", "w1"), "term-1");
  assert.equal(s.tabs[0].exited, true);
  assert.equal(s.tabs.length, 1, "the tab is still there to scroll back through");
});

test("marking an exit reaches no tab but a terminal one", () => {
  const s: TabState = {
    tabs: [{ id: "same", kind: "chat", title: "Design the brand", conversationId: "c1" }],
    activeId: "same",
  };
  const untouched = markTerminalExited(s, "same");
  assert.equal(untouched.tabs[0].exited, undefined);
  assert.equal(untouched.tabs[0].title, "Design the brand");
  assert.deepEqual(markTerminalExited(s, "absent").tabs, s.tabs);
});

