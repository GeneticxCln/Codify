import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * The Liquid Mercury / Nanotech Matrix theme: viscous metallic waves running up
 * and down the window's margins.
 *
 * **A wave, not a particle.** Every other effect here is a thing that moves; a
 * fluid is a *surface* that moves, and the difference is visible. So each band
 * here is a horizontal cross-section of a sum of sines evaluated at that band's
 * own height — the classic standing-wave sum — and the whole band is stroked as
 * one polyline. That is what gives it a crest and a trough rather than a row of
 * dots, and why the bands shear against each other instead of staying in
 * parallel.
 *
 * **Viscous means slow.** The phase speed is low and the amplitudes are small,
 * because a fluid that moves quickly reads as water and this is meant to read
 * as something metallic and heavy. The `ACTIVE_RATE` multiplier applies on top,
 * so a turn in flight makes the field *turbulent* rather than merely faster:
 * a second, higher-frequency term is faded in, which is the brief's "nanotech
 * particles coalesce into high-frequency wave patterns", and it is the one
 * piece of motion here that reports state.
 *
 * **The margins, because of what is behind them.** The bands ride up the left
 * and right edges and are cut off before the middle. A full-width field of
 * moving metal in front of a transcript would be the worst offender in the app
 * for legibility, and every gutter effect in this codebase refuses it for the
 * same reason.
 */
export type NanofluidProps = AtmosphereCanvasProps;

/** Horizontal bands, each one polyline across its gutter. */
const BANDS = 26;
/** The gutters, as a fraction of the width. Nothing is drawn inside them. */
const GUTTER = 0.22;

interface Band {
  /** Where the band sits vertically, 0..1. */
  at: number;
  /** Rise in fractions of the window's height per second. */
  rise: number;
  /** Amplitude in canvas pixels, and its own slow phase. */
  amp: number;
  phase: number;
  /** Wavelength in canvas pixels, so bands are not all in step. */
  wave: number;
  left: boolean;
}

const TAU = Math.PI * 2;

/** The height of the fluid's surface at one band, in canvas pixels. */
function surface(b: Band, y: number, x: number, t: number, w: number, chaos: number): number {
  const k = TAU / b.wave;
  const slow = Math.sin(k * x + t * 0.5 + b.phase);
  const cross = Math.sin(k * 0.6 * y + b.phase * 1.7) * 0.35;
  // The chaos term only exists while the agent is working, and it is a
  // *higher* frequency: that is the difference between a fluid moving quickly
  // and a fluid breaking up.
  const fast = chaos > 0
    ? Math.sin(k * 2.7 * x - t * 1.9 + b.phase * 3.1) * chaos
    : 0;
  return (slow + cross + fast) * b.amp * (0.6 + 0.4 * (w / 1400));
}

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
export function createNanofluidPainter(read: (name: string) => string, active: boolean): AtmospherePainter {

  let bands: Band[] = [];
  // Eased rather than switched: a jump from calm to turbulent on the first
  // frame of a run is a flash, and a flash is worse than the effect.
  let chaos = 0;

  const seed = (): void => {
    bands = Array.from({ length: BANDS }, (_, i) => {
      const left = i % 2 === 0;
      return {
        at: (i / BANDS) * 1.2 - 0.1,
        rise: (0.035 + Math.random() * 0.05) * (Math.random() < 0.5 ? -1 : 1),
        amp: 3 + Math.random() * 7,
        phase: Math.random() * TAU,
        wave: 90 + Math.random() * 150,
        left,
      };
    });
  };

  return {
    step(dt) {
      const target = active ? 1 : 0;
      chaos += (target - chaos) * Math.min(1, dt * 1.6);
      for (const b of bands) {
        b.at += b.rise * dt * (active ? 1.8 : 1);
        // A band that leaves the window comes back at the other end rather
        // than being removed, so the gutter never has a visible seam in it.
        if (b.at > 1.2) b.at = -0.2;
        if (b.at < -0.2) b.at = 1.2;
      }
    },
    draw(ctx, size, tick) {
      // Resolved per frame rather than once at creation, so a theme or a
      // custom tint restyles this canvas on the next frame instead of
      // waiting for a remount. See the hook's note on live reads.
        const crest = channelsOf(read("--fluid-crest"));
        const trough = channelsOf(read("--fluid-trough"));
      if (bands.length === 0) seed();
      const { width: w, height: h } = size;
      ctx.clearRect(0, 0, w, h);
      ctx.lineJoin = "round";
      ctx.lineCap = "round";

      for (const b of bands) {
        const y = b.at * h;
        const x0 = b.left ? 0 : w * (1 - GUTTER);
        const x1 = b.left ? w * GUTTER : w;
        // The two families are drawn in different inks, which is what makes a
        // metal liquid read as *metal*: the trough carries the dark body of
        // the fluid and the crest is the thin highlight on top of it.
        ctx.strokeStyle = withAlpha(trough, 0.5);
        ctx.lineWidth = 5;
        ctx.beginPath();
        for (let x = x0; x <= x1; x += 6) {
          const yy = y + surface(b, y, x, tick.seconds, w, chaos);
          if (x === x0) ctx.moveTo(x, yy);
          else ctx.lineTo(x, yy);
        }
        ctx.stroke();

        ctx.strokeStyle = withAlpha(crest, 0.16 + 0.3 * chaos);
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let x = x0; x <= x1; x += 6) {
          const yy = y + surface(b, y, x, tick.seconds, w, chaos);
          if (x === x0) ctx.moveTo(x, yy);
          else ctx.lineTo(x, yy);
        }
        ctx.stroke();
      }
    },
  };
}

export const Nanofluid: React.FC<NanofluidProps> = ({
  className = "",
  maxDimension = 1024,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--fluid-crest": "#e2e8f0",
    "--fluid-trough": "#334155",
  };


  const canvasRef = useAtmosphereCanvas({ maxDimension, fps, animated, active, vars, create: (read) => createNanofluidPainter(read, active) });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
