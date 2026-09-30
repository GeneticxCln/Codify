/**
 * The real render loops, driven by hand.
 *
 * Both backdrop loops (`useAtmosphereCanvas` behind every themed backdrop, and `MatrixRain`'s own)
 * were held by tests that read their source for `bindContextRecovery(`, `makeFrameGuard(`,
 * `recovery.isLost()` and so on: proof that the words are in the file, not that a driver reset
 * leaves the backdrop drawing again, or that a painter that throws every frame stops. This mounts
 * the real component in jsdom and replaces the three things the loop depends on that a test has to
 * own to make the outcome deterministic:
 *
 * - **the frame clock.** `requestAnimationFrame` is a manual queue (one pending callback, which is
 *   how both loops use it): `frame(t)` runs it at timestamp `t`, so "a 144 Hz display still gets
 *   30 frames" is a loop over timestamps and not a sleep. `armed` and `cancelled` count the calls,
 *   which is what "armed a loop" and "tore it down" mean.
 * - **the 2d context.** A recording proxy: every method call is kept (`calls`), every property set
 *   is stored, so a test can read back what was drawn and where.
 * - **the canvas.** jsdom lays nothing out, so `getBoundingClientRect` answers a size a test
 *   chooses and `resize()` changes it and notifies the `ResizeObserver`s the loops registered.
 *
 * And it records what the loops do to the outside: `console.error` / `console.warn` (a painter that
 * gives up, a compositor judged too slow), and how many listeners are attached to the canvas and
 * the document (a remount that leaks one stacks a repaint per remount).
 *
 * `Math.random` is a seeded generator for the duration, so two runs that differ in one prop differ
 * in nothing else: the rain's columns and every painter's particles start in the same places, and
 * a difference in what was drawn is the prop's doing.
 */
import { withDom } from "./dom.ts";
import type { Dom } from "./dom.ts";

export interface RecordedCall {
  name: string;
  args: unknown[];
}

export interface CanvasRigOptions {
  /** The canvas's CSS size. Default 420 x 420. */
  width?: number;
  height?: number;
  /** Seed for `Math.random`. Default 7. */
  seed?: number;
}

export interface CanvasRig {
  dom: Dom;
  /** Every method called on any 2d context, in order. */
  calls: RecordedCall[];
  /** `requestAnimationFrame` calls so far. */
  armed: number;
  /** `cancelAnimationFrame` calls so far. */
  cancelled: number;
  /** First argument of every `console.error`, as text. */
  errors: string[];
  /** First argument of every `console.warn`, as text. */
  warnings: string[];
  /** Whether a frame is waiting to run. */
  pending(): boolean;
  /** Run the waiting frame at timestamp `t` (ms). Throws if none is waiting. */
  frame(t: number): void;
  /**
   * Run `count` frames `stepMs` apart, the first at `from`. Stops early, without failing, if the loop
   * stops asking for frames (that is an outcome a test may be asserting). Returns the next timestamp.
   */
  run(count: number, from: number, stepMs?: number): number;
  /** The one canvas in the container. */
  canvas(): HTMLCanvasElement;
  /**
   * Change the canvas's size and, unless `notify` is false, tell every `ResizeObserver` that watches
   * it. Without the notification the size changes silently, the way it can while a context is gone.
   */
  resize(width: number, height: number, notify?: boolean): void;
  /** Make every call to this context method throw (after recording it), or stop doing so with `null`. */
  breakMethod(name: string | null): void;
  /** Net listeners of `type` on the canvas (`"canvas"`) or the document (`"document"`). */
  listeners(on: "canvas" | "document", type: string): number;
  /** Calls to `name` so far. */
  count(name: string): number;
  /** Every `fillText`, as text and position. */
  fillTexts(): Array<{ text: string; x: number; y: number }>;
  /** Forget what has been recorded (not the listeners, not the frame queue). */
  clear(): void;
}

/** A small deterministic generator (mulberry32). */
function seeded(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export async function withCanvasRig<T>(
  options: CanvasRigOptions,
  body: (rig: CanvasRig) => Promise<T>,
): Promise<T> {
  return withDom(async (dom) => {
    const win = dom.window as unknown as Record<string, unknown> & Window & typeof globalThis;
    const proto = win.HTMLCanvasElement.prototype as unknown as Record<string, unknown>;
    const global = globalThis as unknown as Record<string, unknown>;

    let width = options.width ?? 420;
    let height = options.height ?? 420;
    let broken: string | null = null;

    const calls: RecordedCall[] = [];
    let armed = 0;
    let cancelled = 0;
    let callback: FrameRequestCallback | null = null;
    let nextId = 0;
    const errors: string[] = [];
    const warnings: string[] = [];
    const attached = new Map<string, number>();
    const observers: Array<{ callback: () => void; targets: Set<unknown> }> = [];

    const restore: Array<() => void> = [];
    const replace = (target: Record<string, unknown>, key: string, value: unknown): void => {
      const had = Object.getOwnPropertyDescriptor(target, key);
      Object.defineProperty(target, key, { value, configurable: true, writable: true });
      restore.push(() => {
        if (had) Object.defineProperty(target, key, had);
        else delete target[key];
      });
    };

    // The frame clock.
    const request = (cb: FrameRequestCallback): number => {
      armed += 1;
      callback = cb;
      return ++nextId;
    };
    const cancel = (): void => {
      cancelled += 1;
      callback = null;
    };
    for (const target of [global, win as Record<string, unknown>]) {
      replace(target, "requestAnimationFrame", request);
      replace(target, "cancelAnimationFrame", cancel);
    }

    // A context that records.
    const gradient = { addColorStop() {} };
    replace(proto, "getContext", function (this: HTMLCanvasElement): unknown {
      const store: Record<string, unknown> = { canvas: this };
      return new Proxy(store, {
        get(target, key) {
          if (typeof key !== "string") return undefined;
          if (key in target) return target[key];
          return (...args: unknown[]) => {
            calls.push({ name: key, args });
            if (key === broken) throw new Error(`canvasRig: ${key} is broken`);
            if (key === "createLinearGradient" || key === "createRadialGradient") return gradient;
            if (key === "measureText") return { width: 0 };
            return undefined;
          };
        },
        set(target, key, value) {
          target[key as string] = value;
          return true;
        },
      });
    });

    // A canvas with a size.
    replace(proto, "getBoundingClientRect", () => ({
      width,
      height,
      top: 0,
      left: 0,
      right: width,
      bottom: height,
      x: 0,
      y: 0,
      toJSON() {},
    }));

    // A ResizeObserver a test can fire.
    class Observer {
      private readonly entry: { callback: () => void; targets: Set<unknown> };
      constructor(cb: () => void) {
        this.entry = { callback: cb, targets: new Set() };
        observers.push(this.entry);
      }
      observe(target: unknown): void {
        this.entry.targets.add(target);
      }
      unobserve(target: unknown): void {
        this.entry.targets.delete(target);
      }
      disconnect(): void {
        this.entry.targets.clear();
      }
    }
    for (const target of [global, win as Record<string, unknown>]) replace(target, "ResizeObserver", Observer);

    // Listener accounting, on the canvas and on the document.
    const track = (on: "canvas" | "document", owner: Record<string, unknown>): void => {
      const add = (win.EventTarget.prototype as unknown as Record<string, (...a: unknown[]) => void>)
        .addEventListener;
      const remove = (win.EventTarget.prototype as unknown as Record<string, (...a: unknown[]) => void>)
        .removeEventListener;
      replace(owner, "addEventListener", function (this: unknown, type: string, ...rest: unknown[]) {
        attached.set(`${on}:${type}`, (attached.get(`${on}:${type}`) ?? 0) + 1);
        return add.call(this, type, ...rest);
      });
      replace(owner, "removeEventListener", function (this: unknown, type: string, ...rest: unknown[]) {
        attached.set(`${on}:${type}`, (attached.get(`${on}:${type}`) ?? 0) - 1);
        return remove.call(this, type, ...rest);
      });
    };
    track("canvas", proto);
    track("document", win.document as unknown as Record<string, unknown>);

    // What the loops say to the console.
    const text = (args: unknown[]): string => String(args[0]);
    replace(console as unknown as Record<string, unknown>, "error", (...args: unknown[]) => {
      errors.push(text(args));
    });
    replace(console as unknown as Record<string, unknown>, "warn", (...args: unknown[]) => {
      warnings.push(text(args));
    });

    // Reproducible "random".
    replace(Math as unknown as Record<string, unknown>, "random", seeded(options.seed ?? 7));

    const rig: CanvasRig = {
      dom,
      calls,
      get armed() {
        return armed;
      },
      get cancelled() {
        return cancelled;
      },
      errors,
      warnings,
      pending: () => callback !== null,
      frame(t) {
        const cb = callback;
        if (!cb) throw new Error("canvasRig: no frame is pending, so the loop is not running");
        callback = null;
        cb(t);
      },
      run(count, from, stepMs = 1000 / 30) {
        let t = from;
        for (let i = 0; i < count && callback !== null; i++) {
          rig.frame(t);
          t += stepMs;
        }
        return t;
      },
      canvas() {
        const found = dom.container.querySelector("canvas");
        if (!found) throw new Error("canvasRig: nothing rendered a canvas");
        return found;
      },
      resize(w, h, notify = true) {
        width = w;
        height = h;
        if (!notify) return;
        for (const observer of [...observers]) {
          if (observer.targets.size > 0) observer.callback();
        }
      },
      breakMethod(name) {
        broken = name;
      },
      listeners: (on, type) => attached.get(`${on}:${type}`) ?? 0,
      count: (name) => calls.filter((c) => c.name === name).length,
      fillTexts: () =>
        calls
          .filter((c) => c.name === "fillText")
          .map((c) => ({ text: String(c.args[0]), x: Number(c.args[1]), y: Number(c.args[2]) })),
      clear() {
        calls.length = 0;
        errors.length = 0;
        warnings.length = 0;
      },
    };

    try {
      return await body(rig);
    } finally {
      for (const undo of restore.reverse()) undo();
    }
  });
}
