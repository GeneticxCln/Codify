import { useLayoutEffect, useState, type RefObject } from "react";
import { splitFits, type Drawer } from "./drawers";
import { useUiScale } from "./uiScale";

/** The default root font size the UI scale is a percentage of. */
const BASE_ROOT_PX = 16;

/**
 * Whether the row `ref` points at can hold two panes, for a split (`drawers.ts` has the rule and why).
 *
 * Measured like `useSidebarYield`: a `ResizeObserver` and the scale's own root size, and a boolean rather than a width,
 * so a window being dragged re-renders the app only when the answer changes. Read before paint, so a drawer opening in
 * a narrow window never shows one frame with two cramped panes.
 */
export function useSplitFits(
  ref: RefObject<HTMLElement | null>,
  sidebarShown: boolean,
  drawer: Drawer | null,
): boolean {
  const scale = useUiScale();
  const [fits, setFits] = useState(true);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const rootPx = (BASE_ROOT_PX * scale) / 100;
    const read = (): void => setFits(splitFits(el.getBoundingClientRect().width, rootPx, sidebarShown, drawer));
    read();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(read);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, sidebarShown, drawer, scale]);

  return fits;
}
