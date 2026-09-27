import { useEffect, useRef } from "react";

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
  // Kept in a ref so the effect can read the latest values without listing
  // them as dependencies — see the note above on why that is not laziness.
  const latest = useRef({ vars, create, active });
  latest.current = { vars, create, active };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduced =
      animated ??
      (typeof window !== "undefined" &&
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches);

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
    const frameMs = Math.max(1, 1000 / Math.max(1, fps));

    // The 30 FPS clock. rAF aligns with the compositor; the accumulator is the
    // authority on how many frames are released.
    let last = 0;
    let raf = 0;
    // The rate the effect is *currently* running at, and the one it is easing
    // toward. Frame count is untouched by either: the loop still releases at
    // `fps` and costs what it cost. What changes is how much simulated time a
    // frame is worth, which is what every painter reads (`tick.seconds`) and
    // what `step` is handed, so drift and phase stay consistent with each other
    // and a capped canvas still moves at the same speed per second.
    let rate = 1;
    const loop = (t: number): void => {
      raf = requestAnimationFrame(loop);
      if (t - last < frameMs) return;
      const dt = last === 0 ? 0 : Math.min((t - last) / 1000, frameMs / 1000);
      last = t;
      rate = nextRate(rate, latest.current.active === true, dt);
      const scaled = dt * rate;
      tick.seconds += scaled;
      tick.frame += 1;
      painter.step?.(scaled, tick);
      painter.draw(ctx, size, tick);
    };

    let observer: ResizeObserver | undefined;
    layout();
    // The static frame. Drawn before the loop is armed so that a reduced-motion
    // user and a screenshot taken in the first millisecond see the same thing.
    painter.draw(ctx, size, tick);
    if (!reduced) raf = requestAnimationFrame(loop);

    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => {
        layout();
        painter.draw(ctx, size, tick);
      });
      observer.observe(canvas);
    }

    return () => {
      if (raf) cancelAnimationFrame(raf);
      observer?.disconnect();
    };
  }, [animated, fps, maxDimension]);

  return canvasRef;
}
