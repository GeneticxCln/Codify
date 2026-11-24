/**
 * App-wide AuditPanel drawer: cross-project agent changes, project filter, and goal navigation (Item 10).
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
const { withApp, conversation } = await import("./appHarness.ts");

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("AuditPanel renders cross-project commits and files, filters by project, and opens a goal", async () => {
  const reads: string[] = [];
  const opened: string[] = [];

  await withDom(async (dom) => {
    const { setEngineInfo } = await import("../src/api.ts");
    const { AuditPanel } = await import("../src/components/AuditPanel.tsx");
    setEngineInfo({ port: 19992, token: "tok-audit" });
    const prevFetch = globalThis.fetch;

    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      const path = new URL(url).pathname + new URL(url).search;
      reads.push(path);
      if (path === "/workspaces") {
        return json([
          { id: "ws-a", name: "Alpha", root_path: "/tmp/a", created_at: 1 },
          { id: "ws-b", name: "Beta", root_path: "/tmp/b", created_at: 2 },
        ]);
      }
      if (path.startsWith("/audit/changes")) {
        const wsFilter = new URL(url).searchParams.get("workspace_id");
        const allEntries = [
          {
            goal_id: "g-a",
            workspace_id: "ws-a",
            workspace_name: "Alpha",
            title: "Add banner",
            status: "COMPLETED",
            mode: "code",
            dry_run: false,
            updated_at: 1_700_000_100,
            commits: [
              {
                step_id: "s1",
                step_title: "Write banner",
                sha: "1a2b3c4",
                message: "feat: add banner",
                at: 1_700_000_100,
              },
            ],
            files: ["banner.txt", "README.md"],
            files_count: 2,
          },
          {
            goal_id: "g-b",
            workspace_id: "ws-b",
            workspace_name: "Beta",
            title: "Update config",
            status: "COMPLETED",
            mode: "code",
            dry_run: false,
            updated_at: 1_700_000_200,
            commits: [],
            files: ["config.toml"],
            files_count: 1,
          },
        ];
        const entries = wsFilter ? allEntries.filter((e) => e.workspace_id === wsFilter) : allEntries;
        return json({
          generated_at: 1_700_000_300,
          workspace_id: wsFilter,
          total_goals: entries.length,
          total_commits: entries.reduce((sum, e) => sum + e.commits.length, 0),
          total_files: entries.reduce((sum, e) => sum + e.files_count, 0),
          entries,
        });
      }
      return json({});
    }) as typeof fetch;

    try {
      await dom.render(
        React.createElement(AuditPanel, {
          onOpenGoal: (id) => opened.push(id),
        }),
      );
      for (let i = 0; i < 6; i++) await dom.settle();

      assert.match(dom.text(), /Add banner/);
      assert.match(dom.text(), /feat: add banner/);
      assert.match(dom.text(), /banner\.txt/);
      assert.match(dom.text(), /Update config/);

      // Click goal title to jump to it
      await dom.click(dom.byButton("Add banner"));
      assert.deepEqual(opened, ["g-a"]);

      // Narrow via themed SelectMenu to Beta
      reads.length = 0;
      const trigger = dom.byLabel("Audit project");
      await dom.click(trigger);
      await dom.settle();
      const betaOption = dom.container.querySelector<HTMLButtonElement>('[role="option"][data-value="ws-b"]');
      assert.ok(betaOption, "expected Beta option in SelectMenu");
      await dom.click(betaOption);
      for (let i = 0; i < 6; i++) await dom.settle();

      assert.ok(reads.some((r) => r.includes("workspace_id=ws-b")), `expected filtered fetch, got ${reads.join(", ")}`);
      assert.match(dom.text(), /Update config/);
      assert.doesNotMatch(dom.text(), /Add banner/);
    } finally {
      globalThis.fetch = prevFetch;
    }
  });
});

test("AuditPanel ignores a slower earlier project response after switching project filters", async () => {
  await withDom(async (dom) => {
    const { setEngineInfo } = await import("../src/api.ts");
    const { AuditPanel } = await import("../src/components/AuditPanel.tsx");
    setEngineInfo({ port: 19992, token: "tok-audit" });
    const prevFetch = globalThis.fetch;

    let resolveAll!: (res: Response) => void;
    const allDeferred = new Promise<Response>((resolve) => {
      resolveAll = resolve;
    });

    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname === "/workspaces") {
        return json([
          { id: "ws-a", name: "Alpha", root_path: "/tmp/a", created_at: 1 },
          { id: "ws-b", name: "Beta", root_path: "/tmp/b", created_at: 2 },
        ]);
      }
      if (url.pathname === "/audit/changes") {
        const wsFilter = url.searchParams.get("workspace_id");
        if (!wsFilter) return allDeferred;
        return json({
          generated_at: 1_700_000_300,
          workspace_id: wsFilter,
          total_goals: 1,
          total_commits: 0,
          total_files: 1,
          entries: [
            {
              goal_id: "g-b",
              workspace_id: "ws-b",
              workspace_name: "Beta",
              title: "Beta-only change",
              status: "COMPLETED",
              mode: "code",
              dry_run: false,
              updated_at: 1_700_000_200,
              commits: [],
              files: ["beta.ts"],
              files_count: 1,
            },
          ],
        });
      }
      return json({});
    }) as typeof fetch;

    try {
      await dom.render(React.createElement(AuditPanel));
      for (let i = 0; i < 4; i++) await dom.settle();

      // Switch to Beta while the initial All-projects fetch is still pending.
      await dom.click(dom.byLabel("Audit project"));
      await dom.settle();
      const betaOption = dom.container.querySelector<HTMLButtonElement>('[role="option"][data-value="ws-b"]');
      assert.ok(betaOption, "expected Beta option");
      await dom.click(betaOption);
      for (let i = 0; i < 4; i++) await dom.settle();
      assert.match(dom.text(), /Beta-only change/);

      // Now resolve the slower initial All-projects request: it must NOT clobber Beta's view.
      resolveAll(
        json({
          generated_at: 1_700_000_100,
          workspace_id: null,
          total_goals: 1,
          total_commits: 0,
          total_files: 1,
          entries: [
            {
              goal_id: "g-a",
              workspace_id: "ws-a",
              workspace_name: "Alpha",
              title: "Stale Alpha change",
              status: "COMPLETED",
              mode: "code",
              dry_run: false,
              updated_at: 1_700_000_100,
              commits: [],
              files: ["alpha.ts"],
              files_count: 1,
            },
          ],
        }),
      );
      for (let i = 0; i < 4; i++) await dom.settle();

      assert.match(dom.text(), /Beta-only change/);
      assert.doesNotMatch(dom.text(), /Stale Alpha change/);
    } finally {
      globalThis.fetch = prevFetch;
    }
  });
});

test("Opening a goal from the Audit drawer closes the drawer, and clearing history closes stale drawers", async () => {
  await withApp(
    {
      conversations: [conversation({ id: "c1", title: "Audit thread" })],
      goals: [
        {
          id: "g-audit-1",
          conversation_id: "c1",
          title: "Refactor auth",
          mode: "normal",
          status: "COMPLETED",
        },
      ],
      auditChanges: {
        generated_at: 1_700_000_300,
        workspace_id: null,
        total_goals: 1,
        total_commits: 1,
        total_files: 1,
        entries: [
          {
            goal_id: "g-audit-1",
            workspace_id: "ws-1",
            workspace_name: "Codify",
            title: "Refactor auth",
            status: "COMPLETED",
            mode: "normal",
            dry_run: false,
            updated_at: 1_700_000_200,
            commits: [
              {
                step_id: "s1",
                step_title: "Step 1",
                sha: "deadbee",
                message: "refactor: auth",
                at: 1_700_000_200,
              },
            ],
            files: ["auth.py"],
            files_count: 1,
          },
        ],
      },
    },
    async ({ dom, settle }) => {
      // 1. Open the Audit drawer via the header toggle.
      const auditBtn = dom.byButton("Audit");
      await dom.click(auditBtn);
      for (let i = 0; i < 6; i++) await settle();
      assert.ok(dom.container.querySelector('button[aria-label="Close audit log"]'), "Audit drawer must open");
      assert.match(dom.text(), /Refactor auth/);

      // 2. Click the goal in AuditPanel: it should jump to the goal AND close the Audit drawer.
      await dom.click(dom.byButton("Refactor auth"));
      for (let i = 0; i < 6; i++) await settle();
      assert.ok(
        dom.container.querySelector('button[aria-label="Close audit log"]') === null,
        "Audit drawer must close after clicking a goal to open it",
      );

      // 3. Re-open the Audit drawer, then clear all history in Settings -> Sessions: Audit drawer must close.
      await dom.click(auditBtn);
      for (let i = 0; i < 6; i++) await settle();
      assert.ok(dom.container.querySelector('button[aria-label="Close audit log"]'), "Audit drawer reopened");

      // Open Settings via the header status pill while the Audit drawer stays open behind it.
      await dom.click(dom.byButton("Live"));
      for (let i = 0; i < 8; i++) await settle();
      await dom.click(dom.byButton("Sessions"));
      for (let i = 0; i < 6; i++) await settle();
      const deleteBtn = Array.from(dom.container.querySelectorAll("button")).find((b) =>
        b.textContent?.includes("Delete all history…"),
      ) as HTMLButtonElement | undefined;
      assert.ok(deleteBtn, "Delete all history button missing");
      await dom.click(deleteBtn);
      await settle();
      await dom.fill(
        dom.container.querySelector('input[placeholder="delete all history"]') as HTMLInputElement,
        "delete all history",
      );
      await settle();
      const confirmBtn = Array.from(dom.container.querySelectorAll("button")).find((b) =>
        b.textContent?.includes("Permanently delete history"),
      ) as HTMLButtonElement | undefined;
      assert.ok(confirmBtn, "Confirm delete button missing");
      await dom.click(confirmBtn);
      for (let i = 0; i < 6; i++) await settle();

      assert.ok(
        dom.container.querySelector('button[aria-label="Close audit log"]') === null,
        "Audit drawer must close when all history is cleared so deleted goals do not linger",
      );
    },
  );
});
