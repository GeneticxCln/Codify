/**
 * The decisions a terminal pane makes that are not rendering.
 *
 * Three of them, and each one is a way to end up with a broken terminal that
 * looks like xterm's fault:
 *
 * - **What size to open and resize a PTY at.** `openpty` takes a `PtySize`, and
 *   a pane measures itself *after* it exists. The first size is therefore a
 *   constant, and every later one is validated before it is sent, because a
 *   `ResizeObserver` fires for panes that are hidden and measure nothing.
 * - **When to send a resize.** A window drag fires a resize per frame, and each
 *   one is an IPC round trip to a process that has to re-layout its whole
 *   screen.
 * - **What to do with output that arrives before xterm exists.** The shell
 *   starts printing the moment `codify_terminal_open` returns, and xterm is a
 *   dynamic import — tens of milliseconds during which the first prompt is
 *   already gone. A pane that subscribes after xterm loads loses it, and the
 *   symptom is a terminal that opens blank.
 *
 * Pure on purpose, for the reason `tabs.ts` and `browserHistory.ts` are: none of
 * these can be seen in markup, and all of them are easy to get subtly wrong.
 */

/** A PTY's character grid, as `openpty` and `terminal.rs` take it. */
export interface Grid {
  cols: number;
  rows: number;
}

/**
 * The size a PTY is opened at.
 *
 * 80x24 because that is what a shell is built to assume and what `tput` and
 * `stty` report sanely at before it has been told otherwise. The pane resizes
 * to its real geometry the moment it measures itself, and a shell redraws its
 * prompt when it does — so this number is a first guess, not a commitment.
 * Opening at the measured size instead would mean measuring before the tab
 * exists, which is a chicken-and-egg this module exists to break.
 */
export const DEFAULT_GRID: Grid = { cols: 80, rows: 24 };

/**
 * A usable character grid, or `null` when these counts are not one.
 *
 * `null` means *send nothing*, and refusing is the right answer rather than
 * clamping to 1. A pane that is hidden, or that has not been laid out yet, has
 * no cells at all: xterm's `FitAddon` reports `cols: 0, rows: 0` for it, and
 * forwarding that gives the PTY a zero-cell screen, the shell redraws its entire
 * layout into it, and the visible terminal is a column of wrapped text until the
 * next real measurement arrives. Holding the last good size through a transient
 * zero costs nothing and never produces that.
 *
 * The numbers come from xterm rather than from dividing pixels by a cell size
 * here, because `FitAddon` has already done that arithmetic against the real
 * font metrics — doing it a second time with a cell size guessed from the same
 * container would only ever divide a number by itself and agree with whatever
 * it was handed.
 *
 * The `u16` cap is the command's own parameter type: `codify_terminal_resize`
 * takes `u16`, so a grid beyond that is not a value anyone can send.
 */
export function usableGrid(cols: unknown, rows: unknown): Grid | null {
  if (
    typeof cols !== "number" ||
    typeof rows !== "number" ||
    !Number.isFinite(cols) ||
    !Number.isFinite(rows) ||
    cols < 1 ||
    rows < 1
  ) {
    return null;
  }
  return { cols: Math.min(Math.floor(cols), 0xffff), rows: Math.min(Math.floor(rows), 0xffff) };
}

/**
 * Has the grid actually moved?
 *
 * The de-dupe for a `ResizeObserver`, which fires on every frame of a window
 * drag. Resizing a PTY is not free — the shell re-wraps its scrollback — so
 * sending one per frame is a way to make dragging a window with a terminal in it
 * feel like dragging a window with a build in it.
 */
export function gridChanged(a: Grid | null, b: Grid | null): boolean {
  if (!a || !b) return a !== b;
  return a.cols !== b.cols || a.rows !== b.rows;
}

/**
 * Output that arrived before anyone was listening for it.
 *
 * The race, concretely: `codify_terminal_open` spawns a thread that reads the
 * PTY and emits `terminal-output` immediately, and the reply it hands back
 * already names a terminal whose first prompt is on its way. Opening the tab
 * renders a pane, and the pane's effect then has to `await import("@xterm/xterm")`
 * before it has anything to write into. Everything the shell said in that gap is
 * gone, and the terminal opens blank — which reads as "this is broken", not "we
 * were slow".
 *
 * So the pane subscribes on mount, *before* xterm exists, and every chunk goes
 * in here keyed by terminal id. The matching id is written straight through
 * (there is no pane for another terminal, and this is the whole point); the rest
 * wait, because a chunk for a terminal this pane has not been told about yet is
 * exactly the one that must not be dropped.
 *
 * Bounded by construction: one entry per terminal this pane has heard of, and a
 * pane is a tab, so this is the output of one shell's warm-up. `drain` empties a
 * terminal's list, so the steady state is empty.
 */
export interface OutputBuffer {
  /** Chunks per terminal id, in arrival order. */
  chunks: Map<string, string[]>;
}

export function emptyOutputBuffer(): OutputBuffer {
  return { chunks: new Map() };
}

/** Record a chunk, whether or not anyone is ready for it. */
export function bufferOutput(
  buffer: OutputBuffer,
  id: string,
  data: string,
): OutputBuffer {
  const pending = buffer.chunks.get(id);
  if (pending) {
    // The common case once xterm is live, and the reason this is a Map of
    // arrays rather than a Map of strings: concatenating on every chunk would
    // re-copy the whole warm-up on every write.
    pending.push(data);
    return buffer;
  }
  buffer.chunks.set(id, [data]);
  return buffer;
}

/**
 * Everything buffered for one terminal, and forget it.
 *
 * Returns the empty string for an id that has said nothing, so a caller can
 * `term.write(drain(id, buffer))` unconditionally — an empty write is a no-op in
 * xterm, and a branch here would only ever be a place to forget one.
 */
export function drainOutput(buffer: OutputBuffer, id: string): string {
  const pending = buffer.chunks.get(id);
  if (!pending || pending.length === 0) return "";
  buffer.chunks.delete(id);
  return pending.join("");
}

/** How much is still waiting for this terminal — for the pane's own reporting. */
export function pendingFor(buffer: OutputBuffer, id: string): number {
  return buffer.chunks.get(id)?.length ?? 0;
}
