import test from "node:test";
import assert from "node:assert/strict";

import {
  mergeDays,
  mergeStatsHistory,
  summarizeStatsHistoryImport,
  validateStatsHistoryDocument,
} from "../src/statsHistory.ts";
import type { StatsHistoryDay } from "../src/types.ts";

function historyDay(
  date: string,
  overrides: Partial<StatsHistoryDay["day_stats"]> = {},
): StatsHistoryDay {
  return {
    day: date,
    day_stats: {
      date,
      created: 1,
      succeeded: 1,
      failed: 0,
      cancelled: 0,
      total_tokens: 10,
      calls: 2,
      ...overrides,
    },
    goals: {
      goals: 1,
      active: 0,
      succeeded: 1,
      failed: 0,
      cancelled: 0,
      success_rate: 100,
    },
    usage: {
      input_tokens: 4,
      output_tokens: 6,
      total_tokens: 10,
      calls: 2,
      avg_duration_ms: 100,
    },
  };
}

function document(days: StatsHistoryDay[]): unknown {
  return { exported_at: "2026-09-24T12:00:00.000Z", days };
}

test("accepts a structurally valid chronological history document", () => {
  const days = [historyDay("2026-09-22"), historyDay("2026-09-23")];
  const result = validateStatsHistoryDocument(document(days));

  assert.equal(result.ok, true);
  if (result.ok) assert.deepEqual(result.document.days, days);
});

test("rejects malformed and empty history documents", () => {
  assert.equal(validateStatsHistoryDocument(null).ok, false);
  assert.equal(validateStatsHistoryDocument({ exported_at: "now", days: [] }).ok, false);

  const invalid = historyDay("2026-09-23");
  invalid.day_stats.created = -1;
  assert.equal(validateStatsHistoryDocument(document([invalid])).ok, false);
});

test("names a duplicate day instead of silently accepting it", () => {
  const result = validateStatsHistoryDocument(
    document([historyDay("2026-09-23"), historyDay("2026-09-23")]),
  );

  assert.equal(result.ok, false);
  if (!result.ok) assert.match(result.error, /duplicate day 2026-09-23/);
});

test("names days that violate oldest-first ordering", () => {
  const result = validateStatsHistoryDocument(
    document([historyDay("2026-09-23"), historyDay("2026-09-22")]),
  );

  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.match(result.error, /not chronological/);
    assert.match(result.error, /2026-09-22 appears after 2026-09-23/);
  }
});

test("chart merge sorts dates, reads day_stats, and lets snapshots beat live rows", () => {
  const frozen = historyDay("2026-09-22", { succeeded: 2, total_tokens: 99 });
  const merged = mergeDays(
    [frozen],
    [
      { date: "2026-09-23", created: 1, succeeded: 0, failed: 0, cancelled: 0, total_tokens: 4, calls: 1 },
      { date: "2026-09-22", created: 9, succeeded: 9, failed: 0, cancelled: 0, total_tokens: 999, calls: 9 },
    ],
  );

  assert.deepEqual(merged.map((day) => day.date), ["2026-09-22", "2026-09-23"]);
  assert.equal(merged[0].source, "snapshot");
  assert.equal(merged[0].succeeded, 2);
  assert.equal(merged[0].total_tokens, 99);
  assert.equal(merged[1].source, "live");
});

test("export merge deduplicates by date, lets later sources win, and sorts", () => {
  const imported = historyDay("2026-09-22", { total_tokens: 10 });
  const local = historyDay("2026-09-22", { total_tokens: 20 });
  const older = historyDay("2026-09-21", { total_tokens: 5 });

  const merged = mergeStatsHistory([imported, older], [local]);

  assert.deepEqual(merged.map((day) => day.day), ["2026-09-21", "2026-09-22"]);
  assert.equal(merged[1].day_stats.total_tokens, 20);
});

test("import summary reports new and locally matched dates", () => {
  const imported = [historyDay("2026-09-21"), historyDay("2026-09-22"), historyDay("2026-09-23")];
  const local = [historyDay("2026-09-22")];

  assert.deepEqual(summarizeStatsHistoryImport(imported, local), {
    imported: 3,
    matchedLocal: 1,
    added: 2,
  });
});

test("a restored import still defers to local engine snapshots on a shared day", () => {
  // The engine stores imports separately from the days it froze itself, and
  // the panel merges the two streams on open. Precedence has to be the same
  // whether the import arrived from a file picker ten seconds ago or was
  // hydrated from the store on mount: a local snapshot is a measurement this
  // engine made, and it wins.
  const imported = historyDay("2026-09-22", { total_tokens: 10, succeeded: 5 });
  const localSnapshot = historyDay("2026-09-22", { total_tokens: 20, succeeded: 1 });

  const merged = mergeDays([imported, localSnapshot], []);

  assert.equal(merged.length, 1, "one shared date, not two rows");
  assert.equal(merged[0].total_tokens, 20, "the local snapshot wins");
  assert.equal(merged[0].succeeded, 1);
});

test("restoring an import from the store reports the same merge as a fresh import", () => {
  const imported = [historyDay("2026-09-21"), historyDay("2026-09-22")];
  const local = [historyDay("2026-09-22")];

  const fresh = summarizeStatsHistoryImport(imported, local);
  const restored = summarizeStatsHistoryImport([...imported], [...local]);

  assert.deepEqual(restored, fresh);
  assert.equal(restored.added, 1);
  assert.equal(restored.matchedLocal, 1);
});
