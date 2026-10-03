import React, { useEffect, useRef, useState } from "react";
import { Box, RotateCcw } from "lucide-react";
import { resizeMachine, writeMachine } from "../api";
import { THEME_CHANGE_EVENT } from "../appearance";
import { readRejection } from "../rejection.ts";
import { UI_SCALE_CHANGED } from "../uiScale";
import { xtermThemeFromDocument } from "../terminalTheme";
import { usableGrid } from "../terminalModel";
import type { MachineScreens } from "../machineScreens";
import { terminalFontSize } from "./TerminalPane";

/**
 * A machine: a jailed shell, drawn by xterm.js and driven by a PTY the shell owns (`src-tauri/src/machine.rs`).
 *
 * **It is a window onto a screen that is held elsewhere.** The assistant reads and types into a machine whether or not this
 * pane is mounted, so what the machine has said is held by `machineScreens.ts` above every pane, and this pane is one way of
 * looking at it: it writes the store's replay into its own xterm to arrive at the same screen, then follows the live
 * stream. When the pane goes (the tab is behind another) nothing is lost, and the next one replays to the same place.
 *
 * It is `TerminalPane` for the person's own shell in everything it can share, and nothing in what it cannot: no scrollback
 * history filed per workspace (a jail's screen is not a terminal's history), no clipboard-drawer paste target (that is the
 * person's own shell's), and a header that says the things a person must be able to see at a glance about a jail: that it
 * is one, whether it can reach the network, whether the project in it is the machine's own copy, and a way to start it again
 * from a clean one.
 *
 * xterm is imported inside the effect for the reason `TerminalPane` gives: it needs a real DOM, a module-scope import would
 * make this file unimportable by `node --test`, and a window that never opens a machine should not carry the renderer.
 */
export interface MachinePaneProps {
  machineId: string;
  /** Whether the jail shares the host's network, as the person chose when they opened it. It cannot change. */
  network: boolean;
  /** The shell in it has finished. The screen stays; the input does not. */
  exited?: boolean;
  /**
   * The project under `/work` is the machine's own copy: what it changes is kept in the machine and discarded with it, and the
   * person's files are never written. False is a read-only project (this host could not make the copy), which `projectNote` explains.
   */
  copyOnWrite?: boolean;
  projectNote?: string;
  /** Start the machine again from a clean project. The pane asks nothing: whoever owns the machine decides whether to ask. */
  onReset?: () => void;
  /** Where the screen is held. */
  screens: MachineScreens;
  /** Take the keyboard once the terminal is ready; off for the second half of a split, which must not take it from the first. */
  autoFocus?: boolean;
}

/** What the header says about the network, and what a hover adds. Words, because colour alone is not a label. */
const NETWORK_OFF = "No network";
const NETWORK_ON = "Network on";
const NETWORK_OFF_DETAIL = "This machine has no network: it can reach nothing outside itself.";
const NETWORK_ON_DETAIL =
  "This machine shares this computer's network, so it can reach the internet and services running on this computer. " +
  "It cannot be switched off while the machine runs: open a new machine without a network.";

/** What the header says about the project, and what a hover adds. */
const PROJECT_OWN_COPY = "Project at /work: your changes stay in this machine";
const PROJECT_OWN_COPY_DETAIL =
  "Your project is at /work, and this machine edits its own copy: nothing it changes reaches your files, and all of it " +
  "is discarded when the machine closes or is reset.";
const PROJECT_READ_ONLY = "Project at /work, read-only";
const projectReadOnlyDetail = (note?: string): string =>
  "Your project is mounted at /work and cannot be changed from here. This machine's home is thrown away when it closes." +
  (note ? ` A private copy could not be made on this computer: ${note}.` : "");

export const MachinePane: React.FC<MachinePaneProps> = ({
  machineId,
  network,
  exited = false,
  copyOnWrite = false,
  projectNote,
  onReset,
  screens,
  autoFocus = true,
}) => {
  const hostRef = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState<string | null>(null);

  const idRef = useRef(machineId);
  const exitedRef = useRef(exited);
  const autoFocusRef = useRef(autoFocus);
  const screensRef = useRef(screens);
  useEffect(() => {
    idRef.current = machineId;
  }, [machineId]);
  useEffect(() => {
    exitedRef.current = exited;
  }, [exited]);
  useEffect(() => {
    autoFocusRef.current = autoFocus;
  }, [autoFocus]);
  useEffect(() => {
    screensRef.current = screens;
  }, [screens]);

  useEffect(() => {
    let disposed = false;
    let term: import("@xterm/xterm").Terminal | undefined;
    let observer: ResizeObserver | undefined;
    const unsubscribes: Array<() => void> = [];
    // The size the PTY was last told, so a re-fit that changed nothing is not an IPC round trip.
    let told = "";

    void (async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([import("@xterm/xterm"), import("@xterm/addon-fit")]);
      if (disposed || !hostRef.current) return;

      term = new Terminal({
        cursorBlink: false,
        convertEol: false,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        fontSize: terminalFontSize(),
        scrollback: 5000,
        theme: xtermThemeFromDocument(),
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(hostRef.current);

      const onThemeChange = (): void => {
        if (!disposed && term) term.options.theme = xtermThemeFromDocument();
      };
      window.addEventListener(THEME_CHANGE_EVENT, onThemeChange);
      unsubscribes.push(() => window.removeEventListener(THEME_CHANGE_EVENT, onThemeChange));

      // Arrive at the screen the store has: what the machine has said, in order. Then follow it, from the same point: a
      // chunk is either in the replay or arrives after it, never both, because both happen without yielding.
      const { data, truncated } = screensRef.current.replay(idRef.current);
      if (truncated) term.write("\x1b[2m… the start of this machine's output is no longer kept …\x1b[0m\r\n");
      if (data) term.write(data);
      unsubscribes.push(screensRef.current.follow(idRef.current, (chunk) => term?.write(chunk)));

      term.onData((typed) => {
        if (exitedRef.current) return;
        void writeMachine(idRef.current, typed).catch((err: unknown) =>
          setFailed(readRejection(err, "Could not write to that machine")),
        );
      });

      const settle = (): void => {
        if (disposed || !term) return;
        try {
          fit.fit();
        } catch {
          return;
        }
        const grid = usableGrid(term.cols, term.rows);
        if (!grid) return;
        const key = `${grid.cols}x${grid.rows}`;
        if (key === told) return;
        told = key;
        // The PTY first, the screen after: both in the order the machine will see them, so a resize between two chunks of
        // output lands between them on the screen as it does in the shell.
        screensRef.current.resize(idRef.current, grid.cols, grid.rows);
        void resizeMachine(idRef.current, grid.cols, grid.rows).catch((err: unknown) =>
          setFailed(readRejection(err, "Could not resize that machine")),
        );
      };
      settle();

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
      if (autoFocusRef.current) term.focus();
    })().catch((err: unknown) => {
      if (!disposed) setFailed(readRejection(err, "Could not load the terminal renderer (xterm.js failed to import)"));
    });

    return () => {
      disposed = true;
      for (const off of unsubscribes) off();
      unsubscribes.length = 0;
      observer?.disconnect();
      term?.dispose();
    };
  }, []);

  return (
    <section aria-label="Machine" data-machine-id={machineId} className="flex min-h-0 flex-1 flex-col bg-codify-bg">
      <div className="flex flex-shrink-0 items-center gap-2 border-b border-codify-border bg-codify-surface px-3 py-1.5">
        <Box className="h-3.5 w-3.5 flex-shrink-0 text-codify-info" aria-hidden />
        <span className="flex-shrink-0 text-xs text-codify-secondary">Jailed shell</span>
        <span
          data-testid="machine-network"
          title={network ? NETWORK_ON_DETAIL : NETWORK_OFF_DETAIL}
          className={
            network
              ? "flex-shrink-0 rounded border border-codify-warning/60 bg-codify-warning/20 px-1.5 py-0.5 text-2xs text-codify-warning-ink"
              : "flex-shrink-0 rounded border border-codify-border px-1.5 py-0.5 text-2xs text-codify-muted"
          }
        >
          {network ? NETWORK_ON : NETWORK_OFF}
        </span>
        <span
          data-testid="machine-project"
          className="min-w-0 truncate text-2xs text-codify-muted"
          title={copyOnWrite ? PROJECT_OWN_COPY_DETAIL : projectReadOnlyDetail(projectNote)}
        >
          {copyOnWrite ? PROJECT_OWN_COPY : PROJECT_READ_ONLY}
        </span>
        {onReset && (
          <button
            type="button"
            aria-label="Reset machine"
            title="Start this machine again from a clean project: everything running in it stops and everything it changed is discarded"
            onClick={onReset}
            className="ml-auto flex flex-shrink-0 items-center gap-1 rounded border border-codify-border px-1.5 py-0.5 text-2xs text-codify-secondary hover:bg-codify-raised/60"
          >
            <RotateCcw className="h-3 w-3" aria-hidden />
            Reset
          </button>
        )}
      </div>
      {failed && (
        <div
          role="alert"
          className="mx-3 mt-2 rounded-lg border border-codify-danger bg-codify-danger/40 p-2 text-xs text-codify-danger-ink"
        >
          {failed}
        </div>
      )}
      {exited && (
        <p role="status" className="mx-3 mt-2 text-xs text-codify-muted">
          This machine's shell has exited. Its screen is still here. Reset it to start again from a clean project.
        </p>
      )}
      <div
        ref={hostRef}
        className="min-h-0 flex-1 overflow-hidden px-2 py-1"
        onClick={() => hostRef.current?.querySelector<HTMLElement>("textarea")?.focus()}
      />
    </section>
  );
};
