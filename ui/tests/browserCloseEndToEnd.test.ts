/**
 * Closing a tab in the real App, against a fake shell.
 *
 * Only the *active* browser tab is given a page after a restore; the others are
 * addresses waiting for their turn. Closing one of those used to ask the shell
 * to close a page it never opened, and the shell's honest answer ("no browser
 * tab "browser-4" is open") landed on screen as an error the user had caused by
 * closing a tab. It also has to keep working the other way: closing a tab that
 * *does* own a page must still close it.
 *
 * `terminalEndToEnd.test.ts` explains the harness (a jsdom window, a faked
 * `__TAURI_INTERNALS__`, a fetch router); this one needs less of it.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
import type { Dom } from "./dom.ts";

const React = (await import("react")).default;

/** A fake shell that records every command it is asked to run. */
function fakeShell(): { calls: string[]; args: Array<Record<string, unknown>>; internals: Record<string, unknown> } {
  const calls: string[] = [];
  const args: Array<Record<string, unknown>> = [];
  let events = 0;
  let callbacks = 0;
  const internals: Record<string, unknown> = {
    transformCallback(callback: unknown): number {
      void callback;
      return callbacks++;
    },
    async invoke(cmd: string, a: Record<string, unknown> = {}): Promise<unknown> {
      calls.push(cmd);
      args.push(a);
      if (cmd === "plugin:event|listen") return ++events;
      if (cmd === "codify_get_engine_info") return { port: 51820, token: "e2e-token" };
      if (cmd === "codify_engine_status") return { error: null };
      if (cmd.startsWith("codify_list_") || cmd.includes("configs")) return [];
      return null;
    },
    metadata: { currentWebview: { windowLabel: "main" }, currentWindow: { label: "main" } },
  };
  return { calls, args, internals };
}

function fakeFetch(): typeof fetch {
  return (async (input: RequestInfo | URL): Promise<Response> => {
    const url = String(input);
    const json = (body: unknown): Response =>
      ({ ok: true, status: 200, json: async () => body }) as unknown as Response;
    if (url.includes("/health")) return json({ ok: true, authenticated: true });
    if (url.includes("/workspaces")) {
      return json([
        { id: "ws-a", name: "Alpha", root_path: "/tmp/e2e-alpha", design_contract_path: "", created_at: 1 },
      ]);
    }
    if (url.includes("/models/recent")) return json([]);
    if (url.includes("/models")) return json({ models: [] });
    return json([] as unknown[]);
  }) as unknown as typeof fetch;
}

const settleUi = async (dom: Dom): Promise<void> => {
  await dom.settle();
  for (let i = 0; i < 6; i++) {
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => requestAnimationFrame(() => r(null)));
  }
};

/** A remembered strip of two browser tabs, the first showing. */
const STORED_STRIP = JSON.stringify({
  version: 1,
  layout: {
    tabs: [
      { key: "k_test_1", kind: "browser", url: "https://example.com/first" },
      { key: "k_test_2", kind: "browser", url: "https://example.com/second" },
    ],
    activeIndex: 0,
  },
  pendingRemovals: [],
  pendingWrites: [],
});

test("closing a restored tab that never got a page asks the shell for nothing", async () => {
  const shell = fakeShell();
  const realFetch = globalThis.fetch;
  const realWebSocket = globalThis.WebSocket;
  (globalThis as Record<string, unknown>).WebSocket = class {
    close(): void {}
    addEventListener(): void {}
    removeEventListener(): void {}
  };
  try {
    await withDom(async (dom) => {
      const win = globalThis.window as unknown as Record<string, unknown>;
      win.__TAURI_INTERNALS__ = shell.internals;
      win.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
      globalThis.fetch = fakeFetch();
      if (!(globalThis as Record<string, unknown>).self) {
        (globalThis as Record<string, unknown>).self = dom.window;
      }
      dom.window.localStorage.setItem("CODIFY_TABS", STORED_STRIP);

      const { App } = await import("../src/App.tsx");
      await dom.render(React.createElement(App));
      await settleUi(dom);

      const tabs = [...dom.container.querySelectorAll('[role="tab"]')] as HTMLElement[];
      assert.equal(tabs.length, 2, "the remembered strip did not come back");

      const closers = [...dom.container.querySelectorAll('button[aria-label^="Close"]')] as HTMLElement[];
      assert.equal(closers.length, 2, `expected a close control per tab, found ${closers.length}`);

      await dom.click(closers[1]);
      await settleUi(dom);

      assert.equal(
        shell.calls.filter((c) => c === "codify_browser_close").length,
        0,
        "the shell was asked to close a page that was never opened",
      );
      assert.equal(
        dom.container.querySelector('[role="alert"]')?.textContent ?? "",
        "",
        "closing a tab put an error on screen",
      );
      assert.equal(
        ([...dom.container.querySelectorAll('[role="tab"]')] as HTMLElement[]).length,
        1,
        "the tab did not close",
      );
    });
  } finally {
    globalThis.fetch = realFetch;
    (globalThis as Record<string, unknown>).WebSocket = realWebSocket;
  }
});
