import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * The Bioluminescent Abyss theme: spores rising in the gutters, nowhere else.
 *
 * The brief's constraint is the whole design — *left and right screen gutters,
 * fading out before reaching the middle* — because the middle of this window is
 * where the conversation is. So the spawn band is a fixed fraction of the
 * width on each side, and a spore's opacity is a function of how far it has
 * wandered inward and how high it has climbed. Neither is capped by a timer: a
 * spore that reached the middle would be behind the text, and DESIGN.md's rule
 * is that a backdrop is never decoration under the words being read.
 *
 * "Ultra-soft" is two fills and no blur: a wide, very faint disc and a small
 * brighter core. `shadowBlur` is a per-draw blur pass and would cost more than
 * every other effect in the app put together; the softness here is a large
 * radius at a low alpha, which is what "soft" looked like before blur existed.
 */
export type AbyssSporesProps = AtmosphereCanvasProps;

interface Spore {
  /** 0 at the outer edge of a gutter, 1 at the middle of the window. */
  x: number;
  y: number;
  /** Rise in canvas pixels per second. Slow enough to miss if you watch it. */
  vy: number;
  /** Inward drift, so the fade is a drift and not just a clock. */
  vx: number;
  r: number;
  seed: number;
}

const SPORES = 44;
/** The gutters, as a fraction of the width. Nothing is spawned inside them. */
const GUTTER = 0.2;

/**
 * The drawing logic, as a module-level factory rather than a closure inside
 * the component below.
 *
 * Nothing outside a component could reach a `create` defined in its body, and
 * the UI suite has no renderer and no canvas — so this, the part worth
 * testing, was the part no test could see. `ui/tests/atmosphere.test.ts`
 * drives it against a recording context.
 */
export function createAbyssSporesPainter(read: (name: string) => string): AtmospherePainter {

  let spores: Spore[] = [];

  const seed = (w: number, h: number): void => {
    spores = Array.from({ length: SPORES }, () => {
      // Per spore, not per field: deciding the side once and reusing it gave
      // every spore the same gutter, which is half a theme — the right-hand
      // column stayed empty and the effect read as a left-hand border rather
      // than as a window with two margins. The pixel probe that caught it is
      // the same one in the test suite's spirit: sum a gutter, compare.
      const left = Math.random() < 0.5;
      return {
      x: left ? Math.random() * GUTTER * w : w - Math.random() * GUTTER * w,
      // Seeded across the full height, so the gutters are populated on the
      // first frame rather than filling in from the bottom.
      y: Math.random() * h,
      vy: -(4 + Math.random() * 9),
      vx: (left ? 1 : -1) * (1 + Math.random() * 3),
      r: 1 + Math.random() * 2.2,
      seed: Math.random() * 100,
      };
    });
  };

  return {
    step(dt, tick) {
      const { width: w, height: h } = tick.size;
      for (const s of spores) {
        s.y += s.vy * dt;
        // Inward drift always, but faster in some phases than others — the
        // fade is a *drift* the eye can follow, not a clock running out.
        s.x += s.vx * dt * (0.5 + 0.5 * Math.sin(tick.seconds * 0.5 + s.seed));
        if (s.y < -10) {
          // Recycle at the top into the gutter it came from, at the bottom
          // edge, which is where a rising spore "comes from" anyway.
          const left = s.x < w / 2;
          s.y = h + 10;
          s.x = left ? Math.random() * GUTTER * w : w - Math.random() * GUTTER * w;
        }
      }
    },
    draw(ctx, size, tick) {
      // Resolved per frame rather than once at creation, so a theme or a
      // custom tint restyles this canvas on the next frame instead of
      // waiting for a remount. See the hook's note on live reads.
        const spore = channelsOf(read("--abyss-spore"));
        const deep = channelsOf(read("--abyss-deep"));
      if (spores.length === 0) seed(size.width, size.height);
      const { width: w, height: h } = size;
      ctx.clearRect(0, 0, w, h);
      for (const s of spores) {
        // Fade on three axes at once: inward distance from the window's edge,
        // height (a spore that has climbed far has burnt out), and a slow
        // breath so the field is not a static constellation of dots.
        const inward = Math.min(s.x, w - s.x) / (w * GUTTER);
        const height = 1 - Math.min(1, Math.max(0, s.y / h));
        const breath = 0.55 + 0.45 * Math.sin(tick.seconds * 0.8 + s.seed);
        const alpha =
          Math.max(0, Math.min(1, inward)) ** 1.5 *
          (0.25 + 0.75 * height) *
          0.5 *
          breath;
        if (alpha < 0.01) continue;
        ctx.fillStyle = withAlpha(deep, alpha * 0.35);
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r * 4.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = withAlpha(spore, alpha * 0.75);
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fill();
      }
    },
  };
}

export const AbyssSpores: React.FC<AbyssSporesProps> = ({
  className = "",
  maxDimension = 480,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--abyss-spore": "#10b981",
    "--abyss-deep": "#059669",
  };


  const canvasRef = useAtmosphereCanvas({ maxDimension, fps, animated, active, vars, create: createAbyssSporesPainter });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
