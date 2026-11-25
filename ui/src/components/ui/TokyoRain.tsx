import React from "react";
import {
  ATMOSPHERE_FPS,
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * Tokyo Rain: diagonal neon rain streaks and expanding warm sodium-streetlight
 * water ripples along both gutters. Intensifies when `active`.
 */
export type TokyoRainProps = AtmosphereCanvasProps;

interface Drop {
  u: number;
  y: number;
  len: number;
  vy: number;
  left: boolean;
}

interface Ripple {
  u: number;
  y: number;
  age: number;
  life: number;
  maxR: number;
  left: boolean;
}

const DROPS = 42;
const RIPPLES = 10;

export function createTokyoRainPainter(
  read: (name: string) => string,
  active: boolean | (() => boolean),
): AtmospherePainter {
  const isActive = typeof active === "function" ? active : () => active;
  let drops: Drop[] = [];
  let ripples: Ripple[] = [];
  let seededH = 0;

  const seed = (h: number): void => {
    seededH = h;
    drops = Array.from({ length: DROPS }, (_, i) => {
      const f = ((i * 2654435761) >>> 0) / 4294967295;
      const f2 = (((i + 19) * 2246822519) >>> 0) / 4294967295;
      return {
        u: 0.025 + f * 0.155,
        y: f2 * h,
        len: 14 + f * 22,
        vy: 150 + f2 * 160,
        left: i % 2 === 0,
      };
    });
    ripples = Array.from({ length: RIPPLES }, (_, i) => {
      const f = (((i + 7) * 1597334677) >>> 0) / 4294967295;
      const f2 = (((i + 41) * 2654435761) >>> 0) / 4294967295;
      const life = 1.6 + f * 1.4;
      return {
        u: 0.04 + f * 0.13,
        y: (0.1 + f2 * 0.8) * h,
        age: f2 * life,
        life,
        maxR: 12 + f * 14,
        left: i % 2 === 0,
      };
    });
  };

  return {
    step(dt) {
      const boost = isActive() ? 1.8 : 1;
      for (const d of drops) {
        d.y += d.vy * dt * boost;
        if (d.y > seededH + d.len) d.y = -d.len;
      }
      for (const r of ripples) {
        r.age += dt * boost;
        if (r.age > r.life) {
          r.age -= r.life;
          r.y = ((r.y + seededH * 0.37) % Math.max(1, seededH * 0.84)) + seededH * 0.08;
        }
      }
    },
    draw(ctx, size) {
      const streak = channelsOf(read("--rain-streak"));
      const ripple = channelsOf(read("--rain-ripple"));
      const { width: w, height: h } = size;
      if (drops.length === 0 || seededH !== h) seed(h);
      const busy = isActive();

      ctx.clearRect(0, 0, w, h);
      ctx.lineCap = "round";

      // Diagonal rain streaks confined strictly inside each gutter
      for (const d of drops) {
        const depth = d.u * w;
        const x0 = d.left ? depth : w - depth;
        const slant = d.left ? -d.len * 0.18 : d.len * 0.18;
        const x1 = Math.max(2, Math.min(w - 2, x0 + slant));
        const fade = Math.max(0.2, 1 - d.u / 0.22);
        ctx.strokeStyle = withAlpha(streak, (busy ? 0.56 : 0.38) * fade);
        ctx.lineWidth = busy ? 1.35 : 1.05;
        ctx.beginPath();
        ctx.moveTo(x0, d.y);
        ctx.lineTo(x1, d.y + d.len);
        ctx.stroke();
      }

      // Concentric sodium-streetlight water ripples on glass
      for (const r of ripples) {
        const progress = Math.max(0, Math.min(1, r.age / r.life));
        const radius = Math.min(w * 0.03, 2 + progress * r.maxR);
        const depth = Math.max(radius + 4, Math.min(w * 0.21 - radius, r.u * w));
        const cx = r.left ? depth : w - depth;
        const alpha = (1 - progress) * (busy ? 0.62 : 0.42);
        ctx.strokeStyle = withAlpha(ripple, alpha);
        ctx.lineWidth = 1.25;
        ctx.beginPath();
        ctx.arc(cx, r.y, radius, 0, Math.PI * 2);
        ctx.stroke();
      }
    },
  };
}

export const TokyoRain: React.FC<TokyoRainProps> = ({
  className = "",
  maxDimension = 1024,
  fps = ATMOSPHERE_FPS,
  animated,
  active = false,
}) => {
  const activeRef = React.useRef(active);
  activeRef.current = active;
  const vars = {
    "--rain-streak": "#5ce1ff",
    "--rain-ripple": "#ffba52",
  };

  const canvasRef = useAtmosphereCanvas({
    maxDimension,
    fps,
    animated,
    active,
    vars,
    create: (read) => createTokyoRainPainter(read, () => activeRef.current === true),
  });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
