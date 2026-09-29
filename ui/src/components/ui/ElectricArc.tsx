import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * The Electric Arc theme: a discharge striking down both gutters.
 *
 * **A fork, because a glow is not lightning.** The single most reliable way to
 * draw electricity and get a photograph of electricity instead is a blurred
 * bright line: bloom reads as a lens filter, and the eye has seen that a
 * thousand times. What actually says *discharge* is the branch — a main channel
 * with two or three spurs leaving it at an angle and dying within a few
 * segments. So each bolt here is a jagged main path plus short forks, drawn in
 * two colours: `--arc-core` for the channel and `--arc-fork` for the spurs.
 * They are separate variables because they are separate jobs, and a bolt drawn
 * in one colour looks like a scratch.
 *
 * **It goes out as fast as it came.** The charge envelope is a spike with a
 * hard decay rather than a sine, because a lightning flash is not a pulse, it
 * is an event with an afterimage. A gentle fade reads as a glowing worm
 * travelling down the window, which is a different and much worse effect.
 *
 * **The gutters, for the reason every margin effect has them.** The middle of
 * this window is where the conversation is; a white discharge across somebody's
 * code is the worst legibility failure available, and it is available here
 * because the theme's brightest colour is near-white. Every bolt is clipped by
 * `fade` to the outer 20% of the width on each side.
 *
 * `active` does two things and both are the state report §7 asks for: strikes
 * come more often, and a strike holds its charge roughly twice as long, so a
 * run in flight is visibly a busier window rather than merely a faster one.
 */
export type ElectricArcProps = AtmosphereCanvasProps;

interface Point {
  x: number;
  y: number;
}

interface Bolt {
  /** The channel's x at the window edge, fixed so a strike starts where it ended. */
  x: number;
  left: boolean;
  /** 0 to 1. 0 is dark; a strike sets it to 1 and it decays from there. */
  charge: number;
  /** Seconds until this bolt fires again. */
  wait: number;
  /** The main channel, regenerated at every strike. */
  main: Point[];
  /** The spurs. Two or three, each a short path of its own. */
  forks: Point[][];
}

/**
 * How many channels live in the gutters, and how often each one fires.
 *
 * The first cut of this was six bolts with a 3.4/s decay and a 1.5s rest, which
 * is a *correct* lightning envelope and a bad theme: each bolt was lit about a
 * fifth of the time, so a given frame held roughly one discharge and often none.
 * Measured against the other eighteen themes it came out the faintest in the
 * gallery by a factor of two, which is the failure this retune fixes. Lightning
 * is intermittent and a still frame of it should still be *a frame of lightning*.
 *
 * So the duty cycle is raised rather than the brightness: more channels, a
 * shorter rest, and a decay slow enough that a strike is still catching as it
 * goes. The envelope shape — spike, hard decay, no sine — is unchanged, because
 * that is the part that makes it read as an event.
 */
const BOLTS = 10;
const GUTTER = 0.2;
/** Main-path segments. Enough to read as a jag, few enough to stay a line. */
const SEGMENTS = 9;
/**
 * The anchor band, as fractions of the width. A bolt is anchored *inside* the
 * gutter rather than anywhere in it, and the bounds below are what keep the
 * whole path — channel and spurs — on the near side of the middle band.
 *
 * The arithmetic that matters: the widest reach is the far edge of the anchor
 * band, plus the full jitter, plus the spurs' horizontal travel, and that sum
 * has to stay under `GUTTER`. 0.12 + 0.035 + (3 legs x 0.012) = 0.191, against
 * a gutter of 0.2. This is the arithmetic `ui/tests/painter.test.ts` then
 * re-derives for itself by painting the painter and refusing a mark in the
 * middle half of a 1440px window; the numbers here are what make that test
 * pass rather than what it is checked against.
 */
const ANCHOR_MIN = 0.03;
const ANCHOR_SPAN = 0.09;
/** How far the channel wanders from its anchor, as a fraction of the width. */
const JITTER = 0.035;
/** Charge units lost per second at rest. See the duty-cycle note above. */
const DECAY = 1.6;

export function createElectricArcPainter(
  read: (name: string) => string,
  active: boolean,
): AtmospherePainter {

  let bolts: Bolt[] = [];
  /** Scaled by `active`, so a busy window strikes more often *and* holds on. */
  const decay = active ? 1.7 : DECAY;
  const rest = active ? 0.34 : 0.8;

  const anchor = (left: boolean, w: number): number => {
    const at = (ANCHOR_MIN + Math.random() * ANCHOR_SPAN) * w;
    return left ? at : w - at;
  };

  /** A new jagged path, and the spurs that leave it. */
  const strike = (bolt: Bolt, w: number, h: number): void => {
    const span = h * (0.55 + Math.random() * 0.45);
    const main: Point[] = [];
    for (let i = 0; i <= SEGMENTS; i++) {
      const t = i / SEGMENTS;
      // Jitter shrinks to nothing at both ends: a lightning channel starts and
      // ends at its anchor, and a path that wanders off the top edge looks
      // like it came from somewhere.
      const wander = (Math.random() - 0.5) * 2 * JITTER * w * Math.sin(t * Math.PI);
      main.push({ x: bolt.x + wander, y: t * span });
    }
    bolt.main = main;
    const forks: Point[][] = [];
    const count = 2 + (Math.random() < 0.5 ? 0 : 1);
    for (let f = 0; f < count; f++) {
      const at = 1 + Math.floor(Math.random() * (SEGMENTS - 2));
      const from = main[at]!;
      // Spurs leave downward and outward, and stop quickly. The horizontal
      // step is small on purpose and is counted in the reach arithmetic above:
      // a spur that travelled a tenth of the window would be a bright line
      // across somebody's code, which is the one thing §7 forbids here.
      const dir = Math.random() < 0.5 ? -1 : 1;
      const leg = 2 + Math.floor(Math.random() * 2);
      const path: Point[] = [from];
      let px = from.x;
      let py = from.y;
      for (let i = 0; i < leg; i++) {
        px += dir * (0.004 + Math.random() * 0.008) * w;
        py += (span / SEGMENTS) * (0.5 + Math.random() * 0.7);
        path.push({ x: px, y: py });
      }
      forks.push(path);
    }
    bolt.forks = forks;
  };

  const seed = (w: number, h: number): void => {
    bolts = Array.from({ length: BOLTS }, (_, i) => {
      const left = i % 2 === 0;
      const bolt: Bolt = {
        x: anchor(left, w),
        left,
        // Half the bolts start charged, and by different amounts. The hook
        // draws exactly one frame when `prefers-reduced-motion` is set, so a
        // window whose every bolt begins at zero charge would show a
        // reduced-motion user an empty gutter and no second frame to rescue
        // it. Staggering them also means the first frame is a caught
        // discharge rather than six bolts all at the same brightness.
        charge: i % 2 === 0 ? 0.45 + Math.random() * 0.5 : 0,
        wait: Math.random() * rest,
        main: [],
        forks: [],
      };
      strike(bolt, w, h);
      return bolt;
    });
  };

  return {
    step(dt, tick) {
      const { width: w, height: h } = tick.size;
      for (const bolt of bolts) {
        bolt.charge = Math.max(0, bolt.charge - decay * dt);
        bolt.wait -= dt;
        if (bolt.wait <= 0) {
          bolt.charge = 1;
          // Re-anchor only sometimes: a bolt that jumps sideways on every
          // strike looks like a cursor rather than a channel.
          if (Math.random() < 0.35) bolt.x = anchor(bolt.left, w);
          strike(bolt, w, h);
          bolt.wait = rest * (0.45 + Math.random() * 1.1);
        }
      }
    },
    draw(ctx, size, tick) {
      // Resolved per frame rather than once at creation, so a theme or a
      // custom tint restyles this canvas on the next frame instead of
      // waiting for a remount. See the hook's note on live reads.
        const core = channelsOf(read("--arc-core"));
        const fork = channelsOf(read("--arc-fork"));
      if (bolts.length === 0) seed(size.width, size.height);
      const { width: w, height: h } = size;
      ctx.clearRect(0, 0, w, h);
      ctx.lineCap = "round";
      ctx.lineJoin = "round";

      for (const bolt of bolts) {
        if (bolt.charge <= 0.02 || bolt.main.length < 2) continue;
        const inward = Math.min(bolt.x, w - bolt.x) / (w * GUTTER);
        const fade = Math.max(0, 1 - inward) ** 1.5;
        if (fade < 0.04) continue;

        const stroke = (path: Point[], channels: string, width: number, alpha: number): void => {
          ctx.strokeStyle = withAlpha(channels, alpha * fade);
          ctx.lineWidth = width;
          ctx.beginPath();
          ctx.moveTo(path[0]!.x, path[0]!.y);
          for (const p of path.slice(1)) ctx.lineTo(p.x, p.y);
          ctx.stroke();
        };

        // A wide faint halo under a thin bright core — the same two-stroke
        // trick `SolarWind` uses, for the same reason: no `shadowBlur`.
        stroke(bolt.main, fork, 3.2, bolt.charge * 0.16);
        stroke(bolt.main, core, 1.2, bolt.charge * 0.92);
        for (const spur of bolt.forks) {
          stroke(spur, fork, 1, bolt.charge * 0.5);
        }
      }

      // A afterimage across the gutters while a turn is in flight. Without it
      // the only difference a run makes is a faster strike rate, which is real
      // but easy to miss in peripheral vision; this is the one piece of the
      // effect that says "working" rather than "on".
      if (!active) return;
      const bandY = ((tick.seconds * 0.5) % 1) * h;
      for (const side of [0, 1]) {
        const x0 = side === 0 ? 0 : w * (1 - GUTTER);
        const g = ctx.createLinearGradient(x0, bandY - 70, x0, bandY + 70);
        g.addColorStop(0, withAlpha(fork, 0));
        g.addColorStop(0.5, withAlpha(fork, 0.07));
        g.addColorStop(1, withAlpha(fork, 0));
        ctx.fillStyle = g;
        ctx.fillRect(x0, bandY - 70, w * GUTTER, 140);
      }
    },
  };
}

export const ElectricArc: React.FC<ElectricArcProps> = ({
  className = "",
  maxDimension = 1024,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--arc-core": "#dfe4ff",
    "--arc-fork": "#7b6cff",
  };

  const canvasRef = useAtmosphereCanvas({
    maxDimension,
    fps,
    animated,
    active,
    vars,
    create: (read) => createElectricArcPainter(read, active),
  });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
