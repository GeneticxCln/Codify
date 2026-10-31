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
const { MOTION_BOOT_KEY, MOTION_EVENT, MOTION_KEY, MOTION_SESSION_KEY, motionBootId } =
  await import("../src/motionPreference.ts");

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
  /** The choice in force, if any: a preference, or the session's "Animate anyway". */
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
    if (setting === "allowed" || starved) {
      // Session facts are scoped to the shell launch that wrote them. This page
      // is that launch (`motionBootId` is the page id when there is no shell),
      // so a verdict or an answer here is a fact of this boot.
      dom.window.sessionStorage.setItem(MOTION_BOOT_KEY, motionBootId());
    }
    if (setting === "allowed") {
      // "Animate anyway" is this window's answer to this boot's verdict, not a
      // preference: it lives in the session store and dies with the launch.
      dom.window.sessionStorage.setItem(MOTION_SESSION_KEY, setting);
    } else if (setting) {
      dom.window.localStorage.setItem(MOTION_KEY, setting);
    }
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

const { backdropFps, STARVED_FPS, STARVED_FPS_FLOOR, starvedFps } = await import(
  "../src/hooks/useAtmosphereCanvas.ts",
);

test("a starved window lowers the release rate, and never raises a painter's own cap", () => {
  assert.equal(backdropFps(30, false), 30, "a healthy window's rate was changed");
  assert.equal(backdropFps(30, true), STARVED_FPS, "the shell's verdict did not lower the rate");
  assert.ok(STARVED_FPS > 0 && STARVED_FPS < 30, "the starved rate is not a lower animation rate");
  assert.equal(
    backdropFps(10, true),
    10,
    "a painter that already asked for fewer than the starved rate was sped up",
  );
});

test("the starved rate scales with the window's physical pixels, never below the floor", () => {
  // The compositor pays per physical pixel. The ten-FPS figure was profiled on
  // a ~2 MP window; a 4K-class window is four times the area and gets about a
  // quarter of the rate for the same core share.
  assert.equal(starvedFps(2_000_000), STARVED_FPS);
  assert.equal(starvedFps(8_000_000), 2.5);
  assert.equal(starvedFps(20_000_000), STARVED_FPS_FLOOR, "a huge window went below the floor");
  assert.equal(starvedFps(0), STARVED_FPS, "an unmeasured box was treated as starving");
  assert.equal(
    backdropFps(30, true, 8_000_000),
    2.5,
    "a 4K-class window animated at the 2 MP rate",
  );
  assert.equal(
    backdropFps(2, true, 8_000_000),
    2,
    "a painter that asked for two frames a second was sped up to the starved rate",
  );
});

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
