import { readRejection } from "../rejection.ts";
import React, { useEffect, useRef, useState } from "react";
import { writeTerminal } from "../api";
import { THEME_CHANGE_EVENT } from "../appearance";
import { UI_SCALE_CHANGED, uiScaleFactor } from "../uiScale";
import { xtermThemeFromDocument } from "../terminalTheme";
import {
  listenShellEvent,
  readTerminalExit,
  readTerminalOutput,
  TERMINAL_EXIT,
  TERMINAL_OUTPUT,
} from "../shellEvents";
import {
  bufferOutput,
  DEFAULT_GRID,
  drainOutput,
  emptyOutputBuffer,
  gridChanged,
  usableGrid,
  type Grid,
  type OutputBuffer,
} from "../terminalModel";
import {
  appendTerminalHistory,
  readTerminalHistory,
  replayFor,
} from "../terminalHistory";
import { claimTerminal, releaseTerminal, RESUME_SEAM } from "../terminalBuffer";

/** The terminal's text size in px: 12 at 100%, and the same proportion at any UI scale. */
export function terminalFontSize(): number {
  return Math.round(12 * uiScaleFactor());
}

/**
 * A terminal, rendered by xterm.js and driven by a PTY the shell owns.
 *
 * ## xterm is imported inside the effect, and that is not laziness for its own sake
 *
 * Three reasons, and the first is correctness:
 *
 * 1. xterm needs a real DOM and a measured font. A module-scope import makes
 *    this component unimportable by `node --test`, which is the only harness the
 *    UI has — so the pane's markup could never be rendered and the file could
 *    only be checked by reading its source. Importing inside the effect is what
 *    keeps `terminalPane.test.ts` able to reach the JSX at all.
 * 2. The bundle should not carry a terminal renderer for a tab that was never
 *    opened, and the effect is the first moment we know one was.
 * 3. `renderToStaticMarkup` runs no effects, so a render test pulls in nothing.
 *
 * xterm's **stylesheet** is the exception, and it is imported by `main.tsx`
 * rather than here — a CSS import is a bundler statement that node cannot parse,
 * and one of those would put this file back out of the harness's reach.
 *
 * ## The output stream never touches React state
 *
 * A PTY emits faster than a frame — `ls` in a large directory, a build, `yes`.
 * Every chunk that went through `useState` would re-render this component and
 * everything above it, once per chunk. So the subscription writes **straight
 * into the xterm instance**: no state, no re-render, the cost of a chunk is a
 * buffer append. The only state here is `failed`, which changes twice in a
 * terminal's life.
 *
 * ## The subscription goes up before xterm exists, on purpose
 *
 * `codify_terminal_open` starts a reader thread that emits immediately, and the
 * reply naming the terminal has already lost the race with the first prompt by
 * the time this effect runs — xterm is a dynamic import away. Subscribing after
 * xterm loads loses that output, and a terminal that opens blank reads as broken
 * rather than as slow. So the listener goes up first and every chunk lands in an
 * `OutputBuffer` (`terminalModel.ts`) keyed by id; this terminal's chunks are
 * flushed into xterm the moment it exists.
 *
 * ## What this pane does not do
 *
 * It does not intercept keystrokes. A terminal is a shell, and every key the
 * user presses belongs to whatever the shell has drawn — Ctrl-C, the arrow keys,
 * a bracketed-paste sequence, a tmux prefix. xterm turns a key into exactly the
 * bytes a real terminal would send and this hands them to the PTY. The keys the
 * *window* claims are handled one layer up by `App.tsx`, which sees them before
 * they reach here.
 */
export interface TerminalPaneProps {
  /** The PTY's id, which is also this tab's id. */
  terminalId: string;
  /**
   * The workspace this shell was started in, and the key for the scrollback a
   * reopened pane restores.
   *
   * The tab's own workspace rather than the composer's current selection: the
   * shell's working directory was pinned when it started, and showing it another
   * workspace's earlier session would be answering a question nobody asked.
   */
  workspaceId?: string;
  /** The shell has finished: the scrollback stays, the input stops. */
  exited?: boolean;
  /**
   * Called when the pane has a real size, so the PTY can be told about it. The
   * id comes along because the pane is the only party that knows which terminal
   * it is drawing.
   */
  onResize?: (terminalId: string, grid: Grid) => void;
  /** The shell finished, so the tab can say so too. */
  onExit?: (terminalId: string) => void;
}

export const TerminalPane: React.FC<TerminalPaneProps> = ({
  terminalId,
  workspaceId,
  exited = false,
  onResize,
  onExit,
}) => {
  const hostRef = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState<string | null>(null);

  // Read through refs so the one subscription below is keyed on the mount alone.
  // An effect that depended on a callback prop would tear down the terminal and
  // its listener every time the parent re-created that callback, and the
  // prompt would go with it.
  const terminalIdRef = useRef(terminalId);
  const onResizeRef = useRef(onResize);
  const onExitRef = useRef(onExit);
  const exitedRef = useRef(exited);
  const workspaceIdRef = useRef(workspaceId);

  useEffect(() => {
    terminalIdRef.current = terminalId;
  }, [terminalId]);
  useEffect(() => {
    workspaceIdRef.current = workspaceId;
  }, [workspaceId]);
  useEffect(() => {
    onResizeRef.current = onResize;
  }, [onResize]);
  useEffect(() => {
    onExitRef.current = onExit;
  }, [onExit]);
  useEffect(() => {
    exitedRef.current = exited;
  }, [exited]);

  useEffect(() => {
    let disposed = false;
    let term: import("@xterm/xterm").Terminal | undefined;
    let observer: ResizeObserver | undefined;
    const unsubscribes: Array<() => void> = [];

    // Owned outside the async body so the chunk handler and the flush both see
    // one buffer: the listener is up before xterm is, and what it catches in the
    // meantime has to survive until xterm exists.
    const outputBuffer: OutputBuffer = emptyOutputBuffer();
    let current: Grid = DEFAULT_GRID;

    /**
     * Claim this terminal: what it said while this pane was not mounted is
     * the pane's first write, ahead of any live chunk.
     *
     * Claiming also stops the app-level recorder from filing further chunks —
     * from here the pane is the display *and* the recorder, which is the
     * one-copy bargain: bytes the pane shows are bytes the pane files. The
     * backlog is replayed even for an already-exited shell, whose tail is
     * exactly what a user reopening a finished tab is trying to read; the
     * workspace replay below applies only where this shell has nothing of its
     * own to show.
     */
    const backlogged = claimTerminal(terminalIdRef.current);

    /**
     * What earlier sessions left in this workspace's scrollback, read *now*.
     *
     * Not when xterm arrives: the output listener below files this shell's own
     * chunks into the same record, so a chunk that lands in the gap while
     * xterm is still loading would already be in the record when it was read,
     * be written as part of the "restored session", and be written again by the
     * buffer's flush — the first prompt twice.
     */
    const tail = workspaceIdRef.current
      ? readTerminalHistory(workspaceIdRef.current)
      : "";

    const publish = (grid: Grid): void => {
      if (!gridChanged(current, grid)) return;
      current = grid;
      onResizeRef.current?.(terminalIdRef.current, grid);
    };

    /**
     * Hold an unsubscribe that may land after this pane is gone.
     *
     * `listenShellEvent` resolves asynchronously and there is no telling how
     * late. Dropping a late arrival on the floor leaves a listener calling
     * `setFailed` on an unmounted pane, which is a leak that outlives the tab.
     */
    const track = (pending: Promise<() => void>): void => {
      void pending.then((off) => {
        if (disposed) off();
        else unsubscribes.push(off);
      });
    };

    void (async () => {
      // Subscribed first, for the reason in the module docs.
      track(
        listenShellEvent<unknown>(TERMINAL_OUTPUT, (payload) => {
          const chunk = readTerminalOutput(payload);
          if (!chunk) return;
          // Every chunk is buffered, this terminal's or not: the id is how the
          // right pane knows which of them are its own, and a chunk that
          // arrives before this pane has even been told its id is the one that
          // must not be dropped.
          bufferOutput(outputBuffer, chunk.id, chunk.data);
          if (chunk.id !== terminalIdRef.current) return;
          // File **this** terminal's bytes, and only its own — the one owner per
          // chunk that `terminalBuffer`'s rules are built on. It used to file
          // every chunk that arrived, before this check, on the theory that
          // anything this workspace's terminals said belongs to the workspace.
          // That was right when this was the only listener and is a duplicate
          // now that the app-level recorder holds what no pane is displaying:
          // the *other* terminal's mounted pane files this terminal's background
          // build into the same workspace record the backlog will replay from,
          // so a returning pane prints every line of that build twice. The pane
          // files what it displays, the store files the rest (and merges it on
          // `retireTerminal`, so a tab closed in the background is still part
          // of what the next pane in this workspace restores).
          if (workspaceIdRef.current) {
            appendTerminalHistory(workspaceIdRef.current, chunk.data);
          }
          // No xterm yet means the text waits: it is written in one go the
          // moment the renderer exists, just below. Draining it into nothing
          // would be losing it with extra steps.
          if (!term) return;
          const held = drainOutput(outputBuffer, chunk.id);
          if (held) term.write(held);
        }),
      );
      track(
        listenShellEvent<unknown>(TERMINAL_EXIT, (payload) => {
          const id = readTerminalExit(payload);
          if (id !== terminalIdRef.current) return;
          if (term) {
            // The exit goes *into* the scrollback, not only into a banner above
            // it: scrolling up after a command fails is the normal way to read a
            // failure, and a banner outside the terminal is not in that
            // scrollback.
            term.write("\r\n\x1b[2m— process exited —\x1b[0m\r\n");
            term.options.disableStdin = true;
          }
          // The marker is display-only, as it always was: the workspace tail
          // keeps the byte stream, and a background shell whose exit the store
          // recorded gets its marker merged at retirement instead. Two filers
          // for one marker is how it ends up in a restore twice.
          onExitRef.current?.(id);
        }),
      );

      if (disposed) return;
      const [{ Terminal }, { FitAddon }] = await Promise.all([
        import("@xterm/xterm"),
        import("@xterm/addon-fit"),
      ]);
      if (disposed || !hostRef.current) return;

      term = new Terminal({
        // A shell the user is typing at, not a page: a blinking cursor is noise
        // at a prompt, and `convertEol: false` is what keeps a bare `\n` from
        // being turned into a carriage return the shell did not send.
        cursorBlink: false,
        convertEol: false,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        fontSize: terminalFontSize(),
        scrollback: 5000,
        // Read out of the document rather than written here. These three hexes
        // used to be literals, and they were the *default theme's* — so the
        // terminal was the one grey-blue box in all nine themes, including the
        // two that draw rain behind it. See `ui/src/terminalTheme.ts`.
        theme: xtermThemeFromDocument(),
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(hostRef.current);

      // …and re-read on every change, because a terminal that was themed on
      // open and stale after a switch is the same defect one moment later.
      // Reassigning `options.theme` is xterm's supported way to recolour a live
      // instance: it repaints the buffer, and nothing is re-run and no scroll
      // position moves.
      const onThemeChange = (): void => {
        if (disposed || !term) return;
        term.options.theme = xtermThemeFromDocument();
      };
      window.addEventListener(THEME_CHANGE_EVENT, onThemeChange);
      unsubscribes.push(() =>
        window.removeEventListener(THEME_CHANGE_EVENT, onThemeChange),
      );

      // Chronological order, which is also display order: everything this
      // workspace said up to the last unmount (earlier sessions under the
      // seam, then whatever of this shell was on screen when its pane went
      // away — the tail already holds it, because a mounted pane files as it
      // displays), then the seam marking the gap, then what the shell said
      // while nobody was displaying it. The backlog continues the tail; it is
      // never older than it. A shell with no backlog gets the workspace
      // replay alone, exactly as before the recorder existed.
      const restored = replayFor(tail);
      if (restored) term.write(restored);
      if (backlogged) {
        term.write((restored ? RESUME_SEAM : "") + backlogged);
        // …and filed, because from here this pane is both the display and the
        // recorder: the backlog is bytes it is about to show, and a shell's
        // first prompt is nearly always backlog (the PTY is printing before the
        // tab exists, so the recorder caught it while no pane owned it). Filing
        // only the chunks that arrive *after* this point left that prompt on
        // screen but absent from the workspace scrollback, so the next pane in
        // this workspace restored everything except the start of the session.
        // The store deleted these bytes at claim, so the copy here is the only
        // one — the same merge `retireTerminal` does for a tab closed in the
        // background, and for the same reason.
        if (workspaceIdRef.current) {
          appendTerminalHistory(workspaceIdRef.current, backlogged);
        }
      }
      // Everything the shell said between `open` and now, in one write.
      const held = drainOutput(outputBuffer, terminalIdRef.current);
      if (held) term.write(held);

      term.onData((data) => {
        if (exitedRef.current) return;
        void writeTerminal(terminalIdRef.current, data).catch((err: any) =>
          setFailed(readRejection(err, "Could not write to that terminal")),
        );
      });

      // `fit()` does the pixel work against the real font metrics and settles
      // xterm's own `cols`/`rows`; all that is left is to check them and hand
      // the shell the result. It throws when the host has not been laid out,
      // which is the normal state of a pane that is still mounting, so the
      // ResizeObserver below is the retry rather than a second code path.
      const settle = (): void => {
        if (disposed || !term) return;
        try {
          fit.fit();
        } catch {
          return;
        }
        const grid = usableGrid(term.cols, term.rows);
        if (grid) publish(grid);
      };

      settle();

      // The UI scale is a root font size, and xterm draws its own canvas in a font size we hand it,
      // so it is the one text surface that does not follow along by itself. A new size also changes
      // how many columns fit, and a font change does not resize the host (so the ResizeObserver
      // below stays quiet): the fit and the grid the shell is told have to be redone here.
      const onScaleChange = (): void => {
        if (disposed || !term) return;
        term.options.fontSize = terminalFontSize();
        settle();
      };
      window.addEventListener(UI_SCALE_CHANGED, onScaleChange);
      unsubscribes.push(() => window.removeEventListener(UI_SCALE_CHANGED, onScaleChange));

      if (typeof ResizeObserver !== "undefined" && hostRef.current) {
        observer = new ResizeObserver(settle);
        observer.observe(hostRef.current);
      }

      // A terminal the user cannot type into until they click it is the classic
      // xterm complaint, and the tab bar has just taken the focus.
      term.focus();
    })().catch((err: any) => {
      if (disposed) return;
      setFailed(
        readRejection(err, "Could not load the terminal renderer (xterm.js failed to import)")
      );
    });

    return () => {
      disposed = true;
      for (const off of unsubscribes) off();
      unsubscribes.length = 0;
      observer?.disconnect();
      term?.dispose();
      // Hand the terminal back to the recorder. What the shell says from here
      // is a background terminal's output again: held by the store, replayed
      // by the next pane to claim it. (A closed tab is already retired by
      // `handleCloseTab` before this runs; retiring twice is idempotent, and
      // a release after a retire simply has nothing to release.)
      releaseTerminal(terminalIdRef.current);
    };
  }, []);

  return (
    <section
      aria-label="Terminal"
      data-terminal-id={terminalId}
      className="flex-1 flex flex-col min-h-0 bg-codify-bg"
    >
      {failed && (
        <div
          role="alert"
          className="mx-3 mt-2 p-2 rounded-lg text-xs bg-codify-danger/40 border border-codify-danger text-codify-danger-ink"
        >
          {failed}
        </div>
      )}
      {exited && (
        <p role="status" className="mx-3 mt-2 text-xs text-codify-muted">
          This shell has exited. Its scrollback is still here.
        </p>
      )}
      <div
        ref={hostRef}
        className="flex-1 min-h-0 px-2 py-1 overflow-hidden"
        // Clicking the padding around the terminal should put the cursor in the
        // shell, the way clicking a real terminal does. xterm owns its own
        // textarea once it is open; this only reaches it.
        onClick={() =>
          hostRef.current
            ?.querySelector<HTMLElement>("textarea")
            ?.focus()
        }
      />
    </section>
  );
};
