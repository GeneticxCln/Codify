/**
 * The weather's second job: reporting that the agent is working.
 *
 * Until now a theme's motion was pure decoration — the rain fell because the
 * theme was rainy, at the same rate whatever the app was doing. `active` existed
 * and exactly one effect used it, to brighten. The prop's own docstring drew a
 * line through it: "it changes nothing about how fast anything moves, which is
 * the line between an effect that reports state and one that is decoration."
 *
 * That was the right worry attached to the wrong test, and these tests are
 * partly about the correction. §7 distinguishes motion that carries information
 * from motion that does not — not motion that changes speed from motion that
 * does not. A streaming caret blinks at a constant rate and reports perfectly
 * well. So the rain now falls faster while a turn is in flight, and the
 * interesting properties are not "it is faster" but the four around it: it eases
 * rather than snapping, it costs nothing extra, it is one number shared by six
 * effects, and it comes from the run's own status rather than a second flag.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { ACTIVE_RATE, nextRate } from "../src/hooks/useAtmosphereCanvas.ts";

const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const clock = readFileSync(
  new URL("../src/hooks/useAtmosphereCanvas.ts", import.meta.url),
  "utf8",
);
const rain = readFileSync(
  new URL("../src/components/ui/MatrixRain.tsx", import.meta.url),
  "utf8",
);

test("the rate eases toward its target instead of stepping to it", () => {
  // A snap is the failure. A jump in the corner of the eye, while reading
  // something else, is worse than no signal at all — and it is invisible in a
  // screenshot, which is why this is arithmetic and not markup.
  assert.equal(nextRate(1, false, 1), 1, "an idle effect must stay at 1");
  assert.equal(nextRate(1, true, 0), 1, "with no elapsed time the rate cannot move");

  const first = nextRate(1, true, 1 / 30);
  assert.ok(first > 1 && first < ACTIVE_RATE, "the first active frame must ease, not arrive");

  // And it must actually arrive — an easing that never reaches its target is a
  // slower leak of the same idea, and would be invisible in the first-frame
  // check above.
  let rate = 1;
  for (let i = 0; i < 120; i++) rate = nextRate(rate, true, 1 / 30);
  assert.ok(Math.abs(rate - ACTIVE_RATE) < 0.01, `rate settled at ${rate}, not ${ACTIVE_RATE}`);

  // Coming back down is the same shape, and it matters as much: an effect that
  // ramps up in a second and decays over a minute would feel stuck.
  for (let i = 0; i < 120; i++) rate = nextRate(rate, false, 1 / 30);
  assert.ok(Math.abs(rate - 1) < 0.01, `rate settled at ${rate} on the way down, not 1`);
});

test("the rate is a multiplier on simulated time, not on frames", () => {
  // The claim that makes this affordable: a faster effect is not a busier one.
  // `fps` is the cost, and nothing here changes it.
  assert.ok(ACTIVE_RATE < 2, "at 2x the rain stops reading as weather and starts reading as a fault");
  assert.match(clock, /const scaled = dt \* rate;/, "the clock no longer scales dt by the rate");
  assert.match(
    clock,
    /painter\.step\?\.\(scaled, tick\);/,
    "step is handed real dt while the clock advances scaled time — drift and phase would disagree",
  );
  assert.match(clock, /tick\.frame \+= 1;/, "the frame count is the cost; it must not scale");
});

test("the rain keeps its integer grid while running at a fractional rate", () => {
  // `y` is a cell index, so it cannot hold 1.8. The remainder lives in `frac`
  // and whole cells are earned off it. Without that, a rate of 1.8 would round
  // to 1 (no change) or 2 (a jump) — and a jump is the failure this file is
  // mostly about.
  assert.match(rain, /frac: number;/, "the column has no sub-cell remainder to hold a fractional rate");
  assert.match(rain, /col\.frac \+= rate;/, "the rate is not accumulated per column");
  assert.match(rain, /while \(col\.frac >= 1\)/, "whole cells are not taken off the accumulator");
  assert.match(
    rain,
    /frac: Math\.random\(\)/,
    "every column starts its remainder at zero, so they would all step on the same frame",
  );
});

test("both loops share one rate, so the rain and the grid cannot disagree", () => {
  // `MatrixRain` runs its own loop — it predates the hook and is the documented
  // outlier. Two loops growing two constants is how "working" quietly means two
  // different things depending on which theme you picked.
  assert.match(rain, /import \{ nextRate \} from "\.\.\/\.\.\/hooks\/useAtmosphereCanvas"/);
  assert.doesNotMatch(rain, /ACTIVE_RATE\s*=\s*[\d.]/, "the rain has its own copy of the rate");
  assert.match(rain, /rate = nextRate\(rate, activeRef\.current, dt\)/);
});

test("every effect opts in, and the shell drives both backdrops from the run's own status", () => {
  for (const name of ["CyberGrid", "HudSweep", "AbyssSpores", "NebulaFlow", "NeuralWeb"]) {
    const source = readFileSync(
      new URL(`../src/components/ui/${name}.tsx`, import.meta.url),
      "utf8",
    );
    assert.match(
      source,
      /useAtmosphereCanvas\(\{[^}]*\bactive\b/,
      `${name} does not forward \`active\`, so its weather ignores whether the agent is working`,
    );
  }
  // One flag, from the goal's own status — not a second piece of state that can
  // disagree with the Stop button. This is the same wiring the tests above the
  // atmosphere table already check, extended to the rain, which used to take no
  // props at all.
  assert.match(app, /<RainBackdrop active=\{canStop\} \/>/, "the rain does not know the agent is working");
  assert.match(app, /<WeatherBackdrop active=\{canStop\} \/>/);
  assert.match(
    app,
    /const canStop = canStopGoal\(activeGoal\?\.status\);/,
    "the flag must come from the goal's status, or it can disagree with Stop",
  );
});

test("reduced motion still means no motion at all", () => {
  // The trap in any speed change: a user who asked for no motion gets 1.8x of
  // nothing, which is still motion. Both loops gate *arming* on the preference,
  // so an unscaled loop is one that was never started.
  assert.match(clock, /if \(!reduced\) raf = requestAnimationFrame\(loop\);/);
  assert.match(rain, /if \(!reduced\) raf = requestAnimationFrame\(loop\);/);
  assert.match(
    rain,
    /drawFrame\(\); \/\/ the static frame reduced motion gets/,
    "the motionless theme must still get its one frame, not an empty canvas",
  );
});

test("active changes how fast, never what is rendered", () => {
  // `active` is a ref, not a dependency. If it were a dependency, every state
  // change would tear the loop down and re-arm it — and for an effect with
  // drift that is a visible jump, which is the exact failure the ramp exists to
  // avoid. It would also mean the component remounted on every turn.
  assert.doesNotMatch(
    clock,
    /}, \[animated, fps, maxDimension, active\]\);/,
    "active is a dependency, so every state change tears down and re-arms the loop",
  );
  assert.match(clock, /}, \[animated, fps, maxDimension\]\);/);
  // Same shape on the rain's own loop, which has its own effect body.
  assert.doesNotMatch(rain, /}, \[animated, fps, maxDimension, active\]\);/);
  assert.match(rain, /const activeRef = useRef\(active\);\s*\n\s*activeRef\.current = active;/);
});

test("a theme with no weather is untouched by any of this", () => {
  // `still` publishes nothing, so it has no canvas to speed up. Asserted here
  // because a prop that only some components read is exactly how a new theme
  // quietly becomes the one theme where the signal does not work.
  const backdrop = readFileSync(
    new URL("../src/components/ui/WeatherBackdrop.tsx", import.meta.url),
    "utf8",
  );
  assert.match(
    backdrop,
    /const ATMOSPHERES: Readonly<Record<string, Atmosphere>> = \{[\s\S]{2,}?\n\};/,
    "the weather table could not be read, which would make this assertion vacuous",
  );
  assert.match(
    backdrop,
    /if \(!atmosphere \|\| !theme\.tokens\[atmosphere\.trigger/,
    "the trigger gate is gone, so a motionless theme would get a canvas",
  );
});
