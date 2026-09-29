/**
 * The composer's anchor, in one place, because two components have to agree on
 * it and neither of them should have to know about the other.
 *
 * **What it is for.** The empty transcript's only focusable control is
 * "Import audit report…", which opens a file picker — a dead end for a keyboard
 * user who tabbed into the middle of the window expecting the thing they came
 * here to type into. The composer *is* auto-focused, so on first load a keyboard
 * user does land on it, but that is a one-shot on mount: tab away, click
 * anything, switch conversations, and nothing brings focus back. The empty state
 * therefore offers a skip link, and the one thing that makes a skip link work is
 * that both ends of the fragment name the same element.
 *
 * **Why a constant and not a prop.** `ChatTimeline` renders the empty state and
 * `BottomCommandBar` renders the composer; they are siblings under `App` and
 * neither imports the other. Threading a string prop down to both would be the
 * same coupling with more moving parts, and a literal written twice is a literal
 * that gets renamed on one side only. A plain module is the shape this repo
 * already uses for exactly this reason — see `statusTone.ts`, whose note is that
 * "a decision that lives in JSX is a decision no test can reach" — and it is
 * importable from a test, which a prop threaded through `App` is not.
 */
export const COMPOSER_ANCHOR_ID = "codify-composer";
