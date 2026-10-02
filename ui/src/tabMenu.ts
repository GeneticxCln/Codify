/**
 * The menu on a tab, with no React in it.
 *
 * What a right-click (or the Menu key) on a tab offers is a split, and which split depends on three things: whether one
 * is already showing, whether the tab is the one in view, and whether it could sit beside it. Pure, like `threadMenu.ts`
 * (whose `clampMenuPosition` places the menu), so each rule is a test that needs no renderer.
 *
 * An item that cannot work is **shown with its reason**, not left out and not left silent: a menu that offers nothing on
 * a browser page would read as broken, and one that offered the split and then did nothing would be worse.
 */

import { PAIR_REFUSALS, pairRefusal } from "./panes";
import type { Tab, TabState } from "./tabs";

export type TabMenuItemId = "show-beside" | "split-new-terminal" | "close-split";

export interface TabMenuItem {
  id: TabMenuItemId;
  label: string;
  /** Present when the item cannot work, and says why. */
  reason?: string;
}

/** The menu's own label, for a screen reader. */
export const TAB_MENU_LABEL = "Tab actions";

/** The vertical pitch of one item, and a size to place the first paint with before the real one is measured. */
export const TAB_MENU_ROW_HEIGHT = 24;
export const TAB_MENU_FALLBACK_SIZE = { width: 224, height: 40 };

/**
 * The items for a menu opened on `opened`.
 *
 * While a split is showing, closing it is the one thing to offer, from any tab (opening another tab already puts it in a
 * pane: `panes.resolveSplit`). Otherwise the tab in view offers a split with a new terminal, and any other tab offers to
 * be shown beside the current one.
 */
export function tabMenuItems(opened: Tab, state: TabState, splitShowing: boolean): TabMenuItem[] {
  if (splitShowing) return [{ id: "close-split", label: "Close split" }];

  const active = state.tabs.find((t) => t.id === state.activeId);
  if (opened.id === state.activeId) {
    const why = opened.kind === "browser" ? "browser" : null;
    return [
      {
        id: "split-new-terminal",
        label: "Split with a new terminal",
        ...(why ? { reason: PAIR_REFUSALS[why] } : {}),
      },
    ];
  }
  const why = pairRefusal(active, opened);
  return [
    {
      id: "show-beside",
      label: "Show beside the current tab",
      ...(why ? { reason: PAIR_REFUSALS[why] } : {}),
    },
  ];
}
