/**
 * The Statistics drawer over a long history.
 *
 * Measured against 400 days of activity and 41 models, the drawer drew every day the engine knew about and
 * every model that had ever answered, one row each, and "All" counted only the newest 20,000 model calls
 * without saying so (63% of the true total). These tests pin what it does about both: the newest rows and
 * the biggest spenders first with the count of what is behind "show more", and a sentence when a window
 * reaches past what the engine read.
 *
 * Mounted, with the engine's four stats routes stubbed, because the claim is about what a person sees.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const { DAYS_SHOWN, LANES_SHOWN, coverageNotice, newestDays, topLanes } = await import("../src/statsView.ts");
const React = (await import("react")).default;
const h = React.createElement;

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

const lane = (tokens: number, calls = 1) => ({
  input_tokens: tokens, output_tokens: 0, total_tokens: tokens, calls, avg_duration_ms: 100,
});

const DAY = 86400;
const NOW_S = Date.UTC(2026, 9, 3, 12) / 1000;

/** `n` frozen days ending the day before NOW, oldest first, each a real row. */
function frozenDays(n: number) {
  return Array.from({ length: n }, (_, i) => {
    const date = new Date((NOW_S - (n - i) * DAY) * 1000).toISOString().slice(0, 10);
    const stats = { date, created: 3, succeeded: 2, failed: 1, cancelled: 0, total_tokens: 1000, calls: 10 };
    return { day: date, day_stats: stats, goals: { goals: 3, active: 0, succeeded: 2, failed: 1, cancelled: 0, success_rate: 67 }, usage: lane(1000, 10), daily: [stats], window_days: 0 };
  });
}

function overview(models: number, coverage?: unknown) {
  const by_model = Object.fromEntries(Array.from({ length: models }, (_, i) => [`provider/model-${i}`, lane(1000 * (models - i))]));
  return {
    window_days: 0,
    generated_at: NOW_S,
    goals: { goals: 5, active: 0, succeeded: 4, failed: 1, cancelled: 0, success_rate: 80 },
    usage: { ...lane(50_000, 20), failures: 0, by_role: { fixer: lane(30_000), planner: lane(20_000) }, by_model },
    daily: [],
    ...(coverage ? { coverage } : {}),
  };
}

interface World {
  days?: number;
  models?: number;
  coverage?: unknown;
}

async function withPanel(world: World, body: (dom: Dom) => Promise<void>): Promise<void> {
  await withDom(async (dom) => {
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      if (path === "/stats/overview") return json(overview(world.models ?? 3, world.coverage));
      if (path === "/stats/failures") return json({ total: 0, retries: 0, causes: [], by_role: {}, by_stage: {}, window_days: 0, recovered: 0, recovery_rate: null, generated_at: NOW_S });
      if (path === "/stats/history") return json({ days: frozenDays(world.days ?? 0) });
      if (path === "/stats/import") return json({ imported: false, days: [], source: null, imported_at: null });
      throw new Error(`no answer for ${path}`);
    }) as typeof fetch;
    const api = await import("../src/api.ts");
    api.setEngineInfo({ port: 7431, token: "t" });
    const { StatsPanel } = await import("../src/components/StatsPanel.tsx");
    await dom.render(h(StatsPanel));
    for (let i = 0; i < 8; i++) await dom.settle();
    await body(dom);
  });
}

const card = (dom: Dom, title: string): HTMLElement => {
  const found = [...dom.container.querySelectorAll("div")].find(
    (d) => d.firstElementChild?.textContent?.startsWith(title) && d.className.includes("rounded-xl"),
  );
  assert.ok(found, `no "${title}" card`);
  return found as HTMLElement;
};

const rowsOf = (dom: Dom, title: string): string[] =>
  [...card(dom, title).querySelectorAll(".font-mono.truncate")].map((e) => e.textContent ?? "");

// ── the helpers ─────────────────────────────────────────────────────────────

test("newestDays keeps the newest of an oldest-first list and counts what it left out", () => {
  const days = Array.from({ length: 40 }, (_, i) => i);
  const collapsed = newestDays(days, false);
  assert.equal(collapsed.shown.length, DAYS_SHOWN);
  assert.equal(collapsed.shown[collapsed.shown.length - 1], 39, "the newest day was cut");
  assert.equal(collapsed.shown[0], 40 - DAYS_SHOWN);
  assert.equal(collapsed.hidden, 40 - DAYS_SHOWN);
  assert.deepEqual(newestDays(days, true).shown, days);
  assert.equal(newestDays(days, true).hidden, 0);
  assert.equal(newestDays([1, 2, 3], false).hidden, 0, "a short list was truncated");
});

test("topLanes orders by spend, keeps the biggest, and says what the rest add up to", () => {
  const lanes = Object.fromEntries(Array.from({ length: 12 }, (_, i) => [`m${i}`, lane((i + 1) * 100)]));
  const { shown, hidden, hiddenTokens } = topLanes(lanes, false);
  assert.equal(shown.length, LANES_SHOWN);
  assert.equal(shown[0]![0], "m11", "not sorted by tokens, biggest first");
  assert.equal(hidden, 4);
  assert.equal(hiddenTokens, 100 + 200 + 300 + 400, "the hidden tokens are not the four smallest spenders'");
  assert.equal(topLanes(lanes, true).shown.length, 12);
  assert.equal(topLanes({ a: lane(1) }, false).hidden, 0);
});

const COVER = { goals: 900, goal_cap: 5000, events: 20000, event_cap: 20000, truncated: true, since: NOW_S - 100 * DAY };

test("a window that reaches past where the read begins says from when it counts", () => {
  const all = coverageNotice(COVER, 0, NOW_S * 1000);
  assert.ok(all, "All over a truncated read said nothing");
  assert.match(all, /newest 20,000 model calls/);
  assert.doesNotMatch(all, /goals/, "the goals were not cut, so they are not named");
  assert.match(all, /since 2026-06-25/);
  assert.ok(coverageNotice(COVER, 30, NOW_S * 1000) === null, "a 30-day window starts inside the 100 days that were read");
  assert.ok(coverageNotice({ ...COVER, since: NOW_S - 3 * DAY }, 7, NOW_S * 1000), "a 7-day window starts before a read that begins 3 days ago");
});

test("nothing is said when nothing was cut, or when the engine does not report coverage", () => {
  assert.equal(coverageNotice({ ...COVER, truncated: false, since: null }, 0, NOW_S * 1000), null);
  assert.equal(coverageNotice(undefined, 0, NOW_S * 1000), null);
  assert.match(coverageNotice({ ...COVER, goals: 5000, events: 100 }, 0, NOW_S * 1000) ?? "", /newest 5,000 goals/);
});

// ── the panel ───────────────────────────────────────────────────────────────

test("sixty frozen days show the newest fourteen, and say how many are behind the button", async () => {
  await withPanel({ days: 60 }, async (dom) => {
    const chart = card(dom, "Day by day");
    const rows = chart.querySelectorAll(".font-mono.w-20");
    assert.equal(rows.length, DAYS_SHOWN, "the chart drew more than the newest days");
    assert.match(chart.textContent ?? "", /60 days/);
    const more = dom.byButton("Show 46 earlier days");
    assert.equal(more.getAttribute("aria-expanded"), "false");
    await dom.click(more);
    assert.equal(chart.querySelectorAll(".font-mono.w-20").length, 60, "'show earlier' did not show them");
    await dom.click(dom.byButton(`Show the latest ${DAYS_SHOWN} days`));
    assert.equal(chart.querySelectorAll(".font-mono.w-20").length, DAYS_SHOWN);
  });
});

test("a short history has no button and nothing hidden", async () => {
  await withPanel({ days: 5 }, async (dom) => {
    const chart = card(dom, "Day by day");
    assert.equal(chart.querySelectorAll(".font-mono.w-20").length, 5, "a short history was cut");
    assert.doesNotMatch(chart.textContent ?? "", /Show \d+ earlier/);
  });
});

test("forty-one models show the eight biggest, and the button says how many tokens are behind it", async () => {
  await withPanel({ models: 41 }, async (dom) => {
    const shown = rowsOf(dom, "By model");
    assert.equal(shown.length, LANES_SHOWN);
    assert.equal(shown[0], "provider/model-0", "the biggest spender is not first");
    const hiddenTokens = Array.from({ length: 33 }, (_, i) => 1000 * (41 - (i + 8))).reduce((a, b) => a + b, 0);
    const button = dom.byButton(`Show 33 more · ${(hiddenTokens / 1000).toFixed(1)}k tokens`);
    await dom.click(button);
    assert.equal(rowsOf(dom, "By model").length, 41);
  });
});

test("the roles table is never cut: there are only eight roles", async () => {
  await withPanel({ models: 41 }, async (dom) => {
    assert.equal(rowsOf(dom, "By role").length, 2);
    assert.doesNotMatch(dom.text(), /By role[^]*Show \d+ more[^]*By model/);
  });
});

test("'All' over a read the engine cut says so; a window inside the read does not", async () => {
  const coverage = { goals: 900, goal_cap: 5000, events: 20000, event_cap: 20000, truncated: true, since: NOW_S - 100 * DAY };
  await withPanel({ coverage }, async (dom) => {
    assert.match(dom.text(), /These totals count the newest 20,000 model calls, since \d{4}-\d{2}-\d{2}\./);
    await dom.click(dom.byButton("30d"));
    for (let i = 0; i < 8; i++) await dom.settle();
    assert.doesNotMatch(dom.text(), /These totals count/, "a 30-day window inside the read still carried the notice");
  });
});

test("an engine that does not report coverage shows no notice", async () => {
  await withPanel({}, async (dom) => {
    assert.doesNotMatch(dom.text(), /These totals count/);
  });
});
