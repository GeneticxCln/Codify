/**
 * What the painters actually draw.
 *
 * Every other test in this suite reads these files as text. That is enough to
 * know a painter is mounted, is `aria-hidden`, carries the `pointer-events-none`
 * a decorative overlay needs, and is listed against the right theme — and not one
 * bit of it can see whether the painter paints. `EventHorizon` spent a whole
 * session mounted, wired to its trigger variable, with every assertion in the
 * suite green, drawing **zero pixels**: its disc was centred below the window at
 * an inclination that put the entire field off-canvas. Nothing here would have
 * caught that, because a painter that draws nothing and a painter that draws
 * beautifully are the same string of source code.
 *
 * So this file is the one that looks at the output instead of the input. It
 * drives each painter's factory — hoisted to module scope for exactly this
 * reason, since a closure inside a component is unreachable from a suite with no
 * renderer — against a recording 2D context, and makes three claims.
 *
 * **It paints something.** Not "it calls `draw`": a `moveTo` and nothing else is
 * a path that was built and never filled, which is how a painter silently stops
 * appearing after a refactor. The recorder only counts a mark when `fill`,
 * `stroke` or `fillRect` is actually called, so an empty frame and a full one
 * cannot both be "it drew".
 *
 * **Its static frame is a complete frame.** This is the one that earns the file.
 * `useAtmosphereCanvas` honours `prefers-reduced-motion` by drawing exactly one
 * frame and never arming the loop — so a painter that seeds its field inside
 * `step` and assumes it has been called renders a *blank canvas* to every user
 * who has asked for less motion. That is not a hypothetical: the hook's contract
 * makes it the natural way to write a painter, and the seeding call belongs in
 * `draw` for exactly this reason. Asserted at `frame: 0`, because that is the
 * frame the hook draws.
 *
 * **The gutter painters leave the middle empty.** Five of the effects confine
 * themselves to the window's margins, because the middle of this window is where
 * the conversation is, and a bright moving line across somebody's code is the
 * worst legibility failure in the app. The margin is where the paint lands, and
 * this is the only test that can see the paint.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AtmospherePainter, AtmosphereSize, AtmosphereTick } from "../src/hooks/useAtmosphereCanvas.ts";

// Dynamic, like every other test here that reaches a `.tsx`. The loader is
// registered by the line above, but a *static* import is resolved and fetched
// before that line runs — so a static one fails with ERR_UNKNOWN_FILE_EXTENSION
// and the error names the loader, not the mistake.
const [
  { createAbyssSporesPainter },
  { createAnonFluidPainter },
  { createCyberGridPainter },
  { createElectricArcPainter },
  { createCyberOrganismPainter },
  { createEventHorizonPainter },
  { createHudSweepPainter },
  { createNanofluidPainter },
  { createNebulaFlowPainter },
  { createNeuralWebPainter },
  { createSnowFallPainter },
  { createSolarWindPainter },
  { createToxicLabPainter },
  { createVectorWirePainter, SOLIDS },
] = await Promise.all([
  import("../src/components/ui/AbyssSpores.tsx"),
  import("../src/components/ui/AnonFluid.tsx"),
  import("../src/components/ui/CyberGrid.tsx"),
  import("../src/components/ui/ElectricArc.tsx"),
  import("../src/components/ui/CyberOrganism.tsx"),
  import("../src/components/ui/EventHorizon.tsx"),
  import("../src/components/ui/HudSweep.tsx"),
  import("../src/components/ui/Nanofluid.tsx"),
  import("../src/components/ui/NebulaFlow.tsx"),
  import("../src/components/ui/NeuralWeb.tsx"),
  import("../src/components/ui/SnowFall.tsx"),
  import("../src/components/ui/SolarWind.tsx"),
  import("../src/components/ui/ToxicLab.tsx"),
  import("../src/components/ui/VectorWire.tsx"),
]);

/** The 1440×900 window at a 1× ratio — the shape the app is actually used in. */
const SIZE: AtmosphereSize = { width: 1440, height: 900 };

interface Mark {
  /** How the mark reached the canvas. */
  op: "fill" | "stroke" | "fillRect";
  /** Every coordinate the operation covered. */
  points: ReadonlyArray<readonly [number, number]>;
}

/**
 * A 2D context that records what was painted and paints nothing.
 *
 * It models the one thing that matters for these claims — *a path only becomes
 * ink when it is filled or stroked* — so the recorder is not a log of calls but a
 * log of marks. `clearRect` is deliberately not a mark: it is every painter
 * erasing the previous frame, and counting it would make an empty canvas look
 * like the busiest one in the app.
 *
 * `save`/`restore`/`clip`/`setTransform` are accepted and ignored. Nothing in
 * these painters depends on a transform surviving them, and a stub that
 * implemented the state stack would be testing the stub.
 */
function recordingContext(): { ctx: CanvasRenderingContext2D; marks: Mark[] } {
  const marks: Mark[] = [];
  let path: Array<readonly [number, number]> = [];
  const push = (x: number, y: number): void => {
    path.push([x, y]);
  };
  const ctx = {
    // ── state the painters assign; recorded as nothing, since no claim here
    // is about colour and `getComputedStyle` is not available here anyway.
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    lineCap: "butt",
    lineJoin: "miter",
    font: "",
    textBaseline: "alphabetic",
    globalAlpha: 1,
    save() {},
    restore() {},
    clip() {},
    beginPath() {
      path = [];
    },
    closePath() {},
    moveTo: push,
    lineTo: push,
    quadraticCurveTo(_cx: number, _cy: number, x: number, y: number) {
      push(x, y);
    },
    bezierCurveTo(_c1x: number, _c1y: number, _c2x: number, _c2y: number, x: number, y: number) {
      push(x, y);
    },
    arc(x: number, y: number, r: number) {
      // The centre and the extent, so a disc is not mistaken for a point: a
      // painter whose only mark is a hairline at the very edge of the window is
      // still painting, and this is what says so.
      push(x - r, y - r);
      push(x + r, y + r);
    },
    clearRect() {},
    fillRect(x: number, y: number, w: number, h: number) {
      marks.push({ op: "fillRect", points: [[x, y], [x + w, y + h]] });
    },
    fill() {
      if (path.length) marks.push({ op: "fill", points: [...path] });
    },
    stroke() {
      if (path.length) marks.push({ op: "stroke", points: [...path] });
    },
    fillText(_text: string, x: number, y: number) {
      push(x, y);
    },
    createLinearGradient() {
      return { addColorStop() {} };
    },
    createRadialGradient() {
      return { addColorStop() {} };
    },
    measureText() {
      return { width: 0 };
    },
  };
  return { ctx: ctx as unknown as CanvasRenderingContext2D, marks };
}

/** A `read` that answers every variable, so no painter can fail on a colour. */
const read = (name: string): string =>
  name.endsWith("-rgb") ? "128 128 128" : "#808080";

function tick(frame: number, seconds: number): AtmosphereTick {
  return { frame, seconds, size: SIZE };
}

/**
 * Drive a painter the way the hook does: build it, draw the static frame at
 * frame 0, then advance and draw again. Returns the marks from the *animated*
 * frame, because that is the one a user watches, and the static frame's marks
 * separately because that is the one a reduced-motion user gets and the one with
 * no second chance.
 */
function run(
  create: (read: (name: string) => string, active: boolean) => AtmospherePainter,
  { active = false, frames = 12 }: { active?: boolean; frames?: number } = {},
): { animated: Mark[]; still: Mark[] } {
  const painter = create(read, active);

  const first = recordingContext();
  painter.draw(first.ctx, SIZE, tick(0, 0));

  const later = recordingContext();
  for (let f = 1; f <= frames; f++) {
    const t = tick(f, f / 30);
    painter.step?.(1 / 30, t);
    painter.draw(later.ctx, SIZE, t);
  }
  return { animated: later.marks, still: first.marks };
}

interface Painter {
  name: string;
  create: (read: (name: string) => string, active: boolean) => AtmospherePainter;
  /**
   * Whether this one is required to keep the middle of the window clear.
   *
   * The five margin effects are, by contract in DESIGN.md §7 — and the reason
   * they are listed here rather than derived is that "which painters are
   * gutter-confined" is a design fact about the window, not a property of the
   * drawing code. Deriving it from each file's `GUTTER` constant would make the
   * test agree with whatever the painter happens to do today, which is the
   * opposite of a test.
   */
  gutterOnly?: boolean;
}

const PAINTERS: ReadonlyArray<Painter> = [
  // The five that read `active` take it as a value; the rest do not read it at
  // all and their factories take `read` alone, so the uniform two-argument
  // signature in `Painter` is adapted here rather than by giving every factory a
  // parameter it would not read.
  { name: "AbyssSpores", create: (r) => createAbyssSporesPainter(r), gutterOnly: true },
  { name: "AnonFluid", create: createAnonFluidPainter, gutterOnly: true },
  { name: "CyberGrid", create: (r) => createCyberGridPainter(r) },
  { name: "CyberOrganism", create: createCyberOrganismPainter, gutterOnly: true },
  { name: "ElectricArc", create: createElectricArcPainter, gutterOnly: true },
  { name: "EventHorizon", create: createEventHorizonPainter, gutterOnly: true },
  { name: "HudSweep", create: (r) => createHudSweepPainter(r) },
  { name: "Nanofluid", create: createNanofluidPainter, gutterOnly: true },
  { name: "NebulaFlow", create: (r) => createNebulaFlowPainter(r) },
  // The one painter that reads the flag through a ref, so it takes a getter.
  { name: "NeuralWeb", create: (r) => createNeuralWebPainter(r, () => false) },
  { name: "SnowFall", create: (r) => createSnowFallPainter(r) },
  { name: "SolarWind", create: createSolarWindPainter, gutterOnly: true },
  { name: "ToxicLab", create: createToxicLabPainter, gutterOnly: true },
  { name: "VectorWire", create: createVectorWirePainter, gutterOnly: true },
];

test("the table covers every painter with a factory", () => {
  // The coverage claim. A new painter that is not in this table would be
  // untested and would not know it, so the count is asserted rather than trusted
  // — and `atmosphere.test.ts` is what says which components are painters.
  assert.equal(
    PAINTERS.length,
    14,
    "a painter was added or removed; this table is the list of what gets driven",
  );
  const names = PAINTERS.map((p) => p.name);
  assert.equal(new Set(names).size, names.length, "a painter is listed twice");
});

for (const p of PAINTERS) {
  test(`${p.name} paints something`, () => {
    const { animated } = run(p.create);
    assert.ok(
      animated.length > 0,
      `${p.name} produced no marks in ${12} frames. A painter that fills nothing ` +
        "is indistinguishable from one that was never mounted, and every other " +
        "test in this suite reads source rather than output — so nothing else " +
        "would have noticed.",
    );
  });

  test(`${p.name} paints its static frame, not only the animated one`, () => {
    // The `prefers-reduced-motion` frame. The hook draws exactly this and never
    // arms the loop, so a painter that seeds in `step` shows a reduced-motion
    // user a blank canvas — and there is no second frame coming to rescue it.
    const { still } = run(p.create);
    assert.ok(
      still.length > 0,
      `${p.name} paints nothing at frame 0. useAtmosphereCanvas draws one frame ` +
        "and stops when motion is reduced, so this is the only picture a " +
        "reduced-motion user ever sees: seeding has to happen in `draw`.",
    );
  });
}

for (const p of PAINTERS.filter((x) => x.gutterOnly)) {
  test(`${p.name} leaves the middle of the window empty`, () => {
    const { animated, still } = run(p.create, { active: true });
    // The central half of the width. The transcript sits in the middle of the
    // window and these effects are contractually forbidden from painting there
    // (DESIGN.md §7), so the band is the middle *half* rather than a column
    // measured off a `max-w-*` class: wide enough that a painter a little too
    // generous is caught, narrow enough not to encode a layout decision that
    // belongs to the chat column.
    const lo = SIZE.width * 0.25;
    const hi = SIZE.width * 0.75;
    const inMiddle = [...still, ...animated].flatMap((mark) =>
      mark.points.filter(([x]) => x >= lo && x <= hi).map(([x, y]) => `${mark.op} at ${x},${y}`),
    );
    assert.deepEqual(
      inMiddle.slice(0, 8),
      [],
      `${p.name} painted ${inMiddle.length} point(s) into the middle of the ` +
        `window, which is where the conversation is. The gutters are the whole ` +
        `point of this effect:\n  ${inMiddle.slice(0, 8).join("\n  ")}`,
    );
  });
}

test("a painter that paints nothing is a failure this file would catch", () => {
  // The meta-assertion, and the reason to believe the rest. `EventHorizon`
  // shipped a session mounted, wired and green while drawing nothing, so a test
  // file about painters that cannot fail on a painter that draws nothing is not
  // worth having. This proves the recorder reports an empty painter as empty,
  // by running one.
  const silent = (): AtmospherePainter => ({
    draw(ctx) {
      ctx.clearRect(0, 0, SIZE.width, SIZE.height);
    },
  });
  const { animated, still } = run(() => silent());
  assert.equal(animated.length, 0, "a clear-only painter must record nothing");
  assert.equal(still.length, 0, "and must record nothing on the static frame either");
});

test("every solid's edges address real vertices, and every vertex the same degree", () => {
  // The invariants that catch a broken solid *before* a painter walks it.
  //
  // `TESS_E` once indexed vertices 0–15 over an 8-element array and threw a
  // `TypeError` inside the canvas effect; `ICO_E` declared 29 edges over a
  // vertex set that is a cuboctahedron's, giving vertex 0 a degree of 7 and
  // vertex 4 a degree of 3. Neither shows up in a screenshot you are not staring
  // at, and both were invisible to every test here because the geometry is plain
  // data until something walks it. So it is walked here, once.
  //
  // **Equal degree, not equal edge length.** Equal edge length is the Platonic
  // property and it holds for the icosahedron, but it is *wrong* for the
  // tesseract: a 4-cube projected into 3D has three edge lengths by
  // construction — the outer cube's, the inner cube's at half the radius, and the
  // spokes'. That is not sloppiness in the projection, it is the whole of what
  // makes the shape read as a tesseract rather than as a cube. Equal degree is
  // the invariant both solids share, and it is the one a wrong edge list breaks.
  for (const solid of SOLIDS) {
    const n = solid.v.length;
    const label = solid === SOLIDS[0] ? "icosahedron" : "tesseract";
    for (const [a, b] of [...solid.e, ...solid.chords]) {
      assert.ok(
        Number.isInteger(a) && a >= 0 && a < n && Number.isInteger(b) && b >= 0 && b < n,
        `${label} has an edge [${a}, ${b}] but only ${n} vertices, so the ` +
          "painter would read undefined and throw. This is the bug that stopped " +
          "this theme rendering at all.",
      );
    }
    const degree = new Array(n).fill(0);
    for (const [a, b] of solid.e) {
      degree[a]++;
      degree[b]++;
    }
    const distinct = new Set(degree);
    assert.equal(
      distinct.size,
      1,
      `${label}'s ${n} vertices have ${distinct.size} different degrees ` +
        `(${degree.join(",")}). A solid has one degree everywhere; mixed ` +
        "degrees means the edge list is not this solid's.",
    );
    // And no edge of zero length, which is the coincident-cubes failure: it
    // renders, draws every stroke, and is the wrong picture.
    const dist = (a: number, b: number): number => {
      const [ax, ay, az] = solid.v[a];
      const [bx, by, bz] = solid.v[b];
      return Math.hypot(ax - bx, ay - by, az - bz);
    };
    const zero = solid.e.filter(([a, b]) => dist(a, b) === 0);
    assert.equal(
      zero.length,
      0,
      `${label} has ${zero.length} zero-length edge(s) (${JSON.stringify(zero)}), ` +
        "so two of its vertices sit on top of each other",
    );
  }
});

test("neither solid is degenerate, and neither is a duplicate of the other", () => {
  // The check that would have caught the tesseract fix that stopped the crash and
  // left a cube. Declaring sixteen vertices is enough to stop a `TypeError`; it
  // is not enough to be a tesseract. The inner cube has to sit at a *different*
  // radius, or every spoke is a point and the two cubes are drawn on top of
  // each other — which renders, and is wrong.
  const [ico, tess] = SOLIDS;
  const spread = (v: ReadonlyArray<readonly [number, number, number]>): number =>
    Math.max(...v.map(([x, y, z]) => Math.hypot(x, y, z)));
  assert.notEqual(
    spread(tess.v),
    spread(tess.v.slice(8)),
    "the tesseract's two cubes are at the same radius, so it is one cube drawn " +
      "twice rather than a tesseract",
  );
  // And a real icosahedron has every vertex at the same radius from the centre.
  const radii = new Set(ico.v.map(([x, y, z]) => Math.hypot(x, y, z).toFixed(6)));
  assert.equal(
    radii.size,
    1,
    `the icosahedron's vertices are at ${radii.size} different radii ` +
      `(${[...radii].join(", ")}), so it is not a solid centred on its own origin`,
  );
});

test("the gutter painters are the nine the design names, and not the rest", () => {
  // Keeps the list honest in the other direction: adding a whole-window painter
  // to the `gutterOnly` set would make the middle-empty claim apply to something
  // DESIGN.md never promised, and the list is a design fact, not a preference.
  //
  // Nine, and not eight. `AbyssSpores` is the odd one out and predates the
  // margin effects — DESIGN.md §7 counts "the eight margin effects" because
  // those eight arrived in two batches, and this set has a ninth member that
  // was never one of them. Both facts are true and they are not the same fact,
  // so the set is written out rather than derived from either.
  //
  // The three added with Electric Arc, Toxic Lab and Anon Fluid are in it
  // because each confines itself to the outer 20% of the width on each side,
  // which is the same claim as the six above and is enforced by the
  // middle-empty test the loop above runs for every entry.
  assert.deepEqual(
    PAINTERS.filter((p) => p.gutterOnly).map((p) => p.name).sort(),
    [
      "AbyssSpores",
      "AnonFluid",
      "CyberOrganism",
      "ElectricArc",
      "EventHorizon",
      "Nanofluid",
      "SolarWind",
      "ToxicLab",
      "VectorWire",
    ].sort(),
  );
});
