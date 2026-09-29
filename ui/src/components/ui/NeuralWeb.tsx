import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
  type AtmosphereSize,
  type AtmosphereTick,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, mix, withAlpha } from "../../canvasColour";

/**
 * The Neural Constellation theme: a slow web of points and the faint lines
 * between them, which brightens while the agent is working.
 *
 * The "thinking" pulse is the only part of any atmosphere that is not purely
 * decorative, and it is the reason the effect exists rather than a starfield.
 * It is driven by a **ref**, not a prop on the hook's dependency list: the
 * simulation has drift and a slow crawl, and re-running the effect to pick up a
 * boolean would restart the whole web mid-flight — a visible jump on the exact
 * frame the user wanted to watch. A boolean that only decides how bright a line
 * is can afford to be a frame stale; a restarted simulation cannot.
 *
 * Lines are drawn as strokes between every pair inside a radius, which is
 * O(n²) but n is 34 and the whole frame is under fifty strokes. The alternative
 * — a spatial hash — is a second data structure to be wrong, for a cost that is
 * not yet visible. The nodes are two fills each (a wide dim one and a small
 * bright one) rather than a `shadowBlur`, which is a per-node blur pass and the
 * single most expensive thing a canvas like this can do.
 */
export type NeuralWebProps = AtmosphereCanvasProps;

interface Node {
  x: number;
  y: number;
  /** Drift per second, in canvas pixels. Deliberately tiny. */
  vx: number;
  vy: number;
  r: number;
  /** Phase offset so the glow is not synchronised across the web. */
  seed: number;
}

const NODES = 54;
/** Link radius in canvas pixels; a pair inside this gets a line. */
const LINK = 178;

/**
 * The drawing logic, as a module-level factory rather than a closure inside
 * the component below.
 *
 * Nothing outside a component could reach a `create` defined in its body, and
 * the UI suite has no renderer and no canvas — so this, the part worth
 * testing, was the part no test could see. `ui/tests/atmosphere.test.ts`
 * drives it against a recording context.
 */
export function createNeuralWebPainter(
  read: (name: string) => string,
  /** Read the flag at paint time, not capture it. */
  isActive: () => boolean,
): AtmospherePainter {  let nodes: Node[] = [];
  let energy = 0;
  let pulsePhase = 0;

  const seedNodes = (size: AtmosphereSize): void => {
    nodes = Array.from({ length: NODES }, () => ({
      // Kept inside a margin so no node sits under the window's very edge,
      // where the chrome is and where a link would be clipped mid-stroke.
      x: (0.06 + Math.random() * 0.88) * size.width,
      y: (0.08 + Math.random() * 0.84) * size.height,
      vx: (Math.random() - 0.5) * 5,
      vy: (Math.random() - 0.5) * 5,
      r: 1 + Math.random() * 1.8,
      seed: Math.random() * 100,
    }));
  };

  return {
    step(dt: number, tick: AtmosphereTick): void {
      // Energy follows the flag rather than snapping to it, so starting a run
      // is a swell over about a second rather than a flash.
      energy += ((isActive() ? 1 : 0) - energy) * Math.min(1, dt * 1.6);
      if (isActive()) {
        pulsePhase = (pulsePhase + dt / 2.2) % 1;
      } else {
        pulsePhase = 0;
      }
      // Bounce off a margin rather than wrapping: a wrapping node visibly
      // teleports across the web, which reads as a glitch. The bounds are in
      // canvas pixels, which is what the node coordinates are — a window
      // measurement here would be wrong by exactly the cap factor, silently,
      // on every display whose device pixel ratio is not 1.
      const { width, height } = tick.size;
      for (const n of nodes) {
        n.x += n.vx * dt;
        n.y += n.vy * dt;
        if (n.x < width * 0.04) n.vx = Math.abs(n.vx);
        if (n.x > width * 0.96) n.vx = -Math.abs(n.vx);
        if (n.y < height * 0.06) n.vy = Math.abs(n.vy);
        if (n.y > height * 0.94) n.vy = -Math.abs(n.vy);
      }
    },
    draw(ctx: CanvasRenderingContext2D, size: AtmosphereSize, tick: AtmosphereTick): void {
      // Resolved per frame rather than once at creation, so a theme or a custom
      // tint restyles this canvas on the next frame instead of waiting for a
      // remount. See the hook's note on live reads.
      const web = channelsOf(read("--neural-web"));
      const node = channelsOf(read("--neural-node"));
      const pulse = channelsOf(read("--neural-pulse"));
      if (nodes.length === 0) seedNodes(size);

      ctx.clearRect(0, 0, size.width, size.height);

      // The links. One stroke each, alpha by distance, colour by distance:
      // a link's middle is the theme's faint web colour and its ends carry
      // the node colour, so the web has depth without a gradient per line.
      //
      // The first version drew these in `--neural-web` alone, which is
      // `#1e293b` — a slate that is nearly black, and on a pure black theme a
      // line in it is invisible. The brief's ramp is `#1e293b` *to* `#38bdf8`,
      // so the line is mixed toward the node colour even at rest, and the web
      // colour is the far end of that ramp rather than the whole of it.
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i];
          const b = nodes[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const d = Math.sqrt(dx * dx + dy * dy);
          if (d > LINK) continue;
          const near = 1 - d / LINK;
          const flicker = 0.85 + 0.15 * Math.sin(tick.seconds * 0.7 + a.seed + b.seed);
          ctx.strokeStyle = withAlpha(
            mix(mix(web, node, 0.35 + near * 0.4), pulse, near * energy * 0.8),
            (0.1 + near * 0.26) * flicker * (1 + energy * 0.9),
          );
          ctx.lineWidth = 0.5 + near * 0.6;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }

      // The pulse ring, while the agent is working. One expanding circle,
      // fading as it grows — the "signal leaving the machine" beat, and the
      // only motion in this theme that is about anything.
      if (energy > 0.02 && pulsePhase > 0) {
        const t = pulsePhase;
        const r = t * Math.max(size.width, size.height) * 0.75;
        ctx.strokeStyle = withAlpha(pulse, (1 - t) * 0.28 * energy);
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.arc(size.width / 2, size.height / 2, r, 0, Math.PI * 2);
        ctx.stroke();
      }

      // The nodes: a wide dim disc and a small bright core, no blur. Both
      // alphas are computed once per frame and the fillStyle is set per node
      // because the breathe factor is per node — the colour is not re-parsed,
      // which is what a `toRgba(read(...))` inside this loop would do, 34
      // times a frame.
      const halo = withAlpha(node, 0.05 + energy * 0.14);
      for (const n of nodes) {
        const breathe = 0.75 + 0.25 * Math.sin(tick.seconds * 1.3 + n.seed);
        ctx.fillStyle = halo;
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r * 3, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = withAlpha(node, (0.4 + energy * 0.45) * breathe);
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
        ctx.fill();
      }
    },
  };
}

export const NeuralWeb: React.FC<NeuralWebProps> = ({
  className = "",
  maxDimension = 480,
  fps = 30,
  animated,
  active = false,
}) => {
  // Live input to the painter, read every frame and never a dependency.
  const activeRef = React.useRef(active);
  activeRef.current = active;

  const vars = {
    "--neural-web": "#1e293b",
    "--neural-node": "#38bdf8",
    "--neural-pulse": "#0ea5e9",
  };


  const canvasRef = useAtmosphereCanvas({
    maxDimension,
    fps,
    animated,
    active,
    vars,
    // The ref, handed over as a getter. This is the one painter that reads the
    // flag through a ref rather than taking it as a value, and the reason is
    // still the hook's: the flag changes at runtime and a closure that captured
    // it would be looking at the value from mount. Passing the ref's *current*
    // through instead would reintroduce exactly that, which is why this is
    // `() => activeRef.current` and not `activeRef.current`.
    create: (read) => createNeuralWebPainter(read, () => activeRef.current === true),
  });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
