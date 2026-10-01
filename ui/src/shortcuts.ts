/**
 * The keyboard layer: which keystroke means what.
 *
 * A pure module, for the same reason `tabs.ts` is: the mapping is a table of
 * decisions that is easy to get subtly wrong (a digit with Shift, AltGr
 * pretending to be Ctrl, `Ctrl+9` meaning *last* rather than *ninth*) and
 * impossible to see in markup. `ui/tests/shortcuts.test.ts` walks the table.
 * The wiring in `App.tsx` only dispatches what this returns.
 *
 * ## The rules, and why they are these
 *
 * - **The modifier is `Ctrl`, and only `Ctrl`.** Codify is a Linux desktop app.
 *   `Super` belongs to the desktop (the overview, the launcher, tiling), so an
 *   event that carries `metaKey` is never ours, alone or chorded with `Ctrl`:
 *   binding it would steal a key the window manager already owns.
 * - **`Alt` never fires a shortcut.** AltGr on European layouts reports
 *   `ctrlKey` *and* `altKey` together — without this guard, AltGr+T on a
 *   German keyboard would open a tab while the user types.
 * - **Letters require no `Shift`.** `Ctrl+Shift+T` is somebody else's
 *   muscle memory (reopen a closed tab); firing "new tab" for it would be
 *   the wrong surprise. Digits, by contrast, ignore `Shift`: `Ctrl+Shift+1`
 *   is how the key is physically reached on some layouts.
 * - **Digits are read from `code`, not `key`.** On layouts where `1` is
 *   `Shift+1` (and `key` is `!`), the physical digit key still means
 *   "go to tab 1". `code` is the physical key, layout be damned.
 * - **`Ctrl+9` is the *last* tab, not the ninth** — the browser convention.
 *   Tabs 1–8 jump by position; 9 lands on the end of the strip, which is
 *   the one a keyboard cannot otherwise name when there are more than nine.
 * - **`Ctrl+=` / `Ctrl+-` / `Ctrl+0` scale the window**, as a browser's zoom does.
 *   Bigger is `=` *or* `+` (so Ctrl+Shift+= works), by `key` or by `code`: `+` is
 *   its own key on a German layout and `=` is not where it is on others. Reset
 *   is read from the physical `0`, because on AZERTY that key types `à`.
 *
 * What this module cannot promise: a compositor or window manager that consumes
 * a keystroke before it reaches the webview (a desktop that binds `Ctrl+W`
 * itself, say) degrades to that binding — the shortcut is registered in the
 * DOM, which is the last stop, not the first.
 */

/** What a resolved keystroke asks the shell to do. */
export type ShortcutAction =
  | { type: "new-tab" }
  | { type: "close-active-tab" }
  /** 0-based strip position, from `Ctrl+1..8`. */
  | { type: "focus-tab"; index: number }
  | { type: "focus-last-tab" }
  | { type: "toggle-palette" }
  /** Hide or show the left panel (threads, Browser, Terminal, Settings). */
  | { type: "toggle-sidebar" }
  /** One step bigger or smaller on the UI scale (`uiScale.ts`), or back to its default. */
  | { type: "scale-up" }
  | { type: "scale-down" }
  | { type: "scale-reset" };

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
  /**
   * True for the auto-repeat events a held key generates. Optional so a caller
   * that builds the event by hand does not have to say "not a repeat".
   */
  readonly repeat?: boolean;
}

/** Resolve a key event to a shell action, or `null` — most keys are, and must stay — typing. */
export function resolveShortcut(e: KeyEventLike): ShortcutAction | null {
  if (!e.ctrlKey || e.metaKey || e.altKey) return null;

  if (!e.shiftKey) {
    switch (e.key.toLowerCase()) {
      // A held key sends `keydown` again at the keyboard's repeat rate. These
      // three change the strip or a toggle, so a repeat is not "again": it is how
      // holding Ctrl+T opened a tab per repeat until the strip was full of empty
      // ones, and how holding Ctrl+W would have closed every tab in it. One press,
      // one action.
      case "t":
        return e.repeat ? null : { type: "new-tab" };
      case "w":
        return e.repeat ? null : { type: "close-active-tab" };
      case "k":
        return e.repeat ? null : { type: "toggle-palette" };
      case "b":
        return e.repeat ? null : { type: "toggle-sidebar" };
      default:
        break;
    }
  }

  // Scaling ignores Shift on purpose (Ctrl+Shift+= is how `+` is typed on many layouts), and a held
  // key is one step: auto-repeat would run the window from 100% to 175% in a blink.
  if (e.key === "=" || e.key === "+" || e.code === "Equal" || e.code === "NumpadAdd") {
    return e.repeat ? null : { type: "scale-up" };
  }
  if (e.key === "-" || e.code === "Minus" || e.code === "NumpadSubtract") {
    return e.repeat ? null : { type: "scale-down" };
  }
  if (e.code === "Digit0" || e.code === "Numpad0") {
    return e.repeat ? null : { type: "scale-reset" };
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

/**
 * One line of the shortcut list the About tab shows.
 *
 * `probe` is a real key event that must resolve to `action`: the list is *checked against*
 * `resolveShortcut` (`ui/tests/shortcuts.test.ts`), so a binding that changes, or an action added
 * without a line here, fails a test instead of leaving the About tab describing keys that no longer
 * do that. The list is documentation, and documentation that cannot be wrong without a test noticing
 * is the only kind this project has found to stay true.
 */
export interface ShortcutHelp {
  /** How a person reads the chord. */
  readonly keys: string;
  /** What it does. */
  readonly does: string;
  readonly probe: KeyEventLike;
  readonly action: ShortcutAction["type"];
}

const ctrlKey = (key: string, code: string): KeyEventLike => ({
  key,
  code,
  ctrlKey: true,
  metaKey: false,
  altKey: false,
  shiftKey: false,
});

/** Every shortcut, in the order the About tab lists them. */
export const SHORTCUT_HELP: readonly ShortcutHelp[] = [
  { keys: "Ctrl+T", does: "New tab", probe: ctrlKey("t", "KeyT"), action: "new-tab" },
  { keys: "Ctrl+W", does: "Close the active tab", probe: ctrlKey("w", "KeyW"), action: "close-active-tab" },
  { keys: "Ctrl+1 to 8", does: "Go to that tab", probe: ctrlKey("1", "Digit1"), action: "focus-tab" },
  { keys: "Ctrl+9", does: "Go to the last tab", probe: ctrlKey("9", "Digit9"), action: "focus-last-tab" },
  { keys: "Ctrl+K", does: "Command palette", probe: ctrlKey("k", "KeyK"), action: "toggle-palette" },
  {
    keys: "Ctrl+B",
    does: "Hide or show the left panel (a terminal keeps Ctrl+B for itself)",
    probe: ctrlKey("b", "KeyB"),
    action: "toggle-sidebar",
  },
  { keys: "Ctrl+=", does: "Make the UI bigger", probe: ctrlKey("=", "Equal"), action: "scale-up" },
  { keys: "Ctrl+-", does: "Make the UI smaller", probe: ctrlKey("-", "Minus"), action: "scale-down" },
  { keys: "Ctrl+0", does: "Back to the default size", probe: ctrlKey("0", "Digit0"), action: "scale-reset" },
];
