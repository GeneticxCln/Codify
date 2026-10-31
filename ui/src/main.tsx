import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { readStoredThemeId } from "./appearance";
import { applyTintedTheme } from "./tint";
import { startUiScale } from "./uiScale";
import { prepareMotionSession } from "./motionPreference";
import "./index.css";
// xterm's stylesheet, here rather than in `TerminalPane.tsx`. A CSS import is a
// bundler statement that node cannot parse, so putting it in the component would
// put that component back out of reach of `node --test` — and the whole reason
// xterm is imported inside the pane's effect is that the pane stays importable.
import "@xterm/xterm/css/xterm.css";

// Resolve this launch's id before the first read of the motion store. The
// store's facts (a starvation verdict, "Animate anyway") live in
// `sessionStorage`, and in this webview that survived a process exit: without
// this id, a fresh launch read last launch's answer and started animating on
// exactly the machine the verdict was about. The command is local IPC; a
// preview with no shell falls back to a per-page id.
async function start(): Promise<void> {
  // The chosen theme and scale go onto the root *before* any IPC, so the app
  // never paints the default palette and then switches — an OLED panel would
  // flash grey-black for a frame. `applyTintedTheme` reads storage and applies,
  // tints included, so a user's chosen colour does not flash the stock one for
  // a frame either; the settings pane re-applies on every choice and every edit.
  applyTintedTheme(readStoredThemeId());

  // The window's size, for the same reason and the same place: a window that painted at 100% and then
  // grew to the saved 125% would shift every pixel of layout after the user had begun reading it.
  startUiScale();

  try {
    // `prepareMotionSession` bounds the shell's boot-id call itself, so a shell
    // that never answers delays the first render by at most that bound and then
    // leaves the per-page id in place. The theme above is already painted.
    await prepareMotionSession();
  } catch {
    // The page still has to mount: an unresolved id only means the session
    // facts are not trusted, not that the window is unusable.
  }

  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
}

void start();
