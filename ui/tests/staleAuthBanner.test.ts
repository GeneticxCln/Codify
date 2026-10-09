/**
 * The auth-stale fix: what the pill's state says to *do* about itself.
 *
 * The pill has said "Auth stale" in warning colours since `statusTone.ts` was
 * written, and the health probe has always been able to tell that state apart
 * — a 401 from a live engine is "up but rejected". What neither ever did was
 * help. The desktop app barely notices (the shell self-heals over IPC), but a
 * browser tab against a standalone engine has no IPC, so the state sat there
 * amber forever while every request the app made died with the same silent
 * 401: the failure named once in a tooltip, repeated endlessly in the console,
 * and not one word about what to do.
 *
 * The contract here is about *behaviour*: the banner appears when (and only
 * when) the engine refuses the token and nothing can heal it; the instructions
 * it prints work when they are followed; Retry picks up a pasted token; and a
 * desktop user never sees it. Those are checked by running the banner and the
 * app, not by reading their source for the words.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// `.tsx` needs the esbuild hook registered before any component import; the
// dynamic imports below are deliberate — a static one hoists past the hook.
import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { STALE_AUTH_FIX, ENGINE_STATE_COPY } from "../src/statusTone.ts";

const { staleAuthPasteLines, copyText } = await import("../src/components/StaleAuthBanner.tsx");
const { storedEngineInfo } = await import("../src/api.ts");
const { withApp } = await import("./appHarness.ts");
const { withDom } = await import("./dom.ts");

test("the fix is a decision with a shape, not a floating string", () => {
  // Every field the banner renders has to exist. Deleting one — the command,
  // the paste hint — leaves the banner rendering `undefined`, which no probe
  // would catch and a user would.
  for (const key of ["heading", "body", "command", "pasteHint"] as const) {
    const value = STALE_AUTH_FIX[key];
    assert.ok(typeof value === "string" && value.trim(), `STALE_AUTH_FIX.${key} is missing`);
  }
});

test("the make target the banner names is the one the Makefile defines", () => {
  // `make run-engine-preview` is the developer-facing entry to a fresh
  // handshake. If the target is renamed, the banner names a target that does
  // not exist — worse than naming none, because the user runs it, sees
  // nothing, and stops trusting the banner.
  const makefile = readFileSync(new URL("../../Makefile", import.meta.url), "utf8");
  assert.match(makefile, /^run-engine-preview:/m, "the Makefile lost the preview target");
  assert.match(STALE_AUTH_FIX.command, /make run-engine-preview/, "the banner's command is not the Makefile's target");
});

test("following the banner's paste instructions gives the client the engine's address", () => {
  // Executed, not matched: the lines are JavaScript for the browser console, so
  // run them against a storage and ask the client what it now believes. A renamed
  // key on either side is an instruction that silently does nothing.
  const map = new Map<string, string>();
  const storage = {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
  const lines = staleAuthPasteLines(7431).map((l) => l.replace("<paste the token from the handshake line>", "abc123"));
  new Function("localStorage", lines.join("\n"))(storage);

  const g = globalThis as Record<string, unknown>;
  const before = Object.getOwnPropertyDescriptor(g, "localStorage");
  Object.defineProperty(g, "localStorage", { value: storage, configurable: true, writable: true });
  try {
    assert.deepEqual(storedEngineInfo(false), { port: 7431, token: "abc123" });
  } finally {
    if (before) Object.defineProperty(g, "localStorage", before);
    else delete g.localStorage;
  }
  // The token cannot be known to the tab, so it must stay a placeholder the
  // developer replaces — pretending otherwise would be the banner lying.
  assert.match(staleAuthPasteLines(7431)[1], /paste the token/);
});

test("the pill's hint points at the banner, so the two cannot disagree about where the fix lives", () => {
  assert.match(ENGINE_STATE_COPY["auth-stale"].hint, /banner/);
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

test("the banner is an alert with the commands in copyable blocks, and its three buttons work", async () => {
  // role=alert because this appears while the user is looking at an app that has
  // silently stopped working; the `pre` blocks because the whole point is a copy
  // that survives line-wrapping.
  await withDom(async (dom) => {
    const React = (await import("react")).default;
    const { StaleAuthBanner } = await import("../src/components/StaleAuthBanner.tsx");
    let retried = 0;
    let dismissed = 0;
    const copied: string[] = [];
    await dom.render(
      React.createElement(StaleAuthBanner, {
        port: 7431,
        onRetry: () => void (retried += 1),
        onDismiss: () => void (dismissed += 1),
        writeText: async (text: string) => void copied.push(text),
      }),
    );
    const alert = dom.container.querySelector('[role="alert"]');
    assert.ok(alert, "the banner is not announced as an alert");
    const blocks = [...alert.querySelectorAll("pre")].map((p) => p.textContent ?? "");
    assert.equal(blocks.length, 2, "the commands are not in two pre blocks");
    assert.ok(blocks[0].includes(STALE_AUTH_FIX.command), "the make command is not shown");
    assert.ok(blocks[1].includes('"CODIFY_PORT", "7431"'), "the paste lines do not carry this tab's port");

    await dom.click(dom.byButton("Copy commands"));
    assert.equal(copied.length, 1);
    assert.equal(copied[0], blocks[1], "what was copied is not what was shown");
    assert.ok(dom.byButton("Copied"), "the copy was not acknowledged");

    await dom.click(dom.byButton("Retry now"));
    await dom.click(dom.byButton("Dismiss"));
    assert.deepEqual([retried, dismissed], [1, 1]);
  });
});

// ── in the app ───────────────────────────────────────────────────────────

const banner = (root: HTMLElement): HTMLElement | null =>
  ([...root.querySelectorAll('[role="alert"]')].find((a) =>
    (a.textContent ?? "").includes(STALE_AUTH_FIX.heading),
  ) as HTMLElement | undefined) ?? null;

test("the standalone preview shows the banner when the engine refuses its token, and only then", async () => {
  await withApp({ standalone: true, engineToken: "the-real-token", localStorage: { CODIFY_TOKEN: "stale" } }, async ({ dom, settle }) => {
    await settle();
    assert.ok(banner(dom.container), "the engine refused the token and no banner explained what to do");
  });
  await withApp({ standalone: true, engineToken: "the-real-token", localStorage: { CODIFY_TOKEN: "the-real-token" } }, async ({ dom, settle }) => {
    await settle();
    assert.ok(banner(dom.container) === null, "a working connection showed the banner");
  });
});

test("paste the token, press Retry, and the banner goes away", async () => {
  // The sequence the banner asks for. The client holds a module-level copy made at
  // page load, so a retry that did not re-read storage would re-probe with the old
  // token and leave the banner up after the user did exactly what it said — the
  // loop the first live verification hit.
  await withApp({ standalone: true, engineToken: "the-real-token", localStorage: { CODIFY_TOKEN: "stale" } }, async (ctx) => {
    const { dom, settle } = ctx;
    await settle();
    assert.ok(banner(dom.container));
    dom.window.localStorage.setItem("CODIFY_TOKEN", "the-real-token");
    // The app reads `localStorage` as a bare global, which is a different object from the window's here.
    (globalThis as unknown as { localStorage: Storage }).localStorage.setItem("CODIFY_TOKEN", "the-real-token");
    await dom.click(dom.byButton("Retry now"));
    await settle();
    assert.ok(banner(dom.container) === null, "Retry did not pick up the pasted token");
  });
});

test("a desktop user never sees the banner: the shell supplies the token and the probe re-checks", async () => {
  // The self-healing path. The window starts holding a token the engine refuses;
  // the shell knows the right one. If the probe stopped re-asking after a stale
  // answer, the desktop app would land on the banner instead of clearing the state.
  await withApp({ engineToken: "the-real-token", localStorage: { CODIFY_TOKEN: "stale" } }, async ({ dom, settle }) => {
    await settle();
    assert.ok(banner(dom.container) === null, "a desktop window showed the standalone-preview banner");
    assert.match(dom.container.textContent ?? "", /Live/, "the window did not recover");
  });
});

test("dismissing hides the banner, and the next probe puts the true state back", async () => {
  // Dismiss is a decision about the banner, not about the connection: the banner
  // is a message, not a mute. If dismissal is ever "fixed" into a permanent mute,
  // it should be a change someone wrote on purpose.
  await withApp({ standalone: true, engineToken: "the-real-token", localStorage: { CODIFY_TOKEN: "stale" } }, async ({ dom, settle, act }) => {
    await settle();
    await dom.click(dom.byButton("Dismiss"));
    await settle();
    assert.ok(banner(dom.container) === null, "Dismiss did not hide the banner");
    // The poll is 3 s while the window is visible.
    await act(() => new Promise<void>((r) => setTimeout(r, 3300)));
    await settle();
    assert.ok(banner(dom.container), "the connection is still refused but the banner stayed gone");
  });
});
