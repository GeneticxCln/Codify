import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { initAppearance } from "./appearance";
import "./index.css";
// xterm's stylesheet, here rather than in `TerminalPane.tsx`. A CSS import is a
// bundler statement that node cannot parse, so putting it in the component would
// put that component back out of reach of `node --test` — and the whole reason
// xterm is imported inside the pane's effect is that the pane stays importable.
import "@xterm/xterm/css/xterm.css";

// The chosen theme goes onto the root *before* the first render, so the app
// never paints the default palette and then switches — an OLED panel would
// flash grey-black for a frame. `initAppearance` reads storage and applies;
// the settings pane re-applies on every choice.
initAppearance();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
