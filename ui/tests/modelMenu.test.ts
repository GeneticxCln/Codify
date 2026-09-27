/**
 * The model menu is a square.
 *
 * It was not. `measure()` read the width off the field and capped the height
 * separately, so the panel inherited whatever column layout its parent used —
 * and in `AgentConfigCard` the field sits in a `grid-cols-2` inside a
 * `max-w-6xl` modal, which is about 500px. The result was a roughly 500x260
 * banner: stretched, because the thing it hangs off is stretched.
 *
 * `menuPlacement` is a pure function for exactly this reason. Measuring a
 * component needs a DOM, needs it open, and needs the window to be a size you
 * control; the arithmetic that decides the shape is none of those things, so
 * this suite asserts on it directly rather than on markup that cannot show a
 * pixel.
 *
 * The contract has two halves and neither is optional:
 *
 * - **Square.** Both sides come from one number, so a wide field cannot make a
 *   wide menu.
 * - **Usable anyway.** A square that runs off the window, or shrinks until a
 *   60-character model id has nowhere to go, is not an improvement.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { menuPlacement, MENU_SIDE, MENU_MIN_SIDE } = await import(
  "../src/components/ModelSelect.tsx"
);
import type { FieldRect } from "../src/components/ModelSelect.tsx";

/** A generous desktop window. */
const VIEW = { width: 1440, height: 900 };

function field(overrides: Partial<FieldRect> = {}): FieldRect {
  return { left: 40, top: 300, bottom: 340, width: 500, ...overrides };
}

test("the panel is square, not as wide as its field", () => {
  // The regression, stated as the shape it produced: a 500px field gave a
  // 500x260 banner. Now the field only ever supplies an *upper bound*.
  const place = menuPlacement(field(), VIEW);
  assert.equal(
    place.width,
    place.maxHeight,
    "the two sides must be the same number — that is what square means",
  );
  assert.ok(place.width < 500, "a wide field must not make a wide menu");
});

test("the exact settings-card case lands on the preferred square", () => {
  const place = menuPlacement(field({ width: 500 }), VIEW);
  assert.equal(place.width, MENU_SIDE);
  assert.equal(place.maxHeight, MENU_SIDE);
});

test("a narrow field gets a square that matches it rather than overhanging", () => {
  // A single-column layout: the menu should sit on its row, not hang past it.
  const place = menuPlacement(field({ width: 240 }), VIEW);
  assert.equal(place.width, 240, "it should match the field");
  assert.equal(place.maxHeight, 240, "and stay square while doing so");
});

test("the floor keeps the ids readable when every clamp squeezes", () => {
  // Narrow field, no vertical room, field almost off the right edge. Each of
  // those wants a tiny panel; a tiny panel is where a model id stops being
  // legible, so the floor wins over all of them.
  const place = menuPlacement(
    field({ width: 120, top: 40, bottom: 80, left: 1430 }),
    VIEW,
  );
  assert.equal(place.width, MENU_MIN_SIDE);
  assert.equal(place.maxHeight, MENU_MIN_SIDE);
});

test("the panel never reaches past the right edge of the window", () => {
  // The render clamps `left` from `width`, so an unbounded width here would put
  // the clamp under pressure rather than refusing at the source.
  const place = menuPlacement(field({ left: 1000 }), VIEW);
  const right = place.left + place.width;
  assert.ok(
    right <= VIEW.width - 8,
    `panel reaches ${right}px into a ${VIEW.width}px window`,
  );
});

test("it opens below when there is room, above when there is not", () => {
  const roomy = menuPlacement(field({ top: 300, bottom: 340 }), VIEW);
  assert.equal(roomy.above, false);

  // Near the bottom: 900 - 740 - 12 = 148px below, 700 - 12 = 688 above.
  // 148 is under the 200 threshold, so the menu goes up.
  const crowded = menuPlacement(field({ top: 700, bottom: 740 }), VIEW);
  assert.equal(crowded.above, true, "a 148px strip cannot hold the menu");
});

test("opening above still squares up", () => {
  const place = menuPlacement(field({ top: 700, bottom: 740 }), VIEW);
  assert.equal(place.width, place.maxHeight, "above/below must not change the shape");
  assert.equal(place.width, MENU_SIDE, "there is 688px of room above, so no squeeze");
});

test("a place so small the menu cannot fit reports the floor, not a negative", () => {
  // `space` can go negative in a degenerate layout. A negative height is a
  // `max-height` the browser ignores — the panel then grows to its content and
  // spills off the window with no declared size at all.
  const place = menuPlacement(
    field({ top: -50, bottom: -10, left: 1435 }),
    VIEW,
  );
  assert.ok(place.width > 0, "must never report a non-positive side");
  assert.ok(place.maxHeight > 0, "must never report a non-positive side");
});
