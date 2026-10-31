/**
 * The probe gates a real backdrop, not just its own arithmetic.
 *
 * `frameProbe.test.ts` pins what the numbers mean. This mounts the real hook
 * (through `Nanofluid`) with a fake shell bridge, feeds the probe two CPU
 * readings per pass, and checks both ends of the decision: an affordable frame
 * arms the loop, an unaffordable one records the verdict and arms nothing. The
 * frames are answered by a `requestAnimationFrame` that resolves the probe's
 * ten draws immediately and records the first call after them — the loop — so
 * the test can say "the loop started" or "it did not" without waiting ten
 * seconds for a watchdog.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
const { STARVED_BOOT_KEY } = await import("../src/motionPreference.ts");

/** A 2d context that accepts everything a painter does and records nothing. */
function quietContext(): CanvasRenderingContext2D {
  const gradient = { addColorStop() {} };
  const target: Record<string, unknown> = {};
  return new Proxy(target, {
    get: (t, key) => {
      if (key in t) return t[key as string];
      if (key === "createLinearGradient" || key === "createRadialGradient") return () => gradient;
      if (key === "measureText") return () => ({ width: 0 });
      return () => undefined;
    },
    set: (t, key, value) => {
      t[key as string] = value;
      return true;
    },
  }) as unknown as CanvasRenderingContext2D;
}

type Backdrop = "Nanofluid" | "MatrixRain";

const BACKDROPS: Backdrop[] = ["Nanofluid", "MatrixRain"];

interface Outcome {
  /** Whether the loop armed after the probe. */
  armed: boolean;
  /** Whether this boot was recorded as measured starving. */
  starved: boolean;
  /** How many clock readings the probe made. */
  reads: number;
}

/**
 * Mount the real backdrop with a shell clock answering from `clock`, and say
 * what happened. `clock` is read in order and its last value repeats.
 */
async function mounted(component: Backdrop, clock: number[]): Promise<Outcome> {
  return withDom(async (dom) => {
    let reads = 0;
    let frames = 0;
    let armed = false;
    let callbackId = 1;
    const callbacks = new Map<number, unknown>();
    const internals = {
      /** The event bridge allocates a callback id through this before it invokes. */
      transformCallback: (callback: unknown, once?: boolean): number => {
        const id = callbackId;
        callbackId += 1;
        callbacks.set(id, { callback, once });
        return id;
      },
      callbacks,
      // The motion hooks unlisten on unmount; the real bridge removes the
      // callback by id and this fake has nothing to remove.
      unregisterListener: (): void => {},
      invoke: async (command: string): Promise<number | null> => {
        if (command === "codify_render_cpu_ms") {
          const value = clock[Math.min(reads, clock.length - 1)];
          reads += 1;
          return value;
        }
        // The motion hooks register their shell listener on mount. It is not
        // what this test is measuring, but a missing event id would throw
        // before the probe could run.
        if (command === "plugin:event|listen") return 1;
        return null;
      },
    };
    const win = dom.window as unknown as Record<string, unknown>;
    win.__TAURI_INTERNALS__ = internals;
    (globalThis as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = internals;
    // The event plugin has its own internals object; `unlisten` calls this one,
    // not the command bridge's.
    const eventInternals = { unregisterListener: (): void => {} };
    win.__TAURI_EVENT_PLUGIN_INTERNALS__ = eventInternals;
    (globalThis as unknown as Record<string, unknown>).__TAURI_EVENT_PLUGIN_INTERNALS__ =
      eventInternals;

    const raf = (callback: (t: number) => void): number => {
      frames += 1;
      if (frames <= 10) {
        // The probe's ten draws: resolve each frame as soon as it is asked for.
        queueMicrotask(() => callback(performance.now()));
      } else {
        // The loop's first arming. It is recorded and never run, so a test can
        // count "started" without an endless animation in the test process.
        armed = true;
      }
      return frames;
    };
    for (const target of [globalThis, win] as unknown as Record<string, unknown>[]) {
      Object.defineProperty(target, "requestAnimationFrame", {
        value: raf,
        configurable: true,
        writable: true,
      });
    }
    (dom.window.HTMLCanvasElement.prototype as unknown as Record<string, unknown>).getContext =
      () => quietContext();

    const React = (await import("react")).default;
    const Backdrop =
      component === "Nanofluid"
        ? (await import("../src/components/ui/Nanofluid.tsx")).Nanofluid
        : (await import("../src/components/ui/MatrixRain.tsx")).MatrixRain;
    await dom.render(React.createElement(Backdrop, {}));
    // The 500 ms warm-up, two passes of five frames plus the 120 ms settle, and
    // the IPC round trip the fake answers through. Long enough that the probe has
    // finished and the verdict's re-render has settled before the DOM is torn down.
    await new Promise((resolve) => setTimeout(resolve, 1600));
    await dom.act(async () => {});
    // The probe writes the store from a promise that no `act` wraps, which is
    // exactly how the product does it. Take the React warning as the test
    // acknowledging that the update landed outside its own act.
    dom.takeConsoleErrors();
    // Unmount while the fake bridge is still installed: the hooks' unlisten
    // reads it, and `withDom` resets the globals after the callback returns.
    await dom.unmount();
    return {
      armed,
      starved: dom.window.sessionStorage.getItem(STARVED_BOOT_KEY) === "1",
      reads,
    };
  });
}

for (const component of BACKDROPS) {
  test(`${component}: an affordable frame arms the loop after measuring it`, async () => {
    // 20 ms/frame in the first pass (the boot can be noisy), 2 ms in the settled
    // second pass. The cheaper pass is the verdict, so 2 ms/frame — 6% of a core
    // at 30 FPS — starts the animation.
    const outcome = await mounted(component, [1000, 1100, 1100, 1110]);
    assert.equal(outcome.reads, 4, "the probe did not read both ends of both passes");
    assert.equal(outcome.starved, false, "an affordable frame recorded a starvation verdict");
    assert.equal(
      outcome.armed,
      true,
      "the probe measured a fitting frame and never started the loop",
    );
  });

  test(`${component}: an unaffordable frame records the verdict and does not arm the loop`, async () => {
    // 60 ms per frame at 30 FPS is ~180% of the frame interval, clamped to the
    // banner's 100% — the boot probe refuses a frame with no headroom left.
    const outcome = await mounted(component, [1000, 1300, 1300, 1600]);
    assert.equal(outcome.starved, true, "a full-core frame was treated as affordable");
    assert.equal(
      outcome.armed,
      false,
      "the theme started at full rate on a frame it cannot afford",
    );
  });

  test(`${component}: a page with no shell bridge starts as it always did`, async () => {
    const outcome = await withDom(async (dom) => {
      let armed = false;
      const raf = (): number => {
        armed = true;
        return 1;
      };
      const win = dom.window as unknown as Record<string, unknown>;
      for (const target of [globalThis, win] as unknown as Record<string, unknown>[]) {
        Object.defineProperty(target, "requestAnimationFrame", {
          value: raf,
          configurable: true,
          writable: true,
        });
      }
      (dom.window.HTMLCanvasElement.prototype as unknown as Record<string, unknown>).getContext =
        () => quietContext();
      const React = (await import("react")).default;
      const Backdrop =
        component === "Nanofluid"
          ? (await import("../src/components/ui/Nanofluid.tsx")).Nanofluid
          : (await import("../src/components/ui/MatrixRain.tsx")).MatrixRain;
      await dom.render(React.createElement(Backdrop, {}));
      await dom.act(async () => {});
      await dom.unmount();
      return { armed, reads: 0, starved: false };
    });
    assert.equal(outcome.armed, true, "a browser preview without the shell never animated");
  });
}
