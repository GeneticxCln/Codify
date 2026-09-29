/**
 * The auth-stale fix: what the pill's state says to *do* about itself.
 *
 * The pill has said "Auth stale" in warning colours since `statusTone.ts` was
 * written, and the health probe has always been able to tell that state apart
 * — a 401 from a live engine is "up but rejected". What neither ever did was
 * help. The desktop app barely notices (the shell self-heals over IPC), but a
 * browser tab against a standalone engine has no IPC, so the state sat there
 * ambers forever while every request the app made died with the same silent
 * 401: the failure named once in a tooltip, repeated endlessly in the
 * console, and not one word about what to do.
 *
 * The contract here is about *shape*, not prose. A test pinning whole
 * sentences forces every wording tweak through this file first, which is the
 * wrong direction for prose; a test pinning the shape — which keys exist,
 * that the token reaches the browser console under the keys `api.ts` reads,
 * that the make target is named by name — fails the day someone deletes the
 * fix without replacing it, which is the failure that matters.
 *
 * The suite runs through `node --test` with no renderer, so the component is
 * held as source text: the same contract style `shell.test.ts` and
 * `componentLoader.test.ts` use, and deliberately so — a banner whose exact
 * markup matters (role=alert, the two `pre` blocks) is a banner whose markup
 * can be read as text.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// `.tsx` needs the esbuild hook registered before any component import; the
// dynamic import below is deliberate — a static one hoists past the hook.
import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { STALE_AUTH_FIX, ENGINE_STATE_COPY } from "../src/statusTone.ts";

const BANNER_SRC = readFileSync(
  new URL("../src/components/StaleAuthBanner.tsx", import.meta.url),
  "utf8",
);
const APP_SRC = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const API_SRC = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const STATUS_TONE_SRC = readFileSync(
  new URL("../src/statusTone.ts", import.meta.url),
  "utf8",
);

const { staleAuthPasteLines, copyText } = await import(
  "../src/components/StaleAuthBanner.tsx"
);

test("the fix is a decision with a shape, not a floating string", () => {
  // Every field the banner renders has to exist. Deleting one — the command,
  // the paste hint — leaves the banner rendering `undefined`, which no probe
  // would catch and a user would.
  for (const key of ["heading", "body", "command", "pasteHint"] as const) {
    const value = STALE_AUTH_FIX[key];
    assert.ok(typeof value === "string" && value.trim(), `STALE_AUTH_FIX.${key} is missing`);
  }
  // It lives in statusTone.ts, the plain module the suite can reach without a
  // renderer — the same home the pill's copy has, for the same reason.
  assert.match(
    STATUS_TONE_SRC,
    /export const STALE_AUTH_FIX/,
    "the fix copy moved out of the plain module, where no renderer-free test can reach it",
  );
});

test("the make target the banner names is the one the Makefile defines", () => {
  // `make run-engine-preview` is the developer-facing entry to a fresh
  // handshake. If the target is renamed, the banner names a target that does
  // not exist — worse than naming none, because the user runs it, sees
  // nothing, and stops trusting the banner.
  const makefile = readFileSync(new URL("../../Makefile", import.meta.url), "utf8");
  assert.match(makefile, /^run-engine-preview:/m, "the Makefile lost the preview target");
  assert.match(
    STALE_AUTH_FIX.command,
    /make run-engine-preview/,
    "the banner's command is not the Makefile's target",
  );
});

test("the paste lines use the keys api.ts actually reads", () => {
  // api.ts: `localStorage.getItem("CODIFY_PORT")`, `getItem("CODIFY_TOKEN")`.
  // A renamed key in either place is a paste instruction that silently does
  // nothing — the exact failure mode of instructions nobody re-checks.
  for (const key of ["CODIFY_PORT", "CODIFY_TOKEN"]) {
    assert.match(
      API_SRC,
      new RegExp(`localStorage\\.getItem\\("${key}"\\)`),
      `api.ts no longer reads ${key} from localStorage; the banner's paste lines are now wrong`,
    );
    assert.match(
      staleAuthPasteLines(7431)[0] + staleAuthPasteLines(7431)[1],
      new RegExp(`"${key}"`),
      `the paste lines no longer mention ${key}`,
    );
  }
  // The port this tab is already pointed at is not the stale part; the
  // lines must carry the current port, not a placeholder for it.
  assert.match(
    staleAuthPasteLines(7431)[0],
    /"7431"/,
    "the port is not filled into the paste line",
  );
  // The token, by contrast, cannot be known to the tab — it must stay a
  // placeholder the developer replaces, and pretending otherwise would be
  // the banner lying about what it can reach.
  assert.match(
    staleAuthPasteLines(7431)[1],
    /paste the token/,
    "the token line must be an explicit placeholder, not a value the tab cannot know",
  );
});

test("the banner renders as an alert with the commands in copyable blocks", () => {
  // role=alert because this appears while the user is looking at an app that
  // has silently stopped working; the `pre` blocks because the whole point is
  // a copy that survives line-wrapping.
  assert.match(BANNER_SRC, /role="alert"/);
  assert.match(BANNER_SRC, /<pre /, "the commands are not in a pre block");
  assert.match(BANNER_SRC, /STALE_AUTH_FIX\.command/);
  assert.match(BANNER_SRC, /staleAuthPasteLines\(port\)/);
  // Copy with a fallback, and an acknowledgement that means the copy worked:
  // the two-mechanism pattern scheme.ts established, for the file:// and
  // insecure-origin cases a browser preview genuinely hits.
  assert.match(BANNER_SRC, /navigator\.clipboard/);
  assert.match(BANNER_SRC, /execCommand/);
  assert.match(BANNER_SRC, /"Copied"/);
  // Retry and Dismiss are the user's two ways out; both must exist.
  assert.match(BANNER_SRC, /Retry now/);
  assert.match(BANNER_SRC, /Dismiss/);
});

test("App shows the banner exactly when the connection is auth-stale", () => {
  // The render site, not just the import: a component wired nowhere is the
  // fix that never ships.
  assert.match(
    APP_SRC,
    /engineConnection === "auth-stale"[\s\S]{0,200}<StaleAuthBanner/,
    "the banner is not rendered on the auth-stale state",
  );
  // The pill's own hint points at the banner, so the two cannot drift into
  // contradicting each other about where the fix lives.
  assert.match(
    ENGINE_STATE_COPY["auth-stale"].hint,
    /banner/,
    "the pill's hint does not point at the banner",
  );
  // And App must not swallow the state it just got told about: the probe
  // publishes authOk, the pill reads it, the banner reads it — one source.
  assert.match(APP_SRC, /setAuthOk\(health\.authenticated\)/);
});

test("dismiss is a decision about the banner, not about the connection", () => {
  // Dismiss sets authOk true — hiding the banner — but the next probe (3s,
  // or the user's own Retry) puts the true state back. That is deliberate:
  // the banner is a message, not a mute. This test exists so that if the
  // dismissal is ever "fixed" into a permanent mute, it happens through a
  // state change someone wrote on purpose, not through this line quietly
  // becoming a lie.
  assert.match(APP_SRC, /onDismiss=\{\(\) => setAuthOk\(true\)\}/);
});

test("the retry button re-reads localStorage before it probes", () => {
  // One code path: a retry that disagreed with what the 3s poll would
  // conclude would be a second opinion nobody reconciles. The ref is how the
  // imperative button reaches the effect's own body.
  assert.match(APP_SRC, /forceHealthProbeRef\.current = probe;/);
  // And the paste-then-retry sequence has to work: the paste lands in
  // localStorage, the API client holds a module-level copy made at page
  // load, and a retry that skipped the re-sync would re-probe with the
  // stale token, fail, and leave the banner up after the user did exactly
  // what it said. The first live verification of this banner hit that loop.
  assert.match(
    APP_SRC,
    /onRetry=\{\(\) => \{[\s\S]*?setEngineInfo\(\{[\s\S]*?localStorage\.getItem\("CODIFY_TOKEN"\)[\s\S]*?\}\);[\s\S]*?forceHealthProbeRef\.current\?\.\(\);[\s\S]*?\}\}/,
    "the retry does not re-read localStorage before probing, so a pasted token would not be seen until a reload",
  );
});

test("the clipboard copy works without navigator.clipboard", async () => {
  // The fallback path, driven for real: an insecure-origin preview has no
  // clipboard API, and the button must still acknowledge the copy.
  const created: string[] = [];
  const fakeDoc = {
    createElement(tag: string): HTMLTextAreaElement {
      created.push(tag);
      return {
        set value(_: string) {},
        get value() {
          return "x";
        },
        setAttribute() {},
        get style() {
          return {} as CSSStyleDeclaration;
        },
        select() {},
        remove() {},
      } as unknown as HTMLTextAreaElement;
    },
    body: { appendChild() {}, removeChild() {} },
    execCommand: () => true,
  } as unknown as Document;
  const ok = await copyText("token line", { document: fakeDoc });
  assert.equal(ok, true, "the textarea fallback reported failure");
  assert.deepEqual(created, ["textarea"]);
});

test("a desktop user never sees the banner", () => {
  // The self-healing path: on auth-stale, App calls refreshEngineInfoFromIpc
  // and re-probes. If that effect ever stops re-probing after a fresh IPC
  // answer, the desktop app would land on the banner instead of clearing the
  // state — which is a regression this file would otherwise miss.
  assert.match(
    APP_SRC,
    /health = await checkEngineHealth\(\);[\s\S]{0,80}setEngineUp\(health\.ok\);/,
    "the probe no longer re-checks after a fresh IPC answer",
  );
});
