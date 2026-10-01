import { useEffect, useState } from "react";

/**
 * How big the whole window is, as one number the person chooses.
 *
 * **Why it is the root font size, and not `zoom`.** The type ramp, the radii and Tailwind's
 * spacing are all rem (`ui/tailwind.config.js`), so a percentage on `<html>` scales text, icons,
 * padding and corners together and leaves CSS pixels meaning what they always meant. That last part
 * is the reason: the browser pane's native webview is placed from `getBoundingClientRect()`
 * (docs/09 §7.3) in logical pixels, and CSS `zoom` or the webview's own zoom would make those
 * numbers disagree with the pixels the shell places it in. A root font size cannot.
 *
 * **The default is 125%.** Most text in this UI is 10 or 11px, which reads small on a desktop
 * display; the choice below goes down to 100% for anyone who liked it as it was.
 *
 * **Why steps and not a free slider.** Layout is checked at these sizes (docs/09 §9.2) and a
 * stored value that is not one of them is refused rather than trusted: a hand-edited `3` or `900`
 * would otherwise be a window nobody can read or reach the Settings pane to fix.
 *
 * Applied before the first render (`main.tsx`), so the window never paints at 100% and then jumps.
 */

/** Where the choice lives. */
export const UI_SCALE_KEY = "codify.uiScale";
/** Fired on `window` after every write, in this window; other windows hear `storage`. */
export const UI_SCALE_CHANGED = "codify:ui-scale-changed";

/** The sizes the window can be, in percent of the base. */
export const UI_SCALE_STEPS = [100, 112.5, 125, 150, 175] as const;
export type UiScale = (typeof UI_SCALE_STEPS)[number];

export const DEFAULT_UI_SCALE: UiScale = 125;

/**
 * The step a stored string spells, or null. Only the exact spellings this module writes count
 * (`"112.5"`, not `"112.50"`, `"1.125e2"` or `" 100"`): a value somebody else wrote is not one the
 * layout was checked at, even when `Number` would turn it into one.
 */
function stepSpelled(raw: string): UiScale | null {
  return UI_SCALE_STEPS.find((step) => String(step) === raw) ?? null;
}

function userStorage(): Pick<Storage, "getItem" | "setItem"> | undefined {
  try {
    if (typeof window !== "undefined" && window.localStorage) return window.localStorage;
  } catch {
    // Blocked, or no window yet: the default is the honest answer.
  }
  return undefined;
}

/** The stored choice, or the default for anything that is not one of the steps. */
export function readUiScale(store: Pick<Storage, "getItem"> | undefined = userStorage()): UiScale {
  try {
    const raw = store?.getItem(UI_SCALE_KEY);
    if (raw != null) {
      const step = stepSpelled(raw);
      if (step !== null) return step;
    }
  } catch {
    // Fall through to the default.
  }
  return DEFAULT_UI_SCALE;
}

/** The scale this window is showing, for code that holds a px constant (`uiScaleFactor`). */
let applied: UiScale | null = null;

/** Put a scale on the root. The one place the percentage is written. */
export function applyUiScale(
  scale: UiScale,
  root: HTMLElement | undefined = typeof document !== "undefined" ? document.documentElement : undefined,
): void {
  applied = scale;
  if (root) root.style.fontSize = `${scale}%`;
}

/** Choose a scale: store it, show it, and tell this window's listeners, in that order. */
export function writeUiScale(
  scale: UiScale,
  store: Pick<Storage, "setItem"> | undefined = userStorage(),
): UiScale {
  try {
    store?.setItem(UI_SCALE_KEY, String(scale));
  } catch {
    // A store that refuses writes still gets the size for this session.
  }
  applyUiScale(scale);
  try {
    if (typeof window !== "undefined") window.dispatchEvent(new Event(UI_SCALE_CHANGED));
  } catch {
    // No window to tell.
  }
  return scale;
}

/** One step bigger (`1`) or smaller (`-1`), stopping at the ends rather than wrapping. */
export function stepUiScale(current: UiScale, direction: 1 | -1): UiScale {
  const at = UI_SCALE_STEPS.indexOf(current);
  const next = Math.min(UI_SCALE_STEPS.length - 1, Math.max(0, at + direction));
  return UI_SCALE_STEPS[next];
}

/**
 * Boot: show the stored scale, and keep following it when another window changes it.
 *
 * Called once, before the first render. Returns the stop function (tests use it; the app never
 * stops). The `storage` event fires only in *other* windows, so this is the half of "changed
 * somewhere" that `writeUiScale` cannot cover: it re-applies to this window's root and tells
 * this window's listeners the same way a local write does.
 */
export function startUiScale(): () => void {
  applyUiScale(readUiScale());
  if (typeof window === "undefined") return () => {};
  const follow = (event: StorageEvent): void => {
    if (event.key !== null && event.key !== UI_SCALE_KEY) return;
    applyUiScale(readUiScale());
    window.dispatchEvent(new Event(UI_SCALE_CHANGED));
  };
  window.addEventListener("storage", follow);
  return () => window.removeEventListener("storage", follow);
}

/** The scale this window is showing right now: what was applied, else what is stored. */
export function currentUiScale(): UiScale {
  return applied ?? readUiScale();
}

/** The scale as a multiplier (`1.25`), for px constants that cannot be written in rem. */
export function uiScaleFactor(): number {
  return currentUiScale() / 100;
}

/** The scale this window is showing, as state: re-renders on a change, here or (via `startUiScale`) elsewhere. */
export function useUiScale(): UiScale {
  const [scale, setScale] = useState<UiScale>(currentUiScale);
  useEffect(() => {
    const recompute = (): void => setScale(currentUiScale());
    window.addEventListener(UI_SCALE_CHANGED, recompute);
    return () => window.removeEventListener(UI_SCALE_CHANGED, recompute);
  }, []);
  return scale;
}
