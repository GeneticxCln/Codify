import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { readStoredThemeId } from "./appearance";
import { applyTintedTheme } from "./tint";
import { startUiScale } from "./uiScale";
import "./index.css";
// xterm's stylesheet, here rather than in `TerminalPane.tsx`. A CSS import is a
// bundler statement that node cannot parse, so putting it in the component would
// put that component back out of reach of `node --test` — and the whole reason
// xterm is imported inside the pane's effect is that the pane stays importable.
import "@xterm/xterm/css/xterm.css";

// The chosen theme goes onto the root *before* the first render, so the app
// never paints the default palette and then switches — an OLED panel would
// flash grey-black for a frame. `applyTintedTheme` reads storage and applies,
// tints included, so a user's chosen colour does not flash the stock one for a
// frame either; the settings pane re-applies on every choice and every edit.
applyTintedTheme(readStoredThemeId());

// The window's size, for the same reason and the same place: a window that painted at 100% and then
// grew to the saved 125% would shift every pixel of layout after the user had begun reading it.
startUiScale();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
