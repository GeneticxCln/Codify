/**
 * Which of the engine's stderr lines are worth putting in front of a person.
 *
 * The desktop shell tails the engine's stderr and keeps the last 200 lines
 * (`src-tauri/src/engine_log.rs`). That tail is the *diagnosis* of an engine
 * that stopped — a traceback, a provider error, and above all the bounded
 * shutdown backstop saying it gave up waiting and left anyway — but it is also
 * whatever else the engine said all session, so it cannot simply be printed in
 * full: the one line that matters would be the four hundredth thing on screen.
 *
 * So the tail is filtered, and the filter is here rather than in the component
 * for the same reason the rest of this directory is: a decision about which
 * lines a person should read is a decision that can be wrong on its own, and a
 * test can only be written about it if it is a function.
 *
 * Two rules, in this order:
 *
 * 1. The last few *notable* lines — anything that reads as a warning, an error,
 *    a traceback, or a shutdown. This is the rule that matters: the backstop's
 *    line survives an ocean of noise above it.
 * 2. The last few lines of all, always. A process that dies usually says why
 *    last, and a panel that renders nothing because nothing matched a keyword
 *    is the same blank panel this was built to remove.
 */

/**
 * What counts as notable. Deliberately generous: a line that is only a warning
 * is worth the reader's eye, and the cost of a false positive is one line of
 * screen.
 */
export const STDERR_NOTABLE =
  /\b(warn|warning|error|traceback|exception|critical|fatal|fail(ed|ure)?|refus\w*|unfinished|shutdown|shut down|shutting down|exiting|aborted|timed out|timeout|gone)\b/i;

/** How many notable lines to keep by default. */
const DEFAULT_MATCHES = 6;
/** How many lines to show at the very least, whatever they say. */
const DEFAULT_CONTEXT = 3;

export interface StderrFilterOptions {
  /** Most notable lines to keep, newest first when there are too many. */
  matches?: number;
  /** Lines to show regardless, so the panel is never empty. */
  context?: number;
}

/**
 * The lines worth showing, in the order the engine said them.
 *
 * Blank lines are dropped rather than preserved: a panel of blank space is
 * indistinguishable from a panel that failed to load.
 */
export function notableStderrLines(
  lines: readonly string[],
  { matches = DEFAULT_MATCHES, context = DEFAULT_CONTEXT }: StderrFilterOptions = {},
): string[] {
  const kept = lines.map((line) => line.trim()).filter((line) => line.length > 0);
  if (kept.length === 0) return [];
  // Indices, not the strings themselves: two identical lines are two lines, and
  // a Set of strings would quietly collapse them into one.
  const chosen = new Set<number>();
  for (let i = kept.length - 1; i >= 0 && chosen.size < matches; i -= 1) {
    if (STDERR_NOTABLE.test(kept[i]!)) chosen.add(i);
  }
  for (let i = kept.length - 1; i >= 0 && chosen.size < context; i -= 1) {
    chosen.add(i);
  }
  return [...chosen].sort((a, b) => a - b).map((i) => kept[i]!);
}
