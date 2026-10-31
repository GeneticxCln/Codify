/**
 * The boot frame probe: what a composited frame costs, before the theme moves.
 *
 * The failure this holds is a measurement taken too late. The shell's watchdog
 * needs ten one-second samples to be sure, and by then a software-composited
 * window has already spent those seconds at full rate — the cost the probe is
 * meant to refuse. These tests pin the arithmetic (CPU ticks in, milliseconds
 * per frame out), the two-pass rule that keeps a slow startup from condemning a
 * healthy machine, and the gate in both backdrop loops: neither may arm its
 * `requestAnimationFrame` before the probe has answered.
 */
import test from "node:test";
import assert from "node:assert/strict";

const {
  RENDER_CPU_COMMAND,
  PROBE_FRAMES,
  PROBE_SETTLE_MS,
  PROBE_WARMUP_MS,
  frameCostFits,
  measureFrameCost,
  probeAvailable,
  probeFrameBudget,
  probeVerdict,
  projectedCorePercent,
} = await import("../src/frameProbe.ts");

interface FakeProbe extends Record<string, unknown> {
  draws: number;
  reads: number;
}

/** A probe environment whose clock answers from `costs` and whose frames are free. */
function fakeEnv(costs: Array<number | null>): FakeProbe {
  let read = 0;
  const env: FakeProbe = {
    draws: 0,
    reads: 0,
    readCpuMs: async () => {
      env.reads += 1;
      const value = costs[Math.min(read, costs.length - 1)];
      read += 1;
      return value;
    },
    draw: () => {
      env.draws += 1;
    },
    nextFrame: async () => {},
    sleep: async () => {},
  };
  return env;
}

test("the mean cost is the shell's CPU delta over the frames drawn", async () => {
  const env = fakeEnv([1000, 1100]);
  const cost = await measureFrameCost(env as never, 5, 0);
  assert.equal(cost, 20, "100 ms over five frames is not 20 ms per frame");
  assert.equal(env.draws, 5, "the probe did not draw a frame per measurement unit");
  assert.equal(env.reads, 2, "the probe did not read the clock before and after");
});

test("an unreadable clock measures nothing and draws nothing", async () => {
  const env = fakeEnv([null]);
  assert.equal(await measureFrameCost(env as never, 5, 0), null);
  assert.equal(env.draws, 0, "a probe with no clock still painted frames");
});

test("a clock that goes backwards is no measurement, not a negative cost", async () => {
  const env = fakeEnv([1000, 900]);
  assert.equal(await measureFrameCost(env as never, 5, 0), null);
});

test("the projected core share is the frame against its interval, clamped", () => {
  assert.equal(projectedCorePercent(16.5, 33), 50);
  assert.equal(projectedCorePercent(33, 33), 100);
  assert.equal(projectedCorePercent(66, 33), 100, "a cost over one interval must not print as 200%");
  assert.equal(projectedCorePercent(0, 33), 0);
  assert.equal(projectedCorePercent(33, 0), 0, "a zero interval has no share to report");
  assert.equal(projectedCorePercent(Number.NaN, 33), 0);
});

test("the boot budget refuses only a frame that costs the whole interval", () => {
  // The watchdog's 35% is a sustained line; this probe is a five-frame sample
  // during the mount, and a false alarm costs the session's backdrop. It refuses
  // only a frame with no headroom left at the loop's interval.
  assert.equal(frameCostFits(32, 33), true);
  assert.equal(frameCostFits(33, 33), false, "a full-core frame was treated as affordable");
  assert.equal(frameCostFits(1000, 0), true, "no interval means no budget to exceed");
});

test("the cheaper of two passes is the verdict, so a slow boot is not a slow machine", () => {
  const fits = probeVerdict(80, 4, 33);
  assert.equal(fits.starved, false, "a startup storm in one pass condemned a fast steady state");
  assert.equal(fits.measured, true);
  assert.equal(fits.msPerFrame, 4);

  const slow = probeVerdict(40, 50, 33);
  assert.equal(slow.starved, true);
  assert.equal(slow.percent, 100, "40/33 is over a core and is clamped to 100");

  const unmeasured = probeVerdict(null, null, 33);
  assert.equal(unmeasured.measured, false);
  assert.equal(unmeasured.starved, false, "no measurement was read as 'starved'");
  assert.equal(unmeasured.percent, 0);

  const half = probeVerdict(null, 16.5, 33);
  assert.equal(half.measured, true, "one readable pass is still a measurement");
  assert.equal(half.percent, 50);
});

test("outside the shell the probe is a pass-through and paints nothing", async () => {
  assert.equal(probeAvailable(), false, "a bare node process claimed to be the shell");
  let draws = 0;
  const fits = await probeFrameBudget(() => {
    draws += 1;
  }, 33);
  assert.equal(fits, true, "a page with no shell clock refused to animate");
  assert.equal(draws, 0, "the probe drew frames it could not measure");
  assert.equal(RENDER_CPU_COMMAND, "codify_render_cpu_ms");
  assert.ok(PROBE_FRAMES > 0 && PROBE_SETTLE_MS > 0 && PROBE_WARMUP_MS > 0);
});

// The gate itself is mounted, not read: see `renderProbeGate.test.ts`, which
// runs the real hook against a fake shell bridge for both backdrop loops. A
// source-text version of that claim is the kind of green-but-empty test the
// source-text budget exists to refuse.
