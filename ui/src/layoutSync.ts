/**
 * Two windows, one strip.
 *
 * ## Why this is not a bigger `tabPersistence.ts`
 *
 * The tab layout used to be one window's `localStorage` and that was a complete
 * answer to "where does view state live" — it just was not an answer to "what
 * does a *second* window open onto", because the strip a second window wants is
 * the strip the first window has, not whatever its own profile held last. So the
 * strip moved: the **engine** now holds which tabs exist and in what order
 * (`GET`/`PUT`/`DELETE /shell/tabs`), and `localStorage` stayed as the mirror
 * that makes the strip come back when the engine does not answer.
 *
 * The split is the whole design, and it is drawn on purpose:
 *
 * - **The engine holds identity and order** — the two facts two windows must
 *   agree about. Its rows are per tab, keyed by a durable `key`, because a blob
 *   cannot say which tab a write was about and two writers both saving the whole
 *   layout is how one window closes the other's tabs.
 * - **The client holds what a tab is showing** — a thread, an address, a
 *   back/forward stack — as an opaque JSON `payload` the engine bounds and
 *   parses and otherwise does not understand. See `ShellTabService`: a store
 *   that learns a view's shape is how the two get out of step.
 *
 * ## The key, and why the tab's own id is not it
 *
 * `Tab.id` is a per-process counter (`tabs.ts`), and it has to stay that: it
 * names the webview and the PTY in the shell that owns them. A shared strip needs
 * an identity that outlives the process, so a tab that reaches the engine also
 * carries a `key` — minted once, persisted, and never reused. [`ensureKeys`] is
 * where one is minted, and it is deliberately *not* done in the tab constructors:
 * every path that opens a tab would then have to remember, and the one that
 * forgot would be a tab that syncs as a stranger.
 *
 * ## What a window does, in order
 *
 * 1. **Boot.** Read the mirror and show it — immediately, with no round trip,
 *    because a window that waits for the engine to draw its tabs is a window
 *    that shows nothing when the engine is down. Then ask the engine what the
 *    strip really is, and [`reconcile`] the two.
 * 2. **Change.** Mirror first, then push ([`pushChanges`]), because the mirror is
 *    what makes the next boot work and the engine is what makes the *next
 *    window* work; neither is worth delaying the other, and a window that
 *    crashed between the two would lose the push but not the strip.
 * 3. **Answer.** The push and the pull both return the merged strip, and
 *    [`reconcile`] adopts it. A window that only pushed would never learn that
 *    the other window closed a tab, and a tab closed in the other window has to
 *    disappear here too — that is what "shared" means, and it is the reason a
 *    close is a `DELETE` and not "stop mentioning it".
 *
 * ## What is *not* here
 *
 * No transport, no React, no timers: the engine calls live in `api.ts` and the
 * scheduling lives in `App.tsx`, so this file is the part that can be tested
 * without a browser. There is no websocket, because the engine's WS is scoped to
 * a goal's events and a strip is not a goal's event; a second window learns
 * about a change when it next pushes, pulls, or is focused, and a strip that is
 * a few seconds stale between two windows is a strip that is still correct when
 * you look at it. If that stops being true, the fix is a new engine stream
 * rather than a cleverer merge here.
 */

import { classifyBrowserAddress } from "./browserDispatch";
import { hostOf } from "./browserHistory";
import {
  UNTITLED_THREAD_TITLE,
  emptyTabs,
  focusTab,
  isLocalTab,
  tabId,
  type Tab,
  type TabState,
} from "./tabs";
import {
  MAX_PERSISTED_HISTORY,
  MAX_PERSISTED_TABS,
  TABS_KEY,
  browserStorage,
  emptyLayout,
  layoutFrom,
  parseLayout,
  type PersistedHistory,
  type PersistedLayout,
  type TabStorage,
} from "./tabPersistence";

/**
 * Re-exported rather than reimplemented: the projection from a live strip to a
 * remembered one is one piece of arithmetic about what a tab *is* (no titles, no
 * terminals, history trimmed at the cursor, a redirect reconciled), and it
 * already exists in `tabPersistence`. The sync needs it on every change, and a
 * second copy of those rules is a second thing to keep true.
 */
export { layoutFrom };

/** What the engine calls a tab: identity, place, and an opaque payload. */
export interface EngineTab {
  key: string;
  position: number;
  kind: "chat" | "browser";
  payload: string;
}

/** A tab as the engine holds it, decoded and believed no further than that. */
export interface SharedTab {
  key: string;
  tab: Tab;
}

/** The slice of `Storage` the mirror needs, matching `TabStorage`. */
export type LayoutStorage = TabStorage;

const KEY_PREFIX = "k_";
let keyCounter = 1;

/**
 * A durable tab identity.
 *
 * The prefix is there so a key can never be mistaken for a `Tab.id` in a log or
 * a test failure: they are different namespaces, minted by different counters,
 * and confusing them is exactly the bug that would make a restored tab seat the
 * wrong webview. A monotonic counter rather than `crypto.randomUUID` because the
 * only collision that matters is two windows minting at the same instant, and
 * both would have to produce the same integer — which the engine's upsert makes
 * a merge rather than a failure. `Date.now()` alone would not be enough: two
 * windows opened in the same millisecond are the ordinary case, not the exotic
 * one.
 */
export function tabKey(): string {
  return `${KEY_PREFIX}${Date.now().toString(36)}_${(keyCounter++).toString(36)}`;
}

/** Is this a key this app minted? Used to refuse a stranger's row. */
function isTabKey(value: unknown): value is string {
  return typeof value === "string" && value.startsWith(KEY_PREFIX) && value.length <= 64;
}

// ── keys ────────────────────────────────────────────────────────────────────

/**
 * Give every tab a durable key, and report the ones that were missing.
 *
 * Minting here rather than in the tab constructors is the point: `openTab`,
 * `openConversation`, `openBlankTab`, `openBrowserTab` and a restored tab are
 * five paths, and a key minted in four of them is a tab that syncs as a
 * stranger — it would be pushed, and then the engine would hold two rows for one
 * tab. Anything without a key that should not have one (a terminal, which is not
 * remembered at all) is left alone.
 *
 * Returns the same state object when every tab already had a key, so a caller
 * can use the reference to decide whether a re-render is needed.
 */
export function ensureKeys(state: TabState): { state: TabState; minted: string[] } {
  const minted: string[] = [];
  let changed = false;
  const tabs = state.tabs.map((tab) => {
    if (isLocalTab(tab)) return tab;
    if (isTabKey(tab.key)) return tab;
    changed = true;
    const key = tabKey();
    minted.push(key);
    return { ...tab, key };
  });
  return { state: changed ? { ...state, tabs } : state, minted };
}

// ── the wire shape ──────────────────────────────────────────────────────────

/** A tab as this module serialises it into the engine's `payload`. */
interface WireTab {
  kind: "chat" | "browser";
  conversationId?: string;
  url?: string;
  history?: PersistedHistory;
  workspaceId?: string;
}

/**
 * The engine's payload, as bytes. Opaque to the engine by design, and this is
 * the only place that knows its shape — which is the property that keeps a
 * second version of the app from having to migrate rows it cannot read.
 */
export function encodeTab(tab: Tab): string {
  const wire: WireTab = { kind: tab.kind as "chat" | "browser" };
  if (tab.kind === "chat") {
    if (tab.conversationId) wire.conversationId = tab.conversationId;
  } else {
    if (tab.url) wire.url = tab.url;
    if (tab.history) wire.history = tab.history;
  }
  if (tab.workspaceId) wire.workspaceId = tab.workspaceId;
  return JSON.stringify(wire);
}

/** An address the shell would refuse is not restored, for the same reason as in
 * `tabPersistence.parseLayout`: the refusal arrives before a webview exists. */
function trustedUrl(raw: string): string | null {
  const classified = classifyBrowserAddress(raw, "shared");
  return classified.kind === "shell" ? classified.url : null;
}

/**
 * One engine row, decoded.
 *
 * `null` for a row that cannot be believed, and the reasons are the same ones
 * `parseLayout` gives, because the payload is the same bytes from a place with
 * no type checking at all: a key this app did not mint, a kind that is not one of
 * the two, a payload that is not JSON, an address the shell would refuse, a
 * history whose cursor does not point at the tab's own address. **A row from
 * another window is not a special case here.** Two windows are ordinary, so
 * anything one of them can write, the other has to be able to refuse.
 */
export function decodeTab(row: EngineTab): SharedTab | null {
  if (!isTabKey(row.key)) return null;
  if (row.kind !== "chat" && row.kind !== "browser") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(row.payload);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const wire = parsed as WireTab;
  const workspaceId =
    typeof wire.workspaceId === "string" && wire.workspaceId.trim()
      ? wire.workspaceId
      : undefined;
  if (row.kind === "chat") {
    const conversationId =
      typeof wire.conversationId === "string" && wire.conversationId.trim()
        ? wire.conversationId
        : undefined;
    return {
      key: row.key,
      tab: {
        id: tabId("chat"),
        key: row.key,
        kind: "chat",
        title: UNTITLED_THREAD_TITLE,
        conversationId,
        workspaceId,
      },
    };
  }
  let url: string | undefined;
  if (wire.url !== undefined) {
    if (typeof wire.url !== "string") return null;
    const canonical = trustedUrl(wire.url);
    if (canonical === null) return null;
    url = canonical;
  }
  return {
    key: row.key,
    tab: {
      id: tabId("browser"),
      key: row.key,
      kind: "browser",
      title: url ? hostOf(url) : "",
      url,
      history: sharedHistory(wire.history, url),
      workspaceId,
    },
  };
}

/**
 * A history from the engine, believed to the same rules as the mirror's.
 *
 * Dropped rather than repaired, for the reason `restoredHistory` gives: a
 * disagreeing stack costs the back button, and a repaired one would show an
 * address the tab is not on.
 */
function sharedHistory(raw: unknown, url: string | undefined): PersistedHistory | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const { entries, index } = raw as { entries?: unknown; index?: unknown };
  if (!Array.isArray(entries) || typeof index !== "number" || !Number.isInteger(index)) {
    return undefined;
  }
  const clean: string[] = [];
  for (const entry of entries) {
    if (typeof entry !== "string" || !entry.trim()) return undefined;
    const canonical = trustedUrl(entry);
    if (canonical === null) return undefined;
    clean.push(canonical);
  }
  if (clean.length === 0) return index === -1 ? { entries: [], index: -1 } : undefined;
  if (index < 0 || index > clean.length - 1) return undefined;
  if (url !== undefined && clean[index] !== url) return undefined;
  return { entries: clean, index };
}

/** Engine rows, in the order the engine read them, minus the ones refused. */
export function decodeStrip(rows: readonly EngineTab[]): SharedTab[] {
  const out: SharedTab[] = [];
  for (const row of rows) {
    const shared = decodeTab(row);
    if (shared) out.push(shared);
  }
  return out.slice(0, MAX_PERSISTED_TABS);
}

// ── the mirror ──────────────────────────────────────────────────────────────

/** What the mirror holds: the layout, plus the keys the engine has not seen. */
interface Mirror {
  layout: PersistedLayout;
  /**
   * Keys this window has removed and the engine has not yet acknowledged.
   *
   * Without this a close made offline is a tab that comes *back*: the engine
   * still has the row, and the next push is a set of upserts that never mentions
   * it again. A queue is what makes the close a statement rather than an
   * absence, and it is drained when a `DELETE` succeeds — not before, so a
   * failed delete is retried rather than forgotten.
   */
  pendingRemovals: string[];
  /** Keys pushed but never acknowledged, for the same reason. */
  pendingWrites: string[];
}

const MIRROR_VERSION = 1;

function emptyMirror(): Mirror {
  return {
    layout: emptyLayout(),
    pendingRemovals: [],
    pendingWrites: [],
  };
}

function readMirror(storage: LayoutStorage | undefined): Mirror {
  if (!storage) return emptyMirror();
  try {
    const raw = storage.getItem(TABS_KEY);
    if (!raw) return emptyMirror();
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return emptyMirror();
    const { version, layout, pendingRemovals, pendingWrites } = parsed as Record<string, unknown>;
    if (version !== MIRROR_VERSION) return emptyMirror();
    const strings = (value: unknown): string[] =>
      Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
    return {
      // `parseLayout` does the believing, so the mirror's validation is the
      // mirror's *storage* validation and not a second implementation of it.
      layout: parseMirrorLayout(layout),
      pendingRemovals: strings(pendingRemovals).filter(isTabKey),
      pendingWrites: strings(pendingWrites).filter(isTabKey),
    };
  } catch {
    return emptyMirror();
  }
}

function writeMirror(storage: LayoutStorage | undefined, mirror: Mirror): void {
  if (!storage) return;
  try {
    storage.setItem(
      TABS_KEY,
      JSON.stringify({
        version: MIRROR_VERSION,
        layout: mirror.layout,
        pendingRemovals: mirror.pendingRemovals,
        pendingWrites: mirror.pendingWrites,
      }),
    );
  } catch {
    // Quota, or a store that is off. The strip is then a restart old, which is
    // the cost this module has always accepted rather than an error in a render.
  }
}

/**
 * The layout out of a mirror blob, through the parser that already believes
 * these shapes.
 *
 * Not a second implementation: what a browser tab is, what a history may be and
 * which address the shell would refuse all live in `tabPersistence`, and this
 * module's job is to *carry* that layout, not to restate the rules. The import
 * above is not a cycle either — `tabPersistence` knows nothing about this file.
 */
function parseMirrorLayout(raw: unknown): PersistedLayout {
  return parseLayout(raw === undefined || raw === null ? null : JSON.stringify(raw));
}

// ── pushing ─────────────────────────────────────────────────────────────────

/** One change to send, and the keys a close should retire. */
export interface SyncPlan {
  upserts: EngineTab[];
  removals: string[];
}

/**
 * What this window has to say to the engine.
 *
 * Only tabs that are *remembered* (never a terminal) and only ones the engine
 * has not already been told about, so an unchanged strip costs nothing on a poll.
 * The comparison is on the payload bytes and the position, which is the whole
 * state of a row: two tabs that would serialise identically are not re-sent,
 * and a tab whose address changed is.
 */
export function planChanges(
  state: TabState,
  mirror: Mirror,
  known: ReadonlySet<string>,
  engineRows: readonly EngineTab[] = [],
): SyncPlan {
  const upserts: EngineTab[] = [];
  const removals = mirror.pendingRemovals.filter((key) => known.has(key));
  const pendingWrites = new Set(mirror.pendingWrites);
  // What the engine last said each row holds. Without it "known" could only mean
  // "the key exists", and a tab that navigated after its first push was never
  // sent again: its address changed here and stayed what it was there.
  const held = new Map(engineRows.map((row) => [row.key, row.payload]));
  state.tabs.forEach((tab, index) => {
    if (isLocalTab(tab) || !isTabKey(tab.key)) return;
    const payload = encodeTab(tab);
    if (
      known.has(tab.key) &&
      !pendingWrites.has(tab.key) &&
      (!held.has(tab.key) || held.get(tab.key) === payload)
    ) {
      return;
    }
    upserts.push({
      key: tab.key,
      position: index,
      // Never a local kind: `isLocalTab` returned above, and `Tab` is one interface rather than a union, so the
      // compiler cannot see what that check ruled out.
      kind: tab.kind as EngineTab["kind"],
      payload,
    });
  });
  return { upserts, removals };
}

/**
 * Owe the engine a delete for a tab the user closed.
 *
 * The only place a close becomes a statement rather than an absence. `reconcile`
 * adopts any row that only the engine holds, so a closed tab whose key was never
 * queued here comes back on the next pull or the next boot. A key that is not a
 * tab key (a terminal, or a tab not yet keyed) owes nothing: the engine has never
 * held it.
 */
export function queueRemoval(mirror: Mirror, key: string | undefined): Mirror {
  if (!isTabKey(key) || mirror.pendingRemovals.includes(key)) return mirror;
  return {
    ...mirror,
    pendingRemovals: [...mirror.pendingRemovals, key],
    pendingWrites: mirror.pendingWrites.filter((k) => k !== key),
  };
}

/**
 * Record that these upserts are in flight, before they are sent.
 *
 * `acknowledge` retires them once the engine has answered. A push that dies
 * between the two leaves them here, so the next run re-sends them even for a key
 * the engine already knows — a changed row is not lost because a request was.
 */
export function markPending(mirror: Mirror, plan: SyncPlan): Mirror {
  const pending = new Set(mirror.pendingWrites);
  for (const tab of plan.upserts) pending.add(tab.key);
  if (pending.size === mirror.pendingWrites.length) return mirror;
  return { ...mirror, pendingWrites: [...pending] };
}

/**
 * Record what the engine has now been told, and hand back the settled mirror.
 *
 * A pending removal is *satisfied* in either of two ways, and dropping only the
 * first is how a queue grows forever: it is satisfied when this run deleted the
 * key, and equally when the engine no longer has it at all — another window
 * closed that tab, or a previous run's `DELETE` landed and the response was lost
 * on the way back. `known` is what tells those two apart, so it is an argument
 * rather than something inferred from the plan.
 *
 * The removals that survive are the ones still owed, and those are exactly the
 * ones `reconcile` must hold back: until the engine has confirmed a close, its
 * copy of the row is the pre-close one, and adopting it would put back the tab
 * the user just closed.
 */
export function acknowledge(
  mirror: Mirror,
  plan: SyncPlan,
  known: ReadonlySet<string>,
): Mirror {
  return {
    layout: mirror.layout,
    pendingRemovals: mirror.pendingRemovals.filter((key) => known.has(key)),
    pendingWrites: mirror.pendingWrites.filter((key) =>
      !plan.upserts.some((tab) => tab.key === key),
    ),
  };
}

// ── reconciling ─────────────────────────────────────────────────────────────

/**
 * Fold what the engine says into this window's strip.
 *
 * Three rules, in this order, and the order is the design:
 *
 * 1. **A key the engine no longer has is gone here too** — but only a key the
 *    engine has *confirmed* before (`known`). This is what makes a close in
 *    another window a close in this one, and the qualification is the whole
 *    difference between sharing a strip and losing one: an engine that is empty
 *    because it is *new* (a fresh `codify.db`, an engine that has never been
 *    told) is not evidence that the tabs this window is showing are gone. A
 *    window with a full mirror and an empty engine would empty itself, and the
 *    mirror exists precisely so that the strip comes back when the engine does
 *    not. So a key the engine has never confirmed is kept and pushed, not
 *    dropped. The cost is a close another window made while this one was shut,
 *    which survives one boot; that is the safe direction, because the other
 *    failure is a user's tabs vanishing.
 * 2. **A key this window has wins over the engine's copy of it.** Two windows
 *    can have the same tab open at different addresses (one navigated), and the
 *    window that is looking at it is the authority on what it is looking at.
 *    The push that follows tells the engine about it, so the other window
 *    converges on the next round trip rather than both winning at once.
 * 3. **A key only the engine has is adopted**, at the engine's position, with a
 *    fresh local `id` — because the webview and the PTY this window would name
 *    after it do not exist yet, and the pane seats the page from the tab's own
 *    `url` anyway.
 *
 * `removalsToKeep` are this window's own unacknowledged closes, and they are
 * held back from adoption for as long as they are owed: until the engine has
 * confirmed the delete, its copy of the row is the pre-close one.
 *
 * `activeKey` is the tab this window was showing, named by *key* so it survives
 * both the re-keying of a merge and the loss of a tab. A tab that was closed
 * elsewhere leaves nothing to focus, and the same neighbour-to-the-left rule the
 * mirror uses applies.
 *
 * **The result is referentially stable.** When the rows describe the strip the
 * window already has, the very `state` object handed in comes back — tabs are
 * kept by identity, not rebuilt from the rows (every decode mints a fresh `id`,
 * so a rebuilt strip is *always* unequal). The app reconciles inside
 * `setTabState((prev) => ...)`; an unstable merge there re-renders, re-fires the
 * push effect, pulls again, and loops forever — the shape that kept
 * `tests/terminalEndToEnd.test.ts` pending for as long as App stayed mounted.
 */
export function reconcile(
  state: TabState,
  rows: readonly EngineTab[],
  activeKey: string | null,
  removalsToKeep: readonly string[],
  known: ReadonlySet<string> = new Set(),
): TabState {
  const remote = new Map<string, SharedTab>();
  for (const shared of decodeStrip(rows)) remote.set(shared.key, shared);

  // Rule 1, minus the ones this window is still trying to close and minus the
  // ones the engine has never confirmed.
  const retiring = new Set(removalsToKeep);
  const kept: Tab[] = [];
  let lostActive = activeKey === null;
  for (const tab of state.tabs) {
    const key = tab.key;
    if (key !== undefined && retiring.has(key)) continue;
    if (key !== undefined && known.has(key) && !remote.has(key)) {
      if (key === activeKey) lostActive = true;
      continue;
    }
    kept.push(tab);
  }

  // Rule 2 and 3, in engine order, so a window that has never seen a tab shows
  // the strip the way it is meant to read.
  //
  // The local tab object is **kept as-is**, not rebuilt, even when the engine
  // also holds it: this function runs inside `setTabState((prev) => ...)`, and
  // a pull that changes nothing must hand React back the very objects it had —
  // a rebuilt-but-equal tab is a new object, and a strip of new objects is a
  // new state, which re-renders the app, which re-fires the push effect, which
  // pulls again: the infinite refire that hung the end-to-end test and would
  // have quietly request-looped against a real engine forever. `decodeStrip`
  // cannot be reused for tabs this window already has because it mints a fresh
  // `id` per decode (`id` names *this window's* webview).
  const merged: Tab[] = [];
  const taken = new Set<string>();
  for (const tab of kept) {
    merged.push(tab);
    if (tab.key !== undefined && remote.has(tab.key)) taken.add(tab.key);
  }
  // Same objects, same order, same count — and the engine contributed nothing —
  // is the *no-op* answer, and a no-op must return the previous state.
  let changed = kept.length !== state.tabs.length;
  if (!changed) {
    for (let i = 0; i < kept.length; i++) {
      if (kept[i] !== state.tabs[i]) {
        changed = true;
        break;
      }
    }
  }
  for (const shared of decodeStrip(rows)) {
    if (taken.has(shared.key) || retiring.has(shared.key)) continue;
    merged.push(shared.tab);
    changed = true;
  }

  if (!changed) return state;
  const next: TabState = { tabs: merged, activeId: state.activeId };
  // The active tab is found by key, then by identity: a window whose active tab
  // was a terminal (which is never shared) has no key to look for.
  const stillThere = merged.some(
    (tab) => (activeKey !== null && tab.key === activeKey) || tab.id === state.activeId,
  );
  if (stillThere && !lostActive) {
    const target = merged.find(
      (tab) => (activeKey !== null && tab.key === activeKey) || tab.id === state.activeId,
    );
    // A different active tab is a focus change; the same one needs no re-focus
    // (and `next` is already a real change, or the early return above fired).
    if (target && target.id !== state.activeId) return focusTab(next, target.id);
    return next;
  }
  if (merged.length === 0) return { ...next, activeId: null };
  // Gone from under us: land on the neighbour to the left, as a closed tab does.
  const index = state.tabs.findIndex(
    (tab) => (activeKey !== null && tab.key === activeKey) || tab.id === state.activeId,
  );
  const landing = index > 0 ? Math.min(index - 1, merged.length - 1) : 0;
  return focusTab(next, merged[landing].id);
}

/** The key of the tab this window is showing, for talking to the engine. */
export function activeKeyOf(state: TabState): string | null {
  const active = state.tabs.find((tab) => tab.id === state.activeId);
  return active?.key ?? null;
}

/** An empty strip, named so the boot path reads as a decision. */
export function emptySharedState(): TabState {
  return emptyTabs;
}

/** The mirror, read. Exported for the boot path and for tests. */
export function readLayoutMirror(storage: LayoutStorage | undefined): Mirror {
  return readMirror(storage);
}

/** The mirror, written. */
export function writeLayoutMirror(
  storage: LayoutStorage | undefined,
  mirror: Mirror,
): void {
  writeMirror(storage, mirror);
}

/** A new mirror, for a window that has never synced. */
export function newMirror(layout: PersistedLayout = emptyLayout()): Mirror {
  return layout === emptyLayout() ? emptyMirror() : { ...emptyMirror(), layout };
}

/** The storage a window should mirror into, or `undefined` where there is none. */
export function layoutStorage(): LayoutStorage | undefined {
  return browserStorage();
}

/** The layout half of a mirror, for `tabPersistence`'s own writer. */
export function mirrorLayout(mirror: Mirror): PersistedLayout {
  return mirror.layout;
}

export type { Mirror };
export { MAX_PERSISTED_HISTORY };
