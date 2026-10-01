/**
 * The right-hand drawers, and when the left panel gives way to one.
 *
 * Stats and History are flex siblings of the centre column, not overlays: the native browser webview
 * paints above every DOM overlay (`docs/09` §8.1), so a drawer that covered the transcript would
 * cover a browser tab's page *under* it. A sibling shrinks the centre instead, and that is the
 * problem this module is about: at the default 125% UI scale the left panel (15rem), a drawer
 * (28rem) and the centre column's own needs did not fit a 1280px window, and the transcript was
 * squeezed to about 400px with the step headers wrapping over themselves.
 *
 * The answer is not to make the drawers smaller. The left panel is the one of the three that can
 * leave: it is a list of threads, and a drawer is a thing the person just asked to look at. So while
 * a drawer is open and the window cannot hold both, the panel is not drawn. That is a *derived* state
 * and never a stored one: `codify.sidebar` is written only by the person's own press of the toggle
 * (`docs/09` §8.1), so closing the drawer brings the panel back exactly as it was, and a window that
 * was widened in the meantime never "remembers" a panel it was only asked to hide for a moment.
 *
 * The threshold is in **rem**, because everything it compares is: the panel and the drawers are
 * rem-sized and the UI scale moves the root font size, so a threshold in pixels would be right at one
 * scale and wrong at the next. Pure and DOM-free so the rule has a test that does not need a layout
 * engine; `useSidebarYield.ts` is the thin part that measures.
 */

/** The drawers there are. A union rather than a boolean each: at most one is open, by construction. */
export type Drawer = "stats" | "history";

/** The left panel's width: `w-60`. `sidebarToggle.test.ts` pins the class, so this cannot drift from it. */
export const SIDEBAR_REM = 15;

/** What each drawer asks for: `w-[28rem]` and `w-80`. Pinned against the classes in `drawers.test.ts`. */
export const DRAWER_REM: Readonly<Record<Drawer, number>> = { stats: 28, history: 20 };

/**
 * The least the centre column may be left with before the left panel gives way. Thirty rem is about
 * what the composer's chips (workspace, model, mode) need on one row, and what a transcript needs to
 * read as lines rather than a column of fragments.
 */
export const MIN_CENTRE_REM = 30;

/**
 * Whether the left panel should not be drawn right now.
 *
 * `mainPx` and `rootPx` are the row's width and the root font size in pixels. If either is unknown
 * (a window with no layout yet, or a test), the answer is no: hiding something because it could not
 * be measured is the wrong default, and the drawers' own percentage caps still keep the centre usable.
 */
export function sidebarYields(mainPx: number, rootPx: number, drawer: Drawer | null): boolean {
  if (drawer === null || !(mainPx > 0) || !(rootPx > 0)) return false;
  return mainPx / rootPx < SIDEBAR_REM + DRAWER_REM[drawer] + MIN_CENTRE_REM;
}

/** What pressing a drawer's button does: open it (closing the other), or close it if it is the open one. */
export function nextDrawer(current: Drawer | null, pressed: Drawer): Drawer | null {
  return current === pressed ? null : pressed;
}

/** `current` unless it is `which`, in which case closed: how a drawer's own close button acts. */
export function closeDrawer(current: Drawer | null, which: Drawer): Drawer | null {
  return current === which ? null : current;
}
