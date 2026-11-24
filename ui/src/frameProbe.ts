import { applyShellStarvation } from "./motionPreference.ts";

/**
 * What a composited theme frame costs, measured before the theme starts moving.
 *
 * **The failure this exists for.** On a machine whose WebKitGTK composites in
 * software, every animated full-window layer is blended by the shell's main
 * thread, which is why a backdrop costs a core while the page's own painter is
 * cheap and `rAF` keeps delivering frames. The shell's watchdog can see that
 * cost, but its window is ten one-second samples: by the time it is sure, the
 * first seconds of the theme have already been paid at full rate — the exact
 * seconds this file removes. A probe can make the same measurement in a few
 * frames because it is not waiting for a burst pattern to repeat: it asks the
 * shell how much CPU it spent while the theme drew a handful of frames.
 *
 * **Why the shell's clock and not the page's.** A page-side timer says the
 * frame was committed quickly while a process somewhere else is at a core;
 * that is the documented blind spot `makeFrameBudget` has. Which process pays
 * depends on how WebKit was told to render: the GTK main loop in the UI process,
 * or the WebKit web process when the dma-buf path is disabled (the documented
 * Nvidia workaround sets `WEBKIT_DISABLE_DMABUF_RENDERER=1`). The page cannot
 * read either process's CPU from inside itself; the shell can read both, so it
 * exposes the sum as `codify_render_cpu_ms`.
 *
 * **Before the loop, not after.** Both backdrop loops draw their static frame,
 * then call [`probeFrameBudget`], and arm `requestAnimationFrame` only if it
 * says the frame fits. A machine that cannot pay gets the same starvation
 * verdict the watchdog would have reached — the banner, "Animate anyway", the
 * lower release rate — but before the theme starts rather than ten seconds in.
 *
 * **Two passes, and the cheaper one counts.** The probe runs while the app is
 * still booting, when layout, fonts and the engine handshake can make a few
 * frames expensive for reasons that have nothing to do with the theme. The
 * first pass may include that storm; the second is the settled cost. Taking the
 * minimum is deliberately conservative against a slow start, and a machine
 * that is still too slow after the storm is caught by the shell's watchdog
 * later. A probe that found every machine starving would trade one bad boot for
 * a backdrop nobody ever sees.
 */

/** Frames drawn per pass. Enough for the 10 ms kernel tick to resolve a cost. */
export const PROBE_FRAMES = 5;

/**
 * The share of one core a boot frame may spend before the probe refuses it.
 *
 * The watchdog's 35% (`render_watchdog::STARVE_NUMERATOR`) is a *sustained*
 * line over ten one-second samples. This probe draws five frames while the app
 * is still mounting, and a startup storm in either pass is not evidence about
 * the theme; a one-shot false alarm costs the session its backdrop and shows a
 * banner, while a machine that really is slow is caught by the watchdog a few
 * seconds later. So the probe refuses only a frame that costs a whole interval.
 */
export const FRAME_BUDGET_PERCENT = 100;

/**
 * The rate a backdrop has to afford at all. The starvation verdict is judged here, at 100% of
 * the interval, exactly as it was before backdrops could go faster: raising the ceiling to 60
 * must not make a machine that could run the weather at 30 lose it to a banner.
 */
export const BASE_FPS = 30;

/** The fastest a backdrop is released: one frame per refresh on a 60 Hz panel. */
export const MAX_FPS = 60;

/**
 * The share of one core a running backdrop may hold. Below the watchdog's sustained 35%
 * (`render_watchdog::STARVE_NUMERATOR`), so a governed backdrop is never the thing the watchdog
 * has to stop, and the window keeps the rest of the core for input and the transcript.
 */
export const DECORATION_BUDGET_PERCENT = 25;

/** The rates the governor chooses between, fastest first. Each divides 60, so frames stay even. */
export const RATE_LADDER: readonly number[] = [60, 30, 20, 15, 10];

/**
 * The fastest rate, at most `maxFps`, whose measured frame cost stays inside the decoration
 * budget. A frame too dear for even the slowest rung still gets that rung: the starvation verdict,
 * not this function, is what decides a machine cannot afford the backdrop at all.
 *
 * `null` (no measurement: a preview, a shell that cannot read its clock) is `maxFps`, as the loop
 * always started.
 */
export function governedFps(msPerFrame: number | null, maxFps: number): number {
  const ceiling = Math.max(1, maxFps);
  if (msPerFrame === null || !Number.isFinite(msPerFrame)) return ceiling;
  const rungs = RATE_LADDER.filter((fps) => fps <= ceiling);
  for (const fps of rungs) {
    if (projectedCorePercent(msPerFrame, 1000 / fps) <= DECORATION_BUDGET_PERCENT) return fps;
  }
  return rungs.length > 0 ? rungs[rungs.length - 1] : ceiling;
}

/** Time before the first pass, so the mount storm is not part of the measurement. */
export const PROBE_WARMUP_MS = 750;

/** Time after a pass's last frame for the renderers to composite it and be read. */
export const PROBE_SETTLE_MS = 120;

/** The command the shell exposes for this file; named here so a rename fails a test. */
export const RENDER_CPU_COMMAND = "codify_render_cpu_ms";

/** Whether the page is hosted by the shell, and so can ask for its clock. */
export function probeAvailable(): boolean {
  if (typeof window === "undefined") return false;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: unknown })
    .__TAURI_INTERNALS__;
  return Boolean(internals);
}

/**
 * Read the window's render clock, or `null` when there is no shell to ask.
 *
 * Deliberately not `tauriInvoke`: importing the API client seeds a
 * browser-preview engine address at module load, and this module is imported by
 * unit tests with no DOM at all. The probe needs one Tauri command and none of
 * the transport's state, so it takes the one command directly.
 */
async function readShellCpuMs(): Promise<number | null> {
  if (!probeAvailable()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const value = await invoke<number | null>(RENDER_CPU_COMMAND);
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  } catch {
    // A shell built before the command existed, or a refused IPC: no
    // measurement, and the loop starts as it always did.
    return null;
  }
}

/** The two readings and the drawing a probe needs, injectable for tests. */
export interface FrameProbeEnv {
  /** The window's cumulative rendering CPU time in ms, or `null` if unreadable. */
  readCpuMs: () => Promise<number | null>;
  /** Draw one frame of the theme. */
  draw: () => void;
  /** Resolve after the browser has had the chance to commit a frame. */
  nextFrame: () => Promise<void>;
  /** Resolve after this many milliseconds. */
  sleep: (ms: number) => Promise<void>;
}

/**
 * The mean milliseconds of rendering CPU per drawn frame, or `null` when the
 * clock cannot be read at all.
 *
 * CPU time, not wall time: the settle waits cost nothing by construction, and
 * the frame that is still being composited when the last draw returns is
 * counted because the reading happens after the settle.
 */
export async function measureFrameCost(
  env: FrameProbeEnv,
  frames: number = PROBE_FRAMES,
  settleMs: number = PROBE_SETTLE_MS,
): Promise<number | null> {
  if (!(frames > 0)) return null;
  const before = await env.readCpuMs();
  if (before === null) return null;
  for (let i = 0; i < frames; i += 1) {
    env.draw();
    await env.nextFrame();
  }
  await env.sleep(settleMs);
  const after = await env.readCpuMs();
  if (after === null || after < before) return null;
  return (after - before) / frames;
}

/**
 * The share of one core the shell would spend sustaining these frames at the
 * theme's requested interval, as the banner's percentage.
 *
 * `msPerFrame / frameMs` is exactly that share: one frame of shell work every
 * `frameMs` milliseconds is a fraction `msPerFrame / frameMs` of the core. At
 * 30 FPS, a frame that costs the whole 33 ms interval is 100%.
 */
export function projectedCorePercent(msPerFrame: number, frameMs: number): number {
  if (!(frameMs > 0) || !Number.isFinite(msPerFrame)) return 0;
  return Math.max(0, Math.min(100, Math.round((msPerFrame / frameMs) * 100)));
}

/** Whether one frame fits inside the core budget at the loop's interval. */
export function frameCostFits(msPerFrame: number, frameMs: number): boolean {
  if (!(frameMs > 0)) return true;
  return projectedCorePercent(msPerFrame, frameMs) < FRAME_BUDGET_PERCENT;
}

/** What the two passes say about the frame budget. */
export interface ProbeVerdict {
  /** Whether either pass produced a reading. */
  measured: boolean;
  /** The cheaper pass's cost, or `null` when neither pass could be read. */
  msPerFrame: number | null;
  /** Whether that cost exceeds the frame interval. */
  starved: boolean;
  /** The projected core share, for the banner. */
  percent: number;
}

/**
 * Judge two passes. The cheaper one is the measurement; a pass that could not
 * be read is simply absent, and two absent passes mean "not measured" rather
 * than "starved".
 */
export function probeVerdict(
  first: number | null,
  second: number | null,
  frameMs: number,
): ProbeVerdict {
  const costs = [first, second].filter((cost): cost is number => cost !== null);
  if (costs.length === 0) {
    return { measured: false, msPerFrame: null, starved: false, percent: 0 };
  }
  const msPerFrame = Math.min(...costs);
  return {
    measured: true,
    msPerFrame,
    starved: !frameCostFits(msPerFrame, frameMs),
    percent: projectedCorePercent(msPerFrame, frameMs),
  };
}

/** The real readings, frames and waits. */
function shellEnv(draw: () => void): FrameProbeEnv {
  return {
    readCpuMs: readShellCpuMs,
    draw,
    nextFrame: () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => resolve());
      }),
    sleep: (ms) => new Promise<void>((resolve) => setTimeout(resolve, ms)),
  };
}

/**
 * Measure the theme before its loop starts, and say how fast it may run.
 *
 * Returns the rate to release frames at: the fastest rung of [`RATE_LADDER`], at most `maxFps`,
 * that keeps the measured frame inside [`DECORATION_BUDGET_PERCENT`] of a core
 * ([`governedFps`]). So a window that composites cheaply gets 60 frames a second and one that
 * does not gets fewer, instead of every window getting the same rate and a watchdog stopping the
 * ones that cannot pay for it.
 *
 * Returns `0` when the frame does not fit at [`BASE_FPS`]: this records the starvation verdict
 * the shell's watchdog would have recorded (the store is the same one; the banner and "Animate
 * anyway" are the existing controls). The caller must not arm its loop: the store's announcement
 * re-runs the effect, which will either stand still (the person's choice) or start at the starved
 * rate.
 *
 * Outside the shell, or when the clock cannot be read, this is `maxFps` without drawing: the page
 * has no measurement to make, and a preview must still animate.
 */
export async function probeFrameBudget(draw: () => void, maxFps: number): Promise<number> {
  const ceiling = Math.max(1, maxFps);
  if (!probeAvailable()) return ceiling;
  try {
    const first = launchProbe === null;
    if (first) launchProbe = measureLaunch(draw);
    const verdict = await (launchProbe as Promise<ProbeVerdict | null>);
    if (verdict === null || !verdict.measured || verdict.msPerFrame === null) return ceiling;
    if (verdict.starved) {
      // Recorded once, by the mount that measured. A later mount reads the store, and takes the
      // starved path without asking here at all.
      if (first) applyShellStarvation({ at: Date.now(), measuredMs: verdict.percent });
      return 0;
    }
    return governedFps(verdict.msPerFrame, ceiling);
  } catch {
    // A painter that throws here would throw again in the loop, where the frame
    // guard names it and stops. The probe's job is not to hide that error — but
    // it must not turn it into an unhandled rejection that leaves the theme
    // still with no explanation either. Start the loop and let the guard report.
    return ceiling;
  }
}

/**
 * The one measurement this page load makes, shared by every backdrop that mounts in it.
 *
 * Measured live on the machine this was written for: every theme swap re-mounted the backdrop and
 * re-ran the probe *during the swap's own repaint*, so it read 48–76 ms a frame where the settled
 * window costs ~26, recorded a starvation verdict, and froze the animation the person had just
 * chosen. With nobody touching the window the probe ran ten times in forty seconds, each time a
 * motion or starvation flag re-ran the effect. A frame's cost is a fact about the window and its
 * compositor, not about which theme is drawing, so it is measured once per launch, and two canvases
 * that mount together wait on the same measurement. Whether the cost holds up over time is the
 * shell's watchdog's job (sustained 35%), as it always was.
 */
let launchProbe: Promise<ProbeVerdict | null> | null = null;

async function measureLaunch(draw: () => void): Promise<ProbeVerdict | null> {
  const env = shellEnv(draw);
  await env.sleep(PROBE_WARMUP_MS);
  const first = await measureFrameCost(env);
  if (first === null) return null;
  const second = await measureFrameCost(env);
  return probeVerdict(first, second, 1000 / BASE_FPS);
}

/** Forget this launch's measurement: what a reload does. For tests, which mount many "launches" in one process. */
export function forgetLaunchProbe(): void {
  launchProbe = null;
}
