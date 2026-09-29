import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * The Cyberpunk HUD theme: a flight-deck's furniture, not a simulation.
 *
 * Almost all of this is *static* — the corner brackets and the tick ladder do
 * not move, and that is the point. A HUD is a readout, and a readout that
 * moves is a screensaver. The only motion is the radar sweep, which is the one
 * element whose meaning is "something is out there", and even it is a slow
 * revolution on a fixed axis rather than a search pattern.
 *
 * The brief's "15% opacity" is treated as the *element* alpha and the theme
 * publishes the amber at full strength, because an alpha baked into a hex
 * cannot be varied per element and the sweep needs a brighter tail than the
 * brackets. The 15% lives here, as one constant, rather than in the palette.
 */
export type HudSweepProps = AtmosphereCanvasProps;

/** The brief's element opacity, in one place rather than in five call sites. */
const ELEMENT_ALPHA = 0.15;
/** Seconds for one full revolution of a sweep. */
const SWEEP_PERIOD = 7;

/**
 * The drawing logic, as a module-level factory rather than a closure inside
 * the component below.
 *
 * Nothing outside a component could reach a `create` defined in its body, and
 * the UI suite has no renderer and no canvas — so this, the part worth
 * testing, was the part no test could see. `ui/tests/atmosphere.test.ts`
 * drives it against a recording context.
 */
/**
 * The colours one frame is painted in, read per frame rather than once at
 * creation. A parameter rather than a closure over the factory because the two
 * helpers below live outside `draw` so they can be unit-driven — and a closure
 * would have forced them to capture the colours at *creation* time, leaving a
 * theme change or a custom tint three frames of chrome ahead of the sweep.
 */
interface HudPalette {
  amber: string;
  tick: string;
}

export function createHudSweepPainter(read: (name: string) => string): AtmospherePainter {
  const palette = (): HudPalette => ({
    amber: channelsOf(read("--hud-amber")),
    tick: channelsOf(read("--hud-tick")),
  });

  /** One radar: a ring, a fading tail, and a leading edge. */
  const radar = (
    ctx: CanvasRenderingContext2D,
    cx: number,
    cy: number,
    r: number,
    phase: number,
    seconds: number,
    { amber, tick }: HudPalette,
  ): void => {
    const sweep = (seconds / SWEEP_PERIOD + phase) % 1;
    const angle = sweep * Math.PI * 2;

    // Rings: two, and a crosshair, in the same faint amber as everything else.
    ctx.strokeStyle = withAlpha(amber, ELEMENT_ALPHA * 1.6);
    ctx.lineWidth = 1;
    for (const ring of [r, r * 0.55]) {
      ctx.beginPath();
      ctx.arc(cx, cy, ring, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.moveTo(cx - r, cy);
    ctx.lineTo(cx + r, cy);
    ctx.moveTo(cx, cy - r);
    ctx.lineTo(cx, cy + r);
    ctx.stroke();

    // The tail. Clipped to the radar, so it is 14 radial lines rather than a
    // sweep fill — a filled wedge needs a gradient per frame to fade across
    // its width, and 14 alpha steps look the same at this opacity.
    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.clip();
    const TAIL = 14;
    for (let i = 0; i < TAIL; i++) {
      const a = angle - (i / TAIL) * Math.PI * 0.85;
      const fade = (1 - i / TAIL) ** 2;
      ctx.strokeStyle = withAlpha(amber, fade * 0.3);
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a) * r * 0.2, cy + Math.sin(a) * r * 0.2);
      ctx.lineTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r);
      ctx.stroke();
    }
    ctx.restore();

    // The leading edge, brighter than the tail and in the pale gold.
    ctx.strokeStyle = withAlpha(tick, 0.42);
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(angle) * r, cy + Math.sin(angle) * r);
    ctx.stroke();
  };

  /** Corner brackets. Static, and inset far enough to clear the chrome. */
  const corners = (
    ctx: CanvasRenderingContext2D,
    w: number,
    h: number,
    // `tick` is deliberately not destructured: the brackets are the same amber
    // as everything else, and the ladder below is where the pale one is used.
    { amber }: HudPalette,
  ): void => {
    const inset = Math.min(w, h) * 0.045;
    const arm = Math.min(w, h) * 0.055;
    ctx.strokeStyle = withAlpha(amber, 0.42);
    ctx.lineWidth = 1.4;
    for (const [sx, sy] of [
      [1, 1],
      [-1, 1],
      [1, -1],
      [-1, -1],
    ] as const) {
      const x = sx > 0 ? inset : w - inset;
      const y = sy > 0 ? inset : h - inset;
      ctx.beginPath();
      ctx.moveTo(x + sx * arm, y);
      ctx.lineTo(x, y);
      ctx.lineTo(x, y + sy * arm);
      ctx.stroke();
    }
  };

  /** The tick ladder: short horizontal dashes down both margins. */
  const ticks = (
    ctx: CanvasRenderingContext2D,
    w: number,
    h: number,
    seconds: number,
    { amber, tick }: HudPalette,
  ): void => {
    const step = Math.max(14, h / 34);
    const len = Math.min(w, h) * 0.03;
    ctx.lineWidth = 1;
    for (let i = 0; i * step < h; i++) {
      const y = i * step + ((seconds * 9) % step);
      // Every fifth tick is long; the rest are stubs. A ladder of identical
      // dashes reads as a border, not as a scale.
      const long = i % 5 === 0;
      const alpha = (long ? 0.34 : 0.16) * (0.6 + 0.4 * Math.sin(i * 0.9 + seconds * 0.6));
      ctx.strokeStyle = withAlpha(long ? tick : amber, alpha);
      for (const x of [len * 0.5, w - len * 0.5]) {
        ctx.beginPath();
        ctx.moveTo(x - len / 2, y);
        ctx.lineTo(x + len / 2, y);
        ctx.stroke();
      }
    }
  };

  return {
    draw(ctx, size, tickAt) {
      // Resolved per frame rather than once at creation, so a theme or a custom
      // tint restyles this canvas on the next frame instead of waiting for a
      // remount. See the hook's note on live reads.
      const pal = palette();
      ctx.clearRect(0, 0, size.width, size.height);
      const { width: w, height: h } = size;
      const r = Math.min(w, h) * 0.16;
      // Two radars, on opposite margins, out of phase so the window never
      // has a single dead corner.
      radar(ctx, r * 1.6, h - r * 1.6, r, 0, tickAt.seconds, pal);
      radar(ctx, w - r * 1.6, r * 1.6, r, 0.5, tickAt.seconds, pal);
      corners(ctx, w, h, pal);
      ticks(ctx, w, h, tickAt.seconds, pal);
    },
  };
}

export const HudSweep: React.FC<HudSweepProps> = ({
  className = "",
  maxDimension = 480,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--hud-amber": "#f59e0b",
    "--hud-tick": "#fef3c7",
  };


  const canvasRef = useAtmosphereCanvas({ maxDimension, fps, animated, active, vars, create: createHudSweepPainter });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
