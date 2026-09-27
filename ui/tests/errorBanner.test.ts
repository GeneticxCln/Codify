/**
 * The app-wide error banner is clearable.
 *
 * This is the surface a refusal lands on when there is no pane to put it in —
 * a terminal that would not start, an engine that went away, a settings write
 * the engine refused. It had no dismiss control, and `setError(null)` is only
 * ever called by whichever handler owns the *next* action, so a message from an
 * action the user does not repeat simply stayed on screen. That is what made
 * clicking Terminal in a browser tab feel broken even after the refusal text
 * landed: the one honest sentence the panes can give you was permanent.
 *
 * A freeze rather than a render, for the same reason as the freezes in
 * `browserPane.test.ts`: nothing in the suite mounts `App.tsx`, so the banner's
 * markup cannot be rendered in isolation. Crude on purpose — *deleting* the
 * dismiss control has to fail here rather than in someone's window.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");

/** The banner's JSX, from the `error &&` guard to the end of that block. */
const banner = app.split("{error && (")[1]?.split("\n          )}")[0];

test("the app error banner can be dismissed", () => {
  assert.ok(banner, "App.tsx no longer renders the error banner in the expected shape");
  assert.match(
    banner,
    /onClick=\{\(\) => setError\(null\)\}/,
    "the error banner has no way to clear itself; a refusal the reader has " +
      "understood and cannot dismiss is an obstacle, not a report",
  );
});

test("the dismiss control is an IconButton, so it cannot lose its label", () => {
  // `IconButton` makes `label` a required prop, so a dismiss icon that reached
  // production without an accessible name is a type error rather than something
  // a test has to remember to check. Asserting the component here is what keeps
  // that guarantee pointed at this control.
  assert.match(
    banner,
    /<IconButton[\s\S]*?label="Dismiss error"/,
    "the dismiss control is not an IconButton with a required label",
  );
});

test("the banner is announced, not just coloured red", () => {
  assert.match(
    banner,
    /role="alert"/,
    "the app error banner is not announced to assistive technology",
  );
});

test("a browser pane's refusal stays in its pane, not in the app banner", () => {
  // One surface per refusal. The panes have their own channels (`BrowserPane`'s
  // `error`, `TerminalPane`'s `failed`), and a pane whose message also went to
  // the banner would show the same refusal twice for one mistake — which is how
  // a dismissable banner starts hiding a message that is still being reported.
  //
  // The `assert.ok` is load-bearing: without it a regex that matched nothing
  // would leave an empty string, and `doesNotMatch("")` passes for ever.
  const paneRenders = app.match(/<BrowserPane[\s\S]*?\/>/)?.[0] ?? "";
  assert.ok(paneRenders, "App.tsx no longer renders <BrowserPane>");
  assert.match(
    paneRenders,
    /error=\{[\s\S]*?pendingBrowser\.error/,
    "the browser pane is no longer handed the refusal for its own tab",
  );
  assert.doesNotMatch(
    paneRenders,
    /setError/,
    "the browser pane's own error is being routed through the app banner as well",
  );
});
