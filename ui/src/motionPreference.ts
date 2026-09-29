import { useEffect, useState } from "react";
// `.ts` explicitly: this module is imported by tests through
// `--experimental-strip-types`, which resolves only real files.
import { listenShellEvent } from "./shellEvents.ts";

/**
 * Whether this window animates, and who decided.
 *
 * **Why this exists.** On a machine whose WebKitGTK composites in software (an
 * Nvidia/Wayland session without a working dma-buf path), a full-window 30 FPS
 * backdrop costs about a whole core *in the UI process* — and the cost lands on
 * the GTK main loop, so the window stops answering input while the page itself
 * never sees a slow frame: `rAF` keeps delivering, the painter is cheap, and
 * nothing inside the page can measure what is wrong. The one party that pays is
 * the shell, so the shell is the party that can say so; when it does, this store
 * is how the page reacts.
 *
 * **Two facts, two lifetimes.** The user's own choice is a preference and lives
 * in `localStorage`: someone who picks "reduced" should not have to pick again
 * tomorrow. The shell's starvation verdict is a diagnosis of *one boot* and
 * lives in `sessionStorage`: a driver update, a compositor change or a new
 * monitor can all make it wrong tomorrow, and a diagnosis persisted as
 * `localStorage` would turn "your machine cannot afford this today" into "your
 * machine can never have this", remembered long after it stopped being true.
 *
 * **Why the verdict is not a setting.** A window that cannot answer input
 * cannot be used to reach a settings pane, so the relief has to apply without a
 * click. It is also not silent: the banner names what happened and hands back
 * an "Animate anyway" control, because a decision made *for* someone is only
 * honest if they can see it and reverse it — and the reverse must hold for the
 * rest of the session, which is why the shell announces at most once per boot
 * (its side of the contract) and a reversal outlives a reload (this side:
 * `sessionStorage` survives one, so the reversed verdict stays reversed).
 */

/** Where the user's own choice lives, and for how long. */
export const MOTION_KEY = "codify.motion";
/** What the shell sends when it concludes the window is starving itself. */
export const MOTION_EVENT = "codify:engine-render-starved";

/** The states the user's choice can hold. */
export type MotionSetting = "auto" | "allowed" | "reduced";

/** One reading of both stores. */
export interface MotionState {
  setting: MotionSetting;
  /**
   * Present while the shell's starvation verdict is in force. The measured mean
   * goes here so the banner can name something a person could check, rather
   * than "the app decided".
   */
  shellStarved: null | { at: number; measuredMs: number };
}

/** Whether `sessionStorage` survives a reload — it does, and a test pins it. */
export const VERDICT_SURVIVES_RELOAD = true;

/** The stores this module talks to, so tests can hand in fakes. */
export interface MotionStores {
  user: Storage;
  session: Storage;
}

function userStorage(): Storage | undefined {
  try {
    if (typeof window !== "undefined" && window.localStorage) return window.localStorage;
  } catch {
    // No store (blocked, or no window yet): the honest default is `auto`.
  }
  return undefined;
}

function sessionStores(): Storage | undefined {
  try {
    if (typeof window !== "undefined" && window.sessionStorage) return window.sessionStorage;
  } catch {
    // Same as above.
  }
  return undefined;
}

function stores(explicit?: Partial<MotionStores>): Partial<MotionStores> {
  return {
    user: explicit?.user ?? userStorage(),
    session: explicit?.session ?? sessionStores(),
  };
}

/** Read a `MotionSetting` back, refusing anything but the three spellings. */
function readSetting(store: Storage | undefined): MotionSetting {
  try {
    const raw = store?.getItem(MOTION_KEY);
    if (raw === "allowed" || raw === "reduced" || raw === "auto") return raw;
  } catch {
    // Fall through to the default.
  }
  return "auto";
}

function readVerdict(store: Storage | undefined): MotionState["shellStarved"] {
  try {
    const raw = store?.getItem(MOTION_EVENT);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as MotionState["shellStarved"];
    if (parsed && typeof parsed.at === "number" && typeof parsed.measuredMs === "number") {
      return parsed;
    }
  } catch {
    // A half-written verdict is no verdict.
  }
  return null;
}

/** Both stores, as they read right now. */
export function readMotion(explicit?: Partial<MotionStores>): MotionState {
  const { user, session } = stores(explicit);
  return { setting: readSetting(user), shellStarved: readVerdict(session) };
}

/** Whether the effects should move, given the stores and the media query. */
export function motionAllowed(
  state: MotionState,
  systemReduced: boolean = systemPrefersReducedMotion(),
): boolean {
  if (state.setting === "allowed") return true;
  if (state.setting === "reduced") return false;
  if (state.shellStarved) return false;
  return !systemReduced;
}

/** Write the user's own choice. It outranks the shell's verdict in either direction. */
export function writeSetting(
  setting: MotionSetting,
  explicit?: Partial<MotionStores>,
): MotionState {
  const { user } = stores(explicit);
  try {
    user?.setItem(MOTION_KEY, setting);
  } catch {
    // A store that refuses writes still yields the in-memory truth.
  }
  return readMotion(explicit);
}

/**
 * Record the shell's verdict. Idempotent across repeats of the same
 * announcement, which is what a page reload plus the shell's once-per-boot rule
 * makes impossible anyway; the idempotence is for the writer's sake, not fate.
 */
export function applyShellStarvation(
  verdict: { at: number; measuredMs: number },
  explicit?: Partial<MotionStores>,
): MotionState {
  const { session } = stores(explicit);
  try {
    session?.setItem(MOTION_EVENT, JSON.stringify(verdict));
  } catch {
    // As above: the caller still gets the state it asked for.
  }
  return readMotion(explicit);
}

/** The user reversed the verdict for the rest of this session. */
export function clearShellStarvation(explicit?: Partial<MotionStores>): MotionState {
  const { session } = stores(explicit);
  try {
    session?.removeItem(MOTION_EVENT);
  } catch {
    // Nothing to undo if the store refuses.
  }
  return readMotion(explicit);
}

/**
 * Empty the verdict slot on boot, before any listener exists. The shell
 * re-measures and re-announces within seconds if it is still true; a stale copy
 * would make `Animate anyway` look broken, because the banner would come back
 * before the shell had said anything new.
 */
export function rearmVerdictForBoot(explicit?: Partial<MotionStores>): void {
  const { session } = stores(explicit);
  try {
    session?.removeItem(MOTION_EVENT);
  } catch {
    // As everywhere else in this file.
  }
}

function systemPrefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * Subscribe to the motion decision, reactively.
 *
 * The render loops read the store **at effect time** — the decision belongs to
 * a mount, not to a frame — so a verdict that arrives while a canvas is running
 * must reach it as a *dependency change*, not as a re-render: React effects do
 * not re-run on render alone, and a loop that never hears the news animates on
 * exactly the machine the verdict was about. This hook subscribes to both
 * sources of change (the shell's event as it arrives, and the system
 * preference flipping at the OS level) and returns the boolean the loops put
 * in their dependency lists.
 *
 * `App` holds the richer state for its banner; this is the loops' narrow view —
 * one boolean, and the re-render that carries it.
 */
export function useMotionAllowed(): boolean {
  const [allowed, setAllowed] = useState(() =>
    motionAllowed(readMotion(), systemPrefersReducedMotion()),
  );
  useEffect(() => {
    const recompute = (): void => {
      setAllowed(motionAllowed(readMotion(), systemPrefersReducedMotion()));
    };
    let off: (() => void) | undefined;
    let cancelled = false;
    void listenShellEvent<number>(MOTION_EVENT, () => recompute()).then((unlisten) => {
      if (cancelled) unlisten();
      else off = unlisten;
    });
    // The OS-level preference can flip while the app is open; the store cannot
    // see that, so the media query is a second subscription.
    const mq =
      typeof window !== "undefined" && typeof window.matchMedia === "function"
        ? window.matchMedia("(prefers-reduced-motion: reduce)")
        : undefined;
    mq?.addEventListener?.("change", recompute);
    return () => {
      cancelled = true;
      off?.();
      mq?.removeEventListener?.("change", recompute);
    };
  }, []);
  return allowed;
}
