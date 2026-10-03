/**
 * What a machine's screen shows, held above every pane, so the assistant can read it when none is mounted.
 *
 * **The same reason `terminalBuffer.ts` and `editorBuffers.ts` exist.** The centre column mounts only the tab in front (in a
 * split, two), and a machine's shell never stops: a build goes on while the person reads the conversation. A pane that owned
 * the screen would have none to show the assistant, and `read_machine` would answer "nothing" about a machine that is busy.
 * So one subscription (App's, to `machine-output`) feeds **every** chunk here, whether or not a pane is mounted, and the
 * assistant's `read`, `run` and `key` (`machineSurface.ts`) answer from this.
 *
 * **A screen is a headless xterm** (`@xterm/headless`, the same version as the `@xterm/xterm` a pane draws with). It parses
 * the stream the way a terminal does, so what it holds is the *screen*, not a transcript: rows, a cursor, the alternate
 * buffer of a full-screen program, lines that wrapped. That is what makes "the person and the assistant see the same thing"
 * true, and it is the part a regular expression over raw bytes would get wrong.
 *
 * **The pane replays what the screen has seen.** A raw, bounded log of the stream is kept beside the parsed screen so a pane
 * that mounts later writes the log into its own xterm and arrives at the same screen, then follows live chunks (`follow`).
 * The log is cut at the front when it outgrows its cap, and says so (`truncated`), so a pane can show a seam instead of a
 * screen that begins in the middle of an escape sequence.
 *
 * **Output can arrive before the screen is asked for.** `codify_machine_open` starts a reader that emits at once, and its
 * reply naming the machine loses the race with the first prompt. A chunk for an id nobody has opened yet opens it. A machine
 * that has been closed is remembered as closed, so output that straggles in after the tab has gone does not bring a screen
 * back for a jail that no longer exists.
 *
 * It is loaded on first use (`loadHeadless`), because a window that never opens a machine should not carry a terminal
 * emulator; chunks that arrive while it loads are held in order.
 *
 * Nothing here can run a command or reach the shell: it is a reader of a stream. Typing is `machineSurface.ts` through
 * `api.writeMachine`, which is a Tauri command with its own id space.
 */
import type { Terminal as HeadlessTerminal, IMarker } from "@xterm/headless";

/** The grid a machine is opened at before a pane has measured itself, the same one a terminal starts with. */
export const DEFAULT_COLS = 80;
export const DEFAULT_ROWS = 24;

/** How many lines of what has scrolled off the screen a machine remembers. */
export const SCROLLBACK_LINES = 2_000;
/** How much of the raw stream is kept for a pane that mounts later. */
export const MAX_LOG_CHARS = 256 * 1024;

export type TerminalCtor = new (options: {
  cols: number;
  rows: number;
  scrollback: number;
  allowProposedApi: boolean;
}) => HeadlessTerminal;

export interface ScreenSnapshot {
  id: string;
  /** The shell in it has exited: the screen stays, the input does not. */
  exited: boolean;
  /** The rows of the screen as drawn, top to bottom, each with its trailing blanks cut. */
  rows: string[];
  /** Zero-based, on the screen. */
  cursorRow: number;
  cursorCol: number;
  /** The lines that scrolled off the top, oldest first, the last `scrollback` of them. */
  scrollback: string[];
  /** A full-screen program is drawing (the alternate buffer), so there is no scrollback to speak of. */
  alternate: boolean;
  /** Whether the application asked for its arrow keys to be sent differently (`ESC O A` and not `ESC [ A`). */
  applicationCursorKeys: boolean;
}

/** A place in a machine's output, kept honest by the terminal as lines scroll away beneath it. */
export interface Mark {
  /** The lines printed since the mark, as logical lines (a wrapped line is one), and whether the start has been lost. */
  since(): { lines: string[]; cut: boolean } | null;
  dispose(): void;
}

export interface MachineScreens {
  /** Make the screen for a machine, if there is not one. Safe to call again, and for an id already closed (it stays closed). */
  open(id: string, grid?: { cols: number; rows: number }): void;
  /** Feed a chunk of what the machine printed. Opens the screen if this is the first the window has heard of it. */
  write(id: string, chunk: string): void;
  /** The shell in a machine ended. */
  exit(id: string): void;
  /** The tab is gone. The screen is disposed and the id is remembered as closed. */
  close(id: string): void;
  /** The pane measured itself, and told the PTY: the screen must have the same grid or it will wrap differently. */
  resize(id: string, cols: number, rows: number): void;
  has(id: string): boolean;
  /** The screen, once everything written so far has been parsed. Null for an id with no screen. */
  read(id: string, scrollback: number): Promise<ScreenSnapshot | null>;
  /** Mark where the screen's cursor is now, after everything written so far has been parsed. */
  mark(id: string): Promise<Mark | null>;
  /** Resolves once every chunk written so far has been parsed. */
  flushed(id: string): Promise<void>;
  /** How many chunks this machine has produced: it changes whenever the screen might have. */
  version(id: string): number;
  /** Called after each chunk is written to the screen. Returns the unsubscribe. */
  watch(id: string, listener: () => void): () => void;
  /** What a pane must write into its own terminal to arrive at this screen, and whether the front of it was cut. */
  replay(id: string): { data: string; truncated: boolean; exited: boolean };
  /** Called with each live chunk. Returns the unsubscribe. The pane's way to follow the stream after `replay`. */
  follow(id: string, onChunk: (chunk: string) => void, onExit?: () => void): () => void;
}

interface Entry {
  term: HeadlessTerminal | null;
  /** Held, in order, until the terminal emulator has loaded. */
  pending: string[];
  /** Resolves when everything written so far has been parsed. */
  settled: Promise<void>;
  log: string;
  truncated: boolean;
  exited: boolean;
  chunks: number;
  watchers: Set<() => void>;
  followers: Set<{ onChunk: (chunk: string) => void; onExit?: () => void }>;
  cols: number;
  rows: number;
}

/** One screen row, with its trailing blanks cut (a typed space after a prompt is a cell, and is still nothing to read). */
const rowText = (line: { translateToString(trimRight?: boolean): string } | undefined): string =>
  line ? line.translateToString(true).trimEnd() : "";

export function createMachineScreens(loadHeadless: () => Promise<TerminalCtor>): MachineScreens {
  const entries = new Map<string, Entry>();
  const closed = new Set<string>();
  let ctor: TerminalCtor | null = null;
  let loading: Promise<TerminalCtor> | null = null;

  const load = (): Promise<TerminalCtor> => {
    if (ctor) return Promise.resolve(ctor);
    loading ??= loadHeadless().then((c) => {
      ctor = c;
      return c;
    });
    return loading;
  };

  /** Write one chunk into the terminal, and settle when it has been parsed. */
  const feed = (entry: Entry, chunk: string): Promise<void> => {
    const term = entry.term;
    if (!term) {
      entry.pending.push(chunk);
      return entry.settled;
    }
    const parsed = new Promise<void>((resolve) => term.write(chunk, resolve));
    entry.settled = entry.settled.then(() => parsed);
    return parsed;
  };

  const create = (id: string, grid?: { cols: number; rows: number }): Entry | null => {
    if (closed.has(id)) return null;
    const existing = entries.get(id);
    if (existing) return existing;
    const entry: Entry = {
      term: null,
      pending: [],
      settled: Promise.resolve(),
      log: "",
      truncated: false,
      exited: false,
      chunks: 0,
      watchers: new Set(),
      followers: new Set(),
      cols: grid?.cols ?? DEFAULT_COLS,
      rows: grid?.rows ?? DEFAULT_ROWS,
    };
    entries.set(id, entry);
    // The emulator is a dynamic import away. Everything written meanwhile waits in `pending`, and the terminal is made, and
    // drained in order, the moment it is here. `settled` is what a reader waits on, so it cannot see a half-loaded screen.
    const ready = load().then((Terminal) => {
      if (closed.has(id) || entries.get(id) !== entry) return;
      entry.term = new Terminal({
        cols: entry.cols,
        rows: entry.rows,
        scrollback: SCROLLBACK_LINES,
        allowProposedApi: true,
      });
      const held = entry.pending.splice(0);
      for (const chunk of held) {
        const term = entry.term;
        const parsed = new Promise<void>((resolve) => term.write(chunk, resolve));
        entry.settled = entry.settled.then(() => parsed);
      }
    });
    entry.settled = ready;
    return entry;
  };

  /**
   * Wait until everything written so far has been parsed. `settled` is *extended* as work is queued (the held chunks are
   * queued the moment the emulator loads), so a single `await entry.settled` can resolve against the promise as it was a
   * moment ago; this waits until it stops changing.
   */
  const settle = async (entry: Entry): Promise<void> => {
    for (;;) {
      const seen = entry.settled;
      await seen;
      if (entry.settled === seen) return;
    }
  };

  const snapshot = (id: string, entry: Entry, scrollback: number): ScreenSnapshot | null => {
    const term = entry.term;
    if (!term) return null;
    const buffer = term.buffer.active;
    const rows: string[] = [];
    for (let y = 0; y < term.rows; y += 1) rows.push(rowText(buffer.getLine(buffer.baseY + y)));
    const first = Math.max(0, buffer.baseY - Math.max(0, scrollback));
    const back: string[] = [];
    if (buffer.type === "normal") for (let y = first; y < buffer.baseY; y += 1) back.push(rowText(buffer.getLine(y)));
    return {
      id,
      exited: entry.exited,
      rows,
      cursorRow: buffer.cursorY,
      cursorCol: buffer.cursorX,
      scrollback: back,
      alternate: buffer.type === "alternate",
      applicationCursorKeys: term.modes.applicationCursorKeysMode,
    };
  };

  return {
    open(id, grid) {
      create(id, grid);
    },

    write(id, chunk) {
      const entry = create(id);
      if (!entry) return;
      entry.chunks += 1;
      entry.log += chunk;
      if (entry.log.length > MAX_LOG_CHARS) {
        entry.log = entry.log.slice(entry.log.length - MAX_LOG_CHARS);
        entry.truncated = true;
      }
      void feed(entry, chunk)
        .then(() => settle(entry))
        .then(() => entry.watchers.forEach((w) => w()));
      entry.followers.forEach((f) => f.onChunk(chunk));
    },

    exit(id) {
      const entry = entries.get(id);
      if (!entry || entry.exited) return;
      entry.exited = true;
      entry.followers.forEach((f) => f.onExit?.());
      entry.watchers.forEach((w) => w());
    },

    close(id) {
      closed.add(id);
      const entry = entries.get(id);
      entries.delete(id);
      if (!entry) return;
      entry.term?.dispose();
      entry.term = null;
      entry.watchers.clear();
      entry.followers.clear();
    },

    resize(id, cols, rows) {
      const entry = entries.get(id);
      if (!entry || cols < 1 || rows < 1) return;
      entry.cols = cols;
      entry.rows = rows;
      // After what has been written so far, as the PTY will have seen it: a resize between two chunks must land between them.
      const term = entry.term;
      if (term) {
        entry.settled = entry.settled.then(() => {
          if (entry.term === term) term.resize(cols, rows);
        });
      }
    },

    has: (id) => entries.has(id),

    async flushed(id) {
      const entry = entries.get(id);
      if (entry) await settle(entry);
    },

    async read(id, scrollback) {
      const entry = entries.get(id);
      if (!entry) return null;
      await settle(entry);
      return snapshot(id, entry, scrollback);
    },

    async mark(id) {
      const entry = entries.get(id);
      if (!entry) return null;
      await settle(entry);
      const term = entry.term;
      if (!term) return null;
      const startType = term.buffer.active.type;
      // `undefined` is the terminal declining to mark a place; the whole screen, said to be cut, is then the honest answer.
      const marker: IMarker | undefined = term.registerMarker(0);
      return {
        since() {
          if (entry.term !== term) return null;
          const buffer = term.buffer.active;
          if (!marker) return { lines: logicalLines(term, 0, buffer.length), cut: true };
          // A full-screen program that started or ended since the mark changed what "the lines since" means: the screen is
          // all there is to say, and the caller says so by getting the whole of it.
          if (buffer.type !== startType) return { lines: logicalLines(term, 0, buffer.length), cut: true };
          const cut = marker.isDisposed;
          const from = cut ? 0 : Math.max(0, marker.line);
          return { lines: logicalLines(term, from, buffer.length), cut };
        },
        dispose() {
          marker?.dispose();
        },
      };
    },

    version: (id) => entries.get(id)?.chunks ?? 0,

    watch(id, listener) {
      const entry = entries.get(id);
      if (!entry) return () => {};
      entry.watchers.add(listener);
      return () => {
        entry.watchers.delete(listener);
      };
    },

    replay(id) {
      const entry = entries.get(id);
      return { data: entry?.log ?? "", truncated: entry?.truncated ?? false, exited: entry?.exited ?? false };
    },

    follow(id, onChunk, onExit) {
      const entry = entries.get(id);
      if (!entry) return () => {};
      const follower = { onChunk, onExit };
      entry.followers.add(follower);
      return () => {
        entry.followers.delete(follower);
      };
    },
  };
}

/**
 * The lines from `from` to the end of the buffer as a person reads them: a line that wrapped is one line, and the blank
 * lines the terminal pads its screen with, at the end, are not output.
 */
function logicalLines(term: HeadlessTerminal, from: number, to: number): string[] {
  const buffer = term.buffer.active;
  const out: string[] = [];
  for (let y = from; y < to; y += 1) {
    const line = buffer.getLine(y);
    if (!line) continue;
    const text = line.translateToString(true);
    if (line.isWrapped && out.length > 0) out[out.length - 1] += text;
    else out.push(text);
  }
  // Trimmed after the joining, never before: a space at the point a line wrapped belongs to the line.
  const trimmed = out.map((text) => text.trimEnd());
  while (trimmed.length > 0 && trimmed[trimmed.length - 1] === "") trimmed.pop();
  return trimmed;
}

/** The emulator, loaded on first use. A module-scope import would put a terminal emulator in every window's bundle. */
export async function loadHeadless(): Promise<TerminalCtor> {
  const mod = (await import("@xterm/headless")) as unknown as {
    Terminal?: TerminalCtor;
    default?: { Terminal?: TerminalCtor };
  };
  const Terminal = mod.Terminal ?? mod.default?.Terminal;
  if (!Terminal) throw new Error("@xterm/headless did not export a Terminal");
  return Terminal;
}
