/**
 * Tabs: what is open, which one is showing, and what happens when one closes.
 *
 * A pure module on purpose. The render harness in `ui/tests/` proves what lands
 * on screen; it does not click anything. Tab arithmetic — the neighbour you land
 * on when one closes, the fact that opening a thread you already have open does
 * not give you two of it — is exactly the kind of logic that is easy to get
 * subtly wrong and impossible to see in markup, so it lives here where
 * `node --test` can reach it directly.
 *
 * Three kinds of tab share one strip because the user thinks of them the same
 * way: "what have I got open". A chat tab is one thread; a terminal
 * and a browser tab own their own state and are not bound to a thread — which
 * is why a browser tab's back/forward stack lives *on the tab* (`history`) and
 * dies with it, rather than in a side table App has to remember to prune.
 */
import type { BrowserHistory } from "./browserHistory";
import { emptyHistory, hostOf, visit } from "./browserHistory";
// The placeholder an unnamed thread is called by. It lives in its own module
// because three surfaces name threads — the strip, the panel, and the first
// prompt that gives one its name — and three copies of the string is three
// chances for them to disagree.
import { UNTITLED_THREAD_TITLE } from "./threadTitle";

export { UNTITLED_THREAD_TITLE };

/** What a tab is. Named by the strip, resolved by the kind. */
export type TabKind = "chat" | "terminal" | "browser";

export interface Tab {
  id: string;
  kind: TabKind;
  /** What the strip shows. A conversation's title, or a shell label, or a URL. */
  title: string;
  /**
   * The thread a chat tab shows, and the key that makes "open this thread"
   * idempotent. Undefined for terminal and browser tabs, and undefined on a
   * chat tab for as long as its thread is still unnamed.
   */
  conversationId?: string;
  /**
   * A browser tab's current address, and the fact that it has one.
   *
   * Undefined means *no webview exists yet*: the tab is an address bar waiting
   * for its first address. That distinction is load-bearing twice over — the
   * pane opens a window where it would otherwise navigate one, and closing such
   * a tab skips `codify_browser_close` rather than surfacing the shell's "no
   * browser tab is open" as an error the user caused.
   */
  url?: string;
  /** A browser tab's back/forward stack. See `browserHistory.ts`. */
  history?: BrowserHistory;
  /**
   * A terminal tab's PTY, once the shell has answered.
   *
   * Undefined for the moment between the tab appearing and `codify_terminal_open`
   * coming back, which is the same shape of gap a browser tab has before its
   * first address. The pane uses it to say "starting" rather than "broken", and
   * unlike a browser tab it is *not* needed to decide whether closing should
   * reach the shell: `terminal::close` is a no-op for an id it does not know.
   */
  ptyId?: string;
  /** A terminal tab whose shell has finished. The scrollback stays. */
  exited?: boolean;
  /**
   * The folder this tab belongs to — the workspace it was opened in.
   *
   * On a terminal tab it is the one `pin_cwd` resolved at open time; on a chat
   * tab it is the workspace the thread lives in. The tab remembers it rather
   * than reading the composer's current selection, because a shell's working
   * directory was decided the moment it started and changing the composer's
   * workspace afterwards must not pretend otherwise.
   *
   * It is also the answer to "which folder am I in", which used to be one pill
   * in the header claiming the composer's workspace for every tab at once. With
   * two tabs in two folders that pill was wrong for one of them; this is the
   * same fact per tab, where it cannot disagree with what it describes.
   */
  workspaceId?: string;
}

export interface TabState {
  tabs: Tab[];
  /** `null` when nothing is open, which is a real state rather than tab zero. */
  activeId: string | null;
}

export const emptyTabs: TabState = { tabs: [], activeId: null };

/** A monotonically increasing id, so a tab can be named without a UUID round trip. */
let nextId = 1;
export function tabId(prefix: string = "tab"): string {
  return `${prefix}-${nextId++}`;
}

/**
 * The chat tab showing a thread, if one is already open.
 *
 * The reason opening a conversation is idempotent: clicking a thread in the
 * sidebar twice would otherwise leave two tabs pointed at the same transcript,
 * both streaming the same events — a duplicated view is a duplicated mistake.
 */
export function tabForConversation(
  state: TabState,
  conversationId: string
): Tab | undefined {
  return state.tabs.find(
    (t) => t.kind === "chat" && t.conversationId === conversationId
  );
}

export function activeTab(state: TabState): Tab | undefined {
  return state.tabs.find((t) => t.id === state.activeId);
}

export function tabIndex(state: TabState, id: string): number {
  return state.tabs.findIndex((t) => t.id === id);
}

/**
 * Open a tab and show it.
 *
 * For the browser and terminal kinds, which are one tab per instance: two
 * browser tabs are two webview windows and two terminals are two shells, and
 * collapsing either would make the second one unreachable.
 *
 * **Chat does not come through here.** A thread is opened by
 * [`openConversation`], which has one job this does not: a thread that is
 * already open is *focused*, not opened again. Putting that branch here would
 * mean every caller of `openTab` — browser, terminal, and anything added later —
 * inherited a rule about transcripts it has no reason to know.
 */
export function openTab(state: TabState, tab: Tab): TabState {
  return { tabs: [...state.tabs, tab], activeId: tab.id };
}

/** Show a tab. An unknown id is ignored rather than blanking the shell. */
export function focusTab(state: TabState, id: string): TabState {
  if (!state.tabs.some((t) => t.id === id)) return state;
  return { ...state, activeId: id };
}

/**
 * Close a tab, landing on a neighbour.
 *
 * Which neighbour is the decision this function exists to settle. The one to the
 * left is what browsers do, and it is right here for a reason beyond habit: a
 * transcript read top-to-bottom leaves you at the bottom, and the tab you were
 * reading before is the one you want back. Closing the leftmost tab lands on the
 * one that took its place.
 *
 * Closing the last tab leaves the shell open on nothing — `activeId: null` —
 * rather than inventing a tab to land on. An empty shell is the honest state,
 * and it is what the sidebar's "New chat" is for.
 */
export function closeTab(state: TabState, id: string): TabState {
  const at = tabIndex(state, id);
  if (at === -1) return state;
  const tabs = state.tabs.filter((t) => t.id !== id);
  if (state.activeId !== id) return { ...state, tabs };
  if (tabs.length === 0) return emptyTabs;
  // The left neighbour, or the one that slid into this slot when it was the first.
  const next = tabs[Math.max(0, at - 1)];
  return { tabs, activeId: next.id };
}

/**
 * Close the browser tab the shell says has gone.
 *
 * A browser tab's webview is its own OS window, so it can be closed without the
 * tab bar's permission — the user clicking that window's own close button. This
 * is that fact arriving, and the tab goes with it, through the same neighbour
 * arithmetic every other close gets.
 *
 * Refuses anything that is not a browser tab with that id, which makes this
 * safe to call on *any* close signal: an unknown id is the ordinary case when
 * the tab bar already closed the tab and the event is merely the consequence of
 * it, and a chat or terminal tab must never be closable because something
 * about a webview window was reported.
 */
export function closeBrowserTab(state: TabState, tabId: string): TabState {
  const tab = state.tabs.find((t) => t.id === tabId && t.kind === "browser");
  if (!tab) return state;
  return closeTab(state, tabId);
}

/**
 * Move a tab from one position to another.
 *
 * Out-of-range positions are clamped rather than dropped: a drag that lands one
 * pixel past the last tab is a move to the end, not a tab that vanishes.
 */
export function reorderTabs(state: TabState, from: number, to: number): TabState {
  const n = state.tabs.length;
  if (n === 0 || from < 0 || from >= n) return state;
  const target = Math.max(0, Math.min(to, n - 1));
  if (target === from) return state;
  const tabs = [...state.tabs];
  const [moved] = tabs.splice(from, 1);
  tabs.splice(target, 0, moved);
  return { ...state, tabs };
}

/** Rename a tab. An unknown id changes nothing. */
export function renameTab(state: TabState, id: string, title: string): TabState {
  if (!state.tabs.some((t) => t.id === id)) return state;
  return {
    ...state,
    tabs: state.tabs.map((t) => (t.id === id ? { ...t, title } : t)),
  };
}

/**
 * Open a thread in the strip, or bring its tab forward if it is already open.
 *
 * ## One tab per thread, and why the obvious "dedupe" is the rule
 *
 * Two tabs pointed at one conversation would be two live event streams over the
 * same goal, and a duplicated view is a duplicated mistake — so opening the same
 * thread twice focuses the tab that exists rather than adding a second. That is
 * the whole of the idempotence, and it is a *deduplication*, not a *cap*: there
 * is no fixed number of chat tabs, the strip grows with the threads you are
 * actually working in, and the one you want is the one you last opened.
 *
 * This replaced a single shared "Chats" tab, which was tried and reversed. It
 * looked tidier and it was wrong in the way that matters: with one tab, starting
 * a new thread silently replaced the thread you were reading, and the strip
 * held no record of what you had open. The side panel already lists every
 * thread, so the strip's job is not to be a second list of them — it is to be
 * the set of things currently open, which for a chat means the thread.
 *
 * ## The title is taken as given, empty included
 *
 * A fresh conversation is stored with an empty title, and a tab that kept the
 * *previous* thread's name over it would be the one thing a label must never
 * do. Re-showing the *same* thread keeps the name the tab already had, so a tab
 * opened before the engine named the thread picks that name up later.
 */
export function openConversation(
  state: TabState,
  conversationId: string,
  title: string = "",
  workspaceId?: string,
): TabState {
  const open = tabForConversation(state, conversationId);
  if (open) {
    // Same thread, possibly a better name for it: a thread that has been
    // renamed since its tab opened takes the new name, and one that has not
    // keeps what it had. The folder is refreshed for the same reason: a tab
    // that opened before the thread's workspace was known picks it up here.
    const focused = focusTab(state, open.id);
    return renameTab(
      workspaceId ? withWorkspace(focused, open.id, workspaceId) : focused,
      open.id,
      title || open.title,
    );
  }
  return openTab(state, {
    id: tabId("chat"),
    kind: "chat",
    title: title || UNTITLED_THREAD_TITLE,
    conversationId,
    workspaceId,
  });
}

/**
 * Record which folder a tab belongs to, once it is known.
 *
 * Separate from `openConversation` so the two do not have to be called
 * together: a tab opened from a goal, where the conversation has not been read
 * yet, has no folder to carry and must not be given a guessed one. An unknown
 * tab id changes nothing.
 */
export function withWorkspace(
  state: TabState,
  id: string,
  workspaceId: string,
): TabState {
  if (!state.tabs.some((t) => t.id === id)) return state;
  return {
    ...state,
    tabs: state.tabs.map((t) =>
      t.id === id && t.workspaceId !== workspaceId
        ? { ...t, workspaceId }
        : t,
    ),
  };
}

/**
 * Close the tab showing a thread that is leaving the panel.
 *
 * Archiving hides a thread, and its tab goes with it: a tab left pointing at an
 * archived conversation is a transcript with no row above it, and the strip is
 * meant to be what is open. The chat column is not at risk the way it was under
 * the single-tab model — every other thread has its own tab, and "New chat" is
 * one click away.
 *
 * A thread with no tab is the ordinary case (archived from the panel without
 * being read) and changes nothing.
 */
export function closeConversation(
  state: TabState,
  conversationId: string
): TabState {
  const open = tabForConversation(state, conversationId);
  if (!open) return state;
  return closeTab(state, open.id);
}

/** The shell tabs, in strip order. Chat tabs are the default filter. */
export function tabsOfKind(state: TabState, kind: TabKind): Tab[] {
  return state.tabs.filter((t) => t.kind === kind);
}

/**
 * Give a browser tab its first address, and seed that address as visited.
 *
 * ## The id is the caller's, and that is the whole contract
 *
 * `browser::open` names the webview window `browser-<tab_id>` and
 * `browser::navigate` looks that label up again (`src-tauri/src/browser.rs`), so
 * the tab id *is* the shell's handle on that page. This used to mint its own id
 * while the caller separately asked the shell to open a window under a
 * different freshly-minted one — two ids for one page, which meant the first
 * address worked and every later one was refused with "no such browser tab".
 * The tab and the window have to be named by the same string, so the caller
 * passes the id it already gave the tab and this fills the tab in.
 *
 * The seeding is the part worth having here rather than in the caller: a stack
 * that starts empty while the tab already shows a page would make back a no-op
 * on the first page, which is not what a browser does and is a bug nobody
 * notices until it is fixed by accident.
 *
 * Note what this does *not* do, unlike `openConversation`: two browser tabs on
 * the same URL are two tabs. Idempotence is for chat, where a duplicate is a
 * duplicated transcript streaming the same events twice. Two browser tabs on
 * one site are two independent webview windows with two independent histories,
 * and collapsing them would make the second window unreachable.
 */
export function openBrowserTab(
  state: TabState,
  id: string,
  url: string,
  workspaceId?: string,
): TabState {
  return openTab(state, {
    id,
    kind: "browser",
    title: hostOf(url),
    // The folder, for the same reason a chat tab carries one: the strip shows
    // the folder a tab belongs to, and leaving browser tabs out made that true
    // of two kinds of tab and not the third. Optional, because a caller that
    // has not loaded the workspace list yet has nothing to record — and a tab
    // with no folder shows none rather than a guess.
    workspaceId,
    url,
    history: visit(emptyHistory(), url),
  });
}

/**
 * A terminal tab, named by the shell.
 *
 * The id is the PTY's, because `terminal.rs` numbers its own sessions and the
 * pane needs that string for every write, resize and close. Inventing a tab id
 * and holding a second one beside it would be a mapping with no payoff: nothing
 * else in the shell cares what a terminal tab is called.
 */
export function openTerminalTab(
  state: TabState,
  ptyId: string,
  workspaceId: string
): TabState {
  return openTab(state, {
    id: ptyId,
    kind: "terminal",
    title: "Terminal",
    ptyId,
    workspaceId,
  });
}

/** Mark a terminal tab's shell as finished, leaving its scrollback alone. */
export function markTerminalExited(state: TabState, id: string): TabState {
  if (!state.tabs.some((t) => t.id === id && t.kind === "terminal")) return state;
  return {
    ...state,
    tabs: state.tabs.map((t) => (t.id === id ? { ...t, exited: true } : t)),
  };
}

/**
 * Record where a browser tab now is, after the shell accepted a navigation.
 *
 * One place that both the address bar and back/forward go through, so the
 * title, the address and the stack cannot disagree about what the tab is
 * showing. Called *after* the shell answers: a refused navigation must leave
 * the tab exactly where it was, or the address bar would claim a page that
 * never loaded.
 */
export function setBrowserUrl(
  state: TabState,
  id: string,
  url: string,
  history: BrowserHistory
): TabState {
  return {
    ...state,
    tabs: state.tabs.map((t) =>
      t.id === id && t.kind === "browser"
        ? { ...t, url, title: hostOf(url), history }
        : t
    ),
  };
}
