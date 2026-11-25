import React from "react";
import {
  ATMOSPHERE_FPS,
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * Aurora Borealis: undulating polar light curtains along both gutters.
 *
 * Vertical luminous drapery ribbons sway gently along the left and right edges,
 * layering mint (`--aurora-mint`) lower curtains with polar-violet
 * (`--aurora-violet`) upper shimmer. While a run is in flight (`active`), the
 * curtains ripple faster and brighten their leading crests.
 */
export type AuroraCurtainProps = AtmosphereCanvasProps;

const RIBBONS_PER_SIDE = 5;
const SEGMENTS = 24;

export function createAuroraCurtainPainter(
  read: (name: string) => string,
  active: boolean | (() => boolean),
): AtmospherePainter {
  const isActive = typeof active === "function" ? active : () => active;

  return {
    draw(ctx, size, tick) {
      const mint = channelsOf(read("--aurora-mint"));
      const violet = channelsOf(read("--aurora-violet"));
      const { width: w, height: h } = size;
      const busy = isActive();
      const t = tick.seconds;

      ctx.clearRect(0, 0, w, h);
      ctx.lineCap = "round";

      for (const side of [-1, 1] as const) {
        const isLeft = side === -1;
        for (let r = 0; r < RIBBONS_PER_SIDE; r++) {
          const frac = (r + 0.5) / RIBBONS_PER_SIDE;
          const baseDepth = (0.025 + frac * 0.145) * w;
          const swayAmp = (0.012 + (r % 3) * 0.006) * w;
          const phase = r * 1.37 + (isLeft ? 0 : 2.4);
          const speed = (0.42 + r * 0.09) * (busy ? 1.45 : 1);
          const edgeFade = Math.max(0.18, 1 - frac * 0.72);
          const colour = r % 2 === 0 ? mint : violet;

          // Wide soft curtain veil
          ctx.beginPath();
          for (let i = 0; i <= SEGMENTS; i++) {
            const u = i / SEGMENTS;
            const y = u * h;
            const wave =
              Math.sin(u * 4.2 + t * speed + phase) * swayAmp +
              Math.sin(u * 8.5 - t * speed * 0.65 + phase * 1.7) * (swayAmp * 0.35);
            const depth = Math.max(4, Math.min(w * 0.21, baseDepth + wave));
            const x = isLeft ? depth : w - depth;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
          }
          ctx.strokeStyle = withAlpha(colour, (busy ? 0.16 : 0.1) * edgeFade);
          ctx.lineWidth = Math.max(8, w * 0.016);
          ctx.stroke();

          // Crisp luminous aurora filament along the same wave
          ctx.strokeStyle = withAlpha(
            r % 2 === 0 ? mint : violet,
            (busy ? 0.5 : 0.34) * edgeFade,
          );
          ctx.lineWidth = 1.6;
          ctx.stroke();
        }
      }
    },
  };
}

export const AuroraCurtain: React.FC<AuroraCurtainProps> = ({
  className = "",
  maxDimension = 1024,
  fps = ATMOSPHERE_FPS,
  animated,
  active = false,
}) => {
  const activeRef = React.useRef(active);
  activeRef.current = active;
  const vars = {
    "--aurora-mint": "#4ef2c2",
    "--aurora-violet": "#a78bfa",
  };

  const canvasRef = useAtmosphereCanvas({
    maxDimension,
    fps,
    animated,
    active,
    vars,
    create: (read) => createAuroraCurtainPainter(read, () => activeRef.current === true),
  });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
