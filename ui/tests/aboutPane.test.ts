/**
 * Settings → About, mounted.
 *
 * The pane is read-only and says only what the app can truthfully know, so these tests are about the
 * edges of that: what it prints when there is a desktop shell and when there is not, when the shell
 * will not say, when the engine is down or the token is stale, and that the boot token never reaches
 * the page. The shortcut list is checked against the real mapping in `shortcuts.test.ts`.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const { SHORTCUT_HELP } = await import("../src/shortcuts.ts");
const React = (await import("react")).default;
const h = React.createElement;

const TOKEN = "boot-token-must-never-be-shown";

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

const RUNTIME = {
  interpreter: {
    executable: "/home/me/Codify/.venv/bin/python",
    version: "3.12.4",
    version_info: [3, 12],
    implementation: "CPython",
    in_virtualenv: true,
    prefix: "/home/me/Codify/.venv",
    base_prefix: "/usr",
  },
  project_root: "/home/me/Codify",
  checkout_interpreter: "/home/me/Codify/.venv/bin/python3",
  laya_sdk: { importable: false, import_error: null, version: null, disabled_by_env: false },
  warnings: [],
};

interface World {
  /** What the desktop shell answers to a command, or `undefined` for no shell at all. */
  shell?: Record<string, unknown>;
  health?: () => Response;
  runtime?: () => Response;
}

async function withAbout(world: World, body: (dom: Dom) => Promise<void>): Promise<void> {
  await withDom(async (dom) => {
    const win = dom.window as unknown as Record<string, unknown>;
    if (world.shell) {
      win.__TAURI_INTERNALS__ = {
        invoke: async (cmd: string) => (cmd in world.shell! ? world.shell![cmd] : null),
        transformCallback: () => 1,
        metadata: { currentWebview: { windowLabel: "main" }, currentWindow: { label: "main" } },
      };
    } else {
      delete win.__TAURI_INTERNALS__;
    }
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      if (path === "/health") return (world.health ?? (() => json({ ok: true, authenticated: true })))();
      if (path === "/settings/runtime") return (world.runtime ?? (() => json(RUNTIME)))();
      throw new Error(`no answer for ${path}`);
    }) as typeof fetch;
    const api = await import("../src/api.ts");
    api.setEngineInfo({ port: 7431, token: TOKEN });
    const { AboutPane } = await import("../src/components/AboutPane.tsx");
    await dom.render(h(AboutPane));
    for (let i = 0; i < 4; i++) await dom.settle();
    await body(dom);
  });
}

const SHELL = { "plugin:app|name": "Codify", "plugin:app|version": "0.2.0", "plugin:app|tauri_version": "2.11.6" };

test("under the desktop shell it prints the app's version, the shell, and what the engine is", async () => {
  await withAbout({ shell: SHELL }, async (dom) => {
    const text = dom.text();
    assert.match(text, /Version\s*0\.2\.0/);
    assert.match(text, /Desktop shell\s*Tauri 2\.11\.6/);
    assert.match(text, /Status\s*running/);
    assert.match(text, /127\.0\.0\.1:7431/, "the port the window is connected to is not shown");
    assert.match(text, /3\.12\.4/, "the engine's interpreter did not reach the page");
    assert.match(text, /MIT/);
    assert.match(text, /github\.com\/GeneticxCln\/Codify/);
    assert.match(text, /~\/\.codify/);
  });
});

test("every keyboard shortcut the keyboard layer defines is listed, with what it does", async () => {
  await withAbout({ shell: SHELL }, async (dom) => {
    const text = dom.text();
    for (const shortcut of SHORTCUT_HELP) {
      assert.ok(text.includes(shortcut.keys), `${shortcut.keys} is not listed`);
      assert.ok(text.includes(shortcut.does), `what ${shortcut.keys} does is not listed`);
    }
  });
});

test("the boot token is never on the page", async () => {
  await withAbout({ shell: SHELL }, async (dom) => {
    assert.ok(!dom.text().includes(TOKEN), "the boot token was printed");
    assert.ok(!dom.container.innerHTML.includes(TOKEN), "the boot token is in the markup");
  });
});

test("in a standalone browser there is no shell, and it says so instead of a version", async () => {
  await withAbout({}, async (dom) => {
    const text = dom.text();
    assert.match(text, /standalone browser preview/);
    assert.doesNotMatch(text, /Desktop shell/);
    assert.doesNotMatch(text, /Version\s*\d/);
    // The engine half still works without a shell: it is plain HTTP.
    assert.match(text, /Status\s*running/);
  });
});

test("a shell that will not say its version is not turned into a blank one", async () => {
  for (const answer of [null, "", "   ", 42]) {
    await withAbout({ shell: { ...SHELL, "plugin:app|version": answer } }, async (dom) => {
      const text = dom.text();
      assert.match(text, /unavailable: the desktop shell did not report one/, `${JSON.stringify(answer)} was printed as a version`);
      assert.doesNotMatch(text, /standalone browser preview/, "a shell was claimed not to exist");
    });
  }
});

test("an engine that is down, or whose token is stale, is said plainly", async () => {
  await withAbout({ shell: SHELL, health: () => json({ ok: false }, 503), runtime: () => json({}, 503) }, async (dom) => {
    assert.match(dom.text(), /Status\s*not answering/);
    assert.match(dom.text(), /Engine runtime unavailable/, "a missing self-check looked healthy");
  });
  await withAbout({ shell: SHELL, health: () => json({ code: "unauthorized" }, 401) }, async (dom) => {
    assert.match(dom.text(), /running, but this window's token is stale/);
  });
});
