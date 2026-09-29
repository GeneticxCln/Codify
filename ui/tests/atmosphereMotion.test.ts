/**
 * The themes move when motion is allowed, and stand still when it is not.
 *
 * `atmosphere.test.ts` pins the shared clock by matching its source, and one of
 * the lines it matches is `if (!reduced) raf = requestAnimationFrame(loop);`.
 * That line was right and the value feeding it was inverted: the store answers
 * "is motion allowed" and the hook read it as "is motion reduced", so a machine
 * that allowed motion got a still frame and a machine that forbade it got the
 * animation. Every string the source test looks for was present the whole time.
 *
 * These mount the real component and count whether a frame loop was armed.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");

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

/**
 * Mount the Liquid Mercury backdrop under a stored motion setting, and say how
 * many frame loops it armed. The loop is armed by a `requestAnimationFrame`
 * call, and the harness's canvas draws the static frame without one.
 */
async function loopsArmed(setting: "auto" | "allowed" | "reduced"): Promise<number> {
  return withDom(async (dom) => {
    let armed = 0;
    const win = dom.window as unknown as Record<string, unknown>;
    const counting = (): number => {
      armed += 1;
      return armed;
    };
    for (const target of [globalThis, win] as unknown as Record<string, unknown>[]) {
      Object.defineProperty(target, "requestAnimationFrame", {
        value: counting,
        configurable: true,
        writable: true,
      });
    }
    (dom.window.HTMLCanvasElement.prototype as unknown as Record<string, unknown>).getContext =
      () => quietContext();
    dom.window.localStorage.setItem("codify.motion", setting);

    const React = (await import("react")).default;
    const { Nanofluid } = await import("../src/components/ui/Nanofluid.tsx");
    await dom.render(React.createElement(Nanofluid, {}));
    return armed;
  });
}

test("a machine that allows motion gets the animation", async () => {
  assert.ok(
    (await loopsArmed("auto")) > 0,
    "motion is allowed and the backdrop drew one still frame and never started",
  );
});

test("choosing 'allowed' animates", async () => {
  assert.ok((await loopsArmed("allowed")) > 0);
});

test("choosing 'reduced' leaves the backdrop still", async () => {
  assert.equal(
    await loopsArmed("reduced"),
    0,
    "the user asked for no motion and a frame loop was armed anyway",
  );
});
