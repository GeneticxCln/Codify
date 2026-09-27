/**
 * Where a model menu goes, and — the thing this exists for — how big it is.
 *
 * It used to take `width: rect.width` from the field and a *separate* height
 * cap of 260. In `AgentConfigCard` the field sits in a `grid-cols-2` inside a
 * `max-w-6xl` modal, so it is about 500px wide and the panel came out roughly
 * 500x260: a wide shallow banner, stretched because the thing it hangs off is
 * stretched. A menu's shape should not be inherited from whatever column
 * layout its parent happens to use.
 *
 * So both sides come from **one** square. It is then clamped four ways and
 * floored, because a square that runs off the window or shrinks below the ids
 * inside it is not an improvement:
 *
 * - `MENU_SIDE` — the preferred side.
 * - `space` — the room above or below the field.
 * - `rect.width` — a narrow field gets a square that matches it instead of
 *   overhanging the row it came from.
 * - the distance to the window edge.
 *
 * Then `MENU_MIN_SIDE`, so none of those clamps can push it under the width a
 * model id needs.
 *
 * This lives in its own module rather than inside `ModelSelect.tsx` because two
 * very different fields now open the same menu: a role's "Model Identifier" and
 * a provider row's picker. The arithmetic is the part worth testing, and it is
 * impossible to assert on from a component that only measures while it is open.
 */

export interface MenuPlacement {
  left: number;
  top: number;
  width: number;
  /**
   * Cap on the panel's height. Equal to `width` in practice — see
   * `menuPlacement`, whose whole job is that the two agree.
   */
  maxHeight: number;
  /** True = open above the field, so the render sets `bottom` instead of `top`. */
  above: boolean;
}

/** The panel's side in px when there is room. One number for both axes. */
export const MENU_SIDE = 300;

/**
 * Floor. Below this a model id stops being readable — ids run to 60+ characters
 * (`hf.co/zaakirio/…-GGUF:Q8_0`) and the row already truncates them.
 */
export const MENU_MIN_SIDE = 200;

export interface FieldRect {
  left: number;
  top: number;
  bottom: number;
  width: number;
}

/** The gap left between the field and the panel, so the two do not touch. */
export const MENU_GAP = 4;

/** How close to a window edge a panel is allowed to get. */
export const MENU_EDGE = 8;

/**
 * Pure and exported: the arithmetic is the part worth testing, and a second
 * copy of it in a second component is a second set of bugs.
 */
export function menuPlacement(
  rect: FieldRect,
  view: { width: number; height: number },
): MenuPlacement {
  const below = view.height - rect.bottom - 12;
  const above = rect.top - 12;
  const openAbove = below < 200 && above > below;
  const space = openAbove ? above : below;
  const side = Math.max(
    MENU_MIN_SIDE,
    Math.min(MENU_SIDE, space, rect.width, view.width - rect.left - MENU_EDGE),
  );
  return {
    left: rect.left,
    top: openAbove ? rect.top : rect.bottom,
    width: side,
    maxHeight: side,
    above: openAbove,
  };
}
