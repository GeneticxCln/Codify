/**
 * The thread menu's rules, with no React in them.
 *
 * A right-click menu is two items and two pieces of arithmetic, and every one of
 * them is easy to get wrong in a way nothing else catches. The items are the
 * actions a thread can take. The arithmetic is where the menu lands (a
 * right-click near the bottom-right corner will happily place a menu that runs
 * off the window) and which thread a new thread belongs to.
 *
 * Pure, like `tabs.ts`, because `node --test` can reach this and a screenshot
 * only shows you the one position where it happens to be correct.
 */

/**
 * What a menu item does.
 *
 * `new` starts a thread *on* the thread you are looking at — see
 * `newThreadParentId`. The rest act on the thread the menu was opened on.
 */
export type ThreadMenuItemId = "new" | "rename" | "archive";

export interface ThreadMenuItem {
  id: ThreadMenuItemId;
  label: string;
  /**
   * Archive removes the thread from the panel. It is not destructive — the
   * engine keeps every run either way — but it does take something away, and
   * the one item that does should not sit where a stray click lands with no
   * difference made about it.
   */
  caution?: boolean;
  /** Acts on the thread the menu was opened on, so it needs one to exist. */
  needsThread?: boolean;
}

/**
 * The items, in order.
 *
 * **New thread is first**, and it is first on purpose. A right-click anywhere in
 * the thread panel is the gesture for "I want to work on something new", and the
 * item that does that should not be the one the pointer has to travel to. It is
 * also the only item that needs no thread, which is what lets the same menu open
 * on empty panel and on a row.
 *
 * There is no "Open". There was one, and it was the same call as clicking the
 * row the menu was opened on (`onSelect`), so on a thread that was already open
 * it did nothing at all — a control that looked alive and was not. The row is a
 * button: clicking it opens the thread, and that is where opening lives. A menu
 * item that can only ever be a slower version of the click under the pointer is
 * not an action, it is a decoration.
 */
export const THREAD_MENU_ITEMS: readonly ThreadMenuItem[] = [
  { id: "new", label: "New thread" },
  { id: "rename", label: "Rename", needsThread: true },
  { id: "archive", label: "Archive", caution: true, needsThread: true },
];

/**
 * The items a menu opened on `hasThread` should show.
 *
 * On empty panel there is no thread for Rename or Archive to act on, and
 * showing them anyway would offer two controls that silently do nothing — the
 * exact thing the rest of this app refuses to ship. `New thread` is the whole
 * menu there, which is honest: on empty panel, starting one *is* the only thing
 * you can do.
 */
export function threadMenuItems(hasThread: boolean): ThreadMenuItem[] {
  return THREAD_MENU_ITEMS.filter((item) => !item.needsThread || hasThread);
}

/**
 * Which thread a "New thread" from this menu hangs off.
 *
 * ## Why this is a function and not a handler
 *
 * Because getting it wrong is invisible. A "New thread" that creates a
 * conversation with no `parent_id` is a *brand-new chat*: a different object
 * with the same name, and the only symptom is a new empty tab that has nothing
 * to do with the chat you right-clicked in. That is the bug this exists to make
 * impossible to write twice.
 *
 * ## The rule, and why it is one rule
 *
 * - A right-click **on a row** branches off *that* row. You pointed at it; the
 *   thread you happened to be looking at is a coincidence of which tab is
 *   active, and a menu that ignored the thing under the pointer would be
 *   surprising every time it disagreed with the reader.
 * - A right-click on **empty panel** branches off **the thread you are looking
 *   at**, because that is the chat the gesture was made in. With nothing open
 *   there is no thread to be in, and the answer is `undefined`: a top-level
 *   thread, which is an ordinary outcome and not a failure.
 *
 * Both cases are the same sentence — "the thread this gesture is about" — which
 * is why they are one function with one fallback rather than two branches in a
 * component.
 *
 * ## Why a candidate has to be in the panel
 *
 * Because the "thread you are looking at" is not necessarily a thread *this
 * panel is showing*. A tab from another workspace stays open when the workspace
 * selector changes — two projects open at once is legitimate, and closing the
 * strip on a switch would destroy live transcripts — so the active tab can name
 * a thread in a different project from the one listed here. Branching off it
 * asks the engine for a child in this workspace whose parent is in another one,
 * which it refuses (`parent_workspace_mismatch`, a 422). The user gets an error
 * banner from a right-click that should have created a thread, and the menu
 * printed no `on "…"` line first because it could not resolve the name either.
 *
 * So the panel is the boundary, and it is a *parameter* rather than a look at
 * some ambient state: the same function then decides this in a test instead of
 * in a component's JSX, where only reading the source can check it.
 */
export function newThreadParentId(
  rowConversationId: string | undefined,
  activeConversationId: string | undefined,
  panelConversationIds: readonly string[],
): string | undefined {
  const inPanel = (id: string | undefined) =>
    id && panelConversationIds.includes(id) ? id : undefined;
  return inPanel(rowConversationId) || inPanel(activeConversationId) || undefined;
}

/** The menu's own label, for a screen reader and for the row that opened it. */
export const THREAD_MENU_LABEL = "Conversation actions";

/**
 * Fallback size, used for the very first paint and whenever the real one has not
 * been measured yet.
 *
 * It is a guess in the only sense that matters — the component measures itself
 * (`ThreadMenu.tsx`) and re-clamps with the truth as soon as it can, so this
 * only decides where the menu sits for one frame. Numbers taken from the real
 * markup: `w-36` (144px) and rows at `THREAD_MENU_ROW_HEIGHT` each, plus the
 * caption line when the menu was opened on a thread.
 *
 * `threadMenu.test.ts` asserts the fallback fits the items it is a fallback
 * for, so growing or shrinking the menu again fails a test instead of quietly
 * desynchronising the guess from the thing it guesses at.
 */
export const THREAD_MENU_FALLBACK_SIZE = { width: 144, height: 104 };

/**
 * The vertical pitch of one menu item, in pixels.
 *
 * Exported rather than assumed in a test, because "the fallback fits the rows"
 * is a claim about the same number the layout is built from. A hardcoded 28 in
 * the test would survive a redesign of the row and start asserting something
 * that is no longer true — which is how a fallback ends up outgrowing the menu
 * it is guessing at, or shrinking below it and clamping the real menu to a box
 * smaller than itself.
 */
export const THREAD_MENU_ROW_HEIGHT = 24;

export interface Point {
  x: number;
  y: number;
}

export interface Size {
  width: number;
  height: number;
}

export interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * Where a menu of `size` may go so that it stays inside `bounds`.
 *
 * ## The window and not the panel
 *
 * The menu belongs to a 240px-wide sidebar, and clamping it inside that column
 * sounds tidier. It is also worse: a menu that has been squeezed sideways to
 * stay in its column sits *under the pointer the user is holding*, which is
 * where a click lands by accident. The window is the real constraint — a menu
 * that runs off the screen is unreachable, and one that overlaps the transcript
 * for two seconds is not.
 *
 * ## The two edge cases, and why they are not the same branch
 *
 * When `bounds` is *narrower* than the menu, the arithmetic below would produce
 * a negative slack and slide the menu to `bounds.x - slack`, i.e. off the left
 * of the screen. There is no placement that fits, so the only honest answer is
 * the near edge and a menu that overflows. Pinning to `bounds.x` is that
 * answer; letting the subtraction run is the bug.
 */
export function clampMenuPosition(
  point: Point,
  size: Size,
  bounds: Bounds,
): Point {
  const slackX = bounds.width - size.width;
  const slackY = bounds.height - size.height;
  const maxX = slackX > 0 ? bounds.x + slackX : bounds.x;
  const maxY = slackY > 0 ? bounds.y + slackY : bounds.y;
  return {
    x: Math.min(Math.max(point.x, bounds.x), maxX),
    y: Math.min(Math.max(point.y, bounds.y), maxY),
  };
}
