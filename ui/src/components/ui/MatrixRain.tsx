import React, { useEffect, useRef } from "react";

import { nextRate } from "../../hooks/useAtmosphereCanvas";
import {
  bindContextRecovery,
  makeFrameBudget,
  makeFrameGuard,
  FRAMES_BEFORE_GIVING_UP,
} from "../../canvasRecovery.ts";
import { useMotionAllowed } from "../../motionPreference.ts";

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
 * so opting out removes the animation entirely rather than slowing it.
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

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // As in the shared hook: an explicit `animated` wins (a preview must
    // animate to be a preview), otherwise the motion store, computed above.
    // Both mean "play", and `reduced` is the opposite of that.
    const reduced = animated === undefined ? !motionAllowed : !animated;

    // The theme's own colours, read live so a settings change restyles an
    // already-running canvas on its next tick without a remount.
    const styles = getComputedStyle(document.documentElement);
    const readVar = (name: string, fallback: string): string =>
      styles.getPropertyValue(name).trim() || fallback;

    const rainColor = readVar("--cmatrix-rain", "#003300");
    const textColor = readVar("--cmatrix-text", "#a3ffb8");
    const bgColor = readVar("--cmatrix-bg", "#000000");
    // The glyphs are the theme's too, which is the whole reason the monochrome
    // ASCII theme is four lines of tokens rather than a forked component. The
    // OLED theme publishes nothing here and keeps the default set; a theme that
    // publishes hex bytes gets hex bytes. Case survives a custom property, so
    // `ABCDEF` stays uppercase.
    const glyphs = readVar("--cmatrix-glyphs", GLYPHS) || GLYPHS;

    // The rain paints on black; when the page is not black the canvas is
    // transparent so the surface shows through.
    const paintsBlack = bgColor.toLowerCase() === "#000000";

    const CELL = 14; // glyph cell, CSS pixels
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    let cssW = 0;
    let cssH = 0;
    let cell = CELL;
    let columns: Column[] = [];
    let cellRows = 0;

    const layout = (): void => {
      const rect = canvas.getBoundingClientRect();
      cssW = Math.max(1, rect.width);
      cssH = Math.max(1, rect.height);
      // Cap the longer edge: fillRect and glyph draws scale with area, and a
      // full-screen canvas at dpr 2 would quadruple both for no visible gain
      // at 30 FPS.
      const cap = maxDimension;
      const scale = Math.min(1, cap / Math.max(cssW, cssH));
      canvas.width = Math.round(cssW * dpr * scale);
      canvas.height = Math.round(cssH * dpr * scale);
      // Cell size in *canvas* pixels, so column count follows the cap, not
      // the CSS box.
      cell = CELL * dpr * scale;
      const cols = Math.max(1, Math.floor(canvas.width / cell));
      cellRows = Math.max(1, Math.floor(canvas.height / cell));
      columns = Array.from({ length: cols }, () => ({
        y: Math.floor(Math.random() * cellRows),
        // Random, not zero: a shared starting remainder would make every column
        // take its next whole cell on the same frame, and the rain would pulse
        // in lockstep instead of showering.
        frac: Math.random(),
        trail: 4 + Math.floor(Math.random() * 9),
        speed: Math.random() < 0.25 ? 2 : 1,
      }));
    };

    // ── drawing ────────────────────────────────────────────────────────────
    const drawFrame = (): void => {
      // The classic trick: instead of clearing, paint a translucent black
      // veil — old glyphs fade into the trail over several frames. Full alpha
      // on the first paint so the canvas starts black, not grey-on-white.
      ctx.fillStyle = paintsBlack
        ? "rgba(0, 0, 0, 0.12)"
        // Transparent compositing cannot fade to a colour; fall back to a
        // full repaint that still reads as rain.
        : bgColor;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.font = `${Math.round(cell * 0.82)}px monospace`;
      ctx.textBaseline = "top";
      for (let i = 0; i < columns.length; i++) {
        const col = columns[i];
        const x = i * cell;
        // The head: one glyph in the light green, the only high-contrast
        // pixel in the column.
        ctx.fillStyle = textColor;
        ctx.fillText(randomGlyph(glyphs), x, col.y * cell);
        // The trail: `trail` cells behind the head, in the deep dark green.
        ctx.fillStyle = rainColor;
        for (let d = 1; d <= col.trail; d++) {
          const ty = col.y - d * col.speed;
          if (ty >= 0) ctx.fillText(randomGlyph(glyphs), x, ty * cell);
        }
      }
    };

    const step = (rate: number): void => {
      for (const col of columns) {
        col.frac += rate;
        // A rate of 1.8 earns a cell and most of the next one per frame, so
        // this runs twice on a fast frame and not at all on a slow one. That is
        // the intended arithmetic — the column moves at `rate` cells per
        // second, not `rate` steps per frame.
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
    const frameMs = Math.max(1, 1000 / Math.max(1, fps));
    let last = 0;
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
      if (t - last < frameMs) return;
      const dt = last === 0 ? 0 : Math.min((t - last) / 1000, frameMs / 1000);
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
        last = 0;
        drawFrame();
      },
    });

    let observer: ResizeObserver | undefined;
    layout();
    drawFrame(); // the static frame reduced motion gets
    if (!reduced) raf = requestAnimationFrame(loop);

    // Suspended frames are not slow frames: a window nobody is looking at gets
    // about one a second by design, and that must not be the measurement that
    // decides the rain is unaffordable.
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
        drawFrame();
      });
      observer.observe(canvas);
    }

    return () => {
      if (raf) cancelAnimationFrame(raf);
      observer?.disconnect();
      recovery.detach();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [animated, motionAllowed, fps, maxDimension]);

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
