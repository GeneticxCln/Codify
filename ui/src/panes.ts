/**
 * Split panes: which two tabs may share the centre column, and what happens to the pair as the tab strip moves.
 *
 * The centre column shows one thing, and a split shows two: a chat, a terminal or an editor beside any of those, but never two
 * chats and never a browser page. The pair is `{ panes, focused }` and lives **beside** the tab state, never in it: `tabs.ts`, `tabPersistence.ts`,
 * `layoutSync.ts` and the engine's shared strip never see it, so none of their return shapes can quietly drop it, and
 * a split is view state in the sense of `docs/09` §2.1. The active tab stays what it always was, the tab the person
 * is working in, which is now the *focused pane's*; every rule below is about keeping those two in step.
 *
 * Pure and DOM-free, like `tabs.ts`, so each rule has a test that needs no renderer (`docs/09` §12).
 *
 * What may be paired is a short list on purpose:
 *
 *  - **A browser page may not.** It is a native child window seated over one measured rectangle, which the shell
 *    shows or hides as a whole (`docs/09` §7.3); two visible pages, or one in half a column, would be Rust changes
 *    this does not make. Leaving them out is also what makes a DOM divider safe: no native view is ever in the
 *    centre while a split is showing.
 *  - **Two conversations may not.** There is one message box, one "a goal is running" state and one `insertRequest`.
 */

import type { Tab, TabState } from "./tabs";

/** Which of the two panes, left to right. */
export type PaneSide = 0 | 1;

/**
 * The pair, and which half the person last used.
 *
 * `focused` is stored so a tab that arrives from outside the pair has somewhere to go: it takes the focused pane's
 * place (`resolveSplit`).
 */
export interface Split {
  readonly panes: readonly [string, string];
  readonly focused: PaneSide;
}

/** What is in the two panes right now, resolved to tabs. */
export interface ShownSplit {
  left: Tab;
  right: Tab;
  focused: PaneSide;
}

/**
 * The narrowest a pane may be, in rem. Everything it is compared with is rem (`drawers.ts`), so the UI scale moves it
 * with the text. Twenty-two is about sixty columns of terminal and a composer row that wraps rather than breaks; it is
 * a judgement, pinned by a test, and the one number to turn if a real window says otherwise.
 */
export const MIN_PANE_REM = 22;

/** An even split. */
export const DEFAULT_RATIO = 0.5;

/** Why two tabs cannot be paired. */
export type PairRefusal = "browser" | "two-chats" | "same" | "missing";

/** The sentence for each, said where a split was asked for and could not be made. */
export const PAIR_REFUSALS: Readonly<Record<PairRefusal, string>> = {
  browser:
    "A browser page can't be shown in a split: it is a separate native view that cannot share the column with another pane.",
  "two-chats": "Two conversations can't be side by side yet: they would share one message box.",
  same: "That tab is already in view.",
  missing: "Open a tab first.",
};

/**
 * Why `a` and `b` cannot share the column, or null when they can.
 *
 * Checked in an order that names the real obstacle: a tab that is not there beats everything, being the same tab
 * beats being a page, and a page is named before two chats are.
 */
export function pairRefusal(a: Tab | undefined, b: Tab | undefined): PairRefusal | null {
  if (!a || !b) return "missing";
  if (a.id === b.id) return "same";
  if (a.kind === "browser" || b.kind === "browser") return "browser";
  if (a.kind === "chat" && b.kind === "chat") return "two-chats";
  return null;
}

export function canPair(a: Tab | undefined, b: Tab | undefined): boolean {
  return pairRefusal(a, b) === null;
}

const tabById = (state: TabState, id: string | null): Tab | undefined =>
  id === null ? undefined : state.tabs.find((t) => t.id === id);

export type StartResult =
  | { ok: true; state: TabState; split: Split }
  | { ok: false; why: PairRefusal };

/**
 * Show `otherId` beside the active tab: the active tab on the left, the other on the right, and the right one focused,
 * because it is the one just asked for. Opens and closes nothing.
 */
export function startSplit(state: TabState, otherId: string): StartResult {
  const why = pairRefusal(tabById(state, state.activeId), tabById(state, otherId));
  if (why !== null || state.activeId === null) return { ok: false, why: why ?? "missing" };
  return {
    ok: true,
    state: { ...state, activeId: otherId },
    split: { panes: [state.activeId, otherId], focused: 1 },
  };
}

export type Partner = { kind: "tab"; id: string } | { kind: "new-terminal" } | { kind: "refused"; why: PairRefusal };

/**
 * Who the active tab is split with when nobody was named (the shortcut, and "split" in the palette).
 *
 * A chat takes the nearest terminal or editor, whichever is nearer: the conversation is the thing you want a file or a shell
 * beside. An editor takes the nearest chat (the assistant it is being edited with), then the nearest terminal. A terminal
 * takes the nearest other terminal, then the nearest chat; it never picks an editor for you, and neither does an editor pick
 * another editor. Nearest is by distance in the strip, and a tie goes to the right. A shell that has exited is never chosen
 * for you: it is not what was meant, and a new terminal is more use than a dead one. With nobody to share with, a new
 * terminal is asked for; a chat is never offered a chat.
 */
export function splitPartner(state: TabState): Partner {
  const at = state.tabs.findIndex((t) => t.id === state.activeId);
  const active = state.tabs[at];
  if (!active) return { kind: "refused", why: "missing" };
  if (active.kind === "browser") return { kind: "refused", why: "browser" };

  const nearest = (wants: (t: Tab) => boolean): Tab | undefined => {
    for (let d = 1; d < state.tabs.length; d += 1) {
      for (const i of [at + d, at - d]) {
        const t = state.tabs[i];
        if (t && wants(t)) return t;
      }
    }
    return undefined;
  };
  const liveTerminal = (t: Tab): boolean => t.kind === "terminal" && !t.exited;
  const chat = (t: Tab): boolean => t.kind === "chat";

  const pick =
    active.kind === "chat"
      ? nearest((t) => liveTerminal(t) || t.kind === "editor")
      : active.kind === "editor"
        ? (nearest(chat) ?? nearest(liveTerminal))
        : (nearest(liveTerminal) ?? nearest(chat));
  return pick ? { kind: "tab", id: pick.id } : { kind: "new-terminal" };
}

export interface Resolution {
  /** What to draw: null while there is no split, or it is waiting. */
  shown: ShownSplit | null;
  /** What to keep: the split as it now stands. The same object when nothing changed. */
  split: Split | null;
}

/**
 * Whether the split is showing, and what it now is.
 *
 * A split is **showing** while the active tab is one of its two, and then the active tab is the focused pane. When the
 * active tab is somewhere else:
 *
 *  1. it takes the **focused** pane's place if that is a valid pair;
 *  2. else it takes the **other** pane's place if that is (a chat shown while the terminal had focus replaces the
 *     chat, never makes two);
 *  3. else the split **waits**: kept, not drawn, and back as it was when the active tab returns to either pane. A link
 *     clicked in the chat opens a browser tab, and that must not destroy the split it was clicked in.
 *
 * A pane whose tab has gone ends the split. When nothing changed, the very `split` that came in comes back, so a
 * caller that writes the result back to state does not loop.
 */
export function resolveSplit(state: TabState, split: Split | null): Resolution {
  if (split === null) return { shown: null, split: null };
  const [leftId, rightId] = split.panes;
  const left = tabById(state, leftId);
  const right = tabById(state, rightId);
  if (!left || !right) return { shown: null, split: null };

  const shownFor = (l: Tab, r: Tab, focused: PaneSide, kept: Split): Resolution => ({
    shown: { left: l, right: r, focused },
    split: kept,
  });

  if (state.activeId === leftId || state.activeId === rightId) {
    const focused: PaneSide = state.activeId === leftId ? 0 : 1;
    const kept = focused === split.focused ? split : { panes: split.panes, focused };
    return shownFor(left, right, focused, kept);
  }

  const incoming = tabById(state, state.activeId);
  if (!incoming) return { shown: null, split };

  const replacing = (slot: PaneSide): Resolution | null => {
    const other = slot === 0 ? right : left;
    if (!canPair(incoming, other)) return null;
    const panes: readonly [string, string] = slot === 0 ? [incoming.id, rightId] : [leftId, incoming.id];
    const kept: Split = { panes, focused: slot };
    return slot === 0 ? shownFor(incoming, right, 0, kept) : shownFor(left, incoming, 1, kept);
  };
  return replacing(split.focused) ?? replacing(split.focused === 0 ? 1 : 0) ?? { shown: null, split };
}

/**
 * What closing `closedId` does to the split, or null when it does nothing to it.
 *
 * Closing a showing pane's tab ends the split and goes to the *other* pane's tab, which is what you were still
 * looking at; closing one of a waiting split's tabs just drops it, and moves nothing.
 */
export function closeInSplit(
  state: TabState,
  split: Split | null,
  closedId: string,
): { split: null; activate: string | null } | null {
  if (split === null || !split.panes.includes(closedId)) return null;
  const showing = split.panes.includes(state.activeId ?? "");
  const partner = split.panes[0] === closedId ? split.panes[1] : split.panes[0];
  return { split: null, activate: showing ? partner : null };
}

/** How far a divider may go when the row cannot be measured. */
const UNMEASURED_MIN = 0.2;

/**
 * A divider position kept inside what both panes need.
 *
 * Neither pane may be narrower than `MIN_PANE_REM` in the row's own units, so the limit moves with the UI scale. A row
 * too narrow for two minimum panes is split evenly (there is no room to move), a ratio that is not a number is an even
 * split, and a row that has no size yet keeps the divider off the edges rather than trusting a zero.
 */
export function clampRatio(ratio: number, containerPx: number, rootPx: number): number {
  if (!Number.isFinite(ratio)) return DEFAULT_RATIO;
  if (!(containerPx > 0) || !(rootPx > 0)) {
    return Math.min(Math.max(ratio, UNMEASURED_MIN), 1 - UNMEASURED_MIN);
  }
  const least = (MIN_PANE_REM * rootPx) / containerPx;
  if (least >= 0.5) return DEFAULT_RATIO;
  return Math.min(Math.max(ratio, least), 1 - least);
}
