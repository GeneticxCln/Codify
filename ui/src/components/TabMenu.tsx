import React from "react";
import { Columns2, SquareTerminal, X } from "lucide-react";

import { Button } from "./ui/Button";
import { clampMenuPosition, type Bounds } from "../threadMenu";
import {
  TAB_MENU_FALLBACK_SIZE,
  TAB_MENU_LABEL,
  type TabMenuItem,
  type TabMenuItemId,
} from "../tabMenu";

/**
 * The menu a right-click on a tab opens.
 *
 * Built the way `ThreadMenu` is, and for the same reasons (read there): a presentational component that takes a point,
 * some items and callbacks; it measures itself and re-clamps to the window; it closes through a transparent sheet under
 * it, so a click elsewhere, or a right-click that would have opened the webview's own menu, closes it with no listener
 * on `document` to leak; and focus lands on the first item so the arrow keys and Escape work at once.
 *
 * One difference: an item that cannot work is `aria-disabled` and **stays reachable**, with its reason drawn under it. A
 * `disabled` button is skipped by the keyboard, so a person using one would never hear why the split was refused.
 */
export interface TabMenuProps {
  items: readonly TabMenuItem[];
  /** The pointer position, in window coordinates. Clamped before use. */
  x: number;
  y: number;
  onChoose: (id: TabMenuItemId) => void;
  onClose: () => void;
  /** The area the menu has to stay inside. Defaults to the window. */
  bounds?: Bounds;
}

const ICONS: Record<TabMenuItemId, React.FC<{ className?: string }>> = {
  "show-beside": Columns2,
  "split-new-terminal": SquareTerminal,
  "close-split": X,
};

export const TabMenu: React.FC<TabMenuProps> = ({ items, x, y, onChoose, onClose, bounds }) => {
  const menuRef = React.useRef<HTMLDivElement>(null);
  const [size, setSize] = React.useState(TAB_MENU_FALLBACK_SIZE);

  React.useLayoutEffect(() => {
    const box = menuRef.current?.getBoundingClientRect();
    if (box && box.width > 0) {
      setSize((prev) =>
        prev.width === box.width && prev.height === box.height ? prev : { width: box.width, height: box.height },
      );
    }
    menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
  }, []);

  const pos = clampMenuPosition(
    { x, y },
    size,
    bounds ?? {
      x: 0,
      y: 0,
      width: typeof window === "undefined" ? 0 : window.innerWidth,
      height: typeof window === "undefined" ? 0 : window.innerHeight,
    },
  );

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    const rows = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'));
    const at = rows.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      rows[(at + 1) % rows.length]?.focus();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      rows[(at - 1 + rows.length) % rows.length]?.focus();
    }
  };

  return (
    <>
      <div
        className="fixed inset-0 z-40"
        onClick={onClose}
        onContextMenu={(event) => {
          event.preventDefault();
          onClose();
        }}
      />
      <div
        ref={menuRef}
        role="menu"
        aria-label={TAB_MENU_LABEL}
        onKeyDown={onKeyDown}
        style={{ left: pos.x, top: pos.y }}
        className="fixed z-50 w-56 rounded-md border border-codify-border bg-codify-raised py-1 shadow-lg"
      >
        {items.map((item) => {
          const Icon = ICONS[item.id];
          const blocked = item.reason !== undefined;
          return (
            <div key={item.id}>
              <Button
                role="menuitem"
                tone="ghost"
                size="sm"
                align="start"
                weight="normal"
                tabIndex={-1}
                aria-disabled={blocked ? "true" : undefined}
                onClick={() => {
                  if (blocked) return;
                  onClose();
                  onChoose(item.id);
                }}
                className={"w-full rounded-md px-2.5 text-left " + (blocked ? "opacity-60 cursor-not-allowed" : "")}
              >
                <Icon className="h-3 w-3 flex-shrink-0" />
                {item.label}
              </Button>
              {blocked && (
                <p className="px-2.5 pb-1 text-2xs leading-relaxed text-codify-muted">{item.reason}</p>
              )}
            </div>
          );
        })}
      </div>
    </>
  );
};
