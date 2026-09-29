/**
 * The UI's mirror of the shell's navigation guard, and the pin that keeps it
 * honest.
 *
 * `ui/src/browserDispatch.ts` classifies an address *before* the round trip,
 * so a refusal lands in the tab the user typed in, in the same frame, and no
 * page is ever seated for an address that cannot load. The shell
 * (`src-tauri/src/browser.rs`) stays the enforcement point — its guard sees
 * every navigation a page attempts, redirects included — which makes this
 * module a *mirror*, not a second guard, and a mirror that disagreed with the
 * original would be worse than no mirror: stricter, and addresses that work
 * dead-end at the address bar; looser, and the shell refuses what the UI
 * already seated.
 *
 * So the pin runs in **both directions**:
 *
 * - everything this module refuses, the shell's guard must refuse too
 *   (asserted from Rust, in
 *   `browser.rs::the_ui_dispatch_mirror_agrees_with_the_navigation_guard`,
 *   which reads this file's tables and runs them through
 *   `navigation_allowed`);
 * - everything this module allows, the shell's guard must allow too
 *   (asserted below, from the same tables, through the classification this
 *   module actually performs).
 *
 * The tables below spell addresses the way a user types them — bare hosts
 * included — which is also the shape the Rust test feeds its own parser
 * (it prefixes the scheme `normaliseAddress` would). Adding a row to either
 * table therefore exercises both implementations at once; disagreeing with
 * the guard fails the suite on whichever side runs first.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  classifyBrowserAddress,
  NON_WEB_ADDRESS_MESSAGE,
} from "../src/browserDispatch.ts";

/**
 * Addresses a user might type that the shell refuses, spelled as they would
 * be typed. The Rust test `the_ui_dispatch_mirror_agrees_with_the_navigation_guard`
 * parses this list (it must stay a flat array of double-quoted strings) and
 * asserts the guard refuses each one.
 */
const REFUSED_IN_UI = [
  "http://localhost/",
  "https://LOCALHOST:8443/login",
  "http://localhost./",
  "http://api.localhost/",
  "http://tauri.localhost/",
  "http://127.0.0.1/",
  "http://127.0.0.1:8080/engine/health",
  "http://127.9.9.9:1/",
  "http://2130706433/",
  "http://0x7f000001/",
  "http://0177.0.0.1/",
  "http://[::1]/",
  "http://[::1]:3000/",
  "http://[0:0:0:0:0:0:0:1]/",
  "http://[::ffff:127.0.0.1]/",
  "http://0.0.0.0:9/",
  "http://0/",
  "http://[::]/",
  "tauri://localhost/",
  "file:///etc/passwd",
  "javascript:alert(document.cookie)",
  "data:text/html,<h1>hi</h1>",
  "about:blank",
  "devtools://devtools/bins/devtools",
  // Typed the way a user types them — the pane's normaliseAddress puts the
  // scheme on, and the classification must still refuse.
  "localhost",
  "localhost:3000",
  "127.0.0.1",
  "127.0.0.1:8080",
  "0x7f000001",
  "0177.0.0.1",
  "[::1]",
  "0",
];

/**
 * Ordinary sites the shell allows, spelled as they would be typed. The Rust
 * test asserts the guard allows each of these too.
 */
const ALLOWED_IN_UI = [
  "https://example.com/",
  "http://example.com:8080/path?q=1#frag",
  "https://docs.rs/tauri/latest/",
  "http://localhost.evil.example/",
  "https://127.0.0.1.nip.io/",
  // Bare hosts, the way they are actually typed.
  "example.com",
  "docs.rs/tauri/latest/",
  "example.com:8080/path?q=1",
];

test("every refusal is the shell's own sentence, addressed to what was typed", () => {
  for (const raw of REFUSED_IN_UI) {
    const verdict = classifyBrowserAddress(raw, "tab-1");
    assert.equal(verdict.kind, "refuse", `${raw} must be refused`);
    if (verdict.kind !== "refuse") continue;
    const isHttp = /^https?:\/\//i.test(raw);
    if (isHttp) {
      assert.equal(
        verdict.reason,
        NON_WEB_ADDRESS_MESSAGE.replace("%ADDRESS%", JSON.stringify(raw)),
        "a refused http(s) address carries the shell's wording verbatim",
      );
    } else {
      // What the pane would have sent: the https-prefixed form, which either
      // parses (and is refused as non-web) or does not parse at all.
      assert.match(verdict.reason, /^(refusing to navigate|not a navigable URL)/);
    }
  }
});

test("every allowed address is handed to the shell, parsed", () => {
  for (const raw of ALLOWED_IN_UI) {
    const verdict = classifyBrowserAddress(raw, "tab-1");
    assert.equal(verdict.kind, "shell", `${raw} must reach the shell`);
    if (verdict.kind !== "shell") continue;
    assert.equal(verdict.tabId, "tab-1");
    // Parsed and absolute: the shell's Url::parse has no base, so what
    // leaves here must not depend on where it came from.
    assert.match(verdict.url, /^https?:\/\//);
  }
});

test("bare hosts get the scheme a browser would assume", () => {
  const verdict = classifyBrowserAddress("example.com", "tab-1");
  assert.equal(verdict.kind, "shell");
  if (verdict.kind !== "shell") return;
  assert.equal(verdict.url, "https://example.com/");
});

test("an empty address is a shell question with no answer to give", () => {
  // Blank input is swallowed upstream (the pane's commit), but the module
  // still has to answer it with something that is not a page seat.
  const verdict = classifyBrowserAddress("", "tab-1");
  assert.equal(verdict.kind, "refuse");
});

test("an address the UI passed is exactly what reaches the shell", () => {
  // The shell re-parses, so the UI's normalisation cannot be load-bearing —
  // but what it sends must at least be the same address the user meant.
  const verdict = classifyBrowserAddress("http://Example.COM/Path", "tab-7");
  assert.equal(verdict.kind, "shell");
  if (verdict.kind !== "shell") return;
  assert.equal(verdict.url, "http://example.com/Path");
  assert.equal(verdict.tabId, "tab-7");
});
