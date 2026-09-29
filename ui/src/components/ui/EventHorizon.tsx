import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * The Deep Void Event Horizon theme: orbital arcs curving away at the margins.
 *
 * **Orbits, not lines.** Every other weather here either falls or rises. This
 * one turns, and turning is the whole of the theme — so each particle is given
 * an orbit rather than a velocity: an angle, a radius, and an angular speed,
 * which means the field bends in a way a drifting particle cannot fake. Gravity's
 * signature is a curve that is not the curve of its own velocity.
 *
 * **The disc is centred on the chat column, and only the gutters are drawn.**
 * The obvious reading of "an accretion disc" is a centre far away with rings
 * around it, which is a mistake here for a reason that is worth recording: a
 * centre placed *below* the window puts the whole disc off-screen, because the
 * only part of a disc low enough to be a disc's crown is the part directly above
 * the centre — which is the transcript, not a gutter. So the centre is the middle
 * of the window, each orbit is cut into a left arc and a right arc by the gutter
 * fade, and the disc is *behind* the conversation rather than across it. What
 * the reader sees is the two arcs at each edge, which is the thing the brief
 * actually asked for.
 *
 * **They vanish at the edges rather than wrapping.** A particle that reaches the
 * side of the window is *gone* — the brief says streams "disappearing into the
 * edges", and that is also what keeps the middle of the screen clear. Respawning
 * it on the far side would put a bright arc crossing the transcript, which is the
 * thing every other gutter effect here refuses to do.
 *
 * **Double speed, a hot core and a pair of jets, while the agent works.** The
 * orbital rate is multiplied by 2 — a bigger step than the 1.8× the other effects
 * use, because the whole field is one rotation and 1.8× would be a difference
 * nobody could see — the tight inner orbits flare as matter falls in, and jets
 * ignite at the two limbs. All three are information: they mean a tool call is in
 * flight, and a rate that merely crept upward would be decoration that happened
 * to be correlated with work.
 */
export type EventHorizonProps = AtmosphereCanvasProps;

interface Mote {
  /** Angle on its orbit, radians. */
  a: number;
  /** Orbit radius as a fraction of `RADIUS` times the window's width. */
  r: number;
  /** Angular speed, rad/s, signed: negative orbits the other way. */
  spin: number;
  /** Radius of the drawn dot. */
  dot: number;
  /** 1 at the tightest orbit and 0 at the widest, so brightness carries depth. */
  near: number;
  /** Per-mote brightness jitter, so 90 orbits do not glow identically. */
  depth: number;
}

const MOTES = 90;
/**
 * Orbit radii as fractions of the disc's scale: a tight bright core and a wide
 * faint dust, which is what makes this read as an accretion disc rather than as
 * a clock face. The wide end overshoots 1 so the outer orbits leave the window
 * through the corners and are swallowed there.
 */
const R_MIN = 0.68;
const R_MAX = 1.1;
/**
 * The disc's scale, as a fraction of the window's **width**.
 *
 * Width, not height, because the arcs live in the left and right gutters and the
 * width is the axis an orbit has to span to reach them. A scale derived from the
 * height leaves a wide window with orbits that never arrive at a gutter.
 */
const RADIUS = 0.62;
/**
 * The disc is inclined, so an orbit's vertical extent is this fraction of its
 * horizontal one. Near enough to a circle that an orbit sweeps *down* a gutter
 * rather than skimming it, which is what puts arcs at every height in the margin
 * instead of only in the corners.
 */
const INCLINE = 0.52;
/** The gutters, as a fraction of the width per side. Nothing is drawn inside. */
const GUTTER = 0.24;
/**
 * A streak is the last few samples of the mote's own orbit, not a stored trail:
 * the orbit is a known ellipse, so the point the mote occupied `s` seconds ago is
 * that ellipse evaluated at `a - spin * s`. Nothing is buffered, and the streak
 * comes out *longer for a faster mote* and shorter for a slow one, which is what
 * makes the field read as streams of different energy rather than as 90
 * identical marks. A single dot cannot do this at all — the curve is the entire
 * premise of the theme, and a dot has none.
 */
const TRAIL = 10;
/** How far back in time a streak reaches, in seconds. */
const TRAIL_SECONDS = 0.5;

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
export function createEventHorizonPainter(read: (name: string) => string, active: boolean): AtmospherePainter {

  let motes: Mote[] = [];

  const seed = (): void => {
    motes = Array.from({ length: MOTES }, () => {
      const near = Math.random();
      return {
        a: Math.random() * Math.PI * 2,
        r: R_MIN + near * (R_MAX - R_MIN),
        // Slow, and slower for the wide orbits, so the field turns as a spiral
        // rather than as a rigid disc.
        spin: (Math.random() < 0.5 ? -1 : 1) * (0.06 + Math.random() * 0.14),
        dot: 0.6 + Math.random() * 1.5,
        near,
        depth: Math.random(),
      };
    });
  };

  return {
    step(dt) {
      const rate = active ? 2 : 1;
      for (const m of motes) {
        m.a += m.spin * dt * rate * (1 - m.r * 0.45);
      }
    },
    draw(ctx, size, tick) {
      // Resolved per frame rather than once at creation, so a theme or a
      // custom tint restyles this canvas on the next frame instead of
      // waiting for a remount. See the hook's note on live reads.
        const arc = channelsOf(read("--void-arc"));
        const dust = channelsOf(read("--void-dust"));
        const beam = channelsOf(read("--void-beam"));
      if (motes.length === 0) seed();
      const { width: w, height: h } = size;
      ctx.clearRect(0, 0, w, h);

      const cx = w / 2;
      const cy = h / 2;
      const R = w * RADIUS;

      for (const m of motes) {
        const rx = m.r * R;
        const ry = m.r * R * INCLINE;
        const rate = active ? 2 : 1;
        const spin = m.spin * rate;
        const base = (0.1 + 0.46 * m.near) * (0.35 + 0.65 * m.depth);
        // Matter falling in: the tight orbits flare while a run is in flight.
        const heat = active ? 1 + 1.1 * m.near : 1;
        const colour = m.near > 0.55 ? arc : dust;

        // Walk backwards along the orbit, oldest sample first, keeping only
        // what is visible. A sample can be dropped in the middle of a streak —
        // the head has just crossed a gutter line — so this cannot be a single
        // pre-filtered path, but it also never has a *hole*: a trail spans a
        // fraction of a radian, far too little to reach from one gutter to the
        // other, so a kept streak is always one unbroken piece of orbit.
        const pts: Array<{ x: number; y: number; a: number }> = [];
        for (let k = TRAIL; k >= 1; k--) {
          const a = m.a - (spin * TRAIL_SECONDS * k) / TRAIL;
          const x = cx + Math.cos(a) * rx;
          const y = cy + Math.sin(a) * ry;
          // Past the edge of the window the mote has been swallowed, and this is
          // where it stops existing rather than wrapping to the far side.
          if (x < -20 || x > w + 20 || y < -20 || y > h + 20) continue;
          // Only the gutters are painted, so one orbit becomes a left arc and a
          // right arc with nothing between them. `t` runs 1 at the window edge
          // to 0 at the inner gutter line, and the smoothstep is what keeps
          // that inner end from reading as a vertical rule down the screen.
          const t = 1 - Math.min(x, w - x) / (w * GUTTER);
          if (t <= 0) continue;
          pts.push({ x, y, a: Math.min(0.9, base * t * t * (3 - 2 * t) * heat) });
        }
        if (pts.length < 2) continue;

        // One stroke per streak, tapered by a gradient along its own length
        // rather than by a stroke per segment: nine strokes per mote is nine
        // draw calls per mote, and the taper is the same shape either way.
        const head = pts[pts.length - 1];
        const tail = pts[0];
        const g = ctx.createLinearGradient(tail.x, tail.y, head.x, head.y);
        g.addColorStop(0, withAlpha(colour, tail.a * 0.2));
        g.addColorStop(1, withAlpha(colour, head.a));
        ctx.strokeStyle = g;
        ctx.lineWidth = m.dot;
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(tail.x, tail.y);
        for (let k = 1; k < pts.length; k++) ctx.lineTo(pts[k].x, pts[k].y);
        ctx.stroke();

        // A small bright head, so the direction of travel is readable at a
        // glance — a tapered streak alone only says *something* is moving.
        if (head.a < 0.05) continue;
        ctx.fillStyle = withAlpha(colour, head.a);
        ctx.beginPath();
        ctx.arc(head.x, head.y, m.dot * 0.7, 0, Math.PI * 2);
        ctx.fill();
      }

      if (!active) return;
      /**
       * Jets, at the two limb points of the innermost orbit and only while
       * work is in flight. A limb point is the one place on an orbit the viewer
       * sees edge-on, which is also where a real accretion disc flares. The
       * streak runs along the orbit's own tangent rather than outward from the
       * centre, because a spoke from the middle of the window is a starburst
       * across the transcript — the one shape this theme is not. They are fixed
       * rather than precessing: a beam that travels around reads as a starburst
       * too, and the point is that it *ignites*.
       */
      const limb = R * R_MIN;
      const pulse = 0.6 + 0.4 * Math.sin(tick.seconds * 2.4);
      for (const x of [cx - limb, cx + limb]) {
        const g = ctx.createLinearGradient(x, cy, x, cy - h * 0.2);
        g.addColorStop(0, withAlpha(beam, 0.26 * pulse));
        g.addColorStop(1, withAlpha(beam, 0));
        ctx.strokeStyle = g;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x, cy);
        ctx.lineTo(x, cy - h * 0.2);
        ctx.stroke();
      }
    },
  };
}

export const EventHorizon: React.FC<EventHorizonProps> = ({
  className = "",
  maxDimension = 1024,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--void-arc": "#c084fc",
    "--void-dust": "#64748b",
    "--void-beam": "#a855f7",
  };


  const canvasRef = useAtmosphereCanvas({ maxDimension, fps, animated, active, vars, create: (read) => createEventHorizonPainter(read, active) });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
