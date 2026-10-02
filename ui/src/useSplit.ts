import { useEffect, useMemo, useState } from "react";
import { resolveSplit, type ShownSplit, type Split } from "./panes";
import type { TabState } from "./tabs";

/**
 * The split, held beside the tab state and reconciled with it every render.
 *
 * `panes.resolveSplit` is the rule: whether the split is showing, and what it becomes when the active tab moves
 * (a tab shown from outside takes the focused pane's place; one that cannot sit beside the other, a browser page, makes it
 * wait). It is applied *in the render* so the first frame is already right, and written back after, so what the person
 * did (a replaced pane, a split that ended because its tab closed) is what is kept. Writing back only when the answer
 * is a different object is what stops that loop: `resolveSplit` returns the very one it was given when nothing changed.
 */
export function useSplit(tabState: TabState): {
  /** What is in the two panes right now, or null while there is no split or it is waiting. */
  shown: ShownSplit | null;
  /** The split as held, which may be waiting. */
  split: Split | null;
  setSplit: (next: Split | null) => void;
} {
  const [split, setSplit] = useState<Split | null>(null);
  const resolution = useMemo(() => resolveSplit(tabState, split), [tabState, split]);
  useEffect(() => {
    if (resolution.split !== split) setSplit(resolution.split);
  }, [resolution, split]);
  return { shown: resolution.shown, split: resolution.split, setSplit };
}
