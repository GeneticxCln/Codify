import React from "react";
import {
  ATMOSPHERE_FPS,
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * Oscilloscope CRT: Tektronix phosphor traces and calibrated graticule ticks
 * framed in the left and right margins. While a run is in flight (`active`),
 * the harmonic modulation intensifies and the trigger channel illuminates.
 */
export type ScopeTraceProps = AtmosphereCanvasProps;

const SAMPLES = 44;

export function createScopeTracePainter(
  read: (name: string) => string,
  active: boolean | (() => boolean),
): AtmospherePainter {
  const isActive = typeof active === "function" ? active : () => active;

  return {
    draw(ctx, size, tick) {
      const trace = channelsOf(read("--scope-trace"));
      const grid = channelsOf(read("--scope-grid"));
      const { width: w, height: h } = size;
      const busy = isActive();
      const t = tick.seconds;

      ctx.clearRect(0, 0, w, h);
      ctx.lineCap = "round";

      for (const isLeft of [true, false]) {
        const axisX = isLeft ? w * 0.1 : w * 0.9;
        const amp = w * (busy ? 0.068 : 0.048);

        // Subtle graticule center spine and division ticks
        ctx.strokeStyle = withAlpha(grid, 0.14);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(axisX, h * 0.04);
        ctx.lineTo(axisX, h * 0.96);
        for (let i = 1; i <= 11; i++) {
          const y = (i / 12) * h;
          const half = i % 3 === 0 ? 7 : 3.5;
          ctx.moveTo(axisX - half, y);
          ctx.lineTo(axisX + half, y);
        }
        ctx.stroke();

        // Channel A: primary phosphor harmonic trace
        const freqA = busy ? 11.5 : 7.5;
        const speedA = busy ? 2.6 : 1.3;
        const phaseOffset = isLeft ? 0 : Math.PI * 0.65;

        ctx.beginPath();
        for (let i = 0; i <= SAMPLES; i++) {
          const u = i / SAMPLES;
          const y = u * h;
          const env = Math.sin(u * Math.PI);
          const v =
            Math.sin(u * freqA - t * speedA + phaseOffset) * 0.72 +
            Math.sin(u * freqA * 2.1 + t * speedA * 0.7) * 0.28;
          const x = Math.max(6, Math.min(w - 6, axisX + v * amp * (0.35 + 0.65 * env)));
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = withAlpha(trace, busy ? 0.22 : 0.12);
        ctx.lineWidth = 5;
        ctx.stroke();
        ctx.strokeStyle = withAlpha(trace, busy ? 0.78 : 0.56);
        ctx.lineWidth = 1.6;
        ctx.stroke();

        // Channel B: amber trigger harmonic trace
        ctx.beginPath();
        for (let i = 0; i <= SAMPLES; i++) {
          const u = i / SAMPLES;
          const y = u * h;
          const env = Math.sin(u * Math.PI);
          const v = Math.cos(u * 5.2 + t * (busy ? 1.9 : 0.9) - phaseOffset);
          const x = Math.max(6, Math.min(w - 6, axisX + v * (amp * 0.6) * (0.3 + 0.7 * env)));
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = withAlpha(grid, busy ? 0.55 : 0.32);
        ctx.lineWidth = 1.25;
        ctx.stroke();
      }
    },
  };
}

export const ScopeTrace: React.FC<ScopeTraceProps> = ({
  className = "",
  maxDimension = 1024,
  fps = ATMOSPHERE_FPS,
  animated,
  active = false,
}) => {
  const activeRef = React.useRef(active);
  activeRef.current = active;
  const vars = {
    "--scope-trace": "#2ef2c4",
    "--scope-grid": "#ffbe55",
  };

  const canvasRef = useAtmosphereCanvas({
    maxDimension,
    fps,
    animated,
    active,
    vars,
    create: (read) => createScopeTracePainter(read, () => activeRef.current === true),
  });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
