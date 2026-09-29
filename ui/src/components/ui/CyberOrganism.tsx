import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * The Bio-Luminescent Cyber-Organism theme: a resting nerve network in the
 * gutters, firing while the agent works.
 *
 * **How this differs from the two themes it resembles.** `neural-constellation`
 * draws a web across the *whole* window with links between every node, and
 * `abyss-bioluminescent` draws spores *rising* in the gutters with no
 * connections at all. This is the third thing the pair was missing: nodes that
 * are joined by **branching tendrils**, and both confined to the outer margins.
 * A tendril is a short polyline that forks off a node and drifts; the signal
 * travels *along* it, which is the part a plain node field cannot do — which is
 * why the effect is worth being a component rather than a recolour of either.
 *
 * **Signals are the only state-reporting motion here.** At rest a node dims and
 * brightens on its own slow phase, like a resting brainwave: a rhythm nobody
 * could learn, and therefore brand rather than information. While the agent is
 * working, a bright pulse is launched from a node and runs down its tendrils at
 * 1.8×, and the nodes themselves hold a higher floor of brightness. That is
 * the line DESIGN.md §7 draws — motion that carries something the user did not
 * already have — and the rate alone is not what puts it on the right side of
 * that line. It is that a signal *means* a tool call is in flight.
 */
export type CyberOrganismProps = AtmosphereCanvasProps;

interface Node {
  /** 0 at the window edge, 1 at the inner edge of its gutter. */
  x: number;
  y: number;
  r: number;
  phase: number;
  left: boolean;
}

interface Tendril {
  /** Which node it grows from, by index into the node list. */
  from: number;
  /** The fork, as absolute canvas pixels. Recomputed when a node respawns. */
  tx: number;
  ty: number;
  /** A second segment, so the branch bends rather than being a straight line. */
  mx: number;
  my: number;
}

interface Signal {
  node: number;
  /** 0 at the node, 1 at the end of the tendril. */
  t: number;
  speed: number;
}

const NODES = 26;
const TENDRILS = 34;
const SIGNALS = 8;
/** The gutters, as a fraction of the width. Nothing is drawn inside them. */
const GUTTER = 0.21;

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
export function createCyberOrganismPainter(read: (name: string) => string, active: boolean): AtmospherePainter {

  let nodes: Node[] = [];
  let tendrils: Tendril[] = [];
  let signals: Signal[] = [];

  const place = (n: Node, w: number, h: number): void => {
    n.x = n.left ? Math.random() * GUTTER * w : w - Math.random() * GUTTER * w;
    n.y = Math.random() * h;
  };

  const seed = (w: number, h: number): void => {
    nodes = Array.from({ length: NODES }, () => {
      const left = Math.random() < 0.5;
      const n: Node = { x: 0, y: 0, r: 1.4 + Math.random() * 2.4, phase: Math.random() * Math.PI * 2, left };
      place(n, w, h);
      return n;
    });
    tendrils = Array.from({ length: TENDRILS }, (_, i) => {
      const from = i % NODES;
      return { from, tx: 0, ty: 0, mx: 0, my: 0 };
    });
    grow();
    signals = [];
  };

  /** Re-aim every tendril at its node. Cheap, and only on seed. */
  const grow = (): void => {
    for (const t of tendrils) {
      const n = nodes[t.from];
      // Outward and downward: a root system branching away from the window's
      // edge, which is why the field is densest at the margins.
      const dir = n.left ? -1 : 1;
      const reach = 30 + Math.random() * 80;
      const drop = (Math.random() - 0.5) * 60;
      t.tx = n.x + dir * reach;
      t.ty = n.y + drop;
      t.mx = n.x + dir * reach * 0.45;
      t.my = n.y + drop * 0.3;
    }
  };

  return {
    step(dt) {
      for (const s of signals) {
        s.t += s.speed * dt * (active ? 1.8 : 1);
        if (s.t > 1) s.t = 0;
      }
      // Signals are only launched while the agent is working. At rest the
      // nodes keep their slow pulse and nothing travels, which is the
      // difference between a resting brainwave and a firing one.
      if (active && signals.length < SIGNALS) {
        const n = Math.floor(Math.random() * NODES);
        signals.push({ node: n, t: 0, speed: 0.35 + Math.random() * 0.4 });
      }
      while (signals.length > SIGNALS) signals.shift();
    },
    draw(ctx, size, tick) {
      // Resolved per frame rather than once at creation, so a theme or a
      // custom tint restyles this canvas on the next frame instead of
      // waiting for a remount. See the hook's note on live reads.
        const node = channelsOf(read("--organ-node"));
        const tendril = channelsOf(read("--organ-tendril"));
      if (nodes.length === 0) seed(size.width, size.height);
      const { width: w } = size;
      ctx.clearRect(0, 0, w, size.height);

      // Tendrils first, so a node's glow sits on top of its own roots rather
      // than being veiled by them.
      for (const t of tendrils) {
        const n = nodes[t.from];
        const inward = Math.min(n.x, w - n.x) / (w * GUTTER);
        const fade = Math.max(0, 1 - inward) ** 1.5;
        if (fade < 0.03) continue;
        const alpha = fade * (active ? 0.3 : 0.16);
        ctx.strokeStyle = withAlpha(tendril, alpha);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(n.x, n.y);
        ctx.quadraticCurveTo(t.mx, t.my, t.tx, t.ty);
        ctx.stroke();
      }

      for (const n of nodes) {
        const inward = Math.min(n.x, w - n.x) / (w * GUTTER);
        const fade = Math.max(0, 1 - inward) ** 1.5;
        if (fade < 0.03) continue;
        // The resting pulse is slow and per-node; the floor rises while the
        // agent is working, so the whole field is visibly "up".
        const rest = 0.5 + 0.5 * Math.sin(tick.seconds * 0.6 + n.phase);
        const alpha = fade * (active ? 0.5 + 0.35 * rest : 0.16 + 0.3 * rest);
        if (alpha < 0.02) continue;
        ctx.fillStyle = withAlpha(tendril, alpha * 0.3);
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r * 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = withAlpha(node, alpha);
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
        ctx.fill();
      }

      // A signal: a bright dot travelling the tendril's quadratic, drawn at
      // the point the curve reaches at parameter `t`. Bezier evaluation, not
      // a lerp between endpoints — a lerp slides along a straight line that
      // the tendril is visibly not on.
      for (const s of signals) {
        const n = nodes[s.node];
        const t = tendrils[s.node % tendrils.length];
        const u = 1 - s.t;
        const x = u * u * n.x + 2 * u * s.t * t.mx + s.t * s.t * t.tx;
        const y = u * u * n.y + 2 * u * s.t * t.my + s.t * s.t * t.ty;
        ctx.fillStyle = withAlpha(node, 0.85);
        ctx.beginPath();
        ctx.arc(x, y, 1.6, 0, Math.PI * 2);
        ctx.fill();
      }
    },
  };
}

export const CyberOrganism: React.FC<CyberOrganismProps> = ({
  className = "",
  maxDimension = 1024,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--organ-node": "#00e5ff",
    "--organ-tendril": "#0077ff",
  };


  const canvasRef = useAtmosphereCanvas({ maxDimension, fps, animated, active, vars, create: (read) => createCyberOrganismPainter(read, active) });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
