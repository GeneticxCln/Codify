/**
 * Runtime DOM tests for `StateBackupCard` in Settings → Sessions.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
const { setEngineInfo } = await import("../src/api.ts");
const { StateBackupCard } = await import("../src/components/StateBackupCard.tsx");

test("StateBackupCard creates a snapshot, stages a next-start restore, and cancels it", async () => {
  const calls: Array<{ method: string; url: string; body: unknown }> = [];
  const backups: Array<{ filename: string; path: string; size_bytes: number; created_at: number }> = [];
  let pending: { source: string; size_bytes: number; staged_at: number } | null = null;

  await withDom(async (dom) => {
    setEngineInfo({ port: 7430, token: "test-token" });
    const realFetch = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      calls.push({ method, url, body });

      if (url.endsWith("/state/backup") && method === "GET") {
        return new Response(
          JSON.stringify({
            db_path: "/tmp/state/codify.db",
            db_bytes: 65536,
            backups_dir: "/tmp/state/backups",
            backups,
            pending_restore: pending,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.endsWith("/state/backup") && method === "POST") {
        const entry = {
          filename: "codify-backup-20261124-120000.db",
          path: "/tmp/state/backups/codify-backup-20261124-120000.db",
          size_bytes: 65536,
          created_at: 1795518000,
        };
        backups.unshift(entry);
        return new Response(JSON.stringify(entry), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/state/restore") && method === "POST") {
        const b = body as { filename?: string; source_name?: string };
        pending = {
          source: b.filename ?? b.source_name ?? "uploaded.db",
          size_bytes: 65536,
          staged_at: 1795518010,
        };
        return new Response(JSON.stringify(pending), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/state/restore") && method === "DELETE") {
        pending = null;
        return new Response(JSON.stringify({ cancelled: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { status: 404 });
    }) as typeof fetch;

    try {
      await dom.render(React.createElement(StateBackupCard));
      await dom.settle();

      assert.match(dom.text(), /Backup and restore/);
      assert.match(dom.text(), /API keys are excluded/);

      const backupBtn = [...dom.container.querySelectorAll("button")].find((b) =>
        (b.textContent ?? "").includes("Back up"),
      );
      assert.ok(backupBtn, "Back up button missing");
      await dom.click(backupBtn);
      await dom.settle();

      assert.match(dom.text(), /codify-backup-20261124-120000\.db/);

      const row = dom.container.querySelector('[data-backup-row="codify-backup-20261124-120000.db"]');
      assert.ok(row, "saved backup row missing");
      const restoreBtn = [...row.querySelectorAll("button")].find((b) =>
        (b.textContent ?? "").includes("Restore"),
      );
      assert.ok(restoreBtn, "Restore button missing on backup row");
      await dom.click(restoreBtn);
      await dom.settle();

      assert.ok(dom.container.querySelector('[data-pending-restore="true"]'), "pending restore banner missing");
      assert.match(dom.text(), /Restore staged for next start/);

      const cancelBtn = [...dom.container.querySelectorAll("button")].find((b) =>
        (b.textContent ?? "").includes("Cancel restore"),
      );
      assert.ok(cancelBtn, "Cancel restore button missing");
      await dom.click(cancelBtn);
      await dom.settle();

      assert.ok(
        dom.container.querySelector('[data-pending-restore="true"]') === null,
        "pending restore banner still present after cancel",
      );
      assert.ok(calls.some((c) => c.method === "POST" && c.url.endsWith("/state/backup")));
      assert.ok(calls.some((c) => c.method === "POST" && c.url.endsWith("/state/restore")));
      assert.ok(calls.some((c) => c.method === "DELETE" && c.url.endsWith("/state/restore")));
    } finally {
      globalThis.fetch = realFetch;
    }
  });
});
