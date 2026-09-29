import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * Falling snow, for the Winter Snow and Festive Night themes.
 *
 * **One painter, two themes, and the difference is a variable rather than a
 * fork.** Two snow themes that shared nothing but their component would be two
 * files that had to be kept in step forever, and the obvious way to tell them
 * apart — a second canvas — is the expensive one. So the *shape* of the weather
 * is written once here, and a theme says what it is by publishing colour:
 *
 * - `--snow-flake` is the trigger. Its presence means "this theme wants snow",
 *   and both themes publish it.
 * - `--snow-flake-2` is a second flake colour, for a palette that needs one —
 *   a bluer flake in winter, a faintly green one under Christmas lights. The
 *   painter mixes the two by depth, so a field of one colour is never quite
 *   flat.
 * - `--snow-glow` and `--snow-glow-2` are **the whole difference between the two
 *   themes.** When a theme publishes a glow, the field grows warm lights rising
 *   through the snow. Winter Snow publishes none, so it is quiet and cold; Festive
 *   Night publishes gold and berry, so the same snowfall has something lit in it.
 *
 * That is the `--cmatrix-glyphs` pattern the ASCII rain already established — a
 * monochrome theme is a token edit rather than a forked component — and it is
 * why this is a single file for two themes and not the sixth time somebody copies
 * the rain.
 *
 * ## What is drawn, and why it is not six hundred dots
 *
 * Three depth tiers. A near flake is larger, faster and more opaque than a far
 * one, and that is the only thing that makes a flat field read as *depth* rather
 * than as a texture. The tiers are a fixed count each rather than a random
 * radius per flake, because a random radius is what produces a handful of
 * snowflakes so large they read as bugs.
 *
 * The drift is a velocity, not an accumulated offset: each flake's horizontal
 * speed is a sine of the clock, so a flake can never walk itself off the side of
 * the window over a long session — which is the failure a field of "x += Math.sin"
 * only shows after an hour of reading code and an hour of watching.
 */
export type SnowFallProps = AtmosphereCanvasProps;

interface Flake {
  x: number;
  y: number;
  /** Radius in canvas pixels. Tier decides it. */
  r: number;
  /** Fall speed, canvas pixels per second. Larger for nearer flakes. */
  vy: number;
  /** Peak horizontal speed from this flake's own sway, px/s. */
  sway: number;
  /** Phase offsets, so no two flakes are the same flake. */
  phase: number;
  /** 0 far … 1 near. Drives size, speed and opacity together. */
  depth: number;
  /** Which of the two flake colours this one is. */
  warm: boolean;
}

interface Light {
  x: number;
  y: number;
  r: number;
  vy: number;
  phase: number;
  /** Which of the two glow colours. */
  second: boolean;
}

/** Per tier: how many, how big, how fast, how faint. Three of each, summed. */
const TIERS: ReadonlyArray<{ n: number; r: number; vy: number; alpha: number }> = [
  // 160 arcs per frame, which is nothing for a canvas and the difference between
  // "snow" and "a few specks". The first version of this was 96 and looked thin
  // on a 1440px window, because the canvas is capped at 1024 and then scaled up:
  // a count that reads as dense on the canvas reads as sparse on the screen.
  { n: 92, r: 0.7, vy: 9, alpha: 0.3 },
  { n: 50, r: 1.4, vy: 17, alpha: 0.5 },
  { n: 18, r: 2.5, vy: 30, alpha: 0.72 },
];

/** Rising warm lights, only when a theme publishes a glow colour. */
const LIGHTS = 15;

const isHex = (value: string): boolean => /^#[0-9a-f]{6}$/i.test(value.trim());

/**
 * The drawing logic, as a module-level factory rather than a closure inside
 * the component below.
 *
 * Nothing outside a component could reach a `create` defined in its body, and
 * the UI suite has no renderer and no canvas — so this, the part worth
 * testing, was the part no test could see. `ui/tests/atmosphere.test.ts`
 * drives it against a recording context.
 */
export function createSnowFallPainter(read: (name: string) => string): AtmospherePainter {
  /**
   * This theme's five snow variables, read per frame.
   *
   * A function rather than a straight block because two of them are *derived*:
   * the second flake falls back to the first, and the second glow to the first,
   * when a theme publishes only one. Keeping the derivation next to the reads
   * means a tint that changes `--snow-flake` moves the fallback with it.
   */
  const palette = () => {
    const flakeA = channelsOf(read("--snow-flake"));
    const second = read("--snow-flake-2");
    const flakeB = isHex(second) ? channelsOf(second) : flakeA;
    const glowRaw = read("--snow-glow");
    const glow2Raw = read("--snow-glow-2");
    const hasGlow = isHex(glowRaw);
    const glowA = hasGlow ? channelsOf(glowRaw) : "";
    const glowB = isHex(glow2Raw) ? channelsOf(glow2Raw) : glowA;
    return { flakeA, flakeB, glowA, glowB, hasGlow };
  };

  let flakes: Flake[] = [];
  let lights: Light[] = [];

  const seed = (w: number, h: number): void => {
    // Re-read rather than take a parameter: `seed` runs on the first frame and
    // on every resize, and it needs `hasGlow` to decide whether the warm lights
    // exist at all — a fact about the *current* theme, not about the painter.
    const { hasGlow } = palette();
    flakes = TIERS.flatMap((tier) =>
      Array.from({ length: tier.n }, () => {
        const depth = Math.random();
        return {
          // Seeded across the whole window, so the first frame is a full field
          // rather than snow arriving from the top edge over the first second.
          x: Math.random() * w,
          y: Math.random() * h,
          r: tier.r,
          vy: tier.vy * (0.75 + 0.5 * depth),
          sway: 4 + Math.random() * 9,
          phase: Math.random() * Math.PI * 2,
          depth,
          warm: Math.random() < 0.32,
        };
      }),
    );
    lights = hasGlow
      ? Array.from({ length: LIGHTS }, () => ({
          x: Math.random() * w,
          y: Math.random() * h,
          // 1.2–3.4, not 2–5.5. The first version drew the halo at
          // `r * 7`, which on a 5px light is a 35px disc: a screen of soft
          // gold bokeh that read as a lens effect rather than as lights. A
          // light is small, and it is the *core* that carries the colour.
          r: 1.2 + Math.random() * 2.2,
          vy: -(5 + Math.random() * 9),
          phase: Math.random() * Math.PI * 2,
          second: Math.random() < 0.35,
        }))
      : [];
  };

  return {
    step(dt, tick) {
      const { width: w, height: h } = tick.size;
      // The wind is one number for the whole field, and it changes slowly. A
      // per-flake random horizontal speed is a hailstorm; a field that leans
      // together is weather.
      const wind = Math.sin(tick.seconds * 0.11) * 5 + 3.5;
      for (const f of flakes) {
        f.y += f.vy * dt;
        f.x += (wind + Math.sin(tick.seconds * 0.6 + f.phase) * f.sway) * dt;
        if (f.y > h + f.r * 2) {
          f.y = -f.r * 2;
          f.x = Math.random() * w;
        } else if (f.x > w + 20) {
          f.x = -20;
        } else if (f.x < -20) {
          f.x = w + 20;
        }
      }
      for (const l of lights) {
        l.y += l.vy * dt;
        l.x += (wind * 0.6 + Math.sin(tick.seconds * 0.4 + l.phase) * 3) * dt;
        if (l.y < -l.r * 6) {
          l.y = h + l.r * 6;
          l.x = Math.random() * w;
        } else if (l.x > w + 30) {
          l.x = -30;
        } else if (l.x < -30) {
          l.x = w + 30;
        }
      }
    },
    draw(ctx, size, tick) {
      // Resolved per frame rather than once at creation, so a theme or a custom
      // tint restyles this canvas on the next frame instead of waiting for a
      // remount. See the hook's note on live reads.
      const { flakeA, flakeB, glowA, glowB } = palette();
      if (flakes.length === 0) seed(size.width, size.height);
      const { width: w, height: h } = size;
      ctx.clearRect(0, 0, w, h);

      for (const f of flakes) {
        const tier = TIERS.findIndex((t) => t.r === f.r);
        const base = tier < 0 ? TIERS[0].alpha : TIERS[tier].alpha;
        const alpha = base * (0.55 + 0.45 * f.depth);
        if (alpha < 0.02) continue;
        ctx.fillStyle = withAlpha(f.warm ? flakeB : flakeA, alpha);
        ctx.beginPath();
        ctx.arc(f.x, f.y, f.r, 0, Math.PI * 2);
        ctx.fill();
      }

      for (const l of lights) {
        // Two fills, no blur: a wide faint disc and a small bright core, the
        // same "soft before shadowBlur existed" trick the abyss spores use. A
        // per-draw `shadowBlur` pass would cost more than every other effect
        // in this app put together, for lights that are 2–6px across.
        const twinkle =
          0.35 + 0.65 * (0.5 + 0.5 * Math.sin(tick.seconds * 0.7 + l.phase));
        const colour = l.second ? glowB : glowA;
        ctx.fillStyle = withAlpha(colour, 0.13 * twinkle);
        ctx.beginPath();
        ctx.arc(l.x, l.y, l.r * 3.2, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = withAlpha(colour, 0.7 * twinkle);
        ctx.beginPath();
        ctx.arc(l.x, l.y, l.r * 0.7, 0, Math.PI * 2);
        ctx.fill();
      }
    },
  };
}

export const SnowFall: React.FC<SnowFallProps> = ({
  className = "",
  maxDimension = 1024,
  fps = 30,
  active = false,
  animated,
}) => {
  // Names, not values — the hook resolves them against a live getComputedStyle,
  // so a theme applied while this canvas runs restyles it on the next frame.
  // The two glows default to empty, which is the mechanism: a theme that
  // publishes neither gets no lights, and no branch on the theme's id anywhere.
  const vars = {
    "--snow-flake": "#e8f4ff",
    "--snow-flake-2": "",
    "--snow-glow": "",
    "--snow-glow-2": "",
  };


  const canvasRef = useAtmosphereCanvas({ maxDimension, fps, animated, active, vars, create: createSnowFallPainter });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
