/**
 * Whether the left panel (threads, New Project, Browser, Terminal, Settings) is showing.
 *
 * View state, not a record: it is how this window is laid out, so it lives in `localStorage` like the
 * theme and the UI scale, and never in a tab row or the engine's `/shell/tabs` (docs/09 §2.1: a tab
 * is what is open, not how the window around it looks). Open is the default and the only thing a
 * missing, empty or unrecognised value means: a panel that stays hidden because a stored word was
 * misread would take Browser, Terminal and Settings out of reach with no hint of why.
 */

/** Where the choice lives. */
export const SIDEBAR_KEY = "codify.sidebar";

type Reader = Pick<Storage, "getItem">;
type Writer = Pick<Storage, "setItem">;

function userStorage(): Storage | undefined {
  try {
    if (typeof window !== "undefined" && window.localStorage) return window.localStorage;
  } catch {
    // Blocked, or no window yet: open is the honest default.
  }
  return undefined;
}

/** Whether the panel should start open. Only the exact word `closed` hides it. */
export function readSidebarOpen(store: Reader | undefined = userStorage()): boolean {
  try {
    return store?.getItem(SIDEBAR_KEY) !== "closed";
  } catch {
    return true;
  }
}

/** Remember the choice. A store that refuses just means it is not remembered. */
export function writeSidebarOpen(open: boolean, store: Writer | undefined = userStorage()): void {
  try {
    store?.setItem(SIDEBAR_KEY, open ? "open" : "closed");
  } catch {
    // Not remembered; the window still shows what was chosen.
  }
}
