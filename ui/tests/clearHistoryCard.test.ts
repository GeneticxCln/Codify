/**
 * Settings → Sessions → Delete all history (`ClearHistoryCard` + `clearAllConversations`).
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

interface Call {
  method: string;
  path: string;
  body: unknown;
}

const SESSION_SETTINGS = {
  session_compact_auto: { value: 1, min: 0, max: 1 },
  session_compact_keep_turns: { value: 6, min: 1, max: 40 },
};

const CLEARED_REPLY = {
  deleted: true as const,
  files_touched: false as const,
  scrubbed: true,
  removed: {
    events: 42,
    plan_steps: 3,
    proposals: 0,
    recordings: 2,
    summaries: 1,
    suggestions: 2,
    observations: 1,
    stats_days: 4,
    turns: 7,
    threads: 3,
  },
};

async function withSessionsPane(
  postClear: (body: unknown) => Response,
  run: (dom: Dom, calls: Call[], clearedCount: () => number) => Promise<void>,
  backupStatus: () => Response = () =>
    json({ db_path: "/tmp/codify.db", db_bytes: 0, backups_dir: "/tmp/backups", backups: [], pending_restore: null }),
): Promise<void> {
  await withDom(async (dom) => {
    const calls: Call[] = [];
    let cleared = 0;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const method = (init?.method ?? "GET").toUpperCase();
      const parsed = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      calls.push({ method, path: url.pathname, body: parsed });
      if (method === "GET" && url.pathname === "/settings/engine") return json(SESSION_SETTINGS);
      if (method === "GET" && url.pathname === "/state/backup") return backupStatus();
      if (method === "POST" && url.pathname === "/history/clear") return postClear(parsed);
      throw new Error(`unexpected ${method} ${url.pathname}`);
    }) as typeof fetch;

    const { SessionSettingsPane } = await import("../src/components/SessionSettingsPane.tsx");
    await dom.render(h(SessionSettingsPane, { onHistoryCleared: () => { cleared += 1; } }));
    await dom.settle();
    await run(dom, calls, () => cleared);
  });
}

test("the card explains what goes and what stays, and keeps deletion locked behind the typed phrase", async () => {
  await withSessionsPane(() => json(CLEARED_REPLY), async (dom, calls, clearedCount) => {
    assert.match(dom.text(), /Delete all history/);
    assert.match(dom.text(), /workspace files are NOT\s+touched/);

    const openBtn = Array.from(dom.container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Delete all history…"),
    );
    assert.ok(openBtn, "expected 'Delete all history…' trigger button");
    await dom.click(openBtn);
    await dom.settle();

    const input = dom.container.querySelector('input[placeholder="delete all history"]') as HTMLInputElement;
    assert.ok(input, "expected confirmation input");
    const submitBtn = Array.from(dom.container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Permanently delete history"),
    ) as HTMLButtonElement;
    assert.ok(submitBtn, "expected submit button");
    assert.equal(submitBtn.disabled, true, "submit must start disabled");

    await dom.fill(input, "delete history");
    await dom.settle();
    assert.equal(submitBtn.disabled, true, "wrong phrase must keep submit disabled");

    // Cancel closes the confirmation box without calling the engine.
    const cancelBtn = Array.from(dom.container.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "Cancel",
    ) as HTMLButtonElement;
    await dom.click(cancelBtn);
    await dom.settle();
    assert.equal(calls.filter((c) => c.path === "/history/clear").length, 0);
    assert.equal(clearedCount(), 0);
  });
});

test("typing the phrase posts /history/clear, notifies the app, and shows what was removed", async () => {
  await withSessionsPane(() => json(CLEARED_REPLY), async (dom, calls, clearedCount) => {
    const openBtn = Array.from(dom.container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Delete all history…"),
    ) as HTMLButtonElement;
    await dom.click(openBtn);
    await dom.settle();

    const input = dom.container.querySelector('input[placeholder="delete all history"]') as HTMLInputElement;
    await dom.fill(input, "  Delete All History ");
    await dom.settle();

    const submitBtn = Array.from(dom.container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Permanently delete history"),
    ) as HTMLButtonElement;
    assert.equal(submitBtn.disabled, false);
    await dom.click(submitBtn);
    await dom.settle();

    assert.deepEqual(
      calls.filter((c) => c.path === "/history/clear"),
      [{ method: "POST", path: "/history/clear", body: { confirm: "  Delete All History " } }],
    );
    assert.equal(clearedCount(), 1);
    assert.match(dom.text(), /Deleted 3 threads, 7 turns, 42 timeline events, 13 derived records/);
    assert.match(dom.text(), /database rebuilt on disk; workspace files untouched/);
  });
});

test("an engine refusal stays on screen without firing onHistoryCleared", async () => {
  await withSessionsPane(
    () => json({ code: "thread_in_progress", message: "a turn is still running — cancel it before deleting all history" }, 409),
    async (dom, _calls, clearedCount) => {
      const openBtn = Array.from(dom.container.querySelectorAll("button")).find((b) =>
        b.textContent?.includes("Delete all history…"),
      ) as HTMLButtonElement;
      await dom.click(openBtn);
      await dom.settle();

      const input = dom.container.querySelector('input[placeholder="delete all history"]') as HTMLInputElement;
      await dom.fill(input, "delete all history");
      await dom.settle();

      const submitBtn = Array.from(dom.container.querySelectorAll("button")).find((b) =>
        b.textContent?.includes("Permanently delete history"),
      ) as HTMLButtonElement;
      await dom.click(submitBtn);
      await dom.settle();

      assert.equal(clearedCount(), 0);
      assert.match(dom.text(), /a turn is still running/);
      assert.ok(
        dom.container.querySelector('input[placeholder="delete all history"]'),
        "form stays open so the person can retry once the turn stops",
      );
    },
  );
});

test("clearAllConversations detaches chat tabs onto clean slates and leaves non-chat tabs untouched", async () => {
  const { clearAllConversations, UNTITLED_THREAD_TITLE } = await import("../src/tabs.ts");
  const state = {
    activeId: "chat-1",
    tabs: [
      { id: "chat-1", kind: "chat" as const, title: "Fix auth", conversationId: "c-1", workspaceId: "ws-1" },
      { id: "browser-1", kind: "browser" as const, title: "Docs", url: "https://example.com/", workspaceId: "ws-1" },
      { id: "term-1", kind: "terminal" as const, title: "Shell", workspaceId: "ws-1" },
    ],
  };
  const next = clearAllConversations(state);
  assert.deepEqual(next.tabs[0], {
    id: "chat-1",
    kind: "chat",
    title: UNTITLED_THREAD_TITLE,
    conversationId: undefined,
    workspaceId: "ws-1",
  });
  assert.equal(next.tabs[1], state.tabs[1]);
  assert.equal(next.tabs[2], state.tabs[2]);
  assert.equal(clearAllConversations(next), next, "already-clean state returns the same reference");
});

// ── saved backups keep the history, and the card says so ────────────────────
//
// `POST /history/clear` does not touch `<state_dir>/backups/` (docs/04 §8.3), but the card promised everything
// "permanently" removed: a person asking for a clean profile kept full copies of it on disk and was never told.

const TWO_BACKUPS = () =>
  json({
    db_path: "/tmp/codify.db",
    db_bytes: 0,
    backups_dir: "/home/me/.codify/backups",
    backups: [
      { filename: "codify-backup-20261101-120000.db", path: "/home/me/.codify/backups/a.db", size_bytes: 1, created_at: 1 },
      { filename: "codify-backup-20261110-120000.db", path: "/home/me/.codify/backups/b.db", size_bytes: 1, created_at: 2 },
    ],
    pending_restore: null,
  });

const clearWithPhrase = async (dom: Dom): Promise<void> => {
  const openBtn = Array.from(dom.container.querySelectorAll("button")).find((b) =>
    b.textContent?.includes("Delete all history…"),
  ) as HTMLButtonElement;
  await dom.click(openBtn);
  await dom.settle();
  await dom.fill(dom.container.querySelector('input[placeholder="delete all history"]') as HTMLInputElement, "delete all history");
  await dom.settle();
  const submitBtn = Array.from(dom.container.querySelectorAll("button")).find((b) =>
    b.textContent?.includes("Permanently delete history"),
  ) as HTMLButtonElement;
  await dom.click(submitBtn);
  await dom.settle();
};

test("with saved backups, the card says how many still hold the history and where, before and after", async () => {
  await withSessionsPane(
    () => json(CLEARED_REPLY),
    async (dom) => {
      const before = dom.text();
      assert.match(before, /Saved backups are not deleted: 2 backups in \/home\/me\/\.codify\/backups still hold the history/);
      await clearWithPhrase(dom);
      const after = dom.text();
      assert.match(after, /Deleted 3 threads/);
      assert.ok(
        (after.match(/2 backups in \/home\/me\/\.codify\/backups still hold the history/g) ?? []).length >= 2,
        "the result does not repeat that the backups still hold the history",
      );
    },
    TWO_BACKUPS,
  );
});

test("with no backups, or no answer about them, the card still says backups are not deleted", async () => {
  for (const status of [
    () => json({ db_path: "/tmp/codify.db", db_bytes: 0, backups_dir: "/tmp/backups", backups: [], pending_restore: null }),
    () => json({ code: "workspace_unavailable", message: "down" }, 503),
  ]) {
    await withSessionsPane(() => json(CLEARED_REPLY), async (dom) => {
      assert.match(dom.text(), /Saved backups \(Backup and restore, above\) are not deleted by this\./);
    }, status);
  }
});
