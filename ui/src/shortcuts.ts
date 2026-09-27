/**
 * The keyboard layer: which keystroke means what.
 *
 * A pure module, for the same reason `tabs.ts` is: the mapping is a table of
 * decisions that is easy to get subtly wrong (a digit with Shift, AltGr
 * pretending to be Ctrl, `Cmd+9` meaning *last* rather than *ninth*) and
 * impossible to see in markup. `ui/tests/shortcuts.test.ts` walks the table.
 * The wiring in `App.tsx` only dispatches what this returns.
 *
 * ## The rules, and why they are these
 *
 * - **The modifier is `Cmd` or `Ctrl`, either.** One rule instead of
 *   platform sniffing: `Cmd` on macOS, `Ctrl` everywhere else, and a Linux
 *   user's `Super` never reaches the DOM reliably anyway. Accepting both
 *   keeps the module free of `navigator.userAgent` and behaves like every
 *   other Electron/Tauri app that maps ⌃ to ⌘.
 * - **`Alt` never fires a shortcut.** AltGr on European layouts reports
 *   `ctrlKey` *and* `altKey` together — without this guard, AltGr+T on a
 *   German keyboard would open a tab while the user types.
 * - **Letters require no `Shift`.** `Cmd+Shift+T` is somebody else's
 *   muscle memory (reopen a closed tab); firing "new tab" for it would be
 *   the wrong surprise. Digits, by contrast, ignore `Shift`: `Cmd+Shift+1`
 *   is how the key is physically reached on some layouts.
 * - **Digits are read from `code`, not `key`.** On layouts where `1` is
 *   `Shift+1` (and `key` is `!`), the physical digit key still means
 *   "go to tab 1". `code` is the physical key, layout be damned.
 * - **`Cmd+9` is the *last* tab, not the ninth** — the browser convention.
 *   Tabs 1–8 jump by position; 9 lands on the end of the strip, which is
 *   the one a keyboard cannot otherwise name when there are more than nine.
 *
 * What this module cannot promise: an OS that consumes a keystroke before it
 * reaches the webview (some macOS setups close the window on ⌘W natively)
 * degrades to that platform default — the shortcut is registered in the
 * DOM, which is the last stop, not the first.
 */

/** What a resolved keystroke asks the shell to do. */
export type ShortcutAction =
  | { type: "new-tab" }
  | { type: "close-active-tab" }
  /** 0-based strip position, from `Cmd+1..8`. */
  | { type: "focus-tab"; index: number }
  | { type: "focus-last-tab" }
  | { type: "toggle-palette" };

/**
 * The five fields this decision needs — structurally satisfied by
 * `KeyboardEvent`, so `App.tsx` passes the real thing and the tests pass a
 * literal without a DOM.
 */
export interface KeyEventLike {
  readonly key: string;
  readonly code: string;
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly altKey: boolean;
  readonly shiftKey: boolean;
}

/** Resolve a key event to a shell action, or `null` — most keys are, and must stay — typing. */
export function resolveShortcut(e: KeyEventLike): ShortcutAction | null {
  if (!(e.metaKey || e.ctrlKey) || e.altKey) return null;

  if (!e.shiftKey) {
    switch (e.key.toLowerCase()) {
      case "t":
        return { type: "new-tab" };
      case "w":
        return { type: "close-active-tab" };
      case "k":
        return { type: "toggle-palette" };
      default:
        break;
    }
  }

  // The physical digit keys, layout-independent. 9 is the last tab, so the
  // strip stays reachable past nine entries.
  const digit = /^Digit([1-9])$/.exec(e.code);
  if (digit) {
    const n = Number(digit[1]);
    return n === 9
      ? { type: "focus-last-tab" }
      : { type: "focus-tab", index: n - 1 };
  }

  return null;
}
