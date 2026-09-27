import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, mix, withAlpha } from "../../canvasColour";

/**
 * The Cyberpunk theme's backdrop: a neon horizon grid, a banded sun, and the
 * stutter of a badly calibrated monitor.
 *
 * The clock — the cap, the 30 FPS accumulator, the single static frame under
 * `prefers-reduced-motion`, the resize handling — is `useAtmosphereCanvas`'s,
 * because six effects sharing it is the difference between one bounded budget
 * and six. What is left here is the drawing, and it is cheap by arithmetic:
 * every frame is a full repaint of about forty `lineTo`/`fillRect` calls and one
 * `clearRect`, which is nothing next to the rain's per-glyph `fillText` storm.
 *
 * Colours are the theme's own (`--cyber-cyan`, `--cyber-magenta`,
 * `--cyber-amber`, `--codify-bg`), resolved once when the effect starts. A
 * theme switch unmounts this canvas and mounts another one, so a live re-read
 * would buy nothing here; the variable *is* the contract, which is what stops
 * the component and the theme from drifting apart.
 *
 * Two things in here were wrong on the first pass and are commented where they
 * are: the sun's position, which put a bright disc through the idle hero's
 * heading, and the band geometry, which drew a staircase. The rule both broke
 * is the same one, in DESIGN.md §7: a backdrop is never decoration underneath
 * the words being read, and it is never decoration that reads as an artefact.
 */
export type CyberGridProps = AtmosphereCanvasProps;

/** How far the floor runs before a line is recycled at the horizon, in z units. */
const MAX_Z = 26;
/** Vertical (converging) lines. Cheap; more than this is a texture, not a grid. */
const VANES = 15;
/** Horizontal lines on screen at once. */
const RUNGS = 14;
/** Frames between stutters, and how many frames each stutter lasts. */
const STUTTER_EVERY = 83;
const STUTTER_FRAMES = 3;

export const CyberGrid: React.FC<CyberGridProps> = ({
  className = "",
  maxDimension = 480,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--cyber-cyan": "#00e5ff",
    "--cyber-magenta": "#ff2e9a",
    "--cyber-amber": "#ffb02e",
    "--codify-bg": "#0a0616",
  };

  const create = (read: (name: string) => string): AtmospherePainter => {
    const bg = read("--codify-bg");
    const cyan = channelsOf(read("--cyber-cyan"));
    const magenta = channelsOf(read("--cyber-magenta"));
    const amber = channelsOf(read("--cyber-amber"));

    let phase = 0;

    // ── the sun ────────────────────────────────────────────────────────────
    // One gradient-filled circle, then the bands are *cut out* of it with
    // sky-coloured rectangles that thicken toward the horizon.
    //
    // It was drawn the other way round first — a rect per band, each sized to
    // its own chord — and that reads as a staircase, because a chord is the
    // width at one height and a band is a range of them. Drawing the disc once
    // and subtracting is both smoother and the same amount of work: one `arc`,
    // one gradient, one rect per band. The gradient is the only thing in this
    // component that costs more than a fill, and it costs it once per frame
    // over one disc — which is the difference between "no gradients" as a
    // per-glyph rule and "no gradients" as a superstition.
    const drawSun = (
      ctx: CanvasRenderingContext2D,
      width: number,
      horizonY: number,
    ): void => {
      const r = Math.min(width, horizonY) * 0.13;
      const cx = width / 2;
      const cy = horizonY - r * 0.25;
      const top = cy - r;

      const body = ctx.createLinearGradient(0, top, 0, horizonY);
      body.addColorStop(0, withAlpha(amber, 0.78));
      body.addColorStop(1, withAlpha(magenta, 0.78));
      ctx.fillStyle = body;
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.fill();

      // The cut. Bands thicken as they approach the horizon, which is both the
      // synthwave convention and the thing that makes a set of stripes read as
      // a sun rather than as a barcode.
      let band = Math.max(1, r * 0.05);
      let y = top + band * 1.6;
      while (y < horizonY) {
        ctx.fillStyle = bg;
        ctx.fillRect(0, y, width, band);
        band *= 1.18;
        y += band;
      }
    };

    // ── the floor ──────────────────────────────────────────────────────────
    const drawGrid = (
      ctx: CanvasRenderingContext2D,
      width: number,
      height: number,
      horizonY: number,
    ): void => {
      const depth = height - horizonY;
      const weight = Math.max(1, width / 900);

      // The converging lines: every one of them is a segment from the
      // vanishing point to the near edge, so the whole fan is 15 strokes.
      ctx.lineWidth = weight;
      ctx.strokeStyle = withAlpha(cyan, 0.34);
      ctx.beginPath();
      for (let i = 0; i <= VANES; i++) {
        ctx.moveTo(width / 2, horizonY);
        ctx.lineTo((i / VANES) * width, height);
      }
      ctx.stroke();

      // The rungs: `y = horizonY + depth / z`, so z = 1 is the near edge and
      // large z crowds into the horizon. The scroll is a modulo over a fixed
      // set of offsets, which means a resize cannot desynchronise the rungs
      // from each other and no line state has to be stored.
      //
      // Each rung is stroked on its own because each carries its own colour and
      // its own fade: magenta at the near edge, cyan where they vanish into the
      // horizon. The fan above is a single stroke, because its lines do not need
      // a gradient and 15 strokes beat one stroke only when the strokes differ.
      const scroll = phase % MAX_Z;
      for (let i = 0; i < RUNGS; i++) {
        const z = 1 + (((i * (MAX_Z / RUNGS) - scroll) % MAX_Z) + MAX_Z) % MAX_Z;
        const y = horizonY + depth / z;
        if (y > height + weight) continue;
        const near = 1 - (z - 1) / MAX_Z;
        ctx.strokeStyle = withAlpha(mix(cyan, magenta, near), 0.1 + near * 0.5);
        ctx.lineWidth = weight;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(width, y);
        ctx.stroke();
      }

      // The horizon itself: a bright one-pixel edge where the floor meets the
      // sky, because a grid with no vanishing line reads as wallpaper. Kept
      // dimmer than the first version's 0.85: it sits at the same height as
      // the composer bar, and a bright rule under text is the same mistake as
      // a bright picture behind it.
      ctx.fillStyle = withAlpha(cyan, 0.5);
      ctx.fillRect(0, horizonY - weight, width, weight);
    };

    // The stutter: a whole-frame wash plus one shifted band, on a fixed period.
    // Deterministic on purpose — a random glitch is a flicker, and a flicker a
    // user cannot predict is a flicker they cannot read past.
    const drawStutter = (
      ctx: CanvasRenderingContext2D,
      width: number,
      height: number,
      frame: number,
    ): void => {
      if (frame % STUTTER_EVERY >= STUTTER_FRAMES) return;
      ctx.fillStyle = withAlpha(cyan, 0.07);
      ctx.fillRect(0, 0, width, height);
      const bandY = (frame * 61) % Math.max(1, height - height * 0.05);
      ctx.fillStyle = withAlpha(magenta, 0.16);
      ctx.fillRect(0, bandY, width, height * 0.02);
    };

    return {
      // One z unit per second reads as a floor rather than a sheet of paper:
      // 26 units is one full recycle in 26 seconds. `dt` not a frame count, so
      // halving the frame rate does not halve the speed.
      step(dt) {
        phase += dt;
      },
      draw(ctx, size, tick) {
        const { width, height } = size;
        // Below centre, not above it: above centre the sun owns the middle of
        // the window, which is where the idle hero's heading and its paragraph
        // live. Lower down, the sun clears the empty state and the floor gets
        // the run it needs to read as motion.
        const horizonY = height * 0.62;
        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = bg;
        ctx.fillRect(0, 0, width, height);
        drawSun(ctx, width, horizonY);
        drawGrid(ctx, width, height, horizonY);
        drawStutter(ctx, width, height, tick.frame);
      },
    };
  };

  const canvasRef = useAtmosphereCanvas({ maxDimension, fps, animated, active, vars, create });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
