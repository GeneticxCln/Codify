import { useEffect, useLayoutEffect, useState, type RefObject } from "react";
import { sidebarYields, type Drawer } from "./drawers";
import { useUiScale } from "./uiScale";

/** The default root font size the UI scale is a percentage of. */
const BASE_ROOT_PX = 16;

/**
 * Whether the left panel should give way, for the row `ref` points at.
 *
 * Measured with a `ResizeObserver` and not a `matchMedia` query: the rule is in rem (it follows the
 * UI scale), and a media query in px would not. It holds a boolean rather than the width, so a window
 * being dragged re-renders the app only when the answer changes and not on every pixel. The root font
 * size is the scale itself (`uiScale.ts` sets it as a percentage of 16px), so it is read from there
 * rather than from `getComputedStyle`, which would force a style recalculation on every callback.
 */
export function useSidebarYield(ref: RefObject<HTMLElement | null>, drawer: Drawer | null, split: boolean = false): boolean {
  const scale = useUiScale();
  const [yields, setYields] = useState(false);

  // Before paint, so a drawer opening in a narrow window never shows one frame with both.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const rootPx = (BASE_ROOT_PX * scale) / 100;
    const read = (): void => setYields(sidebarYields(el.getBoundingClientRect().width, rootPx, drawer, split));
    read();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(read);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, drawer, split, scale]);

  // With nothing open there is nothing to yield to, whatever the last measurement said.
  const wanted = drawer !== null || split;
  useEffect(() => {
    if (!wanted) setYields(false);
  }, [wanted]);

  return wanted ? yields : false;
}
