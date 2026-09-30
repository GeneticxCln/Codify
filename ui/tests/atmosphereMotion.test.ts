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
 *
 * Both render loops are held here — the shared hook behind the themed backdrops
 * (through `Nanofluid`) and the rain's own effect (`MatrixRain`) — because a
 * rule wired into one loop is a rule the other silently lacks, and that is a
 * claim about what each *does*: `motionPreference.test.ts` used to make it by
 * matching `useMotionAllowed()` in both files' source.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
const { MOTION_EVENT, MOTION_KEY } = await import("../src/motionPreference.ts");

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

type Loop = "Nanofluid" | "MatrixRain";

interface Conditions {
  /** Which render loop to mount. */
  component?: Loop;
  /** The user's stored choice, if any. */
  setting?: "auto" | "allowed" | "reduced";
  /** The shell's verdict for this boot: the window is being rasterised in software. */
  starved?: boolean;
  /** The explicit `animated` prop a settings preview passes. */
  animated?: boolean;
}

/**
 * Mount a backdrop under these conditions, and say how many frame loops it
 * armed. The loop is armed by a `requestAnimationFrame` call, and the harness's
 * canvas draws the static frame without one.
 */
async function loopsArmed(conditions: Conditions): Promise<number> {
  const { component = "Nanofluid", setting, starved = false, animated } = conditions;
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
    if (setting) dom.window.localStorage.setItem(MOTION_KEY, setting);
    if (starved) {
      dom.window.sessionStorage.setItem(MOTION_EVENT, JSON.stringify({ at: 1, measuredMs: 90 }));
    }

    const React = (await import("react")).default;
    const Backdrop =
      component === "Nanofluid"
        ? (await import("../src/components/ui/Nanofluid.tsx")).Nanofluid
        : (await import("../src/components/ui/MatrixRain.tsx")).MatrixRain;
    await dom.render(React.createElement(Backdrop, animated === undefined ? {} : { animated }));
    return armed;
  });
}

const LOOPS: Loop[] = ["Nanofluid", "MatrixRain"];

for (const component of LOOPS) {
  test(`${component}: a machine that allows motion gets the animation`, async () => {
    assert.ok(
      (await loopsArmed({ component, setting: "auto" })) > 0,
      "motion is allowed and the backdrop drew one still frame and never started",
    );
  });

  test(`${component}: choosing 'allowed' animates`, async () => {
    assert.ok((await loopsArmed({ component, setting: "allowed" })) > 0);
  });

  test(`${component}: choosing 'reduced' leaves the backdrop still`, async () => {
    assert.equal(
      await loopsArmed({ component, setting: "reduced" }),
      0,
      "the user asked for no motion and a frame loop was armed anyway",
    );
  });

  test(`${component}: the shell's verdict stops the loop when the user has not chosen`, async () => {
    assert.equal(
      await loopsArmed({ component, setting: "auto", starved: true }),
      0,
      "the shell measured the window starving itself and this loop ran anyway",
    );
    assert.equal(await loopsArmed({ component, starved: true }), 0, "no stored choice is 'auto'");
  });

  test(`${component}: the user's word outranks the shell's verdict`, async () => {
    assert.ok(
      (await loopsArmed({ component, setting: "allowed", starved: true })) > 0,
      "'Animate anyway' was chosen and the verdict still held the loop",
    );
  });

  test(`${component}: a preview that asks to animate animates, whatever the store says`, async () => {
    // A preview that cannot animate is not a preview, so the explicit prop wins
    // over both the choice and the verdict.
    assert.ok((await loopsArmed({ component, setting: "reduced", starved: true, animated: true })) > 0);
    assert.equal(await loopsArmed({ component, setting: "allowed", animated: false }), 0);
  });
}
