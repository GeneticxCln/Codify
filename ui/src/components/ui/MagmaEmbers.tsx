import React from "react";
import {
  ATMOSPHERE_FPS,
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * Volcanic Obsidian: incandescent embers rising through thermal drafts along
 * both gutters over a warm caldera edge glow. Surges upward when `active`.
 */
export type MagmaEmbersProps = AtmosphereCanvasProps;

interface Ember {
  u: number;
  y: number;
  vy: number;
  size: number;
  phase: number;
  left: boolean;
  hot: boolean;
}

const COUNT = 44;

export function createMagmaEmbersPainter(
  read: (name: string) => string,
  active: boolean | (() => boolean),
): AtmospherePainter {
  const isActive = typeof active === "function" ? active : () => active;
  let embers: Ember[] = [];
  let seededH = 0;

  const seed = (h: number): void => {
    seededH = h;
    embers = Array.from({ length: COUNT }, (_, i) => {
      const f = ((i * 2654435761) >>> 0) / 4294967295;
      const f2 = (((i + 17) * 2246822519) >>> 0) / 4294967295;
      return {
        u: 0.02 + f * 0.16,
        y: f2 * h,
        vy: 18 + f * 38,
        size: 1.4 + f2 * 2.4,
        phase: f * Math.PI * 2,
        left: i % 2 === 0,
        hot: i % 3 === 0,
      };
    });
  };

  return {
    step(dt) {
      const boost = isActive() ? 1.85 : 1;
      for (const e of embers) {
        e.y -= e.vy * dt * boost;
        e.phase += dt * (1.4 * boost);
        if (e.y < -12) {
          e.y = seededH + 8;
        }
      }
    },
    draw(ctx, size, tick) {
      const ember = channelsOf(read("--magma-ember"));
      const glow = channelsOf(read("--magma-glow"));
      const { width: w, height: h } = size;
      if (embers.length === 0 || seededH !== h) seed(h);
      const busy = isActive();

      ctx.clearRect(0, 0, w, h);

      // Subtle caldera warmth along the bottom corners of both gutters
      const bandH = h * 0.24;
      for (const isLeft of [true, false]) {
        const x0 = isLeft ? 0 : w * 0.8;
        const grad = ctx.createLinearGradient(0, h, 0, h - bandH);
        grad.addColorStop(0, withAlpha(ember, busy ? 0.16 : 0.09));
        grad.addColorStop(1, withAlpha(ember, 0));
        ctx.fillStyle = grad;
        ctx.fillRect(x0, h - bandH, w * 0.2, bandH);
      }

      for (const e of embers) {
        const drift = Math.sin(e.phase + tick.seconds * 0.8) * (w * 0.012);
        const depth = Math.max(5, Math.min(w * 0.2, e.u * w + drift));
        const x = e.left ? depth : w - depth;
        const heightFade = Math.max(0.15, Math.min(1, e.y / (h * 0.85)));
        const flicker = 0.65 + 0.35 * Math.sin(e.phase * 2.1);
        const col = e.hot ? glow : ember;

        // Soft halo
        ctx.fillStyle = withAlpha(ember, (busy ? 0.28 : 0.18) * heightFade * flicker);
        ctx.beginPath();
        ctx.arc(x, e.y, e.size * 2.3, 0, Math.PI * 2);
        ctx.fill();

        // Bright ember core
        ctx.fillStyle = withAlpha(col, (busy ? 0.9 : 0.72) * heightFade * flicker);
        ctx.beginPath();
        ctx.arc(x, e.y, e.size, 0, Math.PI * 2);
        ctx.fill();
      }
    },
  };
}

export const MagmaEmbers: React.FC<MagmaEmbersProps> = ({
  className = "",
  maxDimension = 1024,
  fps = ATMOSPHERE_FPS,
  animated,
  active = false,
}) => {
  const activeRef = React.useRef(active);
  activeRef.current = active;
  const vars = {
    "--magma-ember": "#ff7a3d",
    "--magma-glow": "#ffc14d",
  };

  const canvasRef = useAtmosphereCanvas({
    maxDimension,
    fps,
    animated,
    active,
    vars,
    create: (read) => createMagmaEmbersPainter(read, () => activeRef.current === true),
  });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
