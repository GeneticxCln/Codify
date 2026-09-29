import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * The Solarized Flare theme: solar wind running down the gutters of a monochrome
 * amber CRT.
 *
 * **The gutters, not the window.** The brief asks for flares "along the side
 * gutters" and that is the whole reason this reads as telemetry rather than as
 * a screensaver: the middle of this window is where the conversation is, and a
 * field of moving amber across it would be decoration under the words being
 * read. So the spawn band is a fixed fraction of the width on each side — the
 * same constraint `AbyssSpores` works under, and the same reason its opacity is
 * a function of how far inward a thing has wandered.
 *
 * **Low-frequency flares, not particles.** A flare is a *line* that grows, so
 * each one here is a vertical stroke whose length and brightness rise and fall
 * on its own slow clock, and several of them share a column so the gutter reads
 * as a continuous flow rather than as scattered sparks. The wind is one shared
 * number for the whole field, the way the snow's is: a hailstorm is a particle
 * emitter, weather leans.
 *
 * **What "thinking" looks like.** The rate rises through the hook's `active`
 * flag — the same 1.8× every other effect here uses — and on top of that the
 * flares gain a travelling heat pulse: a soft band that runs down each gutter
 * while the agent is working and is absent at rest. That is the one piece of
 * motion here that carries information, and §7 asks for exactly that; the
 * ambient drift is brand, like the rain's.
 */
export type SolarWindProps = AtmosphereCanvasProps;

interface Flare {
  /** Horizontal position inside its gutter, 0 at the window edge, 1 at the inner edge. */
  x: number;
  y: number;
  /** Length in canvas pixels. Grown each frame, not drawn at a fixed size. */
  len: number;
  /** How fast this one grows, px/s. */
  grow: number;
  /** Phase, so no two flares breathe together. */
  phase: number;
  /** Left or right gutter, so recycling returns it to the side it came from. */
  left: boolean;
}

const FLARES = 22;
/** The gutters, as a fraction of the width. Nothing is drawn inside them. */
const GUTTER = 0.19;
const MAX_LEN = 190;

/**
 * The drawing logic, as a module-level factory rather than a closure inside
 * the component below.
 *
 * Nothing outside a component could reach a `create` defined in its body, and
 * the UI suite has no renderer and no canvas — so this, the part worth
 * testing, was the part no test could see. `ui/tests/atmosphere.test.ts`
 * drives it against a recording context.
 *
 * `active` is a parameter for the reason the hook's own note gives about
 * `active`: it changes at runtime, and a dependency would restart the
 * effect. Here it is simply the one value the body used to close over.
 */
export function createSolarWindPainter(read: (name: string) => string, active: boolean): AtmospherePainter {

  let flares: Flare[] = [];

  const seed = (w: number, h: number): void => {
    flares = Array.from({ length: FLARES }, () => {
      const left = Math.random() < 0.5;
      return {
        // Per flare, not per column: deciding the side once and reusing it
        // filled only the left gutter and the effect read as a border.
        x: left
          ? Math.random() * GUTTER * w
          : w - Math.random() * GUTTER * w,
        y: Math.random() * h,
        len: 0,
        grow: 40 + Math.random() * 120,
        phase: Math.random() * Math.PI * 2,
        left,
      };
    });
  };

  return {
    step(dt, tick) {
      const { width: w, height: h } = tick.size;
      for (const f of flares) {
        // The 1.8× is the hook's own `active` rate, applied to growth: while a
        // turn is in flight the flares visibly run, which is the one thing
        // here that reports state. The clock is in seconds, so the cap on
        // frames cannot slow it.
        f.len += f.grow * dt * (active ? 1.8 : 1);
        f.y += 26 * dt;
        if (f.len > MAX_LEN || f.y > h + MAX_LEN) {
          f.len = 0;
          f.y = -Math.random() * h * 0.4;
          f.phase = Math.random() * Math.PI * 2;
          const left = f.left;
          f.x = left
            ? Math.random() * GUTTER * w
            : w - Math.random() * GUTTER * w;
        }
      }
    },
    draw(ctx, size, tick) {
      // Resolved per frame rather than once at creation, so a theme or a
      // custom tint restyles this canvas on the next frame instead of
      // waiting for a remount. See the hook's note on live reads.
        const core = channelsOf(read("--sun-core"));
        const edge = channelsOf(read("--sun-edge"));
      if (flares.length === 0) seed(size.width, size.height);
      const { width: w, height: h } = size;
      ctx.clearRect(0, 0, w, h);
      // A slow shared pulse, so the gutters brighten together the way a
      // display's phosphor does rather than each flare at its own moment.
      const beat = 0.6 + 0.4 * Math.sin(tick.seconds * 0.35);
      for (const f of flares) {
        const inward = Math.min(f.x, w - f.x) / (w * GUTTER);
        if (inward > 1.05) continue;
        // Brightest at the window's edge, gone by the middle: a flare that
        // survived into the transcript would be a bright amber line across
        // somebody's code.
        const fade = Math.max(0, 1 - inward) ** 1.6;
        const breathe = 0.45 + 0.55 * Math.sin(tick.seconds * 1.1 + f.phase);
        const alpha = fade * (0.18 + 0.5 * breathe) * beat;
        if (alpha < 0.02) continue;
        // A line, drawn as two strokes: a wide faint one and a thin bright
        // core. No `shadowBlur` — a per-draw blur pass over 22 strokes is more
        // expensive than every other effect in this app combined.
        ctx.lineCap = "round";
        ctx.strokeStyle = withAlpha(core, alpha * 0.35);
        ctx.lineWidth = 3.5;
        ctx.beginPath();
        ctx.moveTo(f.x, f.y);
        ctx.lineTo(f.x, f.y + f.len);
        ctx.stroke();
        ctx.strokeStyle = withAlpha(edge, alpha);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(f.x, f.y);
        ctx.lineTo(f.x, f.y + f.len);
        ctx.stroke();
      }
      if (!active) return;
      // The heat pulse. One soft band per gutter, travelling down, and only
      // while the agent is working — the single piece of this effect that
      // reports state rather than being weather.
      const bandY = ((tick.seconds * 0.55) % 1) * h;
      for (const side of [0, 1]) {
        const x0 = side === 0 ? 0 : w * (1 - GUTTER);
        const g = ctx.createLinearGradient(x0, bandY - 90, x0, bandY + 90);
        g.addColorStop(0, withAlpha(edge, 0));
        g.addColorStop(0.5, withAlpha(edge, 0.09));
        g.addColorStop(1, withAlpha(edge, 0));
        ctx.fillStyle = g;
        ctx.fillRect(x0, bandY - 90, w * GUTTER, 180);
      }
    },
  };
}

export const SolarWind: React.FC<SolarWindProps> = ({
  className = "",
  maxDimension = 1024,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--sun-core": "#ffb000",
    "--sun-edge": "#ffd700",
  };


  const canvasRef = useAtmosphereCanvas({ maxDimension, fps, animated, active, vars, create: (read) => createSolarWindPainter(read, active) });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
