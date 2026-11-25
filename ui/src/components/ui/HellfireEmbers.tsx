import React from "react";
import {
  ATMOSPHERE_FPS,
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * Diablo: gothic Sanctuary hellfire — rising crimson ash embers, glowing abyssal
 * fissures along the outer edges, and cursed gold rune diamonds pulsing in the
 * gutters. Roars brighter and rises faster while a run is in flight (`active`).
 */
export type HellfireEmbersProps = AtmosphereCanvasProps;

interface Cinder {
  u: number;
  y: number;
  vy: number;
  size: number;
  phase: number;
  left: boolean;
  rune: boolean;
}

const CINDERS = 46;

export function createHellfireEmbersPainter(
  read: (name: string) => string,
  active: boolean | (() => boolean),
): AtmospherePainter {
  const isActive = typeof active === "function" ? active : () => active;
  let cinders: Cinder[] = [];
  let seededH = 0;

  const seed = (h: number): void => {
    seededH = h;
    cinders = Array.from({ length: CINDERS }, (_, i) => {
      const f = ((i * 2654435761) >>> 0) / 4294967295;
      const f2 = (((i + 23) * 2246822519) >>> 0) / 4294967295;
      return {
        u: 0.02 + f * 0.16,
        y: f2 * h,
        vy: 20 + f * 44,
        size: 1.5 + f2 * 2.8,
        phase: f * Math.PI * 2,
        left: i % 2 === 0,
        rune: i % 6 === 0,
      };
    });
  };

  return {
    step(dt) {
      const surge = isActive() ? 1.9 : 1;
      for (const c of cinders) {
        c.y -= c.vy * dt * surge;
        c.phase += dt * 1.8 * surge;
        if (c.y < -14) c.y = seededH + 10;
      }
    },
    draw(ctx, size, tick) {
      const ember = channelsOf(read("--hellfire-ember"));
      const rune = channelsOf(read("--hellfire-rune"));
      const { width: w, height: h } = size;
      if (cinders.length === 0 || seededH !== h) seed(h);
      const busy = isActive();
      const t = tick.seconds;

      ctx.clearRect(0, 0, w, h);

      // Pulsing abyssal hellfire glow rising from the bottom of both gutters
      const pulse = 0.78 + 0.22 * Math.sin(t * (busy ? 3.2 : 1.4));
      const glowH = h * (busy ? 0.34 : 0.25);
      for (const isLeft of [true, false]) {
        const x0 = isLeft ? 0 : w * 0.8;
        const grad = ctx.createLinearGradient(0, h, 0, h - glowH);
        grad.addColorStop(0, withAlpha(ember, (busy ? 0.2 : 0.11) * pulse));
        grad.addColorStop(0.6, withAlpha(rune, (busy ? 0.06 : 0.03) * pulse));
        grad.addColorStop(1, withAlpha(ember, 0));
        ctx.fillStyle = grad;
        ctx.fillRect(x0, h - glowH, w * 0.2, glowH);

        // Jagged gothic ember fissure along the outer edge
        ctx.beginPath();
        const segs = 12;
        for (let i = 0; i <= segs; i++) {
          const u = i / segs;
          const y = h - u * glowH * 1.4;
          const jag =
            Math.sin(u * 13 + (isLeft ? 0.7 : 2.9) + t * 0.6) * (w * 0.012) +
            (0.03 + u * 0.045) * w;
          const depth = Math.max(4, Math.min(w * 0.18, jag));
          const x = isLeft ? depth : w - depth;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = withAlpha(ember, (busy ? 0.42 : 0.22) * pulse);
        ctx.lineWidth = busy ? 1.8 : 1.2;
        ctx.stroke();
      }

      // Rising hellfire cinders & cursed gold diamond rune sparks
      for (const c of cinders) {
        const swirl = Math.sin(c.phase + t * 0.9) * (w * 0.014);
        const depth = Math.max(6, Math.min(w * 0.2, c.u * w + swirl));
        const x = c.left ? depth : w - depth;
        const heightFade = Math.max(0.18, Math.min(1, c.y / (h * 0.88)));
        const flicker = 0.6 + 0.4 * Math.sin(c.phase * 2.4);

        if (c.rune) {
          // Gothic diamond rune sigil spark in cursed gold
          const r = c.size * (busy ? 1.45 : 1.15);
          ctx.strokeStyle = withAlpha(rune, (busy ? 0.82 : 0.56) * heightFade * flicker);
          ctx.lineWidth = 1.2;
          ctx.beginPath();
          ctx.moveTo(x, c.y - r * 1.3);
          ctx.lineTo(x + r, c.y);
          ctx.lineTo(x, c.y + r * 1.3);
          ctx.lineTo(x - r, c.y);
          ctx.closePath();
          ctx.stroke();
          continue;
        }

        // Ember aura + hot core
        ctx.fillStyle = withAlpha(ember, (busy ? 0.28 : 0.18) * heightFade * flicker);
        ctx.beginPath();
        ctx.arc(x, c.y, c.size * 2.2, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = withAlpha(
          c.size > 3.1 ? rune : ember,
          (busy ? 0.92 : 0.72) * heightFade * flicker,
        );
        ctx.beginPath();
        ctx.arc(x, c.y, c.size * 0.9, 0, Math.PI * 2);
        ctx.fill();
      }
    },
  };
}

export const HellfireEmbers: React.FC<HellfireEmbersProps> = ({
  className = "",
  maxDimension = 1024,
  fps = ATMOSPHERE_FPS,
  animated,
  active = false,
}) => {
  const activeRef = React.useRef(active);
  activeRef.current = active;
  const vars = {
    "--hellfire-ember": "#ff595e",
    "--hellfire-rune": "#f2b84b",
  };

  const canvasRef = useAtmosphereCanvas({
    maxDimension,
    fps,
    animated,
    active,
    vars,
    create: (read) => createHellfireEmbersPainter(read, () => activeRef.current === true),
  });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
