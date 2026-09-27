import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import "./index.css";
// xterm's stylesheet, here rather than in `TerminalPane.tsx`. A CSS import is a
// bundler statement that node cannot parse, so putting it in the component would
// put that component back out of reach of `node --test` — and the whole reason
// xterm is imported inside the pane's effect is that the pane stays importable.
import "@xterm/xterm/css/xterm.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
