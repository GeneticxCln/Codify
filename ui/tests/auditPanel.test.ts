/**
 * App-wide AuditPanel drawer: cross-project agent changes, project filter, and goal navigation (Item 10).
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
