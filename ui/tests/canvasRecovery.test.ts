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
 * The second reads the two loops as text, because a recovery module that nothing
 * calls is a module that has fixed nothing, and the two failures look identical
 * from the outside: a blank canvas.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

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

test("both render loops bind the recovery, or the module fixes nothing", () => {
  // A recovery unit that nothing calls is a module that has changed nothing, and
  // an unwired loop is indistinguishable from a wired one by looking at the app:
  // both are a blank canvas.
  for (const file of ["hooks/useAtmosphereCanvas.ts", "components/ui/MatrixRain.tsx"]) {
    const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
    assert.match(
      source,
      /bindContextRecovery\(/,
      `${file} does not listen for context loss — a driver reset leaves its backdrop gone until a reload`,
    );
    assert.match(
      source,
      /makeFrameGuard\(/,
      `${file} has no frame guard, so a throwing painter floods the console at 30 FPS`,
    );
    assert.match(
      source,
      /recovery\.detach\(\)/,
      `${file} never detaches its listeners; every remount stacks another one`,
    );
    assert.match(
      source,
      /recovery\.isLost\(\)/,
      `${file} does not skip drawing while the context is lost`,
    );
  }
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

test("both render loops judge their own frame delivery, or one of them starves the window", () => {
  // The two loops are the same failure twice: the hook is every theme but the
  // rain, and the rain is its own loop. A rule wired into one of them is a rule
  // the other one silently does not have — which is this module's whole reason
  // for existing.
  for (const file of ["hooks/useAtmosphereCanvas.ts", "components/ui/MatrixRain.tsx"]) {
    const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
    assert.match(
      source,
      /makeFrameBudget\(/,
      `${file} never judges its frame delivery, so a software renderer leaves the window unable to answer input`,
    );
    assert.match(
      source,
      /budget\.observe\(t\) === "too-slow"/,
      `${file} builds a budget and ignores its verdict`,
    );
    assert.match(
      source,
      /budget\.reset\(\)/,
      `${file} never resets the window, so a hidden app is judged as a slow machine`,
    );
    assert.match(
      source,
      /removeEventListener\("visibilitychange"/,
      `${file} leaks its visibility listener on every remount`,
    );
  }
});

test("a restore re-measures and resets the clock in both loops", () => {
  // Restoring without resetting `last` hands the next frame a `dt` of however
  // long the context was gone — minutes — and every painter integrates that,
  // which skips its field forward by a visible jump on the frame it returns.
  for (const file of ["hooks/useAtmosphereCanvas.ts", "components/ui/MatrixRain.tsx"]) {
    const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
    // From the hook to the detach: that window is the `onRestored` body and
    // nothing after it, and it ends the same way in both files.
    const body = source.slice(source.indexOf("onRestored"), source.indexOf("recovery.detach()"));
    assert.match(body, /layout\(\)/, `${file} restores without re-measuring the canvas`);
    assert.match(body, /last = 0/, `${file} restores without resetting the frame clock`);
  }
});
