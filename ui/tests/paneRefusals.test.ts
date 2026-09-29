/**
 * What the two panes answer in a plain browser tab.
 *
 * The standalone build has no Rust process, so a browser pane's page and a
 * terminal's shell simply do not exist there. The panes handle that by
 * rendering the message they are refused, and this file pins the half of that
 * contract which lives in `api.ts`: the refusal has to *arrive*, and it has to
 * be the shell's sentence rather than a claim that the command is fictional.
 *
 * It arrived once already in the wrong shape. `fallbackHttpInvoke` had no case
 * for the `codify_browser_*` / `codify_terminal_*` commands, so all seven
 * reached `default:` and threw `Unknown command: codify_browser_open` — and
 * because the *first* address is refused before its tab has a page to show, the
 * error was filed against an id no tab had and the pane read nothing. The user
 * typed an address and the app did nothing at all, with no error and no alert.
 * A thrown error is therefore necessary but not sufficient, which is why
 * `browserPane.test.ts` freezes the other half.
 *
 * These run through the real exported functions, not the private switch, so
 * they also fail if a call site stops using `tauriInvoke`.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

// `api.ts` reads `localStorage` at module scope and `window` inside
// `tauriInvoke`. The loader supplies a store and no `window`, so every call
// below takes the HTTP fallback — which is exactly the situation under test.
const {
  openBrowserWebview,
  navigateBrowserWebview,
  focusBrowserWebview,
  resizeBrowserWebviews,
  closeBrowserWebview,
  openBrowserDevtools,
  closeBrowserDevtools,
  browserDevtoolsState,
  browserDevtoolsAvailable,
  openTerminal,
  writeTerminal,
  resizeTerminal,
  closeTerminal,
} = await import("../src/api.ts");
const { readRejection } = await import("../src/rejection.ts");

/** Every command a pane can send, with arguments shaped like the real call. */
const PANE_COMMANDS: Array<[string, () => Promise<unknown>]> = [
  [
    "openBrowserWebview",
    () =>
      openBrowserWebview("browser-1", "https://a.example", {
        x: 0,
        y: 0,
        width: 800,
        height: 600,
      }),
  ],
  ["focusBrowserWebview", () => focusBrowserWebview("browser-1")],
  [
    "resizeBrowserWebviews",
    () => resizeBrowserWebviews({ x: 0, y: 0, width: 800, height: 600 }),
  ],
  ["openBrowserDevtools", () => openBrowserDevtools("browser-1")],
  ["closeBrowserDevtools", () => closeBrowserDevtools("browser-1")],
  ["browserDevtoolsState", () => browserDevtoolsState("browser-1")],
  ["browserDevtoolsAvailable", () => browserDevtoolsAvailable()],
  ["navigateBrowserWebview", () => navigateBrowserWebview("browser-1", "https://b.example")],
  ["closeBrowserWebview", () => closeBrowserWebview("browser-1")],
  ["openTerminal", () => openTerminal("w1", 80, 24)],
  ["writeTerminal", () => writeTerminal("term-1", "ls\n")],
  ["resizeTerminal", () => resizeTerminal("term-1", 100, 30)],
  ["closeTerminal", () => closeTerminal("term-1")],
];

for (const [name, call] of PANE_COMMANDS) {
  test(`${name} refuses by name instead of claiming the command is unknown`, async () => {
    await assert.rejects(call, (err: Error) => {
      // The sentence has to say *why*, because "Unknown command: …" tells a
      // user their app is broken rather than that it is a browser.
      assert.doesNotMatch(
        err.message,
        /Unknown command/,
        `${name} still reaches the unknown-command default; the user is told a ` +
          "command that exists does not"
      );
      assert.match(
        err.message,
        /desktop shell/i,
        `${name} refused without saying which app provides it`
      );
      return true;
    });
  });
}

test("one refusal sentence covers both panes, so they cannot drift apart", async () => {
  // Seven call sites writing seven similar sentences is how a terminal ends up
  // telling a user about a browser. The shared constant is the guard.
  const messages = await Promise.all(
    PANE_COMMANDS.map(async ([, call]) => {
      try {
        await call();
        return null;
      } catch (err: any) {
        return readRejection(err, "");
      }
    })
  );
  assert.equal(new Set(messages).size, 1, `panes disagreed: ${JSON.stringify(messages)}`);
});

test("the refusal survives a pane that only shows `err.message`", async () => {
  // Every caller in `App.tsx` renders `readRejection(err, "<its own default>")`,
  // so a rejection carrying no message would be replaced by a generic sentence
  // and the reason would be lost. This asserts the message is present, not
  // empty, rather than asserting on the exact wording, which is allowed to
  // improve.
  const err = await openTerminal("w1", 80, 24).then(
    () => null,
    (e: Error) => e
  );
  assert.ok(err, "openTerminal resolved outside the shell, which cannot happen");
  assert.ok(err.message.trim().length > 0, "the refusal arrived with no message");
});
