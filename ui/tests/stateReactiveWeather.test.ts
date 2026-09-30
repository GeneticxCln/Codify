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
 * rather than snapping, it costs nothing extra, it is one number shared by every
 * effect, and it comes from the run's own status rather than a second flag.
 *
 * Every claim here is held by running the real loops: the shared clock behind the
 * themed backdrops and the rain's own, driven a frame at a time by
 * `canvasRig.ts`. This file used to hold most of them by matching the two loops'
 * source for `const scaled = dt * rate;` and `col.frac += rate;`, which is the
 * spelling of the idea and not the idea. The claim that the shell hands the
 * weather the run's own status is held against the mounted App in
 * `backdropShell.test.ts`.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { ACTIVE_RATE, nextRate } from "../src/hooks/useAtmosphereCanvas.ts";
import { MOTION_KEY, writeSetting } from "../src/motionPreference.ts";
import { probe, recorder, type Recorder } from "./atmosphereProbe.ts";
import type { CanvasRig } from "./canvasRig.ts";

const { withCanvasRig } = await import("./canvasRig.ts");
const { MatrixRain } = await import("../src/components/ui/MatrixRain.tsx");
const { effectFor, themesWithWeather } = await import("../src/components/ui/WeatherBackdrop.tsx");

/** A frame later than the last by more than one frame's time at 30 fps, so none is skipped. */
const STEP_MS = 34;
/** What a frame's `dt` is capped at: one frame's time at 30 fps. */
const FRAME_S = 1 / 30;

const sum = (xs: number[]): number => xs.reduce((a, b) => a + b, 0);
const last = <T>(xs: T[]): T => xs[xs.length - 1];

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

test("the rate is a multiplier on simulated time, not on frames", async () => {
  // The claim that makes this affordable: a faster effect is not a busier one.
  // `fps` is the cost, and nothing here changes it. Two real runs of the shared
  // clock, identical but for `active`, are compared.
  assert.ok(ACTIVE_RATE < 2, "at 2x the rain stops reading as weather and starts reading as a fault");

  const play = (active: boolean): Promise<Recorder> =>
    withCanvasRig({}, async (rig) => {
      const rec = recorder();
      await rig.dom.render(probe({ rec, active }));
      rig.run(150, 1000, STEP_MS);
      return rec;
    });
  const idle = await play(false);
  const busy = await play(true);

  assert.equal(busy.draws, idle.draws, "a faster effect painted more often: the rate became a cost");
  assert.equal(last(busy.frames), last(idle.frames), "the frame count scaled with the rate");

  // What the rate scales is the time a frame is worth, and it is one number for
  // both the clock and the painter's `step`: drift and phase cannot disagree.
  assert.ok(Math.abs(last(busy.seconds) - sum(busy.steps)) < 1e-9, "the clock and `step` disagree about elapsed time");
  assert.ok(Math.abs(last(idle.seconds) - sum(idle.steps)) < 1e-9);
  assert.ok(Math.abs(sum(idle.steps) - 149 * FRAME_S) < 1e-9, "an idle effect must run at real time");
  const ratio = sum(busy.steps) / sum(idle.steps);
  assert.ok(ratio > 1.5 && ratio < ACTIVE_RATE, `a working effect covered ${ratio.toFixed(2)}x the time of an idle one`);

  // It eased in, and it arrived.
  assert.ok(busy.steps[1] / FRAME_S > 1 && busy.steps[1] / FRAME_S < 1.2, "the first active frame did not ease");
  assert.ok(Math.abs(last(busy.steps) / FRAME_S - ACTIVE_RATE) < 0.01, "the rate never arrived");
});

test("a display that calls back faster than 30 Hz still gets 30 frames", async () => {
  // The accumulator is the clock and rAF is only the alignment: a 144 Hz
  // monitor must not make the theme's cost a property of the monitor.
  await withCanvasRig({}, async (rig) => {
    const rec = recorder();
    await rig.dom.render(probe({ rec }));
    const frame144 = 1000 / 144;
    rig.run(144, 1000, frame144);
    const loopFrames = rec.draws - 1; // the first draw is the static frame
    assert.ok(loopFrames >= 28 && loopFrames <= 31, `a second of 144 Hz callbacks released ${loopFrames} frames`);
  });
});

test("the canvas is capped, whatever the window is", async () => {
  await withCanvasRig({ width: 4000, height: 2000 }, async (rig) => {
    await rig.dom.render(probe({ rec: recorder(), maxDimension: 1024 }));
    const canvas = rig.canvas();
    assert.equal(Math.max(canvas.width, canvas.height), 1024, "the longer edge was not capped");
    assert.equal(canvas.height * 2, canvas.width, "the cap changed the aspect ratio");
  });
});

test("the rain keeps its integer grid while running at a fractional rate", async () => {
  // `y` is a cell index, so it cannot hold 1.8. The remainder lives in `frac`
  // and whole cells are earned off it. Without that, a rate of 1.8 would round
  // to 1 (no change) or 2 (a jump) — and a jump is the failure this file is
  // mostly about.
  await withCanvasRig({}, async (rig) => {
    await rig.dom.render(React.createElement(MatrixRain, { active: true }));
    rig.run(150, 1000, STEP_MS);
    const cell = 14;
    const off = rig.fillTexts().filter(({ y }) => Math.abs(y / cell - Math.round(y / cell)) > 1e-9);
    assert.deepEqual(off.slice(0, 3), [], "a glyph was drawn between two cells");
  });
});

/** Where each column's head glyph was drawn in one frame: the first `fillText` at each x. */
function headsOf(rig: CanvasRig, cell: number): Map<number, number> {
  const heads = new Map<number, number>();
  for (const { x, y } of rig.fillTexts()) {
    const column = Math.round(x / cell);
    if (!heads.has(column)) heads.set(column, y / cell);
  }
  return heads;
}

/** Run the rain and record each frame's heads: frame 0 is the static frame drawn at mount. */
async function rainHeads(rig: CanvasRig, active: boolean, frames: number): Promise<Array<Map<number, number>>> {
  await rig.dom.render(React.createElement(MatrixRain, { active }));
  const cell = 14;
  const all = [headsOf(rig, cell)];
  let t = 1000;
  for (let f = 0; f < frames; f++) {
    rig.clear();
    t = rig.run(1, t, STEP_MS);
    all.push(headsOf(rig, cell));
  }
  return all;
}

/** Cells each column moved between two frames; a column that wrapped to the top is left out. */
function advances(from: Map<number, number>, to: Map<number, number>): Map<number, number> {
  const moved = new Map<number, number>();
  for (const [column, y] of to) {
    const before = from.get(column);
    if (before !== undefined && y >= before) moved.set(column, y - before);
  }
  return moved;
}

test("the rain starts at its resting speed and eases up to 1.8x, column by column", async () => {
  await withCanvasRig({}, async (rig) => {
    const heads = await rainHeads(rig, true, 160);
    // Frame 1 has no elapsed time, so the rate is exactly 1 and each column moves by its own speed.
    const speed = advances(heads[0], heads[1]);
    assert.ok(speed.size >= 25, "the rain has too few columns to say anything");

    const meanFactor = (from: number, to: number): number => {
      let total = 0;
      let n = 0;
      for (let f = from; f < to; f++) {
        for (const [column, moved] of advances(heads[f - 1], heads[f])) {
          total += moved / (speed.get(column) ?? 1);
          n += 1;
        }
      }
      return total / n;
    };
    const early = meanFactor(2, 6);
    const settled = meanFactor(120, 160);
    assert.ok(early < 1.3, `the rain jumped: ${early.toFixed(2)}x within the first frames`);
    assert.ok(
      Math.abs(settled - ACTIVE_RATE) < 0.1,
      `the rain settled at ${settled.toFixed(2)}x, not ${ACTIVE_RATE}x`,
    );
  });
});

test("the rain's columns do not step in lockstep at a fractional rate", async () => {
  // Every column starts with its own remainder. A shared one would make every
  // column take its next whole cell on the same frame, and the rain would
  // pulse instead of showering.
  await withCanvasRig({}, async (rig) => {
    const heads = await rainHeads(rig, true, 160);
    const speed = advances(heads[0], heads[1]);
    const slow = [...speed].filter(([, cells]) => cells === 1).map(([column]) => column);
    assert.ok(slow.length >= 10, "too few one-cell columns to compare");

    let frames = 0;
    for (let f = 130; f < 160; f++) {
      const moved = advances(heads[f - 1], heads[f]);
      const seen = new Set(slow.filter((c) => moved.has(c)).map((c) => moved.get(c)));
      if (seen.has(1) && seen.has(2)) frames += 1;
    }
    assert.ok(frames >= 20, `columns of the same speed stepped together on ${30 - frames} of 30 frames`);
  });
});

test("the rain and the shared clock settle on the same rate", async () => {
  // `MatrixRain` runs its own loop — it predates the hook and is the documented
  // outlier. Two loops growing two constants is how "working" quietly means two
  // different things depending on which theme you picked.
  const clock = await withCanvasRig({}, async (rig) => {
    const rec = recorder();
    await rig.dom.render(probe({ rec, active: true }));
    rig.run(150, 1000, STEP_MS);
    return last(rec.steps) / FRAME_S;
  });
  const rain = await withCanvasRig({}, async (rig) => {
    const heads = await rainHeads(rig, true, 160);
    const speed = advances(heads[0], heads[1]);
    let total = 0;
    let n = 0;
    for (let f = 120; f < 160; f++) {
      for (const [column, moved] of advances(heads[f - 1], heads[f])) {
        total += moved / (speed.get(column) ?? 1);
        n += 1;
      }
    }
    return total / n;
  });
  assert.ok(Math.abs(clock - rain) < 0.1, `the grid settled at ${clock.toFixed(2)}x and the rain at ${rain.toFixed(2)}x`);
});

test("every weather theme's effect runs differently while the agent is working", async () => {
  // A prop that only some components read is exactly how a new theme quietly
  // becomes the one theme where the signal does not work. So each effect in the
  // backdrop's table is mounted twice, identical but for `active`, and what it
  // drew is compared: an effect that ignores the flag draws the same thing.
  const themes = themesWithWeather();
  assert.ok(themes.length >= 10, "the backdrop table has shrunk or could not be read");
  for (const id of themes) {
    const Effect = effectFor(id);
    assert.ok(Effect, `${id} is in the table with no effect`);
    const drawn = (active: boolean): Promise<string> =>
      withCanvasRig({}, async (rig) => {
        await rig.dom.render(React.createElement(Effect, { active, maxDimension: 1024 }));
        rig.run(90, 1000, STEP_MS);
        return JSON.stringify(
          rig.calls.map((c) => [c.name, ...c.args.map((a) => (typeof a === "number" ? Math.round(a * 1000) : a))]),
        );
      });
    const idle = await drawn(false);
    const busy = await drawn(true);
    assert.ok(idle.length > 1000, `${id} drew almost nothing, so the comparison says nothing`);
    assert.notEqual(busy, idle, `${id} draws the same whether or not the agent is working: it ignores \`active\``);
  }
});

test("active changes how fast, never what is rendered", async () => {
  // `active` is read live, not watched. If it were a dependency, every state
  // change would tear the loop down and re-arm it — and for an effect with
  // drift that is a visible jump, which is the exact failure the ramp exists to
  // avoid. It would also mean the component remounted on every turn.
  await withCanvasRig({}, async (rig) => {
    const rec = recorder();
    await rig.dom.render(probe({ rec, active: false }));
    const canvas = rig.canvas();
    const next = rig.run(5, 1000, STEP_MS);
    await rig.dom.render(probe({ rec, active: true }));
    assert.equal(rec.created, 1, "a change of `active` rebuilt the painter");
    assert.equal(rig.cancelled, 0, "a change of `active` tore the loop down");
    assert.ok(rig.canvas() === canvas, "a change of `active` replaced the canvas");
    rig.run(60, next, STEP_MS);
    assert.ok(last(rec.steps) / FRAME_S > 1.3, "the loop kept running but never answered the new state");
  });

  // The same, for the rain's own loop.
  await withCanvasRig({}, async (rig) => {
    await rig.dom.render(React.createElement(MatrixRain, { active: false }));
    const canvas = rig.canvas();
    rig.run(5, 1000, STEP_MS);
    await rig.dom.render(React.createElement(MatrixRain, { active: true }));
    assert.equal(rig.cancelled, 0, "a change of `active` tore the rain down");
    assert.ok(rig.canvas() === canvas, "a change of `active` replaced the rain's canvas");
  });
});

test("the motion decision, unlike `active`, does re-arm the loop", async () => {
  // `motionAllowed` IS a dependency, deliberately: it is the motion decision
  // (user choice, shell verdict, system preference), and a verdict that arrives
  // mid-run must re-arm the loop or the page keeps animating on exactly the
  // machine the verdict was about. `active` changes every turn; this changes
  // about never.
  await withCanvasRig({}, async (rig) => {
    const rec = recorder();
    await rig.dom.render(probe({ rec }));
    assert.ok(rig.pending(), "the loop was not running before the user's choice");
    await React.act(async () => {
      writeSetting("reduced");
    });
    assert.equal(rec.created, 2, "a change of the motion decision did not rebuild the loop");
    assert.ok(!rig.pending(), "the user chose no motion and a frame was still armed");
    assert.equal(rig.dom.window.localStorage.getItem(MOTION_KEY), "reduced");
  });
});

test("the system's own preference decides when the user has not chosen, and is followed live", async () => {
  // The query string and the subscription are both behaviour: a loop that asks the wrong question
  // animates for everyone who asked for stillness, and one that asks once never hears the OS flip.
  const mounts: Array<[string, (rig: CanvasRig) => Promise<void>]> = [
    ["the shared clock", (rig) => rig.dom.render(probe({ rec: recorder() }))],
    ["the rain", (rig) => rig.dom.render(React.createElement(MatrixRain))],
  ];
  for (const [name, mount] of mounts) {
    await withCanvasRig({}, async (rig) => {
      let reduce = true;
      const listeners = new Set<() => void>();
      Object.defineProperty(rig.dom.window, "matchMedia", {
        configurable: true,
        writable: true,
        value: (query: string) => ({
          media: query,
          // Only the real question gets the real answer.
          get matches() {
            return reduce && query === "(prefers-reduced-motion: reduce)";
          },
          addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
          removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
        }),
      });
      await mount(rig);
      assert.equal(rig.armed, 0, `${name} animated for a system that asked for no motion`);

      reduce = false;
      await React.act(async () => {
        for (const listener of listeners) listener();
      });
      assert.ok(rig.pending(), `${name} never heard the system preference change`);
    });
  }
});

test("reduced motion still means no motion at all", async () => {
  // The trap in any speed change: a user who asked for no motion gets 1.8x of
  // nothing, which is still motion. Both loops gate *arming* on the preference,
  // so an unscaled loop is one that was never started — and each still draws
  // the one frame the motionless theme gets, not an empty canvas.
  await withCanvasRig({}, async (rig) => {
    const rec = recorder();
    await rig.dom.render(probe({ rec, animated: false, active: true }));
    assert.equal(rig.armed, 0, "a loop was armed for a user who asked for none");
    assert.equal(rec.draws, 1, "the still theme did not get its one frame");
  });
  await withCanvasRig({}, async (rig) => {
    await rig.dom.render(React.createElement(MatrixRain, { animated: false, active: true }));
    assert.equal(rig.armed, 0, "the rain armed a loop for a user who asked for none");
    assert.equal(rig.count("fillRect"), 1, "the still rain did not draw its one frame");
    assert.ok(rig.fillTexts().length > 0, "the still rain drew a veil and no glyphs");
  });
});
