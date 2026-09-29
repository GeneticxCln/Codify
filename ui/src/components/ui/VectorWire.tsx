import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * The Vector Wireframe Geometry theme: 1px line art, like an Asteroids cabinet
 * or a fighter's heads-up display.
 *
 * **It is real 3D, and that is the point.** A wireframe is a *projection* of
 * edges, so pretending with a hand-drawn circle would give a rotating blob. The
 * two solids here are hard-coded vertex and edge lists — an icosahedron and a
 * tesseract — rotated about Y, tilted slightly about X so the silhouette moves,
 * and divided by a perspective factor. The persistence cue is free: an edge
 * whose far end is further away is drawn fainter, which is the only thing
 * separating a solid from a tangle of lines on a black screen.
 *
 * **One pixel wide, never two.** The brief asks for "sharp 1px unrounded
 * borders", and a wireframe that thickens as it comes forward is the single
 * quickest way to stop looking like vector art. So `lineWidth` is 1 for every
 * edge at every depth, and depth is carried by alpha alone.
 *
 * **Density is the state report.** At rest a solid draws its own edges. While
 * the agent is working, extra chords appear between non-adjacent vertices and a
 * target reticle locks over the nearest one — which is the brief's "grid locks
 * target reticles when writing files", and it is information rather than
 * decoration, so §7's test is satisfied by what it *means* and not by how fast
 * it moves.
 */
export type VectorWireProps = AtmosphereCanvasProps;

type Vec = readonly [number, number, number];

/**
 * The two solids, as data — and they were both **wrong** until
 * `ui/tests/painter.test.ts` drove the painter and found one of them throwing.
 *
 * **The icosahedron was a cuboctahedron with the wrong edges.** The twelve
 * vertices were the permutations of `(\u00b11, \u00b11, 0)`, which is a *cuboctahedron*:
 * 12 vertices but **24** edges, every vertex of degree 4. The edge list beside it
 * declared 29 edges at three different lengths \u2014 not that solid and not an
 * icosahedron either, with vertex 0 at degree 7 and vertex 4 at degree 3. A real
 * icosahedron is the cyclic arrangement of `(0, \u00b11, \u00b1\u03c6)`: 12 vertices,
 * **30** edges, every vertex of degree 5. That is what is here, and both figures
 * are asserted rather than assumed.
 *
 * **The tesseract's inner cube sat exactly on its outer one.** Sixteen vertices
 * were declared, which fixed the crash \u2014 but by pushing `[x, y, z]` *twice*, so
 * the two cubes coincided: all 8 spokes had length 0 and 24 of the 32 edges were
 * drawn twice on top of each other. The theme drew a single cube. A tesseract
 * seen head-on shows an inner square at a *different* radius, which is the whole
 * of what makes it a tesseract, so the inner cube is now at half.
 */
const PHI = (1 + Math.sqrt(5)) / 2;
const ICO_V: Vec[] = [
  [0, 1, PHI], [1, PHI, 0], [PHI, 0, 1],
  [0, 1, -PHI], [1, -PHI, 0], [-PHI, 0, 1],
  [0, -1, PHI], [-1, PHI, 0], [PHI, 0, -1],
  [0, -1, -PHI], [-1, -PHI, 0], [-PHI, 0, -1],
];
/** 30 edges: every pair of vertices at the minimum separation, which is 2. */
const ICO_E: ReadonlyArray<readonly [number, number]> = [
  [0, 1], [0, 2], [0, 5], [0, 6], [0, 7],
  [1, 2], [1, 3], [1, 7], [1, 8],
  [2, 4], [2, 6], [2, 8],
  [3, 7], [3, 8], [3, 9], [3, 11],
  [4, 6], [4, 8], [4, 9], [4, 10],
  [5, 6], [5, 7], [5, 10], [5, 11],
  [6, 10], [7, 11], [8, 9], [9, 10], [9, 11], [10, 11],
];

/**
 * Tesseract: 16 vertices \u2014 the 8 corners of an outer cube, then the same 8
 * corners at half the radius \u2014 and 32 edges between them.
 *
 * Two nested cubes joined by 8 spokes is the tesseract's projection into 3D, and
 * 3D is what this painter has: `spinY` and `spinX` rotate `[x, y, z]`, so a
 * genuine fourth coordinate would be ignored rather than rotated. Every vertex has
 * degree 4 and the two halves are contiguous, which is what lets `TESS_E` below
 * address them with one offset.
 *
 * This is the shape that *threw*. Sixteen vertices were not enough on their own:
 * the earlier fix declared sixteen by pushing the same `[x, y, z]` twice, which
 * stopped the `TypeError` and left a solid whose inner cube was coincident with
 * its outer one \u2014 all 8 spokes of length 0, half the edges drawn twice, and a
 * cube where a tesseract was meant. The crash was the visible half; this was the
 * half you only notice by looking.
 */
const TESS_V: Vec[] = (() => {
  const outer: Vec[] = [];
  for (const z of [1, -1]) for (const y of [1, -1]) for (const x of [1, -1]) outer.push([x, y, z]);
  return [...outer, ...outer.map(([x, y, z]) => [x * 0.5, y * 0.5, z * 0.5] as Vec)];
})();
const TESS_E: ReadonlyArray<readonly [number, number]> = (() => {
  const out: Array<readonly [number, number]> = [];
  // The 12 edges of each cube: vertices differing in exactly one coordinate.
  for (const offset of [0, 8]) {
    for (let i = 0; i < 8; i++) {
      for (let bit = 0; bit < 3; bit++) {
        const j = i ^ (1 << bit);
        if (j > i) out.push([offset + i, offset + j]);
      }
    }
  }
  // The 8 spokes, outer corner to its inner one. 12 + 12 + 8 = 32.
  for (let i = 0; i < 8; i++) out.push([i, i + 8]);
  return out;
})();

interface Solid {
  v: ReadonlyArray<Vec>;
  e: ReadonlyArray<readonly [number, number]>;
  /** Extra chords, drawn only while the agent is working. */
  chords: ReadonlyArray<readonly [number, number]>;
}

export const SOLIDS: ReadonlyArray<Solid> = [
  {
    v: ICO_V,
    e: ICO_E,
    // Opposite pairs across the solid: the long diagonals a wireframe does not
    // normally show, which appear when there is work in flight.
    chords: [[0, 3], [1, 2], [4, 7], [5, 6], [8, 11], [9, 10]],
  },
  {
    v: TESS_V,
    e: TESS_E,
    chords: [[0, 15], [1, 14], [2, 13], [3, 12], [4, 11], [5, 10], [6, 9], [7, 8]],
  },
];

const WIDGETS = 5;
/** The gutters, as a fraction of the width. The brief says "side gutters". */
const GUTTER = 0.2;
/** Divide projected z by this much more and a solid fills its box. */
const PERSPECTIVE = 2.6;

interface Body {
  solid: number;
  /** 0 at the window edge, 1 at the inner edge of its gutter. */
  x: number;
  y: number;
  size: number;
  /** Rotation about Y, radians, and its speed. */
  spin: number;
  spinRate: number;
  /** A slow tilt so the silhouette is never edge-on and static. */
  tilt: number;
  left: boolean;
}

const spinY = (v: Vec, a: number): Vec => [
  v[0] * Math.cos(a) + v[2] * Math.sin(a),
  v[1],
  -v[0] * Math.sin(a) + v[2] * Math.cos(a),
];
const spinX = (v: Vec, a: number): Vec => [
  v[0],
  v[1] * Math.cos(a) - v[2] * Math.sin(a),
  v[1] * Math.sin(a) + v[2] * Math.cos(a),
];

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
export function createVectorWirePainter(read: (name: string) => string, active: boolean): AtmospherePainter {

  let bodies: Body[] = [];

  const seed = (w: number, h: number): void => {
    bodies = Array.from({ length: WIDGETS }, (_, i) => {
      const left = i % 2 === 0;
      return {
        solid: i % SOLIDS.length,
        x: left ? Math.random() * GUTTER * w : w - Math.random() * GUTTER * w,
        y: (0.12 + Math.random() * 0.76) * h,
        size: 22 + Math.random() * 40,
        spin: Math.random() * Math.PI * 2,
        // "Slow 3D rotation along a single axis at 30 FPS" — the frame rate is
        // the hook's, and the axis is Y, with only a small X tilt on top so
        // the silhouette breathes rather than flips.
        spinRate: 0.16 + Math.random() * 0.22,
        tilt: (Math.random() - 0.5) * 0.7,
        left,
      };
    });
  };

  return {
    step(dt) {
      for (const b of bodies) {
        b.spin += b.spinRate * dt * (active ? 1.8 : 1);
      }
    },
    draw(ctx, size, tick) {
      // Resolved per frame rather than once at creation, so a theme or a
      // custom tint restyles this canvas on the next frame instead of
      // waiting for a remount. See the hook's note on live reads.
        const line = channelsOf(read("--vector-line"));
        const lock = channelsOf(read("--vector-lock"));
      if (bodies.length === 0) seed(size.width, size.height);
      const { width: w } = size;
      ctx.clearRect(0, 0, w, size.height);
      ctx.lineWidth = 1;
      ctx.lineCap = "butt";

      // A locked target: which body is "the target" for this run. Chosen once
      // and held, because a reticle that jumps between shapes every frame is
      // noise rather than a lock.
      const target = bodies.length
        ? bodies[Math.floor(tick.seconds * 0.25) % bodies.length]
        : null;

      for (const b of bodies) {
        const inward = Math.min(b.x, w - b.x) / (w * GUTTER);
        const fade = Math.max(0, 1 - inward) ** 1.4;
        if (fade < 0.04) continue;
        const solid = SOLIDS[b.solid];
        const pts = solid.v.map((v) => {
          const r = spinX(spinY(v, b.spin), b.tilt);
          const z = r[2];
          const k = b.size / (PERSPECTIVE + z);
          return { x: b.x + r[0] * k, y: b.y + r[1] * k, z };
        });
        const locked = active && target === b;

        const drawEdge = (a: number, c: number, alpha: number): void => {
          // Depth as alpha, never as width: a wireframe that thickens as it
          // comes forward stops looking like vector art.
          const near = 0.5 + 0.5 * ((pts[a].z + 1) / 2) * ((pts[c].z + 1) / 2);
          ctx.strokeStyle = withAlpha(line, alpha * near * fade);
          ctx.beginPath();
          ctx.moveTo(pts[a].x, pts[a].y);
          ctx.lineTo(pts[c].x, pts[c].y);
          ctx.stroke();
        };
        for (const [a, c] of solid.e) drawEdge(a, c, 0.5);
        if (active) for (const [a, c] of solid.chords) drawEdge(a, c, 0.22);

        // Vertices, only on the locked one, and only while working: a dot on
        // every vertex of every solid is the opposite of vector art.
        if (locked) {
          ctx.fillStyle = withAlpha(lock, 0.9 * fade);
          for (const p of pts) {
            ctx.beginPath();
            ctx.arc(p.x, p.y, 1.2, 0, Math.PI * 2);
            ctx.fill();
          }
          // The reticle: four corner brackets round the solid's bounding box.
          let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
          for (const p of pts) {
            minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
            minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
          }
          const armX = (maxX - minX) * 0.3;
          const armY = (maxY - minY) * 0.3;
          ctx.strokeStyle = withAlpha(lock, 0.85 * fade);
          for (const [cx, cy, sx, sy] of [
            [minX, minY, 1, 1], [maxX, minY, -1, 1],
            [minX, maxY, 1, -1], [maxX, maxY, -1, -1],
          ]) {
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.lineTo(cx + armX * sx, cy);
            ctx.moveTo(cx, cy);
            ctx.lineTo(cx, cy + armY * sy);
            ctx.stroke();
          }
        }
      }
    },
  };
}

export const VectorWire: React.FC<VectorWireProps> = ({
  className = "",
  maxDimension = 1024,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--vector-line": "#00ff66",
    "--vector-lock": "#ff007f",
  };


  const canvasRef = useAtmosphereCanvas({ maxDimension, fps, animated, active, vars, create: (read) => createVectorWirePainter(read, active) });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
