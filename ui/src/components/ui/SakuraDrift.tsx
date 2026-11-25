import React from "react";
import {
  ATMOSPHERE_FPS,
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * Sakura Midnight: sparse cherry-blossom petals drifting down both gutters
 * beside soft warm lantern motes. Gusts carry the petals briskly when `active`.
 */
export type SakuraDriftProps = AtmosphereCanvasProps;

interface Petal {
  u: number;
  y: number;
  vy: number;
  size: number;
  angle: number;
  spin: number;
  sway: number;
  left: boolean;
  lantern: boolean;
}

const COUNT = 38;

export function createSakuraDriftPainter(
  read: (name: string) => string,
  active: boolean | (() => boolean),
): AtmospherePainter {
  const isActive = typeof active === "function" ? active : () => active;
  let petals: Petal[] = [];
  let seededH = 0;

  const seed = (h: number): void => {
    seededH = h;
    petals = Array.from({ length: COUNT }, (_, i) => {
      const f = ((i * 2654435761) >>> 0) / 4294967295;
      const f2 = (((i + 31) * 1597334677) >>> 0) / 4294967295;
      return {
        u: 0.025 + f * 0.155,
        y: f2 * h,
        vy: 14 + f * 22,
        size: 3 + f2 * 3.5,
        angle: f * Math.PI * 2,
        spin: (f2 - 0.5) * 1.4,
        sway: f * Math.PI * 2,
        left: i % 2 === 0,
        lantern: i % 5 === 0,
      };
    });
  };

  return {
    step(dt) {
      const gust = isActive() ? 1.85 : 1;
      for (const p of petals) {
        p.y += p.vy * dt * gust;
        p.angle += p.spin * dt * gust;
        p.sway += dt * 1.1 * gust;
        if (p.y > seededH + 14) p.y = -12;
      }
    },
    draw(ctx, size, tick) {
      const petal = channelsOf(read("--sakura-petal"));
      const lantern = channelsOf(read("--sakura-lantern"));
      const { width: w, height: h } = size;
      if (petals.length === 0 || seededH !== h) seed(h);
      const busy = isActive();

      ctx.clearRect(0, 0, w, h);

      for (const p of petals) {
        const drift = Math.sin(p.sway + tick.seconds * 0.5) * (w * 0.015);
        const depth = Math.max(8, Math.min(w * 0.205, p.u * w + drift));
        const x = p.left ? depth : w - depth;

        if (p.lantern) {
          // Warm glowing lantern mote
          const pulse = 0.55 + 0.45 * Math.sin(p.sway * 1.3);
          ctx.fillStyle = withAlpha(lantern, (busy ? 0.2 : 0.12) * pulse);
          ctx.beginPath();
          ctx.arc(x, p.y, p.size * 1.7, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = withAlpha(lantern, (busy ? 0.72 : 0.52) * pulse);
          ctx.beginPath();
          ctx.arc(x, p.y, p.size * 0.55, 0, Math.PI * 2);
          ctx.fill();
          continue;
        }

        // Tapered blossom petal polygon (no ctx.translate needed, exact coords recorded)
        const c = Math.cos(p.angle);
        const s = Math.sin(p.angle);
        const rx = p.size;
        const ry = p.size * 0.52;
        ctx.fillStyle = withAlpha(petal, busy ? 0.72 : 0.54);
        ctx.beginPath();
        ctx.moveTo(x + rx * c, yOff(p.y, rx * s));
        ctx.quadraticCurveTo(
          x - ry * s,
          p.y + ry * c,
          x - rx * c,
          yOff(p.y, -rx * s),
        );
        ctx.quadraticCurveTo(
          x + ry * s,
          p.y - ry * c,
          x + rx * c,
          yOff(p.y, rx * s),
        );
        ctx.fill();
      }
    },
  };
}

function yOff(y: number, dy: number): number {
  return y + dy;
}

export const SakuraDrift: React.FC<SakuraDriftProps> = ({
  className = "",
  maxDimension = 1024,
  fps = ATMOSPHERE_FPS,
  animated,
  active = false,
}) => {
  const activeRef = React.useRef(active);
  activeRef.current = active;
  const vars = {
    "--sakura-petal": "#ff9ec7",
    "--sakura-lantern": "#ffd27d",
  };

  const canvasRef = useAtmosphereCanvas({
    maxDimension,
    fps,
    animated,
    active,
    vars,
    create: (read) => createSakuraDriftPainter(read, () => activeRef.current === true),
  });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
