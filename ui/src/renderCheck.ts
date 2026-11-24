import { useEffect, useState } from "react";

import { listenShellEvent } from "./shellEvents";

/**
 * Whether the shell's render watchdog has finished judging this launch (`src-tauri/src/lib.rs`).
 *
 * The watchdog reads the window's rendering CPU for its first thirty seconds and, if most of a ten-second span is
 * over a third of a core, stops the animated backdrops for the session. One-off decorations used to start on a
 * fixed delay inside that window: the wordmark's shimmer waited ten seconds and ran nine, and on a Wayland window
 * that GTK composites on the CPU it cost 60–94% of a core while the theme's backdrop alone cost ~20%. The watchdog
 * read the shimmer as starvation and switched the backdrop off. Holding such decorations until this is true means
 * the watchdog measures the app, not the decoration it is meant to protect.
 */

/** What the shell sends when the watchdog has settled, with a verdict or without. */
export const RENDER_CHECK_SETTLED_EVENT = "codify:render-check-settled";
/** What the page asks at mount, for a page that loads after the event was sent. */
export const RENDER_CHECK_SETTLED_COMMAND = "codify_render_check_settled";

function inShell(): boolean {
  return typeof window !== "undefined" && Boolean((window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);
}

/**
 * Ask the shell once. Outside the shell (browser preview, tests) there is no watchdog to wait for, and a shell
 * built before the command existed answers with an error: both are "settled", which is the behaviour that window
 * had before this existed, minus the wait.
 */
export async function readRenderCheckSettled(): Promise<boolean> {
  if (!inShell()) return true;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return (await invoke<boolean>(RENDER_CHECK_SETTLED_COMMAND)) === true;
  } catch {
    return true;
  }
}

/** [`readRenderCheckSettled`] as React state, flipped by the shell's event when it arrives later. */
export function useRenderCheckSettled(): boolean {
  const [settled, setSettled] = useState(() => !inShell());
  useEffect(() => {
    if (settled) return;
    let cancelled = false;
    let off: (() => void) | undefined;
    const settle = (): void => {
      if (!cancelled) setSettled(true);
    };
    // Listen first, then ask: an event that lands between the two is still heard, and an answer of `true` covers
    // an event that was sent before this page loaded.
    void listenShellEvent<null>(RENDER_CHECK_SETTLED_EVENT, settle).then((unlisten) => {
      if (cancelled) unlisten();
      else off = unlisten;
      void readRenderCheckSettled().then((done) => {
        if (done) settle();
      });
    });
    return () => {
      cancelled = true;
      off?.();
    };
  }, [settled]);
  return settled;
}
