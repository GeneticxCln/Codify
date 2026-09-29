import React from "react";
import { Archive, MessageSquarePlus, Pencil } from "lucide-react";

import { Button } from "./ui/Button";
import {
  THREAD_MENU_FALLBACK_SIZE,
  THREAD_MENU_LABEL,
  threadMenuItems,
  clampMenuPosition,
  type Bounds,
  type ThreadMenuItemId,
  type ThreadMenuItem,
} from "../threadMenu";

/**
 * The menu a right-click on a thread opens.
 *
 * It is a presentational component: it takes a point, some callbacks and
 * draws. Where the point ends up is `clampMenuPosition`'s business, not this
 * file's, the item list is `threadMenu.ts`'s, and which thread a new thread
 * hangs off is `newThreadParentId`'s — the rules that can be wrong in a way a
 * screenshot hides all live in a pure module that `node --test` can reach
 * without a DOM.
 *
 * ## Why it measures itself
 *
 * The fallback size in `threadMenu.ts` decides where the menu sits for the first
 * paint, and then this measures the real thing and re-clamps. A menu that only
 * ever used a guessed size would be wrong for any user whose font size is not
 * the one the guess was taken at, which is the same class of bug as the sidebar
 * badges that overflowed a 240px panel because nobody measured.
 *
 * ## Why there is an overlay instead of a document listener
 *
 * A click anywhere else closes the menu, and the cheapest honest way to know
 * "anywhere else" is a transparent sheet under it. It also disposes of the
 * right-click case for free: the webview's own menu is suppressed on it, so a
 * right-click *outside* an open menu closes it instead of putting the OS menu on
 * screen over the app. No `mousedown` listener on `document`, nothing to
 * unregister, and the menu cannot leak if a listener is missed.
 */
export interface ThreadMenuProps {
  /**
   * The thread it acts on, or `undefined` when the right-click landed on empty
   * panel. Undefined is not a degraded menu — it is a menu whose only item is
   * "New thread", which is everything that can be done there.
   */
  title?: string;
  /**
   * What "New thread" will hang off, as the menu can see it: the thread under
   * the pointer if the right-click hit a row, otherwise the thread on screen.
   * `undefined` means a top-level thread, which is the honest answer when
   * nothing is open — see `newThreadParentId` for why this is passed in rather
   * than looked up in here.
   */
  parentTitle?: string;
  /** The pointer position, in window coordinates. Clamped before use. */
  x: number;
  y: number;
  /**
   * Start a thread on the current chat. Always offered; the only item with no
   * thread needed. The parent is decided by the caller, which is the only place
   * that knows both what was under the pointer and what is on screen.
   */
  onNewThread: () => void;
  onRename: () => void;
  onArchive: () => void;
  onClose: () => void;
  /**
   * The area the menu has to stay inside. Defaults to the window.
   *
   * Optional for one reason: `renderToStaticMarkup` has no `window`, so a
   * component that reached for `innerWidth` itself would be untestable and would
   * render a menu pinned at the origin in every render test — passing the
   * bounds in is what makes "the menu went where it was asked to go" an
   * assertion instead of a hope.
   */
  bounds?: Bounds;
}

/** The icon beside each item, so the menu and the row's buttons look alike. */
const ICONS: Record<ThreadMenuItemId, React.FC<{ className?: string }>> = {
  new: MessageSquarePlus,
  rename: Pencil,
  archive: Archive,
};

export const ThreadMenu: React.FC<ThreadMenuProps> = ({
  title,
  parentTitle,
  x,
  y,
  onNewThread,
  onRename,
  onArchive,
  onClose,
  bounds,
}) => {
  const menuRef = React.useRef<HTMLDivElement>(null);
  const [size, setSize] = React.useState(THREAD_MENU_FALLBACK_SIZE);

  // Measured, not assumed. The window is the boundary rather than the sidebar —
  // `clampMenuPosition` says why, and the short version is that a menu pinned
  // inside a 240px column ends up under the pointer.
  //
  // Focus lands on the first item in the same pass, because a menu opened with
  // the mouse is still a menu the keyboard has to be able to leave: without this,
  // the arrow keys and Escape would only work after a Tab, and the menu items
  // carry `tabIndex={-1}` so a stray Tab skips them rather than walking the
  // thread list underneath.
  React.useLayoutEffect(() => {
    const box = menuRef.current?.getBoundingClientRect();
    if (box && box.width > 0) {
      setSize((prev) =>
        prev.width === box.width && prev.height === box.height
          ? prev
          : { width: box.width, height: box.height }
      );
    }
    menuRef.current
      ?.querySelector<HTMLButtonElement>('[role="menuitem"]')
      ?.focus();
  }, []);

  const pos = clampMenuPosition(
    { x, y },
    size,
    bounds ?? {
      x: 0,
      y: 0,
      width: typeof window === "undefined" ? 0 : window.innerWidth,
      height: typeof window === "undefined" ? 0 : window.innerHeight,
    }
  );

  const run = (fn: () => void) => () => {
    onClose();
    fn();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')
    );
    const index = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      items[(index + 1) % items.length]?.focus();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();      // `(0 - 1) % 3` is `-1`, not `2`, hence the `+ items.length`: without it
      // ArrowUp off the first item lands nowhere and the menu feels stuck.
      items[(index - 1 + items.length) % items.length]?.focus();
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
        aria-label={THREAD_MENU_LABEL}
        onKeyDown={onKeyDown}
        style={{ left: pos.x, top: pos.y }}
        //
        // An explicit width, not `min-w-*` + shrink-to-fit. The items are
        // `w-full`, and a percentage width inside a container that is sizing
        // *itself* from its content is a feedback loop: the items resolved to
        // something far wider than their own text and the menu came out 255px
        // across to hold three words. A fixed width makes the layout
        // deterministic — the labels still measure 77–96px, so 144px is the
        // compact case with room for the row's name to be read.
        className="fixed z-50 w-36 rounded-md border border-codify-border bg-codify-raised py-1 shadow-lg"
      >
        {/*
          What "New thread" is about to do, said out loud — and said once, on one
          line.

          A menu item that opens a thread *on this one* and a menu item that opens
          an unrelated empty chat are the same three words on screen, and the
          difference is the entire point of the item. Naming the parent before the
          click is the guarantee; naming it on the *same* line as everything else is
          the compactness. A second heading line here doubled the menu's height to
          say what one line can.
        */}
        {parentTitle && (
          <div className="px-2.5 pb-1 mb-0.5 text-2xs text-codify-muted truncate border-b border-codify-border">
            on &ldquo;{parentTitle}&rdquo;
          </div>
        )}
        {threadMenuItems(title !== undefined).map((item: ThreadMenuItem) => {
          const Icon = ICONS[item.id];
          const action =
            item.id === "new" ? onNewThread : item.id === "rename" ? onRename : onArchive;
          return (
            <Button
              key={item.id}
              role="menuitem"
              tone="ghost"
              size="sm"
              align="start"
              weight="normal"
              tabIndex={-1}
              onClick={run(action)}
              className={
                "w-full rounded-md px-2.5 text-left " +
                (item.caution ? "text-codify-warning" : "")
              }
            >
              <Icon className="w-3 h-3 flex-shrink-0" />
              {item.label}
            </Button>
          );
        })}
      </div>
    </>
  );
};
