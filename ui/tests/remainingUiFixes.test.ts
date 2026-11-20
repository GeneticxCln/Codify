/**
 * Regression pins for the third audit's UI findings (UIR-1 … UIR-19): the
 * invariants that are checkable without a live engine or a DOM.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
import type { TabState } from "../src/tabs.ts";
registerTsx();

const { DesignDeliverableCard, safeSwatchColor } = await import(
  "../src/components/DesignDeliverableCard.tsx"
);
const { AuditReport, looksLikeAudit } = await import("../src/components/AuditReport.tsx");
const { baseUrlWarning } = await import("../src/components/BaseUrlField.tsx");
const { parseMarkdown, parseInline } = await import("../src/markdown.ts");
const { tabKey, planChanges, newMirror, ensureKeys, emptySharedState, encodeTab } = await import(
  "../src/layoutSync.ts"
);
const { layoutFrom } = await import("../src/tabPersistence.ts");
const { openBrowserTab, openTab, focusTab, emptyTabs } = await import("../src/tabs.ts");
const { loadTints, saveTints } = await import("../src/tint.ts");
const { checkEngineHealth, getGoal, setEngineInfo } = await import("../src/api.ts");
const { openGoalStream } = await import("../src/goalStream.ts");

// ── UIR-1: swatches never become a network request ─────────────────────────

test("UIR-1: a swatch only accepts literal colours", () => {
  for (const ok of ["#0d1117", "#fff", "rgb(1, 2, 3)", "hsl(200 50% 40%)", "rebeccapurple", "oklch(0.7 0.1 200)"]) {
    assert.equal(safeSwatchColor(ok), ok, ok);
  }
  for (const bad of [
    'url("https://evil.example/x.png")',
    "url(https://evil.example)",
    "image-set('a.png' 1x)",
    "red; background-image: url(x)",
    "",
    null,
    42,
    "x".repeat(200),
  ]) {
    assert.equal(safeSwatchColor(bad as unknown), undefined, String(bad));
  }
});

test("UIR-1: the card renders no url() from model output", () => {
  const html = renderToStaticMarkup(
    React.createElement(DesignDeliverableCard, {
      payload: {
        mode: "normal",
        tokens: { colors: [{ name: "bad", value: 'url("https://evil.example/leak")' }, { name: "ok", value: "#123456" }] },
      },
      pinOutcome: null,
      onPin: async () => {},
    }),
  );
  assert.ok(!/style="[^"]*url\(/i.test(html), "no style attribute may carry url()");
  assert.match(html, /background:\s*#123456/);
});

// ── UIR-3: markdown stays linear and stack-safe ─────────────────────────────

test("UIR-3: deeply indented lists and many unclosed links do not throw", () => {
  const deep = Array.from({ length: 3000 }, (_, i) => `${" ".repeat(i * 2)}- item ${i}`).join("\n");
  assert.doesNotThrow(() => parseMarkdown(deep));
  const links = "[a](b ".repeat(20000);
  const started = Date.now();
  assert.doesNotThrow(() => parseInline(links));
  assert.ok(Date.now() - started < 5000, "unclosed links must not be quadratic");
});

// ── UIR-4: a malformed audit is refused or rendered, never a crash ──────────

test("UIR-4: looksLikeAudit refuses lanes of the wrong shape", () => {
  assert.equal(looksLikeAudit({ goal_id: "g", plan_edits: [], errors: "bad" }), false);
  assert.equal(looksLikeAudit({ goal_id: "g", plan_edits: [], usage: [] }), false);
  assert.equal(looksLikeAudit({ goal_id: "g", plan_edits: [] }), true);
});

test("UIR-4: odd nested values render instead of crashing React", () => {
  const doc = {
    goal_id: "g",
    final_status: "FAILED",
    plan_edits: [{ step: 123, changes: { title: null, paths: "x" } }, null, "junk"],
    fallbacks: [{ role: { nested: true }, from: "x", to: null }],
    errors: [{ message: { not: "a string" }, at: "yesterday" }],
    step_outcomes: [{ step: null, status: 7 }],
    usage: { calls: 2, totals: null, by_role: { fixer: null, planner: { total_tokens: "lots" } }, by_model: [] },
    silent_roles: [null, { role: 5 }],
    status_timeline: [{ status: {} }, { status: "DONE" }],
  };
  assert.doesNotThrow(() => renderToStaticMarkup(React.createElement(AuditReport, { doc: doc as never })));
});

// ── UIR-8: blocked storage does not throw ───────────────────────────────────

test("UIR-8: tint storage survives a store that throws", () => {
  const hostile = {
    getItem: () => {
      throw new Error("SecurityError");
    },
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
    removeItem: () => {
      throw new Error("SecurityError");
    },
  };
  assert.deepEqual(loadTints(hostile), {});
  assert.doesNotThrow(() => saveTints(hostile, { dark: { "--x": "#ffffff" } }));
});

// ── UIR-9: path params are encoded, sockets are guarded ─────────────────────

test("UIR-9: goal ids are encoded into HTTP and WebSocket paths, and checkEngineHealth validates shape", async () => {
  setEngineInfo({ port: 7430, token: "tok" });
  const prevFetch = globalThis.fetch;
  const seenUrls: string[] = [];
  try {
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      seenUrls.push(url);
      if (url.endsWith("/health")) {
        return new Response(JSON.stringify({ wrong: true }), { status: 200 });
      }
      return new Response(JSON.stringify({ id: "g/1" }), { status: 200 });
    }) as typeof fetch;

    await getGoal("goal/with space");
    assert.ok(
      seenUrls.some((u) => u.includes("/goals/goal%2Fwith%20space")),
      `expected encoded goal path, got ${seenUrls.join(", ")}`,
    );

    const health = await checkEngineHealth();
    assert.deepEqual(health, { ok: false, authenticated: false });
  } finally {
    globalThis.fetch = prevFetch;
  }

  const prevWS = globalThis.WebSocket;
  const wsUrls: string[] = [];
  try {
    globalThis.WebSocket = class FakeWS {
      constructor(url: string) {
        wsUrls.push(url);
        throw new Error("Immediate construction failure");
      }
      close() {}
    } as unknown as typeof WebSocket;

    let reconnecting = 0;
    const handle = openGoalStream({
      goalId: "a/b?c",
      onEvent: () => {},
      onReconnecting: (attempt) => {
        reconnecting = attempt;
      },
    });
    handle.close();
    assert.ok(wsUrls[0]?.endsWith("/ws/goals/a%2Fb%3Fc"), `got ${wsUrls[0]}`);
    assert.equal(reconnecting, 1, "construction throw schedules reconnect backoff instead of crashing");
  } finally {
    globalThis.WebSocket = prevWS;
  }
});

// ── UIR-10: reorders sync, keys are unique, the fallback is the left neighbour ──

test("UIR-10: a reordered tab is re-sent even with an unchanged payload", () => {
  let state = openBrowserTab(emptySharedState(), "a", "https://a.example/");
  state = openBrowserTab(state, "b", "https://b.example/");
  state = ensureKeys(state).state;
  const rows = state.tabs.map((tab, position) => ({
    key: tab.key!,
    position,
    kind: "browser" as const,
    payload: encodeTab(tab),
  }));
  const known = new Set(rows.map((r) => r.key));
  assert.equal(planChanges(state, newMirror(), known, rows).upserts.length, 0);
  const swapped: TabState = { ...state, tabs: [state.tabs[1], state.tabs[0]] };
  assert.equal(planChanges(swapped, newMirror(), known, rows).upserts.length, 2);
});

test("UIR-10: tab keys carry a per-window nonce", () => {
  const key = tabKey();
  assert.match(key, /^k_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+$/);
  assert.ok(key.length <= 64);
});

test("UIR-10: a vanished active tab falls back to its persisted left neighbour", () => {
  // persisted, terminal, terminal, persisted, [active terminal]
  let live = openBrowserTab(emptyTabs, "b1", "https://a.example/");
  live = openTab(live, { id: "t1", kind: "terminal", title: "T", ptyId: "t1" });
  live = openTab(live, { id: "t2", kind: "terminal", title: "T", ptyId: "t2" });
  live = openBrowserTab(live, "b2", "https://b.example/");
  live = openTab(live, { id: "t3", kind: "terminal", title: "T", ptyId: "t3" });
  live = focusTab(live, "t3");
  const layout = layoutFrom(live);
  assert.equal(layout.tabs.length, 2);
  assert.equal(layout.activeIndex, 1, "b2 is the nearest persisted tab to the left");

  // persisted, terminal(active), persisted → left neighbour is index 0, not 0-by-accident
  let other = openBrowserTab(emptyTabs, "c1", "https://a.example/");
  other = openTab(other, { id: "t4", kind: "terminal", title: "T", ptyId: "t4" });
  other = openBrowserTab(other, "c2", "https://b.example/");
  other = focusTab(other, "t4");
  assert.equal(layoutFrom(other).activeIndex, 0);
});

// ── UIR-16: the base URL warning follows the value ─────────────────────────

test("UIR-16: the base URL warning is derived from props", () => {
  assert.equal(baseUrlWarning("https://api.example.com", false), null);
  assert.match(baseUrlWarning("https://api.example.com", true) ?? "", /localhost/);
  assert.match(baseUrlWarning("https://127.0.0.1:11434", true) ?? "", /http:\/\//);
  assert.equal(baseUrlWarning("http://127.0.0.1:11434", true), null);
  assert.equal(baseUrlWarning("", true), null);
});

