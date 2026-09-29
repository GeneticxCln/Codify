/**
 * The Vector Wireframe geometry, held to the invariant its edges assume.
 *
 * This theme shipped a `TESS_V` built from three nested loops over two values
 * each — 2³ = 8 vertices, one cube — while the edge generator beside it walked
 * `i ^ (1 << bit)` over sixteen indices. Every edge above index 7 read
 * `pts[n].z` off the end of an 8-element array, threw inside the canvas
 * effect's *mount* effect, and unmounted the whole React tree: there is no error
 * boundary, so the visible symptom was not a broken backdrop but an entirely
 * black window whenever the theme was selected. The README gallery carried the
 * evidence for a release as a black rectangle that looked deliberate.
 *
 * Nothing else in the suite could see it. The geometry is plain data until a
 * browser runs the painter; the painter's own tests drive it against a recording
 * context, and this file's predecessor had no geometry check at all. So the
 * invariant is asserted here, as data, with a message that names the number the
 * author expected.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { SOLIDS } = await import("../src/components/ui/VectorWire.tsx");

test("the geometry is not empty", () => {
  // Non-vacuity for every assertion below: a rename that emptied `SOLIDS` would
  // make "no edge is out of range" true and meaningless.
  assert.ok(SOLIDS.length > 0, "SOLIDS is empty, so no index check below means anything");
});

test("no edge or chord points past the end of its own vertex list", () => {
  for (const [i, solid] of SOLIDS.entries()) {
    const n = solid.v.length;
    assert.ok(n > 0, `solid ${i} has no vertices`);
    for (const [label, edges] of [["edge", solid.e], ["chord", solid.chords]] as const) {
      for (const [a, c] of edges) {
        for (const [name, index] of [["a", a], ["b", c]] as const) {
          assert.ok(
            Number.isInteger(index) && index >= 0 && index < n,
            `solid ${i} has ${n} vertices, so its ${label} ${name}=${index} is out of ` +
              `range. Drawing it reads pts[${index}] out of a ${n}-element array and ` +
              "throws inside the canvas effect, which unmounts the app.",
          );
        }
      }
    }
  }
});

test("the second solid is the tesseract its own comment claims", () => {
  // Named directly rather than left to the index test, because the index test
  // is satisfied by *any* vertex count that happens to cover the edges — and a
  // cube has no edges out of range either. This is the assertion that would
  // have caught the missing fourth loop at the moment it was dropped.
  const tesseract = SOLIDS[1];
  assert.equal(
    tesseract.v.length,
    16,
    `the second solid is documented as a tesseract: 16 vertices, 4 per cube ` +
      `corner. It has ${tesseract.v.length}. Two nested pairs of loops over 2 ` +
      "values is 8, which is a cube — and its 16-index edge list then reads off " +
      "the end of the array and takes the app down with it.",
  );
  assert.equal(
    tesseract.e.length,
    32,
    `a 4-cube has 32 edges (16 within each cube, 16 across). Got ${tesseract.e.length}.`,
  );
});

test("both solids are closed: every vertex takes part in at least one edge", () => {
  // A wireframe with a stranded vertex is not visibly wrong, so nothing else
  // would report it, but it means the vertex list and the edge list have drifted
  // apart — which is the same class of mistake as the one above.
  for (const [i, solid] of SOLIDS.entries()) {
    const used = new Set<number>();
    for (const [a, c] of [...solid.e, ...solid.chords]) {
      used.add(a);
      used.add(c);
    }
    assert.equal(
      used.size,
      solid.v.length,
      `solid ${i}: ${solid.v.length} vertices but only ${used.size} of them appear in ` +
        "an edge, so the two lists have drifted apart",
    );
  }
});
