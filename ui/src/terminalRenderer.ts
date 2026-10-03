// `type` imports only: this module is imported by tests through `--experimental-strip-types`, and
// xterm needs a real DOM. The addon itself is loaded inside `attachRenderer`, never at module scope.
import type { ITerminalAddon } from "@xterm/xterm";

/**
 * Which of xterm's renderers a terminal draws with.
 *
 * **Why this exists.** xterm's default renderer is the DOM: one element per run of identically styled
 * cells, restyled on every change. A program that repaints the screen (`cmatrix`, `top`, a progress
 * bar, anything coloured that scrolls) rewrites most of those elements every frame, and that work
 * happens on the one thread that also handles keys and paints the rest of the window. Measured in
 * Chromium on a full-screen colour repaint, the canvas renderer took roughly an eighth to a tenth of
 * the main-thread time of the DOM renderer. That is indicative, not a promise: the app runs in
 * WebKitGTK, which is not Chromium, and the number that matters is the one on the person's machine.
 * The other half of the lag was upstream of the renderer (an event per 4 KiB the PTY read, see
 * `docs/09` §7.1 and `terminal.rs`); this is the half that is paid in the window.
 *
 * **Canvas, not WebGL.** `@xterm/addon-webgl` is faster still, and a WebGL context can be refused or
 * lost under a software-composited WebKitGTK, which is the machine class `motionPreference.ts` was
 * written for. Canvas 2D needs nothing a webview can withhold. WebGL is not shipped here, because it
 * could not be tested on that stack and a renderer that is faster on the author's machine and blank
 * on someone else's is a worse terminal.
 *
 * **It can always fall back, and it can be turned off.** Anything that goes wrong loading or
 * activating the addon leaves the terminal on the DOM renderer it already has: the result is the
 * terminal as it was before this module existed, and `attachRenderer` never throws. A person (or a
 * test whose assertions read xterm's DOM rows, which the canvas renderer does not fill) can pin the
 * DOM renderer with `localStorage["codify.terminalRenderer"] = "dom"`.
 *
 * **It is called after `term.open()`**, because the canvas addon draws into the element `open` makes,
 * and **without being awaited**, because the pane replays the workspace's history and subscribes to
 * the live stream with no `await` between the two (a chunk is in the replay or arrives after it,
 * never both). The terminal shows through the DOM renderer for the few milliseconds the addon takes
 * to load, then switches.
 */

/** Where the choice lives. Only the two spellings below count; anything else is the default. */
export const TERMINAL_RENDERER_KEY = "codify.terminalRenderer";

export type RendererChoice = "canvas" | "dom";

export const DEFAULT_RENDERER: RendererChoice = "canvas";

/** The renderer a store names, or the default when it names nothing, something else, or cannot be read. */
export function readRendererChoice(store?: Pick<Storage, "getItem">): RendererChoice {
  try {
    const from = store ?? (typeof window !== "undefined" ? window.localStorage : undefined);
    const raw = from?.getItem(TERMINAL_RENDERER_KEY);
    if (raw === "dom" || raw === "canvas") return raw;
  } catch {
    // Storage blocked or absent: the default is the honest answer.
  }
  return DEFAULT_RENDERER;
}

/** The part of an xterm `Terminal` this module touches, so a test can hand in a stand-in. */
export interface RendererHost {
  loadAddon(addon: ITerminalAddon): void;
}

/** Builds the canvas addon. Injected by tests; the real one imports it on first use. */
export type AddonLoader = () => Promise<ITerminalAddon>;

type CanvasAddonConstructor = new () => ITerminalAddon;

/**
 * The constructor out of whatever shape the bundler hands back for the module.
 *
 * `@xterm/addon-canvas` ships one UMD file, and a bundler decides how a dynamic `import()` of one
 * looks: some give the named export, some give only `default` holding `module.exports` (esbuild does,
 * which the first version of this loader found out in a real browser: `CanvasAddon is not a
 * constructor`, with the DOM renderer quietly kept). Both are accepted, and neither is trusted
 * blindly, so a shape that is neither is a reported fallback rather than a throw from `new`.
 */
export function canvasAddonFrom(mod: unknown): CanvasAddonConstructor {
  const named = (mod as { CanvasAddon?: unknown } | null)?.CanvasAddon;
  const viaDefault = (mod as { default?: { CanvasAddon?: unknown } } | null)?.default?.CanvasAddon;
  const found = typeof named === "function" ? named : viaDefault;
  if (typeof found !== "function") throw new Error("the module has no CanvasAddon to construct");
  return found as CanvasAddonConstructor;
}

async function loadCanvasAddon(): Promise<ITerminalAddon> {
  const Addon = canvasAddonFrom(await import("@xterm/addon-canvas"));
  return new Addon();
}

/**
 * Whether this webview will give out a 2D canvas context at all.
 *
 * A webview that blocks canvas (a privacy mode that withholds it, a context that cannot be made)
 * answers `getContext("2d")` with `null`. The canvas addon is then worse than useless: it can replace
 * a working DOM renderer with one that has nothing to draw on. Asking first costs one throwaway
 * element and keeps the terminal on the renderer it has.
 */
export function canvas2dAvailable(doc?: Pick<Document, "createElement">): boolean {
  try {
    const from = doc ?? (typeof document !== "undefined" ? document : undefined);
    const canvas = from?.createElement("canvas") as HTMLCanvasElement | undefined;
    return Boolean(canvas?.getContext?.("2d"));
  } catch {
    return false;
  }
}

export interface RendererOptions {
  /** Defaults to what `localStorage` says. */
  choice?: RendererChoice;
  load?: AddonLoader;
  /** Whether a 2D canvas can be had here. Defaults to asking the document. */
  probe?: () => boolean;
  /** True once the pane that owns the terminal has gone: loading finishes after unmount, and must then do nothing. */
  isDisposed?: () => boolean;
  /** Told why a renderer other than the chosen one is in use. Defaults to `console.warn`. */
  warn?: (message: string) => void;
}

export interface RendererOutcome {
  /** What the terminal draws with when this returns. */
  kind: RendererChoice;
  /** Why: "chosen" for a person's pin, otherwise what happened. */
  reason: string;
}

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function disposeQuietly(addon: ITerminalAddon): void {
  try {
    addon.dispose();
  } catch {
    // Already gone, or never finished activating: nothing left to undo.
  }
}

/**
 * Give `term` the canvas renderer when it can have it, and say what it ended up with.
 *
 * Never rejects. The terminal keeps whatever it was drawing with on every path that is not `canvas`.
 */
export async function attachRenderer(term: RendererHost, options: RendererOptions = {}): Promise<RendererOutcome> {
  const choice = options.choice ?? readRendererChoice();
  const warn = options.warn ?? ((message: string) => console.warn(message));
  if (choice === "dom") return { kind: "dom", reason: "chosen" };
  if (!(options.probe ?? canvas2dAvailable)()) {
    const reason = "this webview gives no 2D canvas";
    warn(`terminal: ${reason}; using the DOM renderer`);
    return { kind: "dom", reason };
  }

  let addon: ITerminalAddon;
  try {
    addon = await (options.load ?? loadCanvasAddon)();
  } catch (err) {
    const reason = `the canvas renderer could not be loaded: ${describe(err)}`;
    warn(`terminal: ${reason}; using the DOM renderer`);
    return { kind: "dom", reason };
  }
  if (options.isDisposed?.()) {
    disposeQuietly(addon);
    return { kind: "dom", reason: "the pane closed before the renderer loaded" };
  }
  try {
    term.loadAddon(addon);
  } catch (err) {
    disposeQuietly(addon);
    const reason = `the canvas renderer could not start: ${describe(err)}`;
    warn(`terminal: ${reason}; using the DOM renderer`);
    return { kind: "dom", reason };
  }
  return { kind: "canvas", reason: "default" };
}
