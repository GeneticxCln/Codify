import React, { useEffect, useRef } from "react";

import { backdropFps, nextRate, releaseAt } from "../../hooks/useAtmosphereCanvas";
import { applyEdgeFade } from "../../canvasEdgeFade.ts";
import {
  bindContextRecovery,
  makeFrameBudget,
  makeFrameGuard,
  FRAMES_BEFORE_GIVING_UP,
} from "../../canvasRecovery.ts";
import { useMotionAllowed, useStarvedThisBoot } from "../../motionPreference.ts";
import { probeAvailable, probeFrameBudget } from "../../frameProbe.ts";

/**
 * The CMatrix rain: a lightweight 2D-canvas glyph shower for the OLED theme.
 *
 * Lightweight by arithmetic, not by hope. The canvas is drawn at a *capped*
 * device-pixel size — `maxDimension` bounds the longer edge — and stepped by
 * a fixed-delay timer pinned to 30 FPS (`~33ms`), which is half the frames a
 * 60 Hz rAF loop would push and, on OLED, more than half the power. Density
 * is proportional to area, so a big pane costs the same per pixel as a small
 * one. No shadowBlur, no gradients, no per-glyph brightening passes — the
 * depth illusion comes from two fill colors, which is all the original effect
 * ever was.
 *
 * Colours are read from the CSS variables the themes module exports
 * (`--cmatrix-rain`, `--cmatrix-text`, `--cmatrix-bg`), so the component and
 * the theme cannot disagree: restyle the variable and both the app text and
 * the rain move together.
 *
 * §7 of DESIGN.md bans a decorative loop *in the UI* — this is the documented
 * Appearance exception, in the same family as `logo.gif`: it animates only
 * where the user asked for it (they picked the theme), and under
 * `prefers-reduced-motion` it draws exactly one frame — the trail, frozen —
 * so opting out removes the animation entirely rather than slowing it. Like the
 * shared hook, it measures the window's render cost for a composited frame before the
 * shower starts (`frameProbe.ts`), so the budget is judged before it is spent.
 */
export interface MatrixRainProps {
  className?: string;
  /** Cap on the longer canvas edge in device pixels. Bounds fillRect cost. */
  maxDimension?: number;
  /**
   * Frames per second. 30 is the requirement; the timer floors its delay at
   * one frame's time so a caller cannot ask the loop for impossible frames.
   */
  fps?: number;
  /** Play at all. Defaults to the user's motion preference. */
  animated?: boolean;
  /**
   * Whether the agent is working, which makes the rain fall faster.
   *
   * The shared clock's job for the five other effects, done here by hand
   * because this loop predates the hook and is the documented outlier. The rate
   * constant and the easing are imported rather than copied, so the rain and the
   * grid cannot drift into two different answers to "what does working look
   * like".
   */
  active?: boolean;
}

/** One column of rain: where its glyph is, and how far down the trail runs. */
interface Column {
  /** Glyph position, in cell rows. */
  y: number;
  /**
   * Fraction of a cell already travelled, 0–1.
   *
   * `y` is integral because the glyphs live on a grid, and a grid cannot hold a
   * fractional position. The rate is not integral, so the remainder lives here
   * and whole cells are taken off it as they are earned. Without this, a 1.8×
   * rate would have to round to 1 or 2 — which is either no change at all or a
   * jump, and a jump in the corner of the eye is the thing §7 is about.
   */
  frac: number;
  /** Trail length in cells, 4–12, so columns desynchronise. */
  trail: number;
  /** Movement per tick, in cells — 1 or 2, the fast/slow leader split. */
  speed: number;
}

const GLYPHS = "ｱｲｳｴｵｶｷｸｹｺ0123456789ABCDEF<>=*+-";

export const MatrixRain: React.FC<MatrixRainProps> = ({
  className = "",
  maxDimension = 480,
  fps = 30,
  animated,
  active = false,
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // The "the agent is thinking" flag, held in a ref for the reason the shared
  // hook documents: as a dependency it would tear down and re-arm the loop on
  // every state change, and re-arming this one visibly restarts the shower.
  const activeRef = useRef(active);
  activeRef.current = active;
  // The motion decision, subscribed at the component's top level (a hook call
  // belongs here, not inside the effect below): user choice, shell verdict, or
  // system preference. As a dependency it re-arms the loop when it flips —
  // which is the point: a re-render alone would leave the loop running.
  const motionAllowed = useMotionAllowed();
  // The shell measured this boot spending a core on decoration. Below the
  // person's choice: "Animate anyway" keeps the rain, at the measured rate.
  const starved = useStarvedThisBoot();

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    // See the shared hook: a probe is async, and a torn-down effect must not
    // arm the loop it was probing for.
    let cancelled = false;

    // As in the shared hook: an explicit `animated` wins (a preview must
    // animate to be a preview), otherwise the motion store, computed above.
    // Both mean "play", and `reduced` is the opposite of that.
    const reduced = animated === undefined ? !motionAllowed : !animated;

    // The theme's own colours, cached per theme/style change so per-frame ticks
    // avoid repeated `getPropertyValue` style lookups while still restyling on
    // the next frame without a remount.
    const styles = getComputedStyle(document.documentElement);
    const varCache = new Map<string, string>();
    let onRestyle: (() => void) | null = null;
    const clearVarCache = (): void => {
      varCache.clear();
      onRestyle?.();
    };
    const readVar = (name: string, fallback: string): string => {
      const hit = varCache.get(name);
      if (hit !== undefined) return hit;
      const value = styles.getPropertyValue(name).trim() || fallback;
      varCache.set(name, value);
      return value;
    };
    window.addEventListener("codify:theme-changed", clearVarCache);
    window.addEventListener("storage", clearVarCache);
    let styleObserver: MutationObserver | undefined;
    if (typeof MutationObserver !== "undefined") {
      styleObserver = new MutationObserver(clearVarCache);
      styleObserver.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["style", "class"],
      });
    }

    const CELL = 14; // glyph cell, CSS pixels
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    let cssW = 0;
    let cssH = 0;
    let cell = CELL;
    let fontSpec = `${Math.round(CELL * 0.82)}px monospace`;
    let columns: Column[] = [];
    let cellRows = 0;
    let seededCanvas = false;
    let frameMs = Math.max(1, 1000 / Math.max(1, fps));
    // The fastest the boot probe found this window can afford; what was asked for until it has.
    let ceiling = fps;

    const updateFrameBudget = (w: number, h: number): void => {
      const displayDpr = window.devicePixelRatio || 1;
      const physicalPixels =
        Math.max(1, Math.round(w * h)) * displayDpr * displayDpr;
      const releaseFps = backdropFps(Math.min(fps, ceiling), starved, physicalPixels);
      frameMs = Math.max(1, 1000 / Math.max(1, releaseFps));
    };

    const layout = (): boolean => {
      const rect = canvas.getBoundingClientRect();
      cssW = Math.max(1, rect.width);
      cssH = Math.max(1, rect.height);
      // Cap the longer edge in device pixels: fillRect and glyph draws scale with area.
      const cap = maxDimension;
      const targetW = cssW * dpr;
      const targetH = cssH * dpr;
      const scale = Math.min(1, cap / Math.max(targetW, targetH));
      const nextW = Math.max(1, Math.round(targetW * scale));
      const nextH = Math.max(1, Math.round(targetH * scale));
      const changed = columns.length === 0 || canvas.width !== nextW || canvas.height !== nextH;
      if (canvas.width !== nextW) canvas.width = nextW;
      if (canvas.height !== nextH) canvas.height = nextH;
      if (changed) {
        // Cell size in *canvas* pixels, floored so column count stays bounded
        // by the capped canvas width (<= 96 columns) rather than scaling
        // linearly with wide/4K CSS viewports when `scale < 1`.
        cell = Math.max(CELL * dpr * scale, canvas.width / 96);
        fontSpec = `${Math.round(cell * 0.82)}px monospace`;
        const cols = Math.max(1, Math.floor(canvas.width / cell));
        cellRows = Math.max(1, Math.floor(canvas.height / cell));
        seededCanvas = false;
        columns = Array.from({ length: cols }, () => ({
          y: Math.floor(Math.random() * cellRows),
          // Random, not zero: a shared starting remainder would make every column
          // take its next whole cell on the same frame, and the rain would pulse
          // in lockstep instead of showering.
          frac: Math.random(),
          trail: 4 + Math.floor(Math.random() * 9),
          speed: Math.random() < 0.25 ? 2 : 1,
        }));
      }
      updateFrameBudget(rect.width, rect.height);
      return changed;
    };

    // ── drawing ────────────────────────────────────────────────────────────
    const drawFrame = (): void => {
      const rainColor = readVar("--cmatrix-rain", "#003300");
      const textColor = readVar("--cmatrix-text", "#a3ffb8");
      const bgColor = readVar("--cmatrix-bg", "#000000");
      const glyphs = readVar("--cmatrix-glyphs", GLYPHS) || GLYPHS;
      const paintsBlack = bgColor.toLowerCase() === "#000000";

      // The classic trick: instead of clearing, paint a translucent black
      // veil — old glyphs fade into the trail over several frames. Full alpha
      // on the first paint so the canvas starts black, not grey-on-white.
      ctx.fillStyle = paintsBlack
        ? "rgba(0, 0, 0, 0.12)"
        // Transparent compositing cannot fade to a colour; fall back to a
        // full repaint that still reads as rain.
        : bgColor;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      if (ctx.font !== fontSpec) ctx.font = fontSpec;
      if (ctx.textBaseline !== "top") ctx.textBaseline = "top";
      // Draw heads in one fillStyle pass, then trails in a second pass (2 state
      // switches per frame instead of 2 per column).
      ctx.fillStyle = textColor;
      for (let i = 0; i < columns.length; i++) {
        const col = columns[i];
        ctx.fillText(randomGlyph(glyphs), i * cell, col.y * cell);
      }
      ctx.fillStyle = rainColor;
      if (!paintsBlack || !seededCanvas) {
        // Initial static frame (also reduced-motion / resize / non-black bg):
        // populate every trail cell so the first paint shows full trails.
        for (let i = 0; i < columns.length; i++) {
          const col = columns[i];
          const x = i * cell;
          for (let d = 1; d <= col.trail; d++) {
            const ty = col.y - d * col.speed;
            if (ty >= 0) ctx.fillText(randomGlyph(glyphs), x, ty * cell);
          }
        }
        if (paintsBlack) seededCanvas = true;
      } else {
        // Steady frames on black: the translucent veil above already fades
        // previous frames into trails, so only repaint the immediate neck (to
        // tone down last frame's bright head) plus one shimmering trail cell.
        for (let i = 0; i < columns.length; i++) {
          const col = columns[i];
          const x = i * cell;
          const neckY = col.y - col.speed;
          if (neckY >= 0) ctx.fillText(randomGlyph(glyphs), x, neckY * cell);
          const shimmerD = 2 + Math.floor(Math.random() * Math.max(1, col.trail - 1));
          const shimmerY = col.y - shimmerD * col.speed;
          if (shimmerY >= 0) ctx.fillText(randomGlyph(glyphs), x, shimmerY * cell);
        }
      }
      // The same edge fade every other atmosphere gets. It is painted into the
      // canvas rather than applied as a CSS mask because a mask on a
      // full-window canvas made the software compositor rebuild the window on
      // every frame; the shared hook owns the other loop's call.
      applyEdgeFade(ctx, canvas.width, canvas.height);
    };

    const step = (rate: number): void => {
      for (const col of columns) {
        col.frac += rate;
        // A rate of 1.8 earns a cell and most of the next one per frame, so
        // this runs twice on a fast frame and not at all on a slow one. That is
        // the intended arithmetic — the column moves `rate` cells per frame,
        // and the fractional part is carried here so it can.
        while (col.frac >= 1) {
          col.frac -= 1;
          col.y += col.speed;
          if (col.y * cell > canvas.height + col.trail * cell * col.speed) {
            col.y = -Math.floor(Math.random() * cellRows);
            col.trail = 4 + Math.floor(Math.random() * 9);
          }
        }
      }
    };

    // ── the 30 FPS clock ───────────────────────────────────────────────────
    // rAF only to align with the compositor; the pacing authority is the
    // accumulator, which releases at most one frame per 1000/fps ms. That is
    // the cap: a 144 Hz display still gets 30.
    // The same rate policy as the shared hook, including the starved rate: the
    // rain is the one loop that predates it, not one exempt from it. The
    // compositor pays per physical pixel of the canvas's on-screen box, so the
    // starved rate is scaled by that area — see `starvedFps`. `dpr` is capped
    // at 2 for the canvas's own pixels; the screen's ratio is uncapped, because
    // that is the area the compositor blends.
    const box = canvas.getBoundingClientRect();
    updateFrameBudget(box.width, box.height);
    let last = 0;
    // When the next frame is due (`releaseAt`). `last` stays for `dt`, which is real time.
    let due = 0;
    let raf = 0;
    // As in the shared hook: the frame count is untouched, only how much
    // simulated time each frame is worth.
    let rate = 1;

    // A frame that throws throws again on the next one, thirty times a second,
    // for as long as the window is open: the loop re-arms its own rAF on the
    // first line, so nothing else here would ever stop it.
    const guard = makeFrameGuard({
      onGiveUp: (error) => {
        console.error(
          `[rain] the glyph shower failed on ${FRAMES_BEFORE_GIVING_UP} consecutive ` +
            "frames; leaving it still. The window stays usable.",
          error,
        );
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
      },
    });

    // Whether this machine can afford the animation, judged by the frames the
    // compositor actually delivers — the same rule and the same numbers as the
    // shared hook, because a rule that lives in only one of the two loops is a
    // rule the other one silently does not have (see `canvasRecovery.ts`).
    const budget = makeFrameBudget({
      targetMs: frameMs,
      onTooSlow: (measuredMs) => {
        console.warn(
          `[rain] the compositor is delivering a frame every ${measuredMs.toFixed(0)}ms ` +
            `against a ${frameMs.toFixed(0)}ms request, so the glyph shower is left still ` +
            "rather than spending the window's frame budget on decoration. The window " +
            "stays responsive and the rest of the UI is unaffected.",
        );
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
      },
    });

    const loop = (t: number): void => {
      raf = requestAnimationFrame(loop);
      if (budget.observe(t) === "too-slow") return;
      // A lost context turns every draw into a silent no-op, so the frame budget
      // would be spent painting into a void. The rAF stays armed so the clock is
      // where it was when the context returns.
      if (recovery.isLost()) return;
      // The shared release clock (`releaseAt`): a 30 FPS rain on a 60 Hz panel used to slip to
      // every third vsync about half the time, because two vsyncs are 33.3 ms give or take.
      const nextDue = releaseAt(due, t, frameMs);
      if (nextDue === null) return;
      due = nextDue;
      const elapsed = last === 0 ? 0 : t - last;
      const dt =
        last === 0
          ? 0
          : elapsed < frameMs * 1.5
            ? Math.min(elapsed / 1000, frameMs / 1000)
            : Math.min(elapsed / 1000, 0.1);
      last = t;
      rate = nextRate(rate, activeRef.current, dt);
      guard.run(() => {
        step(rate);
        drawFrame();
      });
    };

    // A driver reset, a lid closing, a monitor changing mode. Without this the
    // rain is simply gone after one, with no error and no way back but a reload.
    const recovery = bindContextRecovery(canvas, {
      onRestored: () => {
        // Back blank, possibly a different size, and the clock's last frame may
        // be minutes old. Re-measure, repaint, and reset the accumulator so the
        // first frame is a whole one rather than a leap.
        layout();
        seededCanvas = false;
        last = 0;
        due = 0;
        drawFrame();
      },
    });

    let observer: ResizeObserver | undefined;
    layout();
    drawFrame(); // the static frame reduced motion gets
    onRestyle = (): void => {
      // Repaint immediately on live tint/theme changes when the loop is not running
      // (reduced motion or stopped), and clear the trail seed so new rain colours
      // cover every cell right away.
      if (!cancelled && raf === 0 && !recovery.isLost()) {
        seededCanvas = false;
        drawFrame();
      }
    };
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
      // Measure the window's render cost for a composited frame before the shower
      // starts, rather than discovering it after the watchdog's window. The
      // probe advances the rain by a handful of cells and draws each one; if a
      // frame does not fit the interval, it records the verdict and this effect
      // re-runs instead of arming. See `frameProbe.ts`.
      const probeDraw = (): void => {
        if (cancelled) return;
        step(1);
        drawFrame();
      };
      void probeFrameBudget(probeDraw, fps).then((rate) => {
        if (rate <= 0 || cancelled) return;
        ceiling = rate;
        const measured = canvas.getBoundingClientRect();
        updateFrameBudget(measured.width, measured.height);
        start();
      });
    }

    // Suspended frames are not slow frames: a window nobody is looking at gets
    // about one a second by design, and that must not be the measurement that
    // decides the rain is unaffordable.
    const onVisibility = (): void => {
      budget.reset();
    };
    document.addEventListener("visibilitychange", onVisibility);

    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => {
        if (!layout()) return;
        // Same reason the loop checks: a lost context makes the draw a no-op, and
        // the restore handler is what will repaint when it comes back.
        if (recovery.isLost()) return;
        drawFrame();
      });
      observer.observe(canvas);
    }

    return () => {
      cancelled = true;
      if (raf) cancelAnimationFrame(raf);
      observer?.disconnect();
      styleObserver?.disconnect();
      window.removeEventListener("codify:theme-changed", clearVarCache);
      window.removeEventListener("storage", clearVarCache);
      recovery.detach();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [animated, motionAllowed, starved, fps, maxDimension]);

  // aria-hidden: the effect is decoration beside real content, and a screen
  // reader reading half-width katakana is not a service to anyone.
  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};

function randomGlyph(set: string): string {
  return set[Math.floor(Math.random() * set.length)];
}
