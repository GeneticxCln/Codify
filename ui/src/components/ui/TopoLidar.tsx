import React from "react";
import {
  ATMOSPHERE_FPS,
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * Topographic Lidar: morphing elevation contour curves in both gutters with a
 * sweeping copper lidar scan line that highlights terrain bands as it passes.
 */
export type TopoLidarProps = AtmosphereCanvasProps;

const BANDS = 7;
const STEPS = 28;

export function createTopoLidarPainter(
  read: (name: string) => string,
  active: boolean | (() => boolean),
): AtmospherePainter {
  const isActive = typeof active === "function" ? active : () => active;

  return {
    draw(ctx, size, tick) {
      const line = channelsOf(read("--lidar-line"));
      const scan = channelsOf(read("--lidar-scan"));
      const { width: w, height: h } = size;
      const busy = isActive();
      const t = tick.seconds;

      ctx.clearRect(0, 0, w, h);
      ctx.lineCap = "round";

      // Sweeping horizontal lidar scan height across the gutters
      const scanY = ((t * (busy ? 0.24 : 0.11)) % 1) * h;
      const scanBand = h * 0.085;

      for (const isLeft of [true, false]) {
        for (let b = 0; b < BANDS; b++) {
          const frac = (b + 0.5) / BANDS;
          const baseDepth = (0.02 + frac * 0.16) * w;
          const seed = b * 1.13 + (isLeft ? 0.4 : 3.1);
          const fade = Math.max(0.2, 1 - frac * 0.65);

          ctx.beginPath();
          for (let s = 0; s <= STEPS; s++) {
            const u = s / STEPS;
            const y = u * h;
            const ridge =
              Math.sin(u * 5.2 + t * 0.28 + seed) * (w * 0.014) +
              Math.cos(u * 9.8 - t * 0.19 + seed * 1.6) * (w * 0.007);
            const depth = Math.max(4, Math.min(w * 0.215, baseDepth + ridge));
            const x = isLeft ? depth : w - depth;
            if (s === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
          }
          ctx.strokeStyle = withAlpha(line, (b % 3 === 0 ? 0.42 : 0.24) * fade);
          ctx.lineWidth = b % 3 === 0 ? 1.5 : 1;
          ctx.stroke();

          // Elevation ping tick where the lidar scan intersects this contour
          const uScan = scanY / Math.max(1, h);
          const ridgeAtScan =
            Math.sin(uScan * 5.2 + t * 0.28 + seed) * (w * 0.014) +
            Math.cos(uScan * 9.8 - t * 0.19 + seed * 1.6) * (w * 0.007);
          const depthAtScan = Math.max(4, Math.min(w * 0.215, baseDepth + ridgeAtScan));
          const px = isLeft ? depthAtScan : w - depthAtScan;
          ctx.fillStyle = withAlpha(scan, (busy ? 0.8 : 0.52) * fade);
          ctx.beginPath();
          ctx.arc(px, scanY, busy ? 2.4 : 1.8, 0, Math.PI * 2);
          ctx.fill();
        }

        // Subtle sweeping lidar scan bar confined strictly to the gutter
        const x0 = isLeft ? 0 : w * 0.8;
        const x1 = isLeft ? w * 0.2 : w;
        const grad = ctx.createLinearGradient(0, scanY - scanBand, 0, scanY + scanBand);
        grad.addColorStop(0, withAlpha(scan, 0));
        grad.addColorStop(0.5, withAlpha(scan, busy ? 0.16 : 0.08));
        grad.addColorStop(1, withAlpha(scan, 0));
        ctx.fillStyle = grad;
        ctx.fillRect(x0, Math.max(0, scanY - scanBand), x1 - x0, scanBand * 2);

        ctx.strokeStyle = withAlpha(scan, busy ? 0.58 : 0.34);
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(x0, scanY);
        ctx.lineTo(x1, scanY);
        ctx.stroke();
      }
    },
  };
}

export const TopoLidar: React.FC<TopoLidarProps> = ({
  className = "",
  maxDimension = 1024,
  fps = ATMOSPHERE_FPS,
  animated,
  active = false,
}) => {
  const activeRef = React.useRef(active);
  activeRef.current = active;
  const vars = {
    "--lidar-line": "#7ee8a8",
    "--lidar-scan": "#f4a261",
  };

  const canvasRef = useAtmosphereCanvas({
    maxDimension,
    fps,
    animated,
    active,
    vars,
    create: (read) => createTopoLidarPainter(read, () => activeRef.current === true),
  });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
