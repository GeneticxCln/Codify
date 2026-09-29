import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * The Toxic Lab theme: reagent bubbling up both gutters of a fume hood.
 *
 * **A bubble is a surface, not a dot.** What makes a drawn circle read as gas
 * rather than as a particle is that it is a *ring with a skin*: a stroked
 * outline that is brighter at the top than at the bottom, because that is where
 * the liquid film is thinnest and where the light gets through. So each bubble
 * here is two strokes — a faint full ring and a brighter arc across its upper
 * third — and a filled highlight only on the largest ones, where there is
 * enough radius for one to be visible without becoming a blob.
 *
 * **They grow as they rise, and they burst.** Both are load-bearing. A bubble
 * that keeps its size all the way up reads as a particle emitter, and one that
 * simply vanishes at the top reads as a bug rather than as a pop. So the radius
 * expands with altitude and a bubble that reaches the ceiling leaves a ring
 * behind it for a third of a second while it resets to the bench.
 *
 * **The wobble is why it is a fluid.** Surface tension makes a rising bubble
 * oscillate sideways as it goes, and the two frequencies beating against each
 * other are what stop a column of circles looking like a column of circles.
 * There are two sines per bubble, incommensurate on purpose.
 *
 * `active` raises the rate through the hook, and additionally makes bubbles
 * burst more often at the top — a busier bench, which is the state report §7
 * asks for and the only motion here that carries information.
 */
export type ToxicLabProps = AtmosphereCanvasProps;

interface Bubble {
  x: number;
  y: number;
  /** Radius in canvas pixels at the bench. It grows with altitude. */
  r: number;
  /** px/s upward. */
  rise: number;
  /** The two sines that make it wobble. */
  phase: number;
  drift: number;
  /** Seconds of burst ring left, and where it was when it burst. */
  burst: number;
  burstX: number;
  burstY: number;
  burstR: number;
  left: boolean;
}

const BUBBLES = 18;
const GUTTER = 0.21;
/** How much taller a bubble is by the time it reaches the top. */
const GROWTH = 2.1;
const BURST_TIME = 0.34;

export function createToxicLabPainter(
  read: (name: string) => string,
  active: boolean,
): AtmospherePainter {

  let bubbles: Bubble[] = [];

  const seed = (w: number, h: number): void => {
    bubbles = Array.from({ length: BUBBLES }, (_, i) => {
      const left = i % 2 === 0;
      return {
        x: left
          ? Math.random() * GUTTER * w
          : w - Math.random() * GUTTER * w,
        y: Math.random() * h,
        r: 2 + Math.random() * 7,
        rise: 16 + Math.random() * 44,
        phase: Math.random() * Math.PI * 2,
        drift: 0.2 + Math.random() * 0.5,
        burst: 0,
        burstX: 0,
        burstY: 0,
        burstR: 0,
        left,
      };
    });
  };

  return {
    step(dt, tick) {
      const { width: w, height: h } = tick.size;
      for (const b of bubbles) {
        b.phase += dt * b.drift;
        b.y -= b.rise * dt * (active ? 1.8 : 1);
        if (b.burst > 0) b.burst = Math.max(0, b.burst - dt);
        const ceiling = -b.r * GROWTH - 8;
        if (b.y > ceiling) continue;
        // Popped. Remember where, then start again from the bench — a new size
        // and a new side, because a bubble that always returns to the same
        // column reads as a loop.
        b.burst = BURST_TIME;
        b.burstX = b.x;
        b.burstY = Math.max(0, b.y);
        b.burstR = b.r * GROWTH;
        b.y = h + b.r * 2;
        b.r = 2 + Math.random() * 7;
        b.rise = 16 + Math.random() * 44;
        const left = Math.random() < 0.5;
        b.left = left;
        b.x = left ? Math.random() * GUTTER * w : w - Math.random() * GUTTER * w;
      }
    },
    draw(ctx, size, tick) {
      // Resolved per frame rather than once at creation, so a theme or a
      // custom tint restyles this canvas on the next frame instead of
      // waiting for a remount. See the hook's note on live reads.
        const reagent = channelsOf(read("--reagent"));
        const skin = channelsOf(read("--reagent-skin"));
      if (bubbles.length === 0) seed(size.width, size.height);
      const { width: w, height: h } = size;
      ctx.clearRect(0, 0, w, h);
      ctx.lineCap = "round";

      for (const b of bubbles) {
        const inward = Math.min(b.x, w - b.x) / (w * GUTTER);
        const fade = Math.max(0, 1 - inward) ** 1.5;

        // The burst ring, drawn before the bubble that made it so the two never
        // fight for the same pixels.
        if (b.burst > 0) {
          const t = 1 - b.burst / BURST_TIME;
          const ringIn = Math.min(b.burstX, w - b.burstX) / (w * GUTTER);
          ctx.strokeStyle = withAlpha(skin, (1 - t) * 0.5 * Math.max(0, 1 - ringIn));
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.arc(b.burstX, b.burstY, b.burstR * (1 + t * 1.6), 0, Math.PI * 2);
          ctx.stroke();
        }

        if (b.y < -b.r * GROWTH - 8 || b.y > h + b.r * 2) continue;
        if (fade < 0.04) continue;

        // Radius at this altitude: a bubble is bigger at the top of the column
        // than at the bench, which is the whole reason it reads as gas.
        const climbed = Math.max(0, Math.min(1, (h - b.y) / h));
        const r = b.r * (1 + (GROWTH - 1) * climbed);
        // Two incommensurate sines, so the wobble never visibly repeats.
        const wobble = Math.sin(b.phase * 1.7) * 5 + Math.sin(b.phase * 0.63 + 1.1) * 3;
        const cx = b.x + wobble;
        const cy = b.y;
        const shimmer = 0.55 + 0.45 * Math.sin(b.phase * 2.3 + tick.seconds * 0.4);

        // The full ring, faint: the body of the bubble.
        ctx.strokeStyle = withAlpha(reagent, 0.2 * fade * shimmer);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.stroke();

        // The skin across the upper third, brighter: the film is thinnest there.
        ctx.strokeStyle = withAlpha(skin, 0.75 * fade * shimmer);
        ctx.lineWidth = Math.max(1, r * 0.22);
        ctx.beginPath();
        // -PI to 0 is the top half in canvas coordinates, where y grows down.
        ctx.arc(cx, cy, r * 0.92, -Math.PI, 0);
        ctx.stroke();

        // A highlight, only where there is room for one to read.
        if (r > 4.5) {
          ctx.fillStyle = withAlpha(skin, 0.5 * fade);
          ctx.beginPath();
          ctx.arc(cx - r * 0.34, cy - r * 0.36, Math.max(0.8, r * 0.16), 0, Math.PI * 2);
          ctx.fill();
        }
      }
    },
  };
}

export const ToxicLab: React.FC<ToxicLabProps> = ({
  className = "",
  maxDimension = 1024,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--reagent": "#9dff3c",
    "--reagent-skin": "#d4ff7a",
  };

  const canvasRef = useAtmosphereCanvas({
    maxDimension,
    fps,
    animated,
    active,
    vars,
    create: (read) => createToxicLabPainter(read, active),
  });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
