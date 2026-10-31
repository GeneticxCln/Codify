import { useEffect, useRef } from "react";

import {
  bindContextRecovery,
  makeFrameBudget,
  makeFrameGuard,
  FRAMES_BEFORE_GIVING_UP,
} from "../canvasRecovery.ts";
import { useMotionAllowed, useStarvedThisBoot } from "../motionPreference.ts";
import { probeAvailable, probeFrameBudget } from "../frameProbe.ts";
import { applyEdgeFade } from "../canvasEdgeFade.ts";

/**
 * The release rate a measured-starved window gets when its person kept the
 * backdrop ("Animate anyway"), on the reference window it was profiled on.
 *
 * The window is the layer the software compositor has to recompose, so the
 * render cost is proportional to the number of *new frames*, not to the
 * painter's own work: halving the rate roughly halves it. Ten is still an
 * animation — the person asked to keep one — and the atmosphere painters read
 * `tick.seconds`, so they keep their speed; it is only revealed in bigger
 * steps. The rain is the documented outlier and advances once per released
 * frame, so it does slow with the rate.
 *
 * **Ten is the reference, not a constant for every display.** Profiled on the
 * software-composited window that set this: at 15 FPS the shell held ~36% of a
 * core (web process ~6%), at 10 FPS ~26% (web ~2%), and at 5 FPS ~15% (web
 * ~2.5%). The per-frame shell cost is ~24–30 ms across those rates: the
 * compositor's full-window blend, with the web process's own painting a
 * rounding error. That is the **UI-process** shape; with the dma-buf path
 * disabled the web process is the one that pays, which is why the measurement
 * sums both. The blend cost is per *physical* pixel, and the referece window was
 * ~2 MP. On the machine that actually froze — 1280×800 logical at 3×, so ~8.3 MP
 * — one composited frame measured ~125 ms, and 5 FPS still held ~55% of a
 * core. Ten FPS there would be a core for decoration. So the starved rate
 * scales with the canvas's physical area: a 4K-class window gets ~2.4 FPS for
 * the same share a 2 MP window gets at ten. Two is the floor — below it the
 * effect is a slideshow with a noticeable pause — and a person who asks to
 * keep it still sees motion rather than a frozen frame.
 */
export const STARVED_FPS = 10;
/** The physical pixel budget the ten-FPS figure was measured at (~2 MP). */
export const STARVED_REFERENCE_PIXELS = 2_000_000;
/** The slowest rate that is still an animation rather than a slideshow. */
export const STARVED_FPS_FLOOR = 2;

/** The starved rate for a canvas of `pixels` physical pixels. */
export function starvedFps(pixels: number): number {
  if (!(pixels > 0) || !Number.isFinite(pixels)) return STARVED_FPS;
  const scaled = STARVED_FPS * (STARVED_REFERENCE_PIXELS / pixels);
  return Math.max(STARVED_FPS_FLOOR, Math.min(STARVED_FPS, scaled));
}

/** The rate to release at, given the painter's own cap and the shell's verdict. */
export function backdropFps(
  fps: number,
  starved: boolean,
  pixels: number = STARVED_REFERENCE_PIXELS,
): number {
  return starved ? Math.min(fps, starvedFps(pixels)) : fps;
}

/**
 * The clock every atmosphere shares.
 *
 * Six animated themes is the point at which the rain's effect body stops being
 * a reasonable thing to copy: a capped device-pixel size, an accumulator that
 * releases at most one frame per `1000/fps` ms, a single static frame when
 * motion is not wanted, a `ResizeObserver`, and colours read from the theme's
 * own variables. That list *is* the bounded-budget contract in DESIGN.md §7, so
 * it lives here once and a painter supplies only the drawing.
 *
 * What the hook owns, and why each part is not optional:
 *
 * - **The cap.** `maxDimension` bounds the longer edge in device pixels. Paint
 *   cost is quadratic in the canvas, and a maximised 4K window is 4× the pixels
 *   of a 1440p one. A painter that forgot the cap would look identical and cost
 *   four times as much, which is the kind of thing that only shows up as a
 *   fan spinning.
 * - **The accumulator, not the frame rate.** `requestAnimationFrame` is used to
 *   stay aligned with the compositor, but it is not the clock: a 144 Hz display
 *   calling this loop 144 times a second still gets 30 frames. Making rAF the
 *   authority would make the theme's cost a property of the user's monitor.
 * - **One frame when motion is off.** Not a slower loop and not a paused one —
 *   the loop is never armed, and the picture is a complete frame of the effect
 *   at phase zero. `prefers-reduced-motion` is a statement that this should not
 *   be moving, and a 1 FPS loop is still moving.
 * - **Live variable reads.** `read` resolves against a *live*
 *   `getComputedStyle`, so a theme applied while a canvas is running restyles it
 *   on the next frame with no remount and no second source of colour.
 * - **The boot probe.** Before the loop is armed, the hook measures what one
 *   composited frame costs the window's renderers — the shell's main thread
 *   and its WebKit web process — and starts only if it fits the budget. See
 *   `frameProbe.ts`: without it the first seconds of the boot are paid at full
 *   rate before the watchdog can be sure they should not be.
 *
 * Dependencies are deliberately just the three numbers. A painter is a closure
 * and a caller cannot memoise one honestly, so including `create` in the deps
 * would re-run this effect on every render — restarting the simulation, which
 * for an effect with drift is a visible jump. A prop that changes at runtime
 * therefore belongs in a ref the painter reads, not in a dependency; see
 * `NeuralWeb.tsx`, whose "the agent is thinking" flag is exactly that case.
 */

/** The capped canvas size, in device pixels. */
export interface AtmosphereSize {
  width: number;
  height: number;
}

/** How far the effect has got. Seconds, not frames, so pacing survives a cap. */
export interface AtmosphereTick {
  /** Frames drawn since mount. 0 on the static frame. */
  frame: number;
  /** Seconds since mount, advanced by the clock and by nothing else. */
  seconds: number;
  /**
   * The capped canvas size in device pixels. Handed to `step` as well as
   * `draw` because a painter that owns drifting things needs somewhere to
   * bounce them, and canvas pixels are not CSS pixels once a cap is in play —
   * `window.innerWidth` is the wrong unit in exactly the place it looks right.
   */
  size: AtmosphereSize;
}

/** What a painter is: something that can advance and something that can draw. */
export interface AtmospherePainter {
  draw(ctx: CanvasRenderingContext2D, size: AtmosphereSize, tick: AtmosphereTick): void;
  /**
   * Advance whatever the painter owns. `dt` is seconds since the previous
   * frame, so drift is a function of time rather than of frame count — a cap
   * that halves the frame rate must not halve the speed.
   */
  step?(dt: number, tick: AtmosphereTick): void;
}

/**
 * The props every atmosphere canvas takes, in one place so the shell can hold a
 * table of five different components and render any of them.
 *
 * `active` means "the agent is working". It used to be read by exactly one
 * effect — `NeuralWeb`, whose web brightens — and ignored by the rest, on the
 * theory that changing how fast something moves is the line between reporting a
 * state and being decoration. That line turned out to be the wrong one to draw
 * *there*: the distinction DESIGN.md §7 draws is between motion that **carries
 * information** and motion that does not, not between motion that changes speed
 * and motion that does not. A caret blinks at a constant rate and still reports
 * something. So every effect now reads `active`, through the clock, as a change
 * in rate — see `ACTIVE_RATE` and `nextRate`.
 *
 * It is on the shared props rather than on one component because the
 * alternative is a union type in the shell and a cast at the call site, and a
 * cast is where a "this one is different" quietly becomes a bug.
 */
export interface AtmosphereCanvasProps {
  className?: string;
  maxDimension?: number;
  fps?: number;
  animated?: boolean;
  /** Whether the agent is working. Read only by effects that report state. */
  active?: boolean;
}

export interface AtmosphereOptions {
  /** Cap on the longer canvas edge in device pixels. */
  maxDimension: number;
  /** Frames per second. 30 is the requirement across the project's effects. */
  fps: number;
  /** Play at all. `undefined` means: follow the user's motion preference. */
  animated?: boolean;
  /**
   * Whether the agent is working, which makes the effect run faster.
   *
   * Read through a ref rather than watched, for the reason the note above gives:
   * a dependency would restart the loop on every state change, and restarting an
   * effect with drift is a visible jump — worse than the thing it is reporting.
   */
  active?: boolean;
  /**
   * CSS custom properties to resolve, as `name` → fallback value. Read once
   * through a live `getComputedStyle`, so this is a list of *names*, not a
   * snapshot of colours.
   */
  vars?: Record<string, string>;
  /**
   * Build the painter. Called once per effect run, after the theme's variables
   * are readable, which is why it is a factory and not an object: the colours
   * are a *fallback* here and the truth is what `read` returns at paint time.
   */
  create: (read: (name: string) => string) => AtmospherePainter;
}

/**
 * How much faster an effect runs while the agent is working.
 *
 * Not 2×. A glyph shower at twice its resting speed stops reading as weather
 * and starts reading as something wrong with the machine, and the whole point
 * is that this should be felt rather than noticed. 1.8× is the largest step
 * that stays legible in peripheral vision — the rain is in the corners of a
 * window someone is not looking at, and that is the only place this is ever
 * read.
 *
 * It is a multiplier on *simulated time*, not on frames: the loop still runs at
 * `fps` and still costs the same. A faster effect here is not a busier one.
 */
export const ACTIVE_RATE = 1.8;

/**
 * Ease the rate toward its target, one frame at a time.
 *
 * Exported because `MatrixRain` runs its own loop — it predates this hook and is
 * the one documented outlier — and two loops quietly growing two constants is
 * how the rain and the grid end up disagreeing about what "working" looks like.
 *
 * The easing is the same one `NeuralWeb` already used for its brightness, down
 * to the coefficient, so the two reactions to the same event settle at the same
 * pace. A step change would be a snap, and a snap in the corner of the eye while
 * reading something else is exactly the kind of motion §7 is about.
 */
/**
 * The `.ts` on the import above is not a style choice. This module is a plain
 * `.ts` file, and `tests/stateReactiveWeather.test.ts` imports *it* directly —
 * through no bundler, so Node's ESM resolver needs the extension. A relative
 * specifier without one is a module-not-found at import time, which is how a
 * working build turns into a red suite.
 */
export function nextRate(current: number, active: boolean, dt: number): number {
  const target = active ? ACTIVE_RATE : 1;
  return current + (target - current) * Math.min(1, dt * 1.6);
}

/**
 * Attach a painter to a canvas. Returns the ref to put on the element.
 *
 * The returned element is expected to be `aria-hidden` and `pointer-events-none`;
 * nothing here can enforce that, so the tests do.
 */
export function useAtmosphereCanvas(
  options: AtmosphereOptions,
): React.RefObject<HTMLCanvasElement | null> {
  const { maxDimension, fps, animated, active, vars, create } = options;
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // The motion decision, subscribed (a hook call belongs at the top level):
  // user choice, shell verdict, or system preference, as a dependency the
  // effect below re-runs on — a re-render alone would leave the loop running.
  const motionAllowed = useMotionAllowed();
  // The shell measured this window spending a core on decoration. `motionAllowed`
  // still answers the person's choice; this is the measurement, and it is what
  // lowers the rate rather than stopping the backdrop they chose to keep.
  const starved = useStarvedThisBoot();
  // Kept in a ref so the effect can read the latest values without listing
  // them as dependencies — see the note above on why that is not laziness.
  const latest = useRef({ vars, create, active });
  latest.current = { vars, create, active };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    // A probe is async; a cleanup while it is in flight must stop it from
    // arming a loop that belongs to an effect React has already torn down.
    let cancelled = false;

    // Whether to move at all: an explicit `animated` prop wins (a preview must
    // animate to be a preview), otherwise the motion store, computed above.
    // Both answer "should this play", and `reduced` is the opposite of that —
    // the store's value used to be read as if it already were the opposite, so
    // a machine that allowed motion got one still frame and a machine that
    // forbade it got the animation.
    const reduced = animated === undefined ? !motionAllowed : !animated;

    const styles = getComputedStyle(document.documentElement);
    const read = (name: string): string => {
      const fallback = latest.current.vars?.[name] ?? "";
      const raw = styles.getPropertyValue(name).trim();
      return raw || fallback;
    };
    const painter = latest.current.create(read);

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const size: AtmosphereSize = { width: 1, height: 1 };

    const layout = (): void => {
      const rect = canvas.getBoundingClientRect();
      const cssW = Math.max(1, rect.width);
      const cssH = Math.max(1, rect.height);
      const scale = Math.min(1, maxDimension / Math.max(cssW, cssH));
      size.width = Math.round(cssW * dpr * scale);
      size.height = Math.round(cssH * dpr * scale);
      canvas.width = size.width;
      canvas.height = size.height;
    };

    const tick: AtmosphereTick = { frame: 0, seconds: 0, size };
    // One draw, and the edge fade that belongs to every atmosphere. The fade is
    // painted into the canvas's own pixels rather than applied as a CSS mask:
    // a mask on a full-window canvas made the software compositor rebuild the
    // window every frame (see `canvasEdgeFade.ts`). Every `painter.draw` call in
    // this effect goes through here, so a new call site cannot ship without it.
    const paint = (): void => {
      painter.draw(ctx, size, tick);
      applyEdgeFade(ctx, size.width, size.height);
    };
    // The compositor pays per physical pixel on screen, which is the canvas's
    // own CSS area at this display's scale — not the capped painting size and
    // not the window, so a tile in the Appearance pane is priced as a tile.
    // `dpr` above is capped at 2 for the canvas's pixels; the screen's own
    // ratio is what the compositor blends, so the budget uses the uncapped one.
    const displayDpr = window.devicePixelRatio || 1;
    const box = canvas.getBoundingClientRect();
    const physicalPixels =
      Math.max(1, Math.round(box.width * box.height)) * displayDpr * displayDpr;
    const releaseFps = backdropFps(fps, starved, physicalPixels);
    const frameMs = Math.max(1, 1000 / Math.max(1, releaseFps));

    // The frame clock. rAF aligns with the compositor; the accumulator is the
    // authority on how many frames are released.
    let last = 0;
    let raf = 0;
    // The rate the effect is *currently* running at, and the one it is easing
    // toward. Frame count is untouched by either: the loop still releases at
    // `releaseFps` and costs what it cost. What changes is how much simulated time a
    // frame is worth, which is what every painter reads (`tick.seconds`) and
    // what `step` is handed, so drift and phase stay consistent with each other
    // and a capped canvas still moves at the same speed per second.
    let rate = 1;

    // A painter that throws throws again next frame, and the one after, thirty
    // times a second for as long as the window is open. The loop re-arms its own
    // rAF on the first line, so nothing here would ever stop it.
    const guard = makeFrameGuard({
      onGiveUp: (error) => {
        console.error(
          `[atmosphere] painter "${latest.current.create.name || "anonymous"}" failed on ` +
            `${FRAMES_BEFORE_GIVING_UP} consecutive frames; leaving the backdrop still. ` +
            "The window stays usable and the rest of the UI is unaffected.",
          error,
        );
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
      },
    });

    // Whether this machine can afford the animation at all, measured by the
    // frames the compositor actually delivers. A backdrop is decoration; a
    // backdrop that costs a core of software rasterisation per frame is a window
    // that stops answering input, and the user cannot tell that apart from a
    // crash. So the loop stops itself, once, and says why — see `makeFrameBudget`
    // for the numbers and DESIGN.md §7 for the contract.
    const budget = makeFrameBudget({
      targetMs: frameMs,
      onTooSlow: (measuredMs) => {
        console.warn(
          `[atmosphere] the compositor is delivering a frame every ` +
            `${measuredMs.toFixed(0)}ms against a ${frameMs.toFixed(0)}ms request, so the ` +
            "backdrop is left still rather than spending the window's frame budget on " +
            "decoration. The window stays responsive and the rest of the UI is unaffected.",
        );
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
      },
    });

    // The frame guard is on the loop and nowhere else. A throw at mount, on a
    // resize, or on a restore happens once and is a real fault worth surfacing;
    // swallowing it behind a guard would turn a broken painter into a blank
    // canvas with no error, which is the failure this file exists to prevent.
    // The flood it does prevent can only happen in a loop.
    const loop = (t: number): void => {
      raf = requestAnimationFrame(loop);
      // Judged before the accumulator, because the interval being judged is
      // delivery, not drawing: a machine that cannot present 30 frames is not
      // fixed by drawing fewer of them, and the accumulator would hide exactly
      // the evidence that it cannot.
      if (budget.observe(t) === "too-slow") return;
      // A lost context makes every draw a silent no-op, so the frame budget is
      // being spent painting into a void. The rAF stays armed so the clock is
      // where it was when the context comes back.
      if (recovery.isLost()) return;
      if (t - last < frameMs) return;
      const dt = last === 0 ? 0 : Math.min((t - last) / 1000, frameMs / 1000);
      last = t;
      rate = nextRate(rate, latest.current.active === true, dt);
      const scaled = dt * rate;
      tick.seconds += scaled;
      tick.frame += 1;
      guard.run(() => {
        painter.step?.(scaled, tick);
        paint();
      });
    };

    // Context loss is a driver reset, a lid closing, or a monitor renegotiating
    // its mode — all ordinary on a machine left open. Without this the backdrop
    // is gone until the app is reloaded, and there is no error to explain it.
    const recovery = bindContextRecovery(canvas, {
      onRestored: () => {
        // The canvas comes back blank, and possibly a different size, and the
        // clock's last frame may be minutes old. Re-measure, repaint, and reset
        // the accumulator so the first frame back is a full dt rather than a
        // leap that skips the painter forward.
        layout();
        last = 0;
        paint();
      },
    });

    let observer: ResizeObserver | undefined;
    layout();
    // The static frame. Drawn before the loop is armed so that a reduced-motion
    // user and a screenshot taken in the first millisecond see the same thing.
    paint();
    const start = (): void => {
      if (!cancelled) raf = requestAnimationFrame(loop);
    };
    if (reduced) {
      // The loop is never armed; the static frame is the whole animation.
    } else if (animated !== undefined || starved || !probeAvailable()) {
      // A preview must animate to be a preview, and a page outside the shell
      // has no shell clock to ask. A window already measured starving starts at
      // the rate the verdict allows — re-probing there would let the budget
      // overrule "Animate anyway" a second time, which is not what the control
      // promises. Only the first, full-rate start waits for the probe.
      start();
    } else {
      // Measure what a composited frame costs the shell *before* moving: the
      // watchdog's ten-second window is ten seconds of full-rate animation, and
      // on the machine that cannot afford it those are the seconds that hurt.
      // The probe draws a handful of frames at the requested interval; if one
      // does not fit, it records the verdict and this effect re-runs into the
      // reduced path instead of arming. See `frameProbe.ts`.
      const probeDraw = (): void => {
        if (cancelled) return;
        tick.frame += 1;
        tick.seconds += frameMs / 1000;
        painter.step?.(frameMs / 1000, tick);
        paint();
      };
      void probeFrameBudget(probeDraw, frameMs).then((fits) => {
        if (fits) start();
      });
    }

    // A window that has just come back was suspended, not slow: its frames
    // arrived at roughly 1 Hz because nobody was looking, and that measurement
    // must not be the one that stops the backdrop for the rest of the session.
    const onVisibility = (): void => {
      budget.reset();
    };
    document.addEventListener("visibilitychange", onVisibility);

    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => {
        layout();
        // Same reason the loop checks: a lost context makes the draw a no-op, and
        // the restore handler is what will repaint when it comes back.
        if (recovery.isLost()) return;
        paint();
      });
      observer.observe(canvas);
    }

    return () => {
      cancelled = true;
      if (raf) cancelAnimationFrame(raf);
      observer?.disconnect();
      recovery.detach();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [animated, motionAllowed, starved, fps, maxDimension]);

  return canvasRef;
}
