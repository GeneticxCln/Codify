/**
 * Where the divider between two panes was left.
 *
 * View state, not a record, like the left panel's (`sidebarPref.ts`): it is how this window is laid out, so it lives
 * in `localStorage` and never in a tab row or the engine's `/shell/tabs` (`docs/09` §2.1). An even split is what a
 * missing, empty or unreadable value means, because a divider stuck against one edge by a misread number would hide
 * most of a pane with no hint of why. What is read is only the position; `panes.clampRatio` keeps it inside what both
 * panes need at the window's size when it is used, so a value saved in a wide window is safe in a narrow one.
 */

import { DEFAULT_RATIO } from "./panes";

/** Where the position lives. */
export const SPLIT_RATIO_KEY = "codify.splitRatio";

type Reader = Pick<Storage, "getItem">;
type Writer = Pick<Storage, "setItem">;

/** The least and most a stored position may be: past these it was not written by this build. */
const LEAST = 0.1;
const MOST = 0.9;

function userStorage(): Storage | undefined {
  try {
    if (typeof window !== "undefined" && window.localStorage) return window.localStorage;
  } catch {
    // Blocked, or no window yet: an even split is the honest default.
  }
  return undefined;
}

/** The remembered position of the divider, as the left pane's share of the row. */
export function readSplitRatio(store: Reader | undefined = userStorage()): number {
  try {
    const raw = store?.getItem(SPLIT_RATIO_KEY);
    if (raw === null || raw === undefined) return DEFAULT_RATIO;
    // A blank string reads as 0, which is outside the limits below like any other nonsense.
    const value = Number(raw);
    return Number.isFinite(value) && value >= LEAST && value <= MOST ? value : DEFAULT_RATIO;
  } catch {
    return DEFAULT_RATIO;
  }
}

/** Remember the position. A store that refuses just means it is not remembered. */
export function writeSplitRatio(ratio: number, store: Writer | undefined = userStorage()): void {
  if (!Number.isFinite(ratio)) return;
  try {
    store?.setItem(SPLIT_RATIO_KEY, String(ratio));
  } catch {
    // Not remembered; the window still shows where it was put.
  }
}
