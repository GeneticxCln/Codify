/**
 * Offline mode card in Settings and update-check gating in AboutPane (Item 9).
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const RUNTIME = {
  interpreter: {
    executable: "/home/me/Codify/.venv/bin/python",
    version: "3.14.0",
    version_info: [3, 14],
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

test("OfflineModeCard loads offline_mode, toggles it via PUT /settings/engine, and triggers refresh", async () => {
  const saves: Array<Record<string, unknown>> = [];
  let refreshed = 0;

  await withDom(async (dom) => {
    const { setEngineInfo } = await import("../src/api.ts");
    const { OfflineModeCard } = await import("../src/components/OfflineModeCard.tsx");
    setEngineInfo({ port: 19991, token: "tok-offline" });
    const prevFetch = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/settings/engine") && method === "GET") {
        return json({
          parallel_width: { value: 1, min: 1, max: 4, default: 1 },
          stats_retention_days: { value: 0, min: 0, max: 3650, default: 0 },
          offline_mode: { value: 0, min: 0, max: 1, default: 0 },
        });
      }
      if (url.endsWith("/settings/engine") && method === "PUT") {
        const parsed = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        saves.push(parsed);
        return json({ saved: { offline_mode: parsed.offline_mode ? 1 : 0 } });
      }
      return json({});
    }) as typeof fetch;

    try {
      await dom.render(
        React.createElement(OfflineModeCard, {
          onChanged: () => {
            refreshed += 1;
          },
        }),
      );
      for (let i = 0; i < 4; i++) await dom.settle();

      const checkbox = dom.container.querySelector<HTMLButtonElement>('[role="checkbox"]');
      assert.ok(checkbox, "expected themed Checkbox for offline mode");
      assert.equal(checkbox.getAttribute("aria-checked"), "false");

      await dom.click(checkbox);
      for (let i = 0; i < 4; i++) await dom.settle();

      assert.deepEqual(saves, [{ offline_mode: true }]);
      assert.equal(refreshed, 1);
      assert.equal(checkbox.getAttribute("aria-checked"), "true");
      assert.match(dom.text(), /Offline mode is on/);
    } finally {
      globalThis.fetch = prevFetch;
    }
  });
});

test("AboutPane pauses and disables update checks while Offline mode is on", async () => {
  await withDom(async (dom) => {
    const { setEngineInfo } = await import("../src/api.ts");
    const { AboutPane } = await import("../src/components/AboutPane.tsx");
    setEngineInfo({ port: 19991, token: "tok-offline" });
    const prevFetch = globalThis.fetch;
    const w = dom.window as unknown as Record<string, unknown>;
    const prevTauri = w.__TAURI_INTERNALS__;
    let updateInvoked = false;
    w.__TAURI_INTERNALS__ = {
      invoke: async (cmd: string) => {
        if (cmd === "codify_update_check") {
          updateInvoked = true;
        }
        if (cmd === "codify_app_facts") {
          return {
            version: "1.8.1",
            os: "linux",
            arch: "x86_64",
            debug: false,
            install: "release",
          };
        }
        return null;
      },
    };
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/settings/engine")) {
        return json({
          parallel_width: { value: 1, min: 1, max: 4, default: 1 },
          stats_retention_days: { value: 0, min: 0, max: 3650, default: 0 },
          offline_mode: { value: 1, min: 0, max: 1, default: 0 },
        });
      }
      if (url.endsWith("/health")) {
        return json({ status: "ok", version: "1.8.1" });
      }
      if (url.endsWith("/runtime")) {
        return json(RUNTIME);
      }
      if (url.endsWith("/info")) {
        return json({
          python: "3.14.0",
          state_dir: "/tmp/codify",
          db_path: "/tmp/codify/codify.db",
          schema_version: 1,
        });
      }
      return json({});
    }) as typeof fetch;

    try {
      await dom.render(React.createElement(AboutPane));
      for (let i = 0; i < 6; i++) await dom.settle();

      assert.match(dom.text(), /Update checks are paused while Offline mode is on/);
      const btn = dom.byButton("Check for updates") as HTMLButtonElement;
      assert.equal(btn.disabled, true, "Check for updates must be disabled while offline");
      await dom.click(btn);
      await dom.settle();
      assert.equal(updateInvoked, false, "codify_update_check must not be invoked while offline");
    } finally {
      globalThis.fetch = prevFetch;
      if (prevTauri === undefined) {
        delete w.__TAURI_INTERNALS__;
      } else {
        w.__TAURI_INTERNALS__ = prevTauri;
      }
    }
  });
});
