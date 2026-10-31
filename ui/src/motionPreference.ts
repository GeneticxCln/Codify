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
 * **Three facts, two lifetimes, and the difference matters.** A preference
 * lives in `localStorage`: someone who picks "reduced" should not have to pick
 * again tomorrow. The shell's starvation verdict is a diagnosis of *one boot*
 * and lives in `sessionStorage`: a driver update, a compositor change or a new
 * monitor can all make it wrong tomorrow, and a diagnosis persisted as
 * `localStorage` would turn "your machine cannot afford this today" into "your
 * machine can never have this", remembered long after it stopped being true.
 * It survives a reload of the same window, which is required rather than
 * incidental: the shell announces at most once per boot, so a reload that
 * emptied this slot would never hear the verdict again.
 * The measurement that a boot was starved is a third fact with the session's
 * lifetime: it outlives the verdict's withdrawal, because keeping the backdrop
 * is not a claim that the window can afford the rate it asked for. It is what
 * holds the canvas at the lower rate after "Animate anyway".
 *
 * **"Animate anyway" is a session choice, not a preference.** It answers this
 * boot's verdict, and persisting it in `localStorage` was the bug that made a
 * click in one launch turn every later launch on that machine into the laggy
 * one: the verdict was cleared by the click that made the animation
 * unaffordable, and only a software-rendering boot happened to reset it. It now
 * lives in `sessionStorage` under `MOTION_SESSION_KEY`, so it survives the
 * reload it was promised to survive (the shell's verdict does too).
 *
 * **`sessionStorage` is not a window lifetime here.** Measured in this shell,
 * the store outlived the process: a fresh launch still read last launch's
 * `allowed`, so the control leaked into exactly the boot it was meant never to
 * reach. So the session facts carry the shell's per-launch boot id
 * (`MOTION_BOOT_KEY`, from `codify_boot_id`). A reload sees the same id and
 * keeps the choice; a new launch sees a different one and ignores all three
 * session facts. The boot id is the lifetime the store cannot provide on its
 * own.
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

/** The persistent preference: `auto` or `reduced`. `allowed` is not stored here. */
export const MOTION_KEY = "codify.motion";
/**
 * "Animate anyway" for this window. It survives a reload of the same shell
 * boot; `MOTION_BOOT_KEY` is what stops an earlier launch's value from being
 * read as this launch's.
 */
export const MOTION_SESSION_KEY = "codify.motion.this-session";
/**
 * Which shell launch wrote the session facts. `sessionStorage` outlived the
 * process in this webview, so the facts need a lifetime written beside them.
 */
export const MOTION_BOOT_KEY = "codify.motion.boot";
/** The shell answers with the id of this launch; see `prepareMotionSession`. */
export const BOOT_ID_COMMAND = "codify_boot_id";

/**
 * How long the boot-id IPC may take before the page stops waiting for it.
 *
 * The id scopes this page's session facts; it is not worth a window that never
 * mounts. A shell that does not answer by then leaves the per-page id in place,
 * which only means a stored verdict is not trusted for this launch.
 */
export const BOOT_ID_TIMEOUT_MS = 1_500;

/** `promise`, or `fallback` after `ms` — whichever comes first; the timer is cleared either way. */
export async function within<T>(promise: Promise<T>, ms: number, fallback: T): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<T>((resolve) => {
        timer = setTimeout(() => resolve(fallback), ms);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}
/** What the shell sends when it concludes the window is starving itself. */
export const MOTION_EVENT = "codify:engine-render-starved";
/**
 * The shell's environment, injected on every main-page load: this window was
 * born on WebKit's software rasteriser. A global rather than an invoke, because
 * the fact is needed at mount and the page already owns a DOM event bus; the
 * shell writes it in `lib.rs`'s `on_page_load`.
 */
export const SOFTWARE_RENDERING_GLOBAL = "__CODIFY_SOFTWARE_RENDERING__";
/** Fired alongside the global, for a page whose listener was already mounted. */
export const SOFTWARE_RENDERING_EVENT = "codify:software-rendering";
/**
 * The command the page asks at mount when the injected global lost its race
 * with the first effect. Named here so a rename fails a test.
 */
export const SOFTWARE_RENDERING_COMMAND = "codify_software_rendering";
/**
 * That this boot was measured starving, kept separately from the verdict.
 *
 * The verdict is what the banner is about, and "Animate anyway" withdraws it.
 * This is the measurement itself: the shell saw this window spend a core on a
 * decoration, and that does not stop being true because the person kept the
 * backdrop. It survives the withdrawal so the backdrop stays at the lower rate
 * the measurement asks for. It is read only while `MOTION_BOOT_KEY` names this
 * launch; a driver update and a new launch both get a fresh answer.
 */
export const STARVED_BOOT_KEY = "codify.starved-this-boot";

/**
 * Fired on `window` after every write to either store.
 *
 * The loops used to hear the shell's event directly and re-read the store, while
 * `App` wrote the verdict from its own listener on the same event. The loops'
 * listener is registered first (children's effects run before their parent's),
 * so it read the store *before* the verdict was written and kept animating on
 * the machine the verdict was about. A store that announces its own changes has
 * one order — write, then tell — and the user's "Animate anyway", which is not a
 * shell event at all, reaches the loops the same way.
 */
export const MOTION_CHANGED = "codify:motion-changed";

function announceChange(): void {
  try {
    if (typeof window !== "undefined") window.dispatchEvent(new Event(MOTION_CHANGED));
  } catch {
    // No window to tell: the writer still gets the state it asked for.
  }
}

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

/** Whether a session fact survives a reload of the same shell launch. */
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

/** The boot id every session fact is scoped to. Resolved before the app mounts. */
let sessionBoot: string | null = null;

/**
 * This page's own id, for a preview with no shell and for a failed boot query.
 *
 * `window.name` is the one identifier a page can read synchronously that a
 * reload keeps and a new window does not — which is exactly the lifetime the
 * session facts want. A page that creates a new browsing context (a new tab, a
 * new app launch) starts with an empty name and gets a new id.
 */
function pageBootId(): string {
  const prefix = "codify-motion:";
  try {
    const name = window.name || "";
    if (name.startsWith(prefix) && name.length > prefix.length) {
      return name.slice(prefix.length);
    }
    let id = "";
    try {
      if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
        id = crypto.randomUUID();
      }
    } catch {
      // An insecure context can refuse the API; the timestamp is still an id.
    }
    if (!id) id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    window.name = prefix + id;
    return id;
  } catch {
    // No window (a pure store test): one id for this module is enough.
    return "no-window";
  }
}

/**
 * Ask the shell for this launch's id, or `null` outside it or when it refuses.
 *
 * The command is local IPC and takes no argument. A shell that predates it (or
 * a browser preview) leaves the caller with the page id instead, which still
 * cannot leak a choice into another launch.
 */
async function bootIdFromShell(): Promise<string | null> {
  if (typeof window === "undefined") return null;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: unknown })
    .__TAURI_INTERNALS__;
  if (!internals) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const id = await within<string | null>(
      invoke<string>(BOOT_ID_COMMAND),
      BOOT_ID_TIMEOUT_MS,
      null,
    );
    return typeof id === "string" && id.length > 0 ? id : null;
  } catch {
    return null;
  }
}

/**
 * Resolve the lifetime of this page's session facts, before the first read.
 *
 * `main.tsx` awaits this before rendering, so `readMotion` never has to guess
 * whether a stored `allowed` belongs to this launch. The cost is one local IPC
 * round trip at boot; the alternative was a first frame at a rate the machine
 * was measured as unable to pay for.
 */
export async function prepareMotionSession(): Promise<string> {
  const id = (await bootIdFromShell()) ?? pageBootId();
  sessionBoot = id;
  return id;
}

/** The id this page's session facts are scoped to. */
export function motionBootId(): string {
  if (!sessionBoot) sessionBoot = pageBootId();
  return sessionBoot;
}

/** Whether the session store's facts were written by this shell launch. */
function sessionFactsAreThisBoot(session: Storage | undefined): boolean {
  try {
    return Boolean(session) && session?.getItem(MOTION_BOOT_KEY) === motionBootId();
  } catch {
    return false;
  }
}

/**
 * Mark the session store as this launch's before writing a fact into it.
 *
 * The boot key is shared by all three facts, so writing it on top of a previous
 * launch's store would adopt that launch's other facts by accident — measured:
 * a new boot's verdict was written, the old `allowed` became "this boot's" at
 * the same moment, and the banner said stopped while the canvases kept moving.
 * Stale facts are removed first, then the key is set.
 */
function adoptSessionBoot(session: Storage | undefined): void {
  try {
    if (session?.getItem(MOTION_BOOT_KEY) === motionBootId()) return;
    session?.removeItem(MOTION_EVENT);
    session?.removeItem(MOTION_SESSION_KEY);
    session?.removeItem(STARVED_BOOT_KEY);
    session?.setItem(MOTION_BOOT_KEY, motionBootId());
  } catch {
    // A store that refuses is a store that keeps no facts; the caller still
    // gets the state it asked for.
  }
}

/**
 * Read this window's motion choice: the session override first, then the
 * stored preference.
 *
 * `allowed` is only honoured from the session store, and only while that
 * store's boot key names this launch — the store outlived the process in this
 * webview. A legacy `allowed` left in `localStorage` by an earlier build is
 * read as the absence of a choice: the animation it bought was for one window,
 * and a later launch is not that window.
 */
function readSetting(user: Storage | undefined, session: Storage | undefined): MotionSetting {
  if (sessionFactsAreThisBoot(session)) {
    try {
      const inSession = session?.getItem(MOTION_SESSION_KEY);
      if (inSession === "allowed" || inSession === "reduced" || inSession === "auto") {
        return inSession;
      }
    } catch {
      // Fall through to the stored preference.
    }
  }
  try {
    const stored = user?.getItem(MOTION_KEY);
    if (stored === "reduced" || stored === "auto") return stored;
  } catch {
    // Fall through to the default.
  }
  return "auto";
}

function readVerdict(store: Storage | undefined): MotionState["shellStarved"] {
  try {
    if (!sessionFactsAreThisBoot(store)) return null;
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
  return { setting: readSetting(user, session), shellStarved: readVerdict(session) };
}

/** Whether this page was told the shell is on WebKit's software rasteriser. */
export function softwareRendering(): boolean {
  try {
    return Boolean(
      (window as unknown as Record<string, unknown>)[SOFTWARE_RENDERING_GLOBAL],
    );
  } catch {
    return false;
  }
}

/**
 * Ask the shell directly, for the boot where the injected global and
 * `SOFTWARE_RENDERING_EVENT` both lost the race with the app's first effect.
 *
 * The global is written by `on_page_load`, which is the shell's "the page has
 * finished loading" — in practice after the page's first effect has run. The
 * event dispatched beside it is missed for the same reason. The question is a
 * fact about the shell's own launch, so the page can simply ask; `null` means
 * there is no shell to ask (the browser preview, a test) or the command
 * refused, and the caller keeps the injected-global path.
 */
export async function softwareRenderingFromShell(): Promise<boolean | null> {
  if (typeof window === "undefined") return null;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: unknown })
    .__TAURI_INTERNALS__;
  if (!internals) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const value = await invoke<boolean>(SOFTWARE_RENDERING_COMMAND);
    return typeof value === "boolean" ? value : null;
  } catch {
    // An older shell without the command, or a refused IPC: no answer, and the
    // event/global path is still the one that got here.
    return null;
  }
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

/**
 * Write the persistent preference (`auto` or `reduced`).
 *
 * `allowed` is not a preference: it is `writeSessionSetting`'s word for one
 * window. Refusing it here is what keeps a click from becoming a setting that
 * outlives the machine's verdict. The caller that needs it (the banner's
 * "Animate anyway") calls the session writer.
 */
export function writeSetting(
  setting: MotionSetting,
  explicit?: Partial<MotionStores>,
): MotionState {
  const { user } = stores(explicit);
  try {
    user?.setItem(MOTION_KEY, setting === "allowed" ? "auto" : setting);
  } catch {
    // A store that refuses writes still yields the in-memory truth.
  }
  announceChange();
  return readMotion(explicit);
}

/**
 * Write this window's override. Only `"allowed"` can sensibly be written —
 * `reduced` is a preference and belongs in `writeSetting` — but the store keeps
 * the value verbatim so a test or a future control can name any of the three.
 */
export function writeSessionSetting(
  setting: MotionSetting,
  explicit?: Partial<MotionStores>,
): MotionState {
  const { session } = stores(explicit);
  adoptSessionBoot(session);
  try {
    session?.setItem(MOTION_SESSION_KEY, setting);
  } catch {
    // As in `writeSetting`.
  }
  announceChange();
  return readMotion(explicit);
}

/**
 * Record the shell's verdict. Idempotent across repeats of the same
 * announcement, which is what a page reload plus the shell's once-per-boot rule
 * makes impossible anyway; the idempotence is for the writer's sake, not fate.
 *
 * The boot probe (`frameProbe.ts`) writes here too: it asks the shell for its
 * own CPU clock and judges a composited frame against the same budget, so a
 * boot-time verdict can arrive before the watchdog's ten-second window. Both
 * are the shell's cost measured by the shell, and neither the banner nor the
 * release rate has a reason to tell them apart.
 */
export function applyShellStarvation(
  verdict: { at: number; measuredMs: number },
  explicit?: Partial<MotionStores>,
): MotionState {
  // The person already answered this boot's measurement. A later verdict — the
  // watchdog can announce after a click that arrived first, and a canvas can
  // re-probe — must not put the banner back or stop the backdrop they chose to
  // keep. The measurement is still recorded, so the rate stays at what it asked
  // for; only the verdict that asks for a decision is dropped.
  const answered = readMotion(explicit);
  if (answered.setting === "allowed") {
    const { session } = stores(explicit);
    adoptSessionBoot(session);
    try {
      session?.setItem(STARVED_BOOT_KEY, "1");
    } catch {
      // The returned state is still the one the person asked for.
    }
    announceChange();
    return readMotion(explicit);
  }

  const { session } = stores(explicit);
  adoptSessionBoot(session);
  try {
    session?.setItem(MOTION_EVENT, JSON.stringify(verdict));
    session?.setItem(STARVED_BOOT_KEY, "1");
  } catch {
    // As above: the caller still gets the state it asked for.
  }
  announceChange();
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
  announceChange();
  return readMotion(explicit);
}

/**
 * Whether the shell measured this window starving at any point this boot.
 *
 * Unlike [`readMotion`]'s verdict, this is still true after "Animate anyway":
 * that control keeps the theme's backdrop, and a backdrop kept is still one the
 * shell has measured; its release rate does not come back with it. See
 * [`useStarvedThisBoot`] for the reactive form.
 */
export function starvedThisBoot(explicit?: Partial<MotionStores>): boolean {
  const { session } = stores(explicit);
  try {
    if (!sessionFactsAreThisBoot(session)) return false;
    return session?.getItem(STARVED_BOOT_KEY) === "1";
  } catch {
    return false;
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
 * [`starvedThisBoot`] as React state.
 *
 * Subscribes to the same two sources as [`useMotionAllowed`] — the shell's event
 * and the store's own announcement — because the flag is written by
 * `applyShellStarvation`, and a canvas mounted before that write has to hear
 * about it as a dependency change rather than infer it from a render.
 */
export function useStarvedThisBoot(): boolean {
  const [starved, setStarved] = useState(() => starvedThisBoot());
  useEffect(() => {
    const recompute = (): void => setStarved(starvedThisBoot());
    // A writer can run between this render and this subscription. App applies
    // the software-rendering verdict in an effect declared above this hook, so
    // the write and its announcement land before the listener below exists;
    // reading once more here is what makes the boot flag true on that path.
    recompute();
    let off: (() => void) | undefined;
    let cancelled = false;
    void listenShellEvent<number>(MOTION_EVENT, () => recompute()).then((unlisten) => {
      if (cancelled) unlisten();
      else off = unlisten;
    });
    // The shell event can arrive before `applyShellStarvation` has written the
    // boot flag; the store's own announcement is the one that carries the
    // write, exactly as it is for `useMotionAllowed`.
    window.addEventListener(MOTION_CHANGED, recompute);
    return () => {
      cancelled = true;
      off?.();
      window.removeEventListener(MOTION_CHANGED, recompute);
    };
  }, []);
  return starved;
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
    // A store write between render and subscription is caught here for the same
    // reason as in `useStarvedThisBoot`.
    recompute();
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
    // The store's own announcement is the authority; the shell event above can
    // arrive before the verdict is written and is kept only as a nudge.
    window.addEventListener(MOTION_CHANGED, recompute);
    return () => {
      cancelled = true;
      off?.();
      mq?.removeEventListener?.("change", recompute);
      window.removeEventListener(MOTION_CHANGED, recompute);
    };
  }, []);
  return allowed;
}
