import type { TintStore } from "./tint";

/**
 * Undo for custom colours.
 *
 * ## Why a whole-store snapshot, and not a diff
 *
 * An entry holds the store as it was *before* the change. A diff would be
 * smaller, and it would be the wrong shape: a change can touch more than one
 * theme (an import, a reset of a scheme) and a diff has to be replayed
 * correctly or the store is subtly wrong forever after. A snapshot cannot be
 * replayed incorrectly. They are a few hexes per tinted theme, twenty-five deep
 * is a few kilobytes, and the whole stack is session state that is deliberately
 * **not** persisted — see below.
 *
 * ## Why a drag is one entry and not two hundred
 *
 * A colour well is a native picker. Dragging the saturation slider across it
 * fires a change event per step of the drag, so a single gesture is dozens of
 * commits. If each were an undo step, the button would be useless — you would
 * press it thirty times to get back to before the drag, and give up first. So
 * consecutive changes to the *same variable* inside `COALESCE_MS` replace each
 * other, and crucially they replace the **store** too: an entry keeps the state
 * from before the whole gesture, not from before its last frame.
 *
 * `Reset all` and an import carry no burst key and never coalesce, because
 * neither is a gesture that produces a stream of them.
 *
 * ## Why this is not in `localStorage`
 *
 * Undo answers "I mis-dragged", which is a thing that happened in the last few
 * seconds. A persisted history is a second source of truth for the store that
 * has to survive an app upgrade, and a store restored from a history written by
 * an older build is a store that may not parse. The store persists; the way back
 * to it does not, and a restart is a perfectly good time to forget it.
 */
export interface HistoryEntry {
  /** The store as it was *before* this change. What undo restores. */
  store: TintStore;
  /** What the user did, in their terms: "Bubble body on Toxic Lab". */
  label: string;
  /**
   * Identifies a gesture that may still be running — `themeId:varName` for a
   * pick. Absent means "never coalesce with anything".
   */
  burst?: string;
  /** Milliseconds since the epoch, from the caller. Only ever compared. */
  at: number;
}

export type TintHistory = ReadonlyArray<HistoryEntry>;

/**
 * How deep the stack goes.
 *
 * Twenty-five is past what anyone will press and far short of being a leak.
 * Dropping the *oldest* rather than refusing the newest is the right way round:
 * a user who has made thirty edits wants the last twenty-five undone, not to
 * find that editing has stopped working.
 */
export const HISTORY_LIMIT = 25;

/**
 * How long a gesture may last and still be one entry.
 *
 * Long enough to cover a slow drag with pauses, short enough that two
 * deliberate picks a second apart stay two. A colour well's dialog stays open
 * between picks, so the gap between the last event of one and the first of the
 * next is the time a person spent deciding — which is exactly the quantity we
 * want to measure.
 */
export const COALESCE_MS = 700;

/**
 * Add a change to the history, collapsing it into the last entry when it is
 * part of the same gesture still in progress.
 *
 * The collapse keeps the *earlier* entry's `store`. That is the whole point and
 * the easiest thing to get backwards: the entry has to hold the state from
 * before the gesture began, or undo would restore the second-to-last frame of
 * a drag and the colour would visibly jump.
 */
export function record(
  history: TintHistory,
  entry: HistoryEntry,
): TintHistory {
  const last = history[history.length - 1];
  const sameBurst =
    entry.burst !== undefined && last?.burst === entry.burst && entry.at - last.at <= COALESCE_MS;
  if (sameBurst) {
    return [
      ...history.slice(0, -1),
      { ...last, label: entry.label, at: entry.at },
    ];
  }
  const next = [...history, entry];
  return next.length > HISTORY_LIMIT ? next.slice(next.length - HISTORY_LIMIT) : next;
}

/** Whether there is anything to undo. The button's disabled state, in one place. */
export function canUndo(history: TintHistory): boolean {
  return history.length > 0;
}

/**
 * The entry undo would restore, and the history without it.
 *
 * `null` when there is nothing, so a caller cannot restore `undefined` and
 * discover later that it cleared somebody's colours. Popping rather than
 * peeking is what makes a second press step further back, which is the whole
 * behaviour of the button.
 */
export function undo(
  history: TintHistory,
): { history: TintHistory; store: TintStore; label: string } | null {
  const last = history[history.length - 1];
  if (!last) return null;
  return { history: history.slice(0, -1), store: last.store, label: last.label };
}
