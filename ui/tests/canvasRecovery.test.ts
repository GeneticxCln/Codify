/**
 * A backdrop that survives a driver reset, and a painter that cannot flood the console.
 *
 * **Why this needed its own file.** Both loops — the shared hook and the matrix
 * rain — hold a `getContext("2d")` and draw into it forever. Neither listened
 * for `contextlost`, which means a GPU driver reset, a laptop lid closing or a
 * monitor renegotiating its mode left the app running with a blank backdrop and
 * *no error*: every `fillRect` after a lost context is a silent no-op, so nothing
 * throws, nothing is logged, and the only remedy is reloading the window. On a
 * machine left open for a week that is not a rare event, it is an ordinary one.
 *
 * The second failure is quieter and worse. The render loop re-arms its own
 * `requestAnimationFrame` on the first line, so a painter that throws does not
 * stop the loop — it throws again on the next frame and the one after, thirty
 * times a second, for as long as the window is open. The backdrop is left
 * half-drawn behind a console flood that looks like noise.
 *
 * So the tests here are two kinds. The first drives the recovery unit against a
 * fake canvas, because the behaviour is in `preventDefault` and in what happens
 * *after* the flag flips — neither of which is visible from a string of source.
 * The second mounts the two real loops (the shared clock and the rain) under
 * `canvasRig.ts` and does to them what a driver reset, a throwing painter and a
 * software compositor do: a recovery module that nothing calls is a module that
 * has fixed nothing, and the two failures look identical from the outside — a
 * blank canvas — so what is held is that the loop *behaves* differently, not that
 * its source contains `bindContextRecovery(`.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import {
  bindContextRecovery,
  makeFrameBudget,
  makeFrameGuard,
  FRAMES_BEFORE_GIVING_UP,
  FRAMES_BEFORE_JUDGING,
  FRAME_BUDGET_WARMUP_MS,
  SLOW_FRAME_FACTOR,
  type RecoverableCanvas,
} from "../src/canvasRecovery.ts";
import { probe, recorder, type Recorder } from "./atmosphereProbe.ts";
import type { CanvasRig } from "./canvasRig.ts";

const { withCanvasRig } = await import("./canvasRig.ts");
const { MatrixRain } = await import("../src/components/ui/MatrixRain.tsx");

/** A canvas that records its listeners and lets a test fire them. */
function fakeCanvas(): RecoverableCanvas & {
  fire(type: string): void;
  listeners: Map<string, ((event: Event) => void)[]>;
} {
  const listeners = new Map<string, ((event: Event) => void)[]>();
  return {
    listeners,
    addEventListener(type, listener) {
      const list = listeners.get(type) ?? [];
      list.push(listener);
      listeners.set(type, list);
    },
    removeEventListener(type, listener) {
      const list = listeners.get(type) ?? [];
      listeners.set(
        type,
        list.filter((l) => l !== listener),
      );
    },
    fire(type) {
      for (const listener of listeners.get(type) ?? []) {
        listener({ type, preventDefault() { this.defaultPrevented = true; } } as Event & {
          defaultPrevented: boolean;
        });
      }
    },
  };
}

test("context loss is cancelled, or the context is gone for good", () => {
  // The single most important line in the module. A canvas that does not call
  // preventDefault() on `contextlost` is never given a `contextrestored`: the
  // browser's default action is to let the context die permanently, and the app
  // needs a reload to get it back.
  const canvas = fakeCanvas();
  bindContextRecovery(canvas);
  const event = { type: "contextlost", prevented: false, preventDefault() { this.prevented = true; } };
  for (const listener of canvas.listeners.get("contextlost") ?? []) {
    listener(event as unknown as Event);
  }
  assert.equal(event.prevented, true, "the default action was not cancelled, so there will be no restore");
});

test("the loop is told when the context goes and when it comes back", () => {
  const canvas = fakeCanvas();
  const seen: string[] = [];
  const binding = bindContextRecovery(canvas, {
    onLost: () => seen.push("lost"),
    onRestored: () => seen.push("restored"),
  });

  assert.equal(binding.isLost(), false, "a fresh canvas is not lost");
  canvas.fire("contextlost");
  assert.equal(binding.isLost(), true, "the loop would keep drawing into a void");
  assert.deepEqual(seen, ["lost"]);

  canvas.fire("contextrestored");
  assert.equal(binding.isLost(), false, "the loop never resumes after a restore");
  assert.deepEqual(seen, ["lost", "restored"]);
});

test("detach removes both listeners, so a remount does not stack them", () => {
  // The effect re-runs on `animated`/`fps`/`maxDimension`. Without detach, each
  // run leaves a live listener holding the previous closure, and a restore
  // repaints every one of them into a canvas that has one.
  const canvas = fakeCanvas();
  const binding = bindContextRecovery(canvas, { onRestored: () => {} });
  binding.detach();
  assert.equal((canvas.listeners.get("contextlost") ?? []).length, 0);
  assert.equal((canvas.listeners.get("contextrestored") ?? []).length, 0);
});

test("a frame that throws is survived once, without giving up", () => {
  const guard = makeFrameGuard();
  let calls = 0;
  const outcome = guard.run(() => {
    calls += 1;
    throw new Error("one bad frame");
  });
  assert.equal(calls, 1, "the frame must actually have been attempted");
  assert.equal(outcome, "failed");
  assert.equal(guard.gaveUp(), false);
  assert.equal(guard.failures(), 1);
});

test("a painter that throws every frame stops the loop instead of flooding", () => {
  // The flood this prevents: 30 uncaught errors a second for as long as the
  // window is open, because the loop re-arms its own rAF before it paints.
  let gave: unknown;
  const guard = makeFrameGuard({ onGiveUp: (error) => (gave = error) });
  const boom = (): never => {
    throw new Error("every frame");
  };

  const outcomes = Array.from({ length: 10 }, () => guard.run(boom));
  assert.equal(guard.gaveUp(), true, "a permanently-throwing painter is still running");
  assert.equal(outcomes.filter((o) => o === "gave-up").length, 10 - FRAMES_BEFORE_GIVING_UP + 1);
  assert.equal((gave as Error).message, "every frame", "the error that ended it is reported");
});

test("a success in between resets the count, so a rare bad frame is not fatal", () => {
  // Consecutive, not cumulative: a painter that throws on the rare frame it has
  // bad input for should keep going, and a cumulative budget would kill it after
  // three such frames spread over an afternoon.
  let calls = 0;
  const guard = makeFrameGuard();
  const bad = (): void => {
    calls += 1;
    throw new Error("transient");
  };
  for (let i = 0; i < 20; i += 1) {
    guard.run(bad);
    guard.run(() => {
      calls += 1;
    });
  }
  assert.equal(calls, 40, "the loop kept calling a painter that fails intermittently");
  assert.equal(guard.gaveUp(), false);
});

test("after giving up, nothing is attempted again", () => {
  let calls = 0;
  const guard = makeFrameGuard({ budget: 1 });
  const boom = (): never => {
    calls += 1;
    throw new Error("stop");
  };
  guard.run(boom);
  assert.equal(guard.run(boom), "gave-up");
  assert.equal(calls, 1, "the painter was called again after the loop had given up");
});

/** The interval a 30 fps effect asks for. */
const TARGET_MS = 1000 / 30;

/** The verdicts that were actually reached, in order: `warming-up` is noise here. */
function reached(verdicts: string[]): string[] {
  return verdicts.filter((v) => v !== "warming-up");
}

/** Feed a budget `n` delivered frames `intervalMs` apart, recording the verdicts. */
function deliver(
  budget: ReturnType<typeof makeFrameBudget>,
  n: number,
  intervalMs: number,
  from = 0,
): { verdicts: string[]; last: number } {
  const verdicts: string[] = [];
  let now = from;
  for (let i = 0; i < n; i++) {
    verdicts.push(budget.observe(now));
    now += intervalMs;
  }
  return { verdicts, last: now };
}

test("a machine that cannot deliver the frames is told to stop animating", () => {
  // The failure this exists for: a compositor that rasterises in software still
  // *draws* the effect cheaply, so nothing inside the page looks expensive —
  // while every frame costs a full-surface composite and the window stops
  // answering input. 200 ms against a 33 ms request is that machine.
  const measured: number[] = [];
  const budget = makeFrameBudget({ targetMs: TARGET_MS, warmupMs: 0, onTooSlow: (ms) => measured.push(ms) });

  // One frame to establish the interval, then the window's worth.
  const { verdicts } = deliver(budget, FRAMES_BEFORE_JUDGING + 1, 200);

  assert.deepEqual(
    verdicts.slice(0, -1),
    verdicts.slice(0, -1).map(() => "warming-up"),
    "a verdict was reached before the window was full",
  );
  assert.deepEqual(reached(verdicts), ["too-slow"]);
  assert.equal(budget.gaveUp(), true);
  assert.equal(measured.length, 1, "onTooSlow must fire once, not once per frame after");
  assert.ok(
    Math.abs(measured[0] - 200) < 1,
    `the verdict should name the measured interval, saw ${measured[0]}`,
  );

  // And it stays given-up: the loop has cancelled its rAF, but a straggler that
  // asks again must not be told the machine got faster.
  assert.equal(budget.observe(100_000), "too-slow");
  assert.equal(measured.length, 1);
});

test("ordinary jitter does not stop the backdrop", () => {
  // A guard that fires on a slow frame or two would leave half the project's
  // users with a still backdrop for the rest of the session. 3× the target is
  // well past jitter — 10 fps at a 30 fps request — and is still not a verdict.
  const measured: number[] = [];
  const budget = makeFrameBudget({ targetMs: TARGET_MS, warmupMs: 0, onTooSlow: (ms) => measured.push(ms) });
  const { verdicts } = deliver(budget, FRAMES_BEFORE_JUDGING + 1, TARGET_MS * 3);
  assert.deepEqual(reached(verdicts), ["ok"]);
  assert.equal(budget.gaveUp(), false);
  assert.deepEqual(measured, []);

  // A healthy machine keeps getting judged: `ok` is a verdict per window, not a
  // one-off, so a loop that degrades later is still caught.
  const later = deliver(budget, FRAMES_BEFORE_JUDGING + 1, TARGET_MS * 3, 10_000);
  assert.deepEqual(reached(later.verdicts), ["ok"]);
  const broken = deliver(budget, FRAMES_BEFORE_JUDGING + 1, 300, later.last);
  // `[0]` and not the whole list: once the verdict is in, every later frame is
  // told the same thing, so the loop that has given up sees it repeat.
  assert.equal(reached(broken.verdicts)[0], "too-slow");
});

test("a suspended window is not a slow machine", () => {
  // A hidden or occluded window gets ~1 frame a second *by design*. Counting
  // those as slow frames would tell every user who ever switched away that their
  // machine cannot animate, permanently.
  const measured: number[] = [];
  const budget = makeFrameBudget({ targetMs: TARGET_MS, warmupMs: 0, onTooSlow: (ms) => measured.push(ms) });

  const slow = deliver(budget, FRAMES_BEFORE_JUDGING - 1, 200);
  assert.deepEqual(measured, [], "a partial window must not produce a verdict");

  // The suspension: 5 s of nothing, then the window comes back and is fast.
  assert.equal(budget.observe(slow.last + 5_000), "warming-up");
  const back = deliver(budget, FRAMES_BEFORE_JUDGING + 1, TARGET_MS, slow.last + 5_000);
  assert.deepEqual(
    reached(back.verdicts),
    ["ok"],
    "a hidden stretch plus a healthy one must not add up to a slow machine",
  );
  assert.deepEqual(measured, []);
});

test("the loop resets the window when the app is looked at again", () => {
  // The hook and the rain call `reset()` on `visibilitychange`. Whatever was
  // measured while the window was away cannot be allowed to carry into the run
  // that starts when the user comes back.
  const budget = makeFrameBudget({ targetMs: TARGET_MS, warmupMs: 0 });
  deliver(budget, FRAMES_BEFORE_JUDGING - 1, 200);
  budget.reset();
  const after = deliver(budget, FRAMES_BEFORE_JUDGING + 1, TARGET_MS, 1_000_000);
  assert.deepEqual(reached(after.verdicts), ["ok"]);
  assert.equal(budget.gaveUp(), false);
});

test("the budget's numbers are the contract, not tuning knobs", () => {
  // 30 fps asks for 33 ms; 4× is 133 ms, which is where an effect has stopped
  // being decoration and started starving the main loop that dispatches input.
  assert.equal(SLOW_FRAME_FACTOR, 4);
  assert.equal(FRAMES_BEFORE_JUDGING, 24);
  assert.equal(FRAME_BUDGET_WARMUP_MS, 2000);
  assert.ok(
    (TARGET_MS * SLOW_FRAME_FACTOR) / 1000 < 0.2,
    "the verdict must be reached inside the second a user calls 'frozen'",
  );
  const budget = makeFrameBudget({ targetMs: TARGET_MS, factor: 1, warmupMs: 0 });
  const { verdicts } = deliver(budget, FRAMES_BEFORE_JUDGING + 1, TARGET_MS * 1.01);
  assert.deepEqual(reached(verdicts), ["too-slow"], "a stricter factor must actually be used");
});

test("the app's own startup is not a verdict", () => {
  // The first second of a window is layout, a mount storm and the engine
  // handshake: frames that are slow because the app is starting. Judging those
  // would stop the backdrop on healthy machines forever.
  const measured: number[] = [];
  const budget = makeFrameBudget({ targetMs: TARGET_MS, onTooSlow: (ms) => measured.push(ms) });

  // A brutal first 1.5 s at 300 ms a frame, inside the warm-up.
  const boot = deliver(budget, 5, 300);
  assert.deepEqual(reached(boot.verdicts), [], "startup frames must not reach a verdict");

  // And once warm, the same delivery is a real verdict: the grace is a delay,
  // not a blanket exemption.
  const later = deliver(budget, FRAMES_BEFORE_JUDGING + 1, 300, boot.last + 500);
  assert.equal(reached(later.verdicts)[0], "too-slow");
  assert.equal(measured.length, 1);
});

// ── the two real loops ────────────────────────────────────────────────────

/** A frame later than the last by more than one frame's time at 30 fps, so none is skipped. */
const STEP_MS = 34;

interface Loop {
  name: string;
  /** Mount it. `fps` is passed so a test can re-run the effect by changing it. */
  mount(rig: CanvasRig, options?: { fps?: number; rec?: Recorder }): Promise<void>;
  /** Frames painted so far, the static frame included: both loops draw one `fillRect` per frame. */
  painted(rig: CanvasRig): number;
}

const LOOPS: Loop[] = [
  {
    name: "the shared clock",
    mount: (rig, o) => rig.dom.render(probe({ rec: o?.rec ?? recorder(), fps: o?.fps })),
    painted: (rig) => rig.count("fillRect"),
  },
  {
    name: "the rain",
    mount: (rig, o) => rig.dom.render(React.createElement(MatrixRain, { fps: o?.fps })),
    painted: (rig) => rig.count("fillRect"),
  },
];

const event = (rig: CanvasRig, type: string): Event =>
  new rig.dom.window.Event(type, { cancelable: true }) as unknown as Event;

for (const loop of LOOPS) {
  test(`${loop.name}: a lost context is cancelled, nothing is drawn into it, and the return repaints`, async () => {
    await withCanvasRig({}, async (rig) => {
      await loop.mount(rig);
      let t = rig.run(5, 1000, STEP_MS);
      const before = loop.painted(rig);

      const lost = event(rig, "contextlost");
      rig.canvas().dispatchEvent(lost);
      assert.ok(lost.defaultPrevented, "the default action was not cancelled, so there will be no restore");

      // Every draw after a lost context is a silent no-op, so the frame budget would be spent
      // painting into a void. The clock stays armed, so it is where it was when the context returns.
      const armed = rig.armed;
      t = rig.run(10, t, STEP_MS);
      assert.equal(loop.painted(rig), before, "the loop kept drawing into a lost context");
      assert.ok(rig.armed > armed && rig.pending(), "the loop stopped asking for frames while the context was gone");

      // It comes back blank and possibly a different size: re-measure (the size changed without a
      // notification, as it can while a context is gone) and repaint at once.
      rig.resize(200, 100, false);
      rig.canvas().dispatchEvent(event(rig, "contextrestored"));
      assert.equal(loop.painted(rig), before + 1, "the return did not repaint");
      assert.equal(rig.canvas().width, 200, "the return did not re-measure the canvas");
      assert.equal(rig.canvas().height, 100);

      const afterRestore = loop.painted(rig);
      rig.run(5, t, STEP_MS);
      assert.equal(loop.painted(rig), afterRestore + 5, "the loop never resumed after the restore");
    });
  });

  test(`${loop.name}: a resize re-measures and repaints, except while the context is lost`, async () => {
    await withCanvasRig({}, async (rig) => {
      await loop.mount(rig);
      const first = loop.painted(rig);
      rig.resize(300, 150);
      assert.equal(rig.canvas().width, 300, "a resize did not re-measure the canvas");
      assert.equal(loop.painted(rig), first + 1, "a resize did not repaint");

      // Drawing into a lost context is a no-op, and the restore is what repaints.
      rig.canvas().dispatchEvent(event(rig, "contextlost"));
      const lost = loop.painted(rig);
      rig.resize(250, 120);
      assert.equal(rig.canvas().width, 250, "the canvas was not re-measured while the context was lost");
      assert.equal(loop.painted(rig), lost, "a resize drew into a lost context");
    });
  });

  test(`${loop.name}: listeners are attached once, survive a re-run of the effect without stacking, and go on unmount`, async () => {
    await withCanvasRig({}, async (rig) => {
      await loop.mount(rig, { fps: 30 });
      const attached = (): number[] => [
        rig.listeners("canvas", "contextlost"),
        rig.listeners("canvas", "contextrestored"),
        rig.listeners("document", "visibilitychange"),
      ];
      assert.deepEqual(attached(), [1, 1, 1], "the loop is not listening for context loss, restore and visibility");

      // The effect re-runs on a change of fps: without a detach each run leaves a live listener
      // holding the previous closure, and a restore repaints every one of them into one canvas.
      await loop.mount(rig, { fps: 31 });
      assert.deepEqual(attached(), [1, 1, 1], "a re-run of the effect stacked its listeners");

      await rig.dom.render(React.createElement("div"));
      assert.deepEqual(attached(), [0, 0, 0], "an unmounted loop left listeners behind");
      assert.equal(rig.pending(), false, "an unmounted loop left a frame armed");
    });
  });

  test(`${loop.name}: a frame that throws every time ends the loop, and says so once`, async () => {
    await withCanvasRig({}, async (rig) => {
      await loop.mount(rig);
      rig.clear();
      rig.breakMethod("fillRect");
      rig.run(20, 1000, STEP_MS);

      // The flood this prevents: 30 uncaught errors a second for as long as the window is open,
      // because the loop re-arms its own rAF before it paints.
      assert.equal(loop.painted(rig), FRAMES_BEFORE_GIVING_UP, "the loop kept painting a frame that never works");
      assert.equal(rig.errors.length, 1, `the loop reported ${rig.errors.length} times, not once`);
      assert.match(rig.errors[0], new RegExp(`failed on ${FRAMES_BEFORE_GIVING_UP} consecutive frames`));
      assert.equal(rig.pending(), false, "the loop is still asking for frames after giving up");
    });
  });

  test(`${loop.name}: a bad frame now and then is survived`, async () => {
    // Consecutive, not cumulative: a painter that throws on the rare frame it has bad input for
    // should keep going.
    await withCanvasRig({}, async (rig) => {
      await loop.mount(rig);
      rig.clear();
      let t = 1000;
      for (let round = 0; round < 10; round++) {
        rig.breakMethod("fillRect");
        t = rig.run(FRAMES_BEFORE_GIVING_UP - 1, t, STEP_MS);
        rig.breakMethod(null);
        t = rig.run(1, t, STEP_MS);
      }
      assert.deepEqual(rig.errors, [], "a loop that fails intermittently was ended");
      assert.ok(rig.pending(), "the loop stopped");
    });
  });

  test(`${loop.name}: a machine that cannot deliver the frames is left still, once`, async () => {
    // A compositor that rasterises in software still *draws* the effect cheaply, so nothing inside
    // the page looks expensive, while every frame costs a full-surface composite and the window
    // stops answering input. 300 ms against a 33 ms request is that machine.
    await withCanvasRig({}, async (rig) => {
      await loop.mount(rig);
      rig.run(80, 1000, 300);
      assert.equal(rig.warnings.length, 1, `the verdict was reached ${rig.warnings.length} times, not once`);
      assert.match(rig.warnings[0], /the compositor is delivering a frame every/);
      assert.equal(rig.pending(), false, "the loop kept running on a machine that cannot keep up");
      const painted = loop.painted(rig);
      assert.ok(painted > 1, "it never drew before the verdict, so the test measured nothing");
    });
  });

  test(`${loop.name}: ordinary delivery is never a verdict`, async () => {
    await withCanvasRig({}, async (rig) => {
      await loop.mount(rig);
      rig.run(400, 1000, STEP_MS);
      assert.deepEqual(rig.warnings, [], "a healthy machine was told it was too slow");
      assert.ok(rig.pending());
    });
  });

  test(`${loop.name}: a window that was looked away from is not judged by the frames it missed`, async () => {
    // A hidden window gets about one frame a second by design. The loop resets its window on
    // `visibilitychange`, so what was measured while away cannot carry into the run that starts
    // when the user comes back. 300 ms frames are a verdict after 24 samples; the reset falls
    // in the middle of them, so the same frames with no reset would have reached one.
    await withCanvasRig({}, async (rig) => {
      await loop.mount(rig);
      let t = rig.run(20, 1000, 300);
      assert.deepEqual(rig.warnings, [], "the verdict came before the window was full");
      rig.dom.window.document.dispatchEvent(event(rig, "visibilitychange"));
      t = rig.run(12, t, 300);
      assert.deepEqual(rig.warnings, [], "frames measured before the reset counted against the run after it");
      assert.ok(rig.pending());
    });
  });
}

test("the shared clock's first frame after a restore is a whole one, not a leap", async () => {
  // Restoring without resetting the clock hands the next frame a `dt` of however long the context
  // was gone, and every painter integrates that. The first frame back advances by nothing.
  await withCanvasRig({}, async (rig) => {
    const rec = recorder();
    await rig.dom.render(probe({ rec }));
    let t = rig.run(10, 1000, STEP_MS);
    rig.canvas().dispatchEvent(event(rig, "contextlost"));
    t = rig.run(5, t, STEP_MS);
    rig.canvas().dispatchEvent(event(rig, "contextrestored"));
    const steps = rec.steps.length;
    rig.run(1, t + 60_000, STEP_MS);
    assert.equal(rec.steps.length, steps + 1, "no frame ran after the restore");
    assert.equal(rec.steps[steps], 0, "the first frame after a restore advanced the simulation");
  });
});
