/**
 * Events the desktop shell emits to the app window.
 *
 * The engine's own event stream is a WebSocket (`goalStream.ts`). This is the
 * other channel — the Tauri IPC one — carrying facts the shell alone knows
 * because they happened to the shell: a terminal's output, a browser page
 * asking for a popup.
 *
 * Small, and guarded on purpose. Outside Tauri there is no shell to emit from,
 * so [`listenShellEvent`] resolves to a no-op unlisten instead of throwing: the
 * same posture `engineFailureReason` takes in `api.ts`, and the reason a caller
 * never has to ask "am I in the desktop app" itself.
 *
 * The event names and payload shapes here are the other end of
 * `src-tauri/src/browser/` and `src-tauri/src/terminal.rs`. Nothing in either
 * type system spans the two, so a test on the Rust side reads *this file* and
 * fails if they ever drift — the same "parse the committed file, do not assume"
 * move as the browser capability test.
 *
 * The browser no longer announces a destroyed window: pages are child
 * webviews of the main window, a child cannot close itself, and the tab strip
 * is the only closer — the separate-window build's closed event is gone with
 * the window it announced. The one thing a page can still ask the shell for —
 * a popup window — is refused by the shell and *announced* here instead, so
 * the user can open the target as a real tab.
 */

/** A browser page tried to open a popup window; the shell refused it. */
export const BROWSER_POPUP_REQUESTED = "browser-popup-requested";

/** The payload `browser-popup-requested` carries. */
export interface BrowserPopupRequested {
  /** The tab that tried to open the popup. */
  tab_id: string;
  /** The address it asked for, already through the shell's URL guard. */
  url: string;
}

/**
 * A `browser-popup-requested` payload, or null when it is not one.
 *
 * Validated rather than cast. The event crosses a process boundary and feeds
 * a handler that *opens a tab* — a payload that is null, a string, or carries
 * a non-addressable field must open nothing at all rather than a tab pointed
 * nowhere. `url` is not re-parsed for web-ness here: the shell already ran
 * the guard before announcing, and a second guard would be a second opinion
 * on a decision that has one owner.
 */
export function readBrowserPopupRequested(
  payload: unknown
): BrowserPopupRequested | null {
  if (typeof payload !== "object" || payload === null) return null;
  const { tab_id, url } = payload as { tab_id?: unknown; url?: unknown };
  if (typeof tab_id !== "string" || tab_id.length === 0) return null;
  if (typeof url !== "string" || url.length === 0) return null;
  return { tab_id, url };
}

/**
 * The page-life events a browser page reports about itself.
 *
 * The payloads are read by `browserPageState.readBrowserPageState`, which is
 * where the validation lives — one reader for the three, because they share
 * a wire shape.
 */
export const BROWSER_PAGE_LOADING = "browser-page-loading";
export const BROWSER_PAGE_LOADED = "browser-page-loaded";
export const BROWSER_PAGE_TITLED = "browser-page-titled";

/**
 * The keyboard went into a browser page: the toolkit's own focus changed to it.
 *
 * A page is a native view, so the app's DOM never sees a press inside one and
 * nothing in the window moves when a person clicks into a page that sits beside
 * a chat. The shell reports the toolkit's focus instead, and the split's focus
 * marker follows it. A report, not a request: the window acts on it only for a
 * page it is drawing.
 */
export const BROWSER_PAGE_FOCUSED = "browser-page-focused";

/**
 * The tab id in a `browser-page-focused` payload, or null when the payload is
 * not one.
 *
 * Validated for the reason every reader here is: the payload crosses a process
 * boundary and feeds a handler that moves the focus, so anything that is not
 * an id must move it nowhere. The id is *not* checked against the open tabs
 * here — which tabs exist is the window's to know, and it asks.
 */
export function readBrowserPageFocused(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) return null;
  const tab_id = (payload as { tab_id?: unknown }).tab_id;
  return typeof tab_id === "string" && tab_id.length > 0 ? tab_id : null;
}

/** A terminal's output, as it arrives. */
export const TERMINAL_OUTPUT = "terminal-output";

/** A terminal's shell finished. The scrollback stays; the input does not. */
export const TERMINAL_EXIT = "terminal-exit";

/**
 * A program in a terminal asked for attention, in one of the standard escape sequences (OSC 9, 777 or 99).
 *
 * The sequence is still in the output as well, so the person can see in their terminal exactly what the program
 * wrote; this is the same bytes read a second way. `seq` is the shell's own count for that terminal, so the
 * window's id (`terminal:<id>:<seq>`) makes two prints two entries and a re-delivered event one.
 */
export const TERMINAL_NOTIFICATION = "terminal-notification";

/** The payload of `terminal-notification`. */
export interface TerminalNotification {
  /** The terminal that printed it — the tab's own id, `term-3`. */
  id: string;
  /** This shell's count for this terminal; monotonic within one session. */
  seq: number;
  /** May be empty: OSC 9 carries only a body, and OSC 99's title half carries only a title. */
  title: string;
  body: string;
}

/**
 * A `terminal-notification` payload, or null when it is not one.
 *
 * Validated rather than cast, like the readers above: the payload crosses a process boundary and feeds an inbox
 * entry, so a sequence whose fields are the right shape but the wrong types must make no entry rather than one
 * that says `undefined`. The two halves are trimmed here and an empty pair is refused — the shell already clips
 * them, and this is the second half of that promise rather than a second opinion. `seq` is what makes the entry's
 * id stable, so it must be a whole number; a missing one cannot be invented without risking a duplicate.
 */
export function readTerminalNotification(payload: unknown): TerminalNotification | null {
  if (typeof payload !== "object" || payload === null) return null;
  const { id, seq, title, body } = payload as {
    id?: unknown;
    seq?: unknown;
    title?: unknown;
    body?: unknown;
  };
  if (typeof id !== "string" || id.length === 0) return null;
  if (typeof seq !== "number" || !Number.isInteger(seq) || seq < 0) return null;
  if (typeof title !== "string" || typeof body !== "string") return null;
  const named = title.trim();
  const said = body.trim();
  if (named.length === 0 && said.length === 0) return null;
  return { id, seq, title: named, body: said };
}

/**
 * The one chunk of `terminal-output`, or null when the payload is not one.
 *
 * Validated for the same reason [`readBrowserPopupRequested`] is, and one step
 * further: `data` is written straight into a live terminal rather than closing a
 * tab, so a payload that is the right object with the wrong field types would
 * reach xterm as `"undefined"` on screen. Both halves must be strings, and the
 * id must be non-empty — a terminal named `""` is a terminal nothing will match.
 *
 * The chunk is *not* required to be non-empty. An empty `data` is a real thing
 * the reader thread can emit (a `read` that returned bytes the lossy decode
 * turned into nothing), and refusing it would silently drop a write that xterm
 * would have handled.
 */
export function readTerminalOutput(
  payload: unknown
): { id: string; data: string } | null {
  if (typeof payload !== "object" || payload === null) return null;
  const { id, data } = payload as { id?: unknown; data?: unknown };
  if (typeof id !== "string" || id.length === 0) return null;
  if (typeof data !== "string") return null;
  return { id, data };
}

/**
 * The terminal id in a `terminal-exit` payload, or null when the payload is not
 * one.
 *
 * An exit is what makes a terminal tab's input stop accepting keys, so a
 * malformed payload here means a shell that has finished but still takes
 * keystrokes — and every one of them goes to `codify_terminal_write` for an id
 * that is no longer in the map.
 */
export function readTerminalExit(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) return null;
  const id = (payload as { id?: unknown }).id;
  return typeof id === "string" && id.length > 0 ? id : null;
}

/**
 * A machine's output, as it arrives. A different event from `terminal-output` on purpose: the app's terminal
 * recorder files every chunk of that one into the workspace scrollback, and what a jail printed is not a
 * terminal's history (`docs/09` §14).
 */
export const MACHINE_OUTPUT = "machine-output";

/** A machine's shell finished. The screen stays; the input does not. */
export const MACHINE_EXIT = "machine-exit";

/**
 * The one chunk of `machine-output`, or null when the payload is not one. Validated like
 * [`readTerminalOutput`], for the same reason: `data` is written straight into a screen, so a payload with the
 * right shape and the wrong field types would reach it as `"undefined"`.
 */
export function readMachineOutput(payload: unknown): { id: string; data: string } | null {
  if (typeof payload !== "object" || payload === null) return null;
  const { id, data } = payload as { id?: unknown; data?: unknown };
  if (typeof id !== "string" || id.length === 0) return null;
  if (typeof data !== "string") return null;
  return { id, data };
}

/** The machine id in a `machine-exit` payload, or null when the payload is not one. */
export function readMachineExit(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) return null;
  const id = (payload as { id?: unknown }).id;
  return typeof id === "string" && id.length > 0 ? id : null;
}

/**
 * How far the shell's download of a release package has come.
 *
 * A package is tens of megabytes and the download takes minutes, so the button that started it
 * says where it has got to over the whole of that time rather than standing on one sentence and
 * looking broken. The shell sends one of these per whole percent — plus the first byte and the
 * last, which are what move a bar off "starting" and onto 100% — so this is a hundred-odd messages
 * over a download, not the thousands of chunks the file actually arrives in.
 *
 * It is the only thing the shell says while the download runs, and it is deliberately small: bytes
 * received, and the size the server announced. The window draws it (`updateProgress.ts`), and
 * nothing acts on it — the answer to the command is still what says where the file landed.
 */
export const UPDATE_PROGRESS = "update-progress";

/** The payload `update-progress` carries. */
export interface UpdateProgress {
  /** Bytes received so far. */
  received: number;
  /**
   * The size the server announced up front, or null when it announced none — a server that sends
   * no `Content-Length`. Null is "size unknown", never "nothing left": the pane falls back to the
   * size the release reported and shows a bar that cannot fill only when neither exists.
   */
  total: number | null;
}

/**
 * An `update-progress` payload, or null when it is not one.
 *
 * Validated rather than cast, like every reader here, and for the same reason: the payload crosses
 * a process boundary and feeds a bar that is drawn from it, so a `received` that is not a count
 * must move nothing rather than paint `NaN` across the pane. A `total` that is present must be a
 * size — zero and a negative mean the emitting end is not describing this download, and a bar that
 * divided by them would read 0% or worse for the whole of a download that is going fine.
 */
export function readUpdateProgress(payload: unknown): UpdateProgress | null {
  if (typeof payload !== "object" || payload === null) return null;
  const { received, total } = payload as { received?: unknown; total?: unknown };
  if (typeof received !== "number" || !Number.isInteger(received) || received < 0) return null;
  if (total === null || total === undefined) return { received, total: null };
  if (typeof total !== "number" || !Number.isInteger(total) || total <= 0) return null;
  return { received, total };
}

/**
 * Subscribe to a shell event; resolves to the unsubscribe function.
 *
 * Resolving to a function even when there is nothing to subscribe to is the
 * point: the caller can hold the result and call it unconditionally in its
 * cleanup, with no branch of its own on whether the shell is there.
 */
export async function listenShellEvent<T>(
  event: string,
  handler: (payload: T) => void,
): Promise<() => void> {
  if (typeof window === "undefined" || !(window as any).__TAURI_INTERNALS__) {
    return () => {};
  }
  const { listen } = await import("@tauri-apps/api/event");
  return listen<T>(event, (received) => handler(received.payload));
}
