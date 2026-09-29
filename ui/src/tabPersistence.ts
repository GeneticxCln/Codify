/**
 * The tab strip, remembered: what was open, and where each browser tab had
 * been.
 *
 * **Why localStorage and not the engine.** `docs/09` §2 holds conversations in
 * the engine, and that decision is right for them: a thread *names goals*, the
 * goals are the audit trail, and a thread that lived in a browser profile would
 * vanish while its runs stayed. Nothing here is in that category. A tab layout
 * is shell state — the pages are native webviews in this process and the
 * terminals are PTYs in this process, so there is no reader of this layout
 * anywhere but the shell that will re-create them. Putting it in the engine
 * would add a table, a migration, two authenticated endpoints and a boot
 * dependency to store something that has no meaning after a restart, and it
 * would make the strip *worse*: a layout restored only after the engine answers
 * is a layout that does not come back when the engine is down, even though the
 * pages would have painted fine. The rule this file follows is the engine's:
 * records go in the engine, view state stays in the client. If a tab ever
 * becomes something another process must read — shared windows, a second
 * machine, a phone showing what the desktop has open — that is the day this
 * moves, and §2 is where it goes.
 *
 * **What is remembered, and what is deliberately not.**
 *
 * - **Chat tabs** by their `conversationId`, which is durable. The transcript
 *   is the engine's; this remembers only which threads had a tab. A chat tab
 *   with no `conversationId` is a *clean slate* — a real state (see
 *   [`openBlankTab`]) — and is remembered as one, because a tab the user left
 *   empty is a tab they get back empty.
 * - **Browser tabs** by their current address and their whole back/forward
 *   stack. The stack is this app's own (`browserHistory.ts` exists because
 *   Tauri exposes no `go_back`), so it is state no other layer holds, and it
 *   is the half of this feature that would actually be missed without it.
 * - **Terminal tabs are not remembered.** A PTY is a live process and its
 *   scrollback is in memory; a restored "Terminal" tab would be a tab
 *   promising a shell that no longer exists. Ephemeral is the honest answer.
 * - **Titles are not remembered.** A chat tab's title is its thread's, and a
 *   browser tab's title is the page's own — both re-arrive on their own within
 *   a second of the tab being restored (`hydrateThread`, and the title hook
 *   §7.2's `browser-page-titled` exists for). Persisting them would store a
 *   fact that is stale the moment it is written; the host is the placeholder
 *   until the real one lands.
 *
 * **Every shape is checked on the way in**, because `localStorage` is not a
 * type. This is a file that a user can edit, a browser extension can touch, a
 * half-finished migration can leave, and three versions of this app can leave
 * in three shapes. The rules are stated once, in [`parseLayout`], and the
 * degradation is always *less*, never *more*: an unreadable value is an empty
 * strip, and a tab that cannot be made whole is dropped rather than guessed at.
 * Nothing here surfaces an error, for the reason `modelFreshness.ts` gives: the
 * cost of getting this wrong is a strip that opens one tab fewer, and the cost
 * of an error the reader has to dismiss is higher.
 */

import { classifyBrowserAddress } from "./browserDispatch";
import { hostOf } from "./browserHistory";
import {
  UNTITLED_THREAD_TITLE,
  focusTab,
  openTab,
  tabId,
  type Tab,
  type TabState,
} from "./tabs";

/** The slice of `Storage` this module needs, so tests can hand it a map. */
export interface TabStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** Where the layout lives. Namespaced with the other `CODIFY_*` keys. */
export const TABS_KEY = "CODIFY_TABS";

/**
 * How many tabs are remembered, and how deep a history.
 *
 * Both caps exist because this is written on every navigation and read at
 * every boot, and both are generous enough that a real session never reaches
 * them. The tab cap keeps the *active* tab plus the leftmost of the rest (a
 * strip of 25 where the user is looking at the 25th must not lose it); the
 * history cap keeps the most recent entries, the way a browser's own stack
 * does, so back stops at a depth that was ever plausible.
 */
export const MAX_PERSISTED_TABS = 24;
export const MAX_PERSISTED_HISTORY = 100;

/** A browser tab's remembered stack. Shaped like `BrowserHistory`. */
export interface PersistedHistory {
  entries: string[];
  index: number;
}

/** One remembered tab. Only the two kinds that can be honestly re-created. */
export interface PersistedTab {
  /**
   * This tab's durable key, which is what the engine holds its row under.
   *
   * Remembered, and this is the field whose absence was a real bug: without it
   * a boot restored its strip from this mirror with every tab **keyless**,
   * `ensureKeys` minted a fresh key for each one, `planChanges` saw keys the
   * engine had never been told about, and the upsert wrote a *second* row for
   * every tab that was already there. `PUT /shell/tabs` only ever upserts and
   * nothing but an explicit delete removes a row, so the strip grew by its own
   * size on every boot — measured as bursts of up to `MAX_PERSISTED_TABS` rows
   * per boot, and as 172 rows in `~/.codify/codify.db` where a session's worth
   * was intended. The id is still minted per process (`PersistedLayout.
   * activeIndex` says why); the key is the identity that must survive.
   */
  key?: string;
  kind: "chat" | "browser";
  /** A chat tab's thread, and the thing that makes restoring it possible. */
  conversationId?: string;
  /** A browser tab's current address. Absent = an address bar that never went anywhere. */
  url?: string;
  history?: PersistedHistory;
  workspaceId?: string;
}

/** The remembered strip: its tabs in order, and which one was showing. */
export interface PersistedLayout {
  tabs: PersistedTab[];
  /**
   * Which tab was showing, as an index into `tabs`.
   *
   * An index and not an id because tab ids are process-local — `tabId()` counts
   * from one in every process, and a restored id would collide with the next
   * one minted. So the layout names a *position*, and restore mints fresh ids
   * in the order it reads them.
   */
  activeIndex: number;
}

/** No remembered strip. Also the answer for unreadable storage. */
export function emptyLayout(): PersistedLayout {
  return { tabs: [], activeIndex: -1 };
}

/** The `localStorage` of a browser, or `undefined` where there is none. */
export function browserStorage(): TabStorage | undefined {
  try {
    return typeof localStorage === "undefined" ? undefined : localStorage;
  } catch {
    // Some contexts throw on *accessing* localStorage (a sandboxed iframe, a
    // document with storage disabled). Unreachable storage is a strip that
    // does not persist, not a crash on first render.
    return undefined;
  }
}

/**
 * A remembered address, in the form the shell would take.
 *
 * The answer comes from `classifyBrowserAddress` — the mirror the address bar
 * already types through — so a stored address is canonicalised by exactly the
 * code that canonicalises a typed one (`example.com` comes back as
 * `https://example.com/`), and a stored address the shell would refuse
 * (`javascript:`, `data:`, a loopback host) is refused here rather than seated
 * and left to fail. This is not a second guard: the shell's
 * `navigation_allowed` is the enforcement point and every restored page still
 * goes through it. This is the *same* refusal arriving before a webview exists,
 * which is the whole reason `browserDispatch.ts` exists.
 */
function restoredUrl(raw: string): string | null {
  const classified = classifyBrowserAddress(raw, "restored");
  return classified.kind === "shell" ? classified.url : null;
}

/**
 * A remembered stack, or `undefined` if it cannot be believed.
 *
 * `undefined` means *no stack*, which the model already means: a browser tab
 * whose `history` is absent is a tab the pane treats as having never navigated.
 * That is the degradation chosen for every disagreement here, and it is
 * chosen because the alternative is showing a back button that goes somewhere
 * the address bar does not claim.
 *
 * The checks, in order, each of which drops the stack rather than repairing it:
 *
 * 1. a non-array `entries`, or an empty one with a cursor that is not `-1`;
 * 2. an entry that is not a non-empty string, or one the shell would refuse —
 *    a stack with a hole in it is a stack whose cursor means something else by
 *    the time it is read;
 * 3. a cursor outside `-1 … entries.length - 1`;
 * 4. a cursor that does not point at the tab's own `url`, which is the
 *    disagreement this rule exists for: the address bar and the stack must not
 *    be able to describe two different pages.
 */
function restoredHistory(raw: unknown, url: string | undefined): PersistedHistory | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const { entries, index } = raw as { entries?: unknown; index?: unknown };
  if (!Array.isArray(entries)) return undefined;
  if (typeof index !== "number" || !Number.isInteger(index)) return undefined;
  const clean: string[] = [];
  for (const entry of entries) {
    if (typeof entry !== "string" || !entry.trim()) return undefined;
    const canonical = restoredUrl(entry);
    if (canonical === null) return undefined;
    clean.push(canonical);
  }
  // An empty stack has exactly one honest cursor, and it is the one
  // `emptyHistory()` uses: before anything has been visited.
  if (clean.length === 0) return index === -1 ? { entries: [], index: -1 } : undefined;
  if (index < 0 || index > clean.length - 1) return undefined;
  if (url !== undefined && clean[index] !== url) return undefined;
  return { entries: clean, index };
}

/**
 * A remembered key, or `undefined` when the bytes are not one.
 *
 * `layoutSync` owns the key's shape (`isTabKey`, next to `tabKey`), and this is
 * deliberately the narrow half of it rather than an imported rule: the worst a
 * drifted copy can do is **drop** a key, which costs one minted key on the next
 * boot — the old behaviour. Accepting a key this function should have refused
 * would be the expensive direction, so it is the one that is not attempted.
 * `layoutSync.decodeTab` refuses an unusable key on the way to the engine
 * regardless, and that is the enforcement point.
 */
function restoredKey(raw: unknown): string | undefined {
  return typeof raw === "string" && raw.startsWith("k_") && raw.length <= 64
    ? raw
    : undefined;
}

/** One remembered tab, or `null` if it cannot be believed. */
function restoredTab(raw: unknown): PersistedTab | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const { kind, key, conversationId, url, history, workspaceId } = raw as Record<
    string,
    unknown
  >;
  if (kind !== "chat" && kind !== "browser") return null;
  const identity = restoredKey(key);
  const folder =
    typeof workspaceId === "string" && workspaceId.trim() ? workspaceId : undefined;
  if (kind === "chat") {
    // A clean slate is a chat tab with no thread, and it is remembered as
    // one. A `conversationId` that is not a non-empty string is not a thread,
    // so it is dropped rather than carried: an id that names nothing hydrates
    // nothing and shows an error the user did not cause.
    const thread =
      typeof conversationId === "string" && conversationId.trim() ? conversationId : undefined;
    return { key: identity, kind: "chat", conversationId: thread, workspaceId: folder };
  }
  // A browser tab's address goes through the same classification a typed one
  // does, and an address that is refused takes the whole tab with it: there is
  // no honest version of a tab whose only content is an address the shell
  // will not take.
  let address: string | undefined;
  if (url !== undefined) {
    if (typeof url !== "string") return null;
    const canonical = restoredUrl(url);
    if (canonical === null) return null;
    address = canonical;
  }
  return {
    key: identity,
    kind: "browser",
    url: address,
    history: restoredHistory(history, address),
    workspaceId: folder,
  };
}

/**
 * Read a layout out of a raw value, believing none of it.
 *
 * The one rule this function has: **more input may produce less output, never
 * more.** A tab that fails any check is dropped; a layout that fails any check
 * is the empty layout. Nothing is repaired, coerced or guessed at, because a
 * guessed-at tab strip is a strip showing a page the user never opened.
 */
export function parseLayout(raw: string | null): PersistedLayout {
  if (!raw) return emptyLayout();
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return emptyLayout();
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return emptyLayout();
  const { tabs, activeIndex } = parsed as { tabs?: unknown; activeIndex?: unknown };
  if (!Array.isArray(tabs)) return emptyLayout();
  const kept: PersistedTab[] = [];
  for (const entry of tabs.slice(0, MAX_PERSISTED_TABS)) {
    const tab = restoredTab(entry);
    if (tab) kept.push(tab);
  }
  const index =
    typeof activeIndex === "number" && Number.isInteger(activeIndex) ? activeIndex : -1;
  return { tabs: kept, activeIndex: index };
}

/** Read the layout, treating any failure as "nothing was open". */
export function readLayout(storage: TabStorage | undefined): PersistedLayout {
  if (!storage) return emptyLayout();
  try {
    return parseLayout(storage.getItem(TABS_KEY));
  } catch {
    // A store that refuses reads will refuse writes too. No strip, no error.
    return emptyLayout();
  }
}

/** Write the layout, losing it quietly if the store says no. */
export function writeLayout(storage: TabStorage | undefined, layout: PersistedLayout): void {
  if (!storage) return;
  try {
    storage.setItem(TABS_KEY, JSON.stringify(layout));
  } catch {
    // Quota is the realistic failure. A strip that is one restart old is a far
    // better outcome than an unhandled throw inside a render.
  }
}

/** A tab worth remembering, and the shape it is remembered in. */
function persistedTab(tab: Tab): PersistedTab | null {
  if (tab.kind === "terminal") return null;
  if (tab.kind === "chat") {
    return {
      key: tab.key,
      kind: "chat",
      conversationId: tab.conversationId,
      workspaceId: tab.workspaceId,
    };
  }
  // The stack is trimmed to its most recent entries *ending at the cursor*, so
  // the tab still shows the same page and back stops at a depth that existed.
  // A stack with no cursor (`index: -1`, never navigated) is kept as it is.
  const history = tab.history;
  let trimmed: PersistedHistory | undefined;
  if (history && history.entries.length > 0) {
    const end = history.index + 1;
    const start = Math.max(0, end - MAX_PERSISTED_HISTORY);
    const entries = history.entries.slice(start, end);
    const index = entries.length - 1;
    // A redirect moves where the *current* entry points; it does not add one.
    // The page reports its live address after a redirect and
    // `setBrowserPageUrl` records that on the tab alone, so the cursor can
    // name the address the redirect came from — which is every page that
    // redirects, and most pages do. Replacing at the cursor is what keeps the
    // stack: dropping it here (the stricter reading of "the stack and the
    // address bar must agree") would silently cost the user their back button
    // on almost every site, and the tests are what caught that.
    if (tab.url && index >= 0) entries[index] = tab.url;
    trimmed = { entries, index };
  } else if (history && history.entries.length === 0) {
    trimmed = { entries: [], index: -1 };
  }
  return {
    key: tab.key,
    kind: "browser",
    url: tab.url,
    history: trimmed,
    workspaceId: tab.workspaceId,
  };
}

/**
 * Project the live strip into the remembered shape.
 *
 * Titles are gone on the way out (see the module docs) and terminal tabs are
 * gone with them, which means the active tab may not survive the projection —
 * a terminal tab that was showing is a tab that will not come back. The
 * fallback is its neighbour to the left, because that is where the strip's
 * attention was, and `0` when there is nothing to the left.
 */
export function layoutFrom(state: TabState): PersistedLayout {
  const projected = state.tabs
    .map((tab) => ({ tab, persisted: persistedTab(tab) }))
    .filter((entry): entry is { tab: Tab; persisted: PersistedTab } => entry.persisted !== null);
  const active = projected.find((entry) => entry.tab.id === state.activeId);
  let kept = projected;
  if (projected.length > MAX_PERSISTED_TABS) {
    // The active tab is kept whatever its position; the rest are the leftmost,
    // which keeps the strip's reading order intact for what survives.
    const others = projected
      .filter((entry) => entry !== active)
      .slice(0, MAX_PERSISTED_TABS - (active ? 1 : 0));
    kept = active ? [active, ...others] : others;
    kept.sort(
      (a, b) => state.tabs.indexOf(a.tab) - state.tabs.indexOf(b.tab),
    );
  }
  const tabs = kept.map((entry) => entry.persisted);
  // `kept` is back in strip order, so the active tab's position in it is the
  // position to remember.
  let activeIndex = active ? kept.indexOf(active) : -1;
  if (activeIndex < 0) {
    // The tab that was showing is not coming back. Land on the nearest
    // remembered tab to its left, or the first one if there was none.
    const vanished = state.tabs.findIndex((tab) => tab.id === state.activeId);
    activeIndex = vanished > 0 ? Math.min(vanished - 1, tabs.length - 1) : 0;
  }
  return { tabs, activeIndex: tabs.length === 0 ? -1 : activeIndex };
}

/** Remember the strip. The one call a write-through effect needs. */
export function persistLayout(state: TabState, storage: TabStorage | undefined): void {
  writeLayout(storage, layoutFrom(state));
}

/**
 * Re-create the strip from a remembered one.
 *
 * Appended to `state` rather than replacing it, so a caller that has already
 * opened something (the app restores into `emptyTabs`, but the signature does
 * not require that) keeps it. Ids are minted fresh — see
 * [`PersistedLayout.activeIndex`] for why — and the strip order is the
 * remembered order.
 *
 * Browser tabs are restored with their address and their stack, which is all
 * the pane needs: §7.2's pane opens a page from the tab's own `url`, so a
 * restored tab is seated by exactly the path a typed address takes and no new
 * one. Nothing is opened *here* — this returns a strip, and the app seats the
 * pages it can see (the pane is the only thing that knows the geometry, and a
 * webview seated at 0×0 is refused by the shell by design).
 */
export function restoreTabs(state: TabState, layout: PersistedLayout): TabState {
  let next = state;
  for (const tab of layout.tabs) {
    if (tab.kind === "chat") {
      next = openTab(next, {
        id: tabId("chat"),
        kind: "chat",
        // The thread's own title arrives with the thread; a restored tab must
        // not borrow the name of whatever it replaced.
        title: UNTITLED_THREAD_TITLE,
        // The key comes back with the tab it names, so this boot re-adopts the
        // engine's row instead of minting a new one beside it. See
        // `PersistedTab.key`.
        key: tab.key,
        conversationId: tab.conversationId,
        workspaceId: tab.workspaceId,
      });
      continue;
    }
    next = openTab(next, {
      id: tabId("browser"),
      kind: "browser",
      key: tab.key,
      // The host, until the page names itself — which it will, through the
      // title hook this feature is not allowed to depend on for anything else.
      title: tab.url ? hostOf(tab.url) : "",
      url: tab.url,
      history: tab.history
        ? { entries: [...tab.history.entries], index: tab.history.index }
        : undefined,
      workspaceId: tab.workspaceId,
    });
  }
  const target =
    layout.activeIndex >= 0 && layout.activeIndex < next.tabs.length
      ? next.tabs[layout.activeIndex]
      : undefined;
  // Nothing to show: leave the caller's `activeId` alone rather than pointing
  // it at a tab that is not there.
  return target ? focusTab(next, target.id) : next;
}
