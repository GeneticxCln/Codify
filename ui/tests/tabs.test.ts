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
import { readFileSync } from "node:fs";

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
  closeBrowserTab,
  closeTab,
  emptyTabs,
  focusTab,
  openConversation,
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

const browser = (id: string, title = "example.com"): Tab => ({
  id,
  kind: "browser",
  title,
  url: "https://example.com/",
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

test("a new thread gets its own tab, and the old one stays", () => {
  // The rule that the single-tab model broke. Under it this length was 1 and the
  // thread you were reading was gone from the strip the moment you started
  // another; the transcript was still there in the side panel, but nothing in
  // the strip said what you had open.
  let s = openConversation(emptyTabs, "c1", "Fix the flaky test");
  s = openConversation(s, "c2", "Refactor the parser");
  assert.equal(s.tabs.length, 2, "two threads, two tabs");
  assert.equal(s.tabs[0].conversationId, "c1", "and the first is untouched");
  assert.equal(s.tabs[0].title, "Fix the flaky test");
  assert.equal(s.tabs[1].conversationId, "c2");
  assert.equal(s.activeId, s.tabs[1].id, "the new one is showing");
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

test("archiving a thread closes its tab and leaves the others", () => {
  // The tab pointed at an archived conversation would be a transcript with no
  // row above it. The chat column is not at risk: every other thread has its own
  // tab, and "New chat" is one click away.
  let s = openConversation(emptyTabs, "c1", "one");
  s = openConversation(s, "c2", "two");
  const c2 = s.tabs[1].id;
  s = closeConversation(s, "c1");
  assert.equal(s.tabs.length, 1);
  assert.equal(s.tabs[0].id, c2, "the thread you are still reading survives");
  assert.equal(tabForConversation(s, "c1"), undefined);
});

test("archiving a thread you never opened changes nothing", () => {
  // The ordinary case: archived from the panel without ever being read. It must
  // not close some other thread's tab by falling through to "close something".
  const s = openConversation(emptyTabs, "c1", "one");
  assert.deepEqual(closeConversation(s, "c2"), s);
});

test("a chat tab is never closed by a browser close signal", () => {
  // `closeBrowserTab` takes an id and a kind. A transcript sharing an id with a
  // webview must be untouchable by it, or closing a page takes a thread away.
  let s = openConversation(emptyTabs, "same", "one");
  s = openBrowserTab(s, "same", "https://example.com");
  const after = closeBrowserTab(s, "same");
  assert.equal(after.tabs.length, 1, "the browser tab went, the chat tab stayed");
  assert.equal(tabForConversation(after, "same")?.title, "one");
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

// ── a browser window closing takes its tab with it ──────────────────────

test("a closed webview window closes the tab behind it", () => {
  // The browser tab's webview is its own OS window, so it can be closed with
  // its own close button and nothing about it passes through the strip. The
  // shell's event lands here, and the tab goes with the window — landing on
  // the left neighbour like any other close.
  const s = {
    ...stateWith(chat("c1"), browser("tab-1")),
    activeId: "tab-1",
  };
  const after = closeBrowserTab(s, "tab-1");
  assert.deepEqual(after.tabs.map((t) => t.id), ["c1"]);
  assert.equal(after.activeId, "c1");
});

test("the event that follows a tab we closed ourselves changes nothing", () => {
  // Closing the tab asks the shell to destroy the webview, and the shell
  // announces the destruction. The tab is already gone; this has to be a
  // no-op rather than a second close.
  const s = stateWith(chat("c1"));
  assert.equal(closeBrowserTab(s, "tab-1"), s);
  assert.equal(closeBrowserTab(s, ""), s);
});

test("a close signal never closes a chat or terminal tab", () => {
  // The signal is a fact about a webview window. A chat tab sharing an id with
  // one (or a terminal tab) must be untouchable by it.
  const s = { ...stateWith(chat("tab-1"), shell("tab-2")), activeId: "tab-2" };
  assert.equal(closeBrowserTab(s, "tab-1"), s);
  assert.equal(closeBrowserTab(s, "tab-2"), s);
});

test("a webview closing for a background tab leaves the active tab alone", () => {
  const s = {
    ...stateWith(browser("tab-1"), chat("c1")),
    activeId: "c1",
  };
  const after = closeBrowserTab(s, "tab-1");
  assert.deepEqual(after.tabs.map((t) => t.id), ["c1"]);
  assert.equal(after.activeId, "c1");
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
  // And the pane is handed the tab's, so the two cannot disagree.
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(app, /workspaceId=\{activeTerminalTab\.workspaceId\}/);
  assert.match(app, /openTerminalTab\(prev, ptyId, selectedWs\.id\)/);
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

test("a browser close signal can never mark a terminal as exited", () => {
  // The two streams both carry an id, and both arrive at handlers that change
  // tab state. A cross is the kind of bug that only shows up when a user has
  // both kinds of tab open, which is every user.
  const s = openTerminalTab(
    openBrowserTab(emptyTabs, "browser-1", "https://example.com"),
    "term-1",
    "w1"
  );
  const afterBrowserClose = closeBrowserTab(s, s.tabs[0].id);
  assert.equal(afterBrowserClose.tabs.length, 1);
  assert.equal(
    afterBrowserClose.tabs[0].exited,
    undefined,
    "closing a browser tab marked a terminal as exited"
  );
});
