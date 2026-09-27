import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, mix, withAlpha, wobble } from "../../canvasColour";

/**
 * The Solar Flare theme: depth, at the edges, without anything in the way.
 *
 * Two effects, both cheap, both deliberately at their faintest. The starfield
 * is the depth cue — a point that recedes toward the vanishing point at the
 * window's centre and grows as it arrives, spawned on a rectangle *ring* rather
 * than across the whole canvas, because the brief asks for the outer edges and
 * because a star behind the transcript is a star in the reading. The streams
 * are the "vector fluid" half: four long polylines whose control points are
 * driven by summed sines, so the shape is continuous and stateless and cannot
 * accumulate drift.
 *
 * The seeds are what stop the two from looking like one effect. A starfield
 * alone reads as space; a starfield with slow curves across it reads as
 * something *moving through* space, which is the difference between a
 * background and a scene.
 */
export type NebulaFlowProps = AtmosphereCanvasProps;

interface Star {
  /** 0 at the vanishing point, 1 at the far edge of the ring. */
  z: number;
  /** Where on the ring, 0–1 around the perimeter. */
  at: number;
  /** Brightness and colour mix. Not all stars are the same violet. */
  seed: number;
}

const STARS = 150;
const STREAMS = 4;

export const NebulaFlow: React.FC<NebulaFlowProps> = ({
  className = "",
  maxDimension = 480,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--flare-indigo": "#6366f1",
    "--flare-ultraviolet": "#a855f7",
  };

  const create = (read: (name: string) => string): AtmospherePainter => {
    const indigo = channelsOf(read("--flare-indigo"));
    const violet = channelsOf(read("--flare-ultraviolet"));

    let stars: Star[] = [];

    const seed = (): void => {
      stars = Array.from({ length: STARS }, () => ({
        z: Math.random(),
        at: Math.random(),
        seed: Math.random(),
      }));
    };

    /**
     * A point on the window's border ring, somewhere along a star's journey
     * outward from the centre to past the edge.
     *
     * The first version had this inverted: scale was `0.15 / z`, so a star was
     * *smallest* at the far end of its travel and the brightness peaked with
     * it — which put the whole field in a bright cluster at the centre of the
     * window, the exact opposite of "along the outer edges". A star now starts
     * at the vanishing point and grows outward, and its alpha is a ramp rather
     * than a bell: nothing at the centre (where it would be a dot on the text),
     * full strength across the outer half, gone by the time it leaves.
     */
    const project = (
      s: Star,
      w: number,
      h: number,
      seconds: number,
    ): { x: number; y: number; travel: number } => {
      // The drift is what makes it a starfield: every star is moving outward,
      // and the ring rotates very slowly so the field never repeats visibly.
      const travel = (s.z + seconds * 0.035) % 1;
      const scale = travel * 1.35;
      const angle = (s.at + seconds * 0.004) % 1;
      // Perimeter parameterisation of a rectangle, from the centre outward.
      const px = (angle * 4) % 4;
      let nx = 0;
      let ny = 0;
      if (px < 1) {
        nx = px;
        ny = 0;
      } else if (px < 2) {
        nx = 1;
        ny = px - 1;
      } else if (px < 3) {
        nx = 2 - px;
        ny = 1;
      } else {
        nx = 0;
        ny = 3 - px;
      }
      const corner = 0.55 + 0.45 * Math.sin(s.seed * 9.1);
      const rx = (nx * 2 - 1) * scale * corner;
      const ry = (ny * 2 - 1) * scale * corner;
      return { x: w / 2 + rx * w * 0.5, y: h / 2 + ry * h * 0.5, travel };
    };

    return {
      draw(ctx, size, tick) {
        if (stars.length === 0) seed();
        const { width: w, height: h } = size;
        ctx.clearRect(0, 0, w, h);

        // The streams first, so the stars sit on top of them. Four polylines
        // across the window's width, each a sum of two sines in x so the curve
        // is a function of time and not a list of remembered points.
        ctx.lineWidth = 1;
        for (let i = 0; i < STREAMS; i++) {
          const baseY = h * (0.18 + 0.64 * (i / (STREAMS - 1)));
          const phase = i * 2.1;
          ctx.strokeStyle = withAlpha(mix(indigo, violet, i / (STREAMS - 1)), 0.14);
          ctx.beginPath();
          for (let step = 0; step <= 24; step++) {
            const x = (step / 24) * w;
            const y =
              baseY +
              wobble(phase, tick.seconds + step * 0.4, 0.5) * h * 0.06 +
              Math.sin(tick.seconds * 0.3 + phase) * h * 0.015;
            if (step === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
          }
          ctx.stroke();
        }

        // The stars. Size and brightness both rise as a star travels outward,
        // which is what sells depth; a star that only moved would read as a
        // slide. The alpha ramp is the other half: invisible at the vanishing
        // point, at full strength in the outer half, and gone by the edge, so
        // the field is the window's margins rather than a cluster in the middle.
        for (const s of stars) {
          const p = project(s, w, h, tick.seconds);
          const rise = Math.min(1, Math.max(0, (p.travel - 0.12) / 0.18));
          const fall = 1 - Math.min(1, Math.max(0, (p.travel - 0.82) / 0.18));
          const alpha = rise * fall * 0.5 * (0.4 + 0.6 * s.seed);
          if (alpha < 0.015) continue;
          const r = 0.4 + p.travel * 1.6;
          ctx.fillStyle = withAlpha(mix(indigo, violet, s.seed), alpha);
          ctx.beginPath();
          ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
          ctx.fill();
        }
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
