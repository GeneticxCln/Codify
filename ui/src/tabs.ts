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
 * Five kinds of tab share one strip because the user thinks of them the same
 * way: "what have I got open". **A chat tab is a project with a thread in it**,
 * not a thread: one project per tab, and a project's threads are opened *into*
 * its tab rather than each taking a tab of their own. A terminal and a browser
 * tab own their own state and are bound to no thread — which is why a browser
 * tab's back/forward stack lives *on the tab* (`history`) and dies with it,
 * rather than in a side table App has to remember to prune. **An editor tab is a
 * file in a workspace** (`path`): opening one that is already open focuses it,
 * and like a terminal it is local to this window — see [`isLocalTab`]. **A machine tab is a jailed shell**
 * (`src-tauri/src/machine.rs`): local too, and bound to nothing else — its id is the shell's own, and whether it
 * has a network is fixed on the tab when a person opens it (`network`).
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
export type TabKind = "chat" | "terminal" | "browser" | "editor" | "machine";

export interface Tab {
  id: string;
  kind: TabKind;
  /** What the strip shows. A conversation's title, or a shell label, or a URL. */
  title: string;
  /**
   * The thread a chat tab is showing, and the key that makes "open this thread"
   * idempotent.
   *
   * Undefined for terminal and browser tabs, for a chat tab for as long as its
   * thread is still unnamed, and — the case this model exists for — for a
   * **clean slate**: a chat tab in a project that has not been given a thread
   * yet. A clean slate is a real state and a useful one, because a tab that
   * starts empty is a tab that does not have to be undone; see
   * [`openBlankTab`].
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
   * This tab's identity across processes, and the key the engine holds it under.
   *
   * Distinct from `id` on purpose, because they answer different questions and
   * have different lifetimes. `id` is minted per process by [`tabId`] and names
   * this window's webview or PTY — a shell-side handle that must not outlive the
   * process, because the next one would mint the same string for a different
   * thing. A shared tab strip needs the other kind: an identity that survives a
   * restart, so a tab the engine is holding can be recognised as the same tab
   * when it comes back. `layoutSync.ensureKeys` mints one, and the engine's
   * `/shell/tabs` rows are keyed by it.
   *
   * Absent on a terminal tab (never remembered) and briefly on any tab that has
   * not been through `ensureKeys` yet.
   */
  key?: string;
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
  /** A terminal or machine tab whose shell has finished. The scrollback (or the screen) stays. */
  exited?: boolean;
  /**
   * The folder this tab belongs to — the workspace it was opened in.
   *
   * On a terminal tab it is the one `pin_cwd` resolved at open time; on a chat
   * tab it is **the tab's identity**: one project per tab, so this is what
   * decides whether an opened thread belongs in this tab or in another one. The
   * tab remembers it rather than reading the composer's current selection,
   * because a shell's working directory was decided the moment it started and
   * changing the composer's workspace afterwards must not pretend otherwise.
   *
   * It is also the answer to "which folder am I in", which used to be one pill
   * in the header claiming the composer's workspace for every tab at once. With
   * two tabs in two folders that pill was wrong for one of them; this is the
   * same fact per tab, where it cannot disagree with what it describes.
   */
  workspaceId?: string;
  /**
   * An editor tab's file, relative to its workspace's root, and with `workspaceId` the key that makes "open this file"
   * idempotent (`openEditorTab`). The text itself is not here: an unsaved buffer lives in `editorBuffers.ts`, above the
   * pane that shows it, for the reason a terminal's scrollback lives above its pane.
   */
  path?: string;
  /**
   * Whether a machine tab's jail shares the host's network. Chosen by the person when they open the machine and **never
   * changed after**: a running jail cannot be moved between network namespaces, so "switch it on" is a new machine. It is
   * on the tab, and in the tab's title, because it is the one fact about a machine a person must not have to look for.
   */
  network?: boolean;
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
 * browser tabs are two pages with two histories and two terminals are two
 * shells, and collapsing either would make the second one unreachable.
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
 * and it is what the header's New Tab control is for.
 */
export function closeTab(state: TabState, id: string): TabState {
  const at = tabIndex(state, id);
  if (at === -1) return state;
  const closed = state.tabs[at];
  const remaining = state.tabs.filter((t) => t.id !== id);
  // A file that was told from another by its folder is just its name again once the other is gone.
  const tabs = closed.kind === "editor" ? retitleEditors(remaining) : remaining;
  if (state.activeId !== id) return { ...state, tabs };
  if (tabs.length === 0) return emptyTabs;
  // The left neighbour, or the one that slid into this slot when it was the first.
  const next = tabs[Math.max(0, at - 1)];
  return { tabs, activeId: next.id };
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
 * A new tab in a project, with nothing in it yet.
 *
 * ## A clean slate, and why no thread is created here
 *
 * The old New Tab called the engine before it opened anything, so a tab was a
 * conversation the moment it appeared — and pressing the button "to look
 * around" left a conversation behind, named from a prompt that was never
 * written. Here the tab is *only* a project and a place to type: the thread is
 * created by the first prompt, in the send path, which is the one place that
 * has the prompt to name it with. Nothing is created until there is something
 * to record, so a tab opened and abandoned costs nothing and is not a row in the
 * side panel.
 *
 * It is a new tab rather than "the tab for this project" on purpose: the button
 * is the one way to get a second window onto the same project, and quietly
 * focusing an existing tab instead would make the control a no-op that looks
 * like it worked.
 */
export function openBlankTab(state: TabState, workspaceId: string): TabState {
  return openTab(state, {
    id: tabId("chat"),
    kind: "chat",
    title: UNTITLED_THREAD_TITLE,
    workspaceId,
  });
}

/**
 * Show a thread: in the tab that already has it, in the tab you are looking at,
 * or in a new one.
 *
 * ## One project per tab, and the project's threads live in it
 *
 * A tab is a project's window, not a thread's, so opening a thread is a question
 * about *which project* it belongs to and never about making room. Three
 * answers, in this order:
 *
 * 1. **A tab already showing it** — focus that tab. Two tabs pointed at one
 *    conversation would be two live event streams over the same goal, and a
 *    duplicated view is a duplicated mistake. This is a *deduplication*, not a
 *    cap: nothing here is limited.
 * 2. **The tab you are looking at, if it is a chat tab in that project** — show
 *    the thread there, replacing whatever was in it. This is the side panel's
 *    entire job now: the panel lists a project's threads, and choosing one shows
 *    it in the project's tab. It is why the panel no longer offers a new-tab
 *    control: opening a thread is not opening a tab.
 * 3. **Otherwise** — a new tab for that project. The strip is the set of
 *    projects you currently have open, and a project you are not looking at is
 *    one you cannot see.
 *
 * Rule 2 is what "multiple threads per project" means in practice: a project's
 * threads are not a queue of tabs waiting to be opened, they are the things one
 * tab shows in turn. A single shared "Chats" tab was tried and reversed long ago
 * for looking tidier while destroying the record of what was open, and this is
 * not that: the project is still a tab, still in the strip, and still yours to
 * close.
 *
 * ## The project has to be known before rules 2 and 3 can apply
 *
 * Both match on `workspaceId`, and a thread whose folder is not known yet
 * matches neither — which sends it to a new tab, exactly as before this change.
 * That is the safe direction: a tab with no recorded folder is not evidence of a
 * shared project, and claiming one would put a thread from somewhere else into a
 * window that is not its own.
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

  const shown = showIn(state, conversationId, title, workspaceId);
  if (shown) return shown;

  return openTab(state, {
    id: tabId("chat"),
    kind: "chat",
    title: title || UNTITLED_THREAD_TITLE,
    conversationId,
    workspaceId,
  });
}

/**
 * Rules 2 and 3, returning `undefined` when neither applies.
 *
 * Separate from `openConversation` so the *fallback* — a new tab — is the last
 * thing that function does, rather than a branch threaded through the middle of
 * it. Every early return here is a case where a tab already existed.
 */
function showIn(
  state: TabState,
  conversationId: string,
  title: string,
  workspaceId: string | undefined,
): TabState | undefined {
  if (!workspaceId) return undefined;

  // Rule 2: the tab being looked at, when it is a chat tab in the same project.
  const active = activeTab(state);
  if (active && active.kind === "chat" && active.workspaceId === workspaceId) {
    return {
      ...state,
      tabs: state.tabs.map((t) =>
        t.id === active.id
          ? { ...t, conversationId, title: title || UNTITLED_THREAD_TITLE, workspaceId }
          : t,
      ),
    };
  }

  // Rule 3: a clean slate in this project that is not the one on screen. A tab
  // holding another thread is left alone — the person chose that thread.
  const blank = state.tabs.find(
    (t) => t.kind === "chat" && t.workspaceId === workspaceId && !t.conversationId,
  );
  if (blank) {
    return {
      ...state,
      activeId: blank.id,
      tabs: state.tabs.map((t) =>
        t.id === blank.id
          ? { ...t, conversationId, title: title || UNTITLED_THREAD_TITLE }
          : t,
      ),
    };
  }
  return undefined;
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
}/**
 * Close the tab showing a thread that is leaving the panel.
 *
 * The tab does not go with the thread any more, and that is the whole change:
 * a tab is a *project*, so closing a project window because one thread in it was
 * archived would destroy a window the person is still working in. The tab goes
 * blank instead — a clean slate in the same project, which is a state a tab is
 * allowed to be in and is the same state a new tab opens in. Archiving a thread
 * you were reading leaves you in its project with nothing in it, which is what
 * archiving a thread is.
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
  return {
    ...state,
    tabs: state.tabs.map((t) =>
      t.id === open.id
        ? { ...t, conversationId: undefined, title: UNTITLED_THREAD_TITLE }
        : t,
    ),
  };
}

/**
 * A tab that belongs to this window alone: never remembered, never keyed, never sent to the engine.
 *
 * A terminal and a machine are live processes, and an editor holds text a person has not saved. None is something to write into
 * `localStorage` or a shared table, and the engine's `/shell/tabs` closes `kind` to `chat | browser` (a row of any other
 * kind is a 422 on an older engine and a 500 reading a newer database). The three places that decide what is remembered
 * (`tabPersistence.persistedTab`, `layoutSync.ensureKeys`, `layoutSync.planChanges`) all ask this one question, because
 * each of them used to treat "not a terminal, not a chat" as a browser, which would have written an editor down as one.
 */
export function isLocalTab(tab: Tab): boolean {
  return tab.kind === "terminal" || tab.kind === "editor" || tab.kind === "machine";
}

/** The editor tab showing a file, if one is already open. */
export function tabForFile(state: TabState, workspaceId: string, path: string): Tab | undefined {
  return state.tabs.find((t) => t.kind === "editor" && t.workspaceId === workspaceId && t.path === path);
}

/**
 * What each open file is called: its name, or as much of its folder as it takes to tell it from another open file with the
 * same name. `index.ts` alone is `index.ts`; beside another, `a/index.ts` and `b/index.ts`. The same path in two
 * workspaces cannot be told apart by path and keeps one title.
 */
function retitleEditors(tabs: Tab[]): Tab[] {
  const editors = tabs.filter((t) => t.kind === "editor" && t.path);
  if (editors.length === 0) return tabs;
  const suffix = (path: string, depth: number): string => path.split("/").slice(-depth).join("/");
  let changed = false;
  const next = tabs.map((tab) => {
    if (tab.kind !== "editor" || !tab.path) return tab;
    const path = tab.path;
    const parts = path.split("/").length;
    let depth = 1;
    while (
      depth < parts &&
      editors.some((other) => other !== tab && other.path !== path && suffix(other.path as string, depth) === suffix(path, depth))
    ) {
      depth += 1;
    }
    const title = suffix(path, depth);
    if (title === tab.title) return tab;
    changed = true;
    return { ...tab, title };
  });
  return changed ? next : tabs;
}

/**
 * Open a file in an editor tab, or focus the one it already has.
 *
 * Idempotent on (workspace, path), for the reason `openConversation` is on a thread: two tabs on one file would be two
 * buffers that disagree about what it says, and only one of them could be right when it was saved. `show: false` opens
 * (or leaves) the tab without making it the active one, which is what the assistant's `open_in_editor` does when the
 * person is not looking at the conversation that asked: it must not take them away from what they are doing. An open
 * that changes nothing returns the very state it was given. `id` names the tab, for a caller that needs it before the state
 * settles; opening a file that is already open ignores it.
 */
export function openEditorTab(
  state: TabState,
  file: { workspaceId: string; path: string },
  options: { show?: boolean; id?: string } = {},
): TabState {
  const show = options.show ?? true;
  const existing = tabForFile(state, file.workspaceId, file.path);
  if (existing) return show && state.activeId !== existing.id ? focusTab(state, existing.id) : state;
  const tab: Tab = {
    // The caller may name it, so one that has to know the id before the state settles (the assistant opening a file asks
    // for the tab back at once) and a state updater that makes the same tab later agree on what it is called.
    id: options.id ?? tabId("editor"),
    kind: "editor",
    title: file.path.split("/").pop() || file.path,
    workspaceId: file.workspaceId,
    path: file.path,
  };
  const tabs = retitleEditors([...state.tabs, tab]);
  return { tabs, activeId: show ? tab.id : state.activeId };
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
 * `browser::navigate` looks that label up again (`src-tauri/src/browser/`), so
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
  const filled: Tab = {
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
  };
  // The tab the address bar was typed in already exists: the New Tab control
  // opens an empty one under this id. Appending a second tab with the same id
  // left the empty one behind on the strip, two children with one React key,
  // and a `codify_browser_close` that closed both.
  const at = tabIndex(state, id);
  if (at === -1) return openTab(state, filled);
  const tabs = state.tabs.map((t, i) => (i === at ? { ...t, ...filled } : t));
  return { tabs, activeId: id };
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

/**
 * A machine tab, named by the shell.
 *
 * The id is the machine's own (`mach-N`), for the reason a terminal's is the PTY's: the pane needs that string for every
 * write, resize and close, and a second id beside it would be a mapping with no payoff. The title says whether the jail
 * has a network, because that is the fact a person has to see at a glance and cannot afford to forget.
 */
export function openMachineTab(
  state: TabState,
  machineId: string,
  workspaceId: string,
  network: boolean
): TabState {
  return openTab(state, {
    id: machineId,
    kind: "machine",
    title: network ? "Machine · network" : "Machine",
    workspaceId,
    network,
  });
}

/** Mark a machine tab's shell as finished, leaving its screen alone. */
export function markMachineExited(state: TabState, id: string): TabState {
  if (!state.tabs.some((t) => t.id === id && t.kind === "machine")) return state;
  return {
    ...state,
    tabs: state.tabs.map((t) => (t.id === id ? { ...t, exited: true } : t)),
  };
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

/**
 * A browser tab's back/forward stack, moved by something other than the shell.
 *
 * Separate from [`setBrowserUrl`] because the two are different facts: that one
 * lands an address the user *asked for* (and re-titles the tab by host, since a
 * fresh address means the old name is stale), this one records a place the page
 * went on its own and touches nothing else. The address bar still follows the
 * page through [`setBrowserPageUrl`]; what lands here is only the way back to
 * where the click came from, which is what `browserHistory.pageNavigation`
 * decides and this only applies.
 */
export function setBrowserHistory(
  state: TabState,
  id: string,
  history: BrowserHistory
): TabState {
  return {
    ...state,
    tabs: state.tabs.map((t) =>
      t.id === id && t.kind === "browser" ? { ...t, history } : t
    ),
  };
}

/**
 * The page's live address, as it reported itself.
 *
 * A redirect lands here: what the address bar says follows the page, not the
 * last address the user typed. The title is re-derived from the host only
 * when the tab is still showing host-shaped names — a page that has titled
 * itself keeps its title, and `renameBrowserTab` is what moves it.
 */
export function setBrowserPageUrl(state: TabState, id: string, url: string): TabState {
  let changed = false;
  const tabs = state.tabs.map((t) => {
    if (t.id === id && t.kind === "browser" && t.url !== url) {
      changed = true;
      return { ...t, url };
    }
    return t;
  });
  // The same state out when nothing matched: a fact for a tab that is gone
  // is a no-op, and the reference says so — no caller re-renders on it.
  return changed ? { ...state, tabs } : state;
}

/**
 * The page's own title, from `on_document_title_changed`.
 *
 * The tab shows what the page calls itself; a title that arrives for an
 * unknown or non-browser tab changes nothing, and an empty one is no answer
 * (the reader in `browserPageState` refuses it before this runs).
 */
export function renameBrowserTab(state: TabState, id: string, title: string): TabState {
  let changed = false;
  const tabs = state.tabs.map((t) => {
    if (t.id === id && t.kind === "browser" && t.title !== title) {
      changed = true;
      return { ...t, title };
    }
    return t;
  });
  return changed ? { ...state, tabs } : state;
}
