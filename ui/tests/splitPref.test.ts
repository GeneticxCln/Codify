/**
 * Where the divider was left: a view preference, like the left panel's, and never a record.
 *
 * It lives in `localStorage` and nowhere else (not a tab, not the engine's strip), an even split is what a missing
 * or unreadable value means, and a storage that refuses is not an error: the divider just is not remembered.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { SPLIT_RATIO_KEY, readSplitRatio, writeSplitRatio } = await import("../src/splitPref.ts");
const { DEFAULT_RATIO } = await import("../src/panes.ts");

const storeWith = (value: string | null) => ({ getItem: (key: string) => (key === SPLIT_RATIO_KEY ? value : null) });

test("a remembered position comes back", () => {
  assert.equal(readSplitRatio(storeWith("0.35")), 0.35);
  assert.equal(readSplitRatio(storeWith("0.5")), 0.5);
});

test("the limits themselves are allowed", () => {
  assert.equal(readSplitRatio(storeWith("0.1")), 0.1);
  assert.equal(readSplitRatio(storeWith("0.9")), 0.9);
});

test("a missing or unreadable value is an even split", () => {
  for (const raw of [null, "", "   ", "abc", "NaN", "Infinity", "-0.2", "0", "1", "1.5", "0.01", "0.99"]) {
    assert.equal(readSplitRatio(storeWith(raw)), DEFAULT_RATIO, JSON.stringify(raw));
  }
});

test("a storage that throws on read is an even split", () => {
  const broken = {
    getItem: () => {
      throw new Error("blocked");
    },
  };
  assert.equal(readSplitRatio(broken), DEFAULT_RATIO);
});

test("no storage at all is an even split", () => {
  assert.equal(readSplitRatio(undefined), DEFAULT_RATIO);
});

test("a position is written under its own key", () => {
  const written: Array<[string, string]> = [];
  writeSplitRatio(0.4, { setItem: (k, v) => void written.push([k, v]) });
  assert.deepEqual(written, [[SPLIT_RATIO_KEY, "0.4"]]);
  assert.equal(SPLIT_RATIO_KEY, "codify.splitRatio");
});

test("a position that is not a number is not written", () => {
  const written: string[] = [];
  for (const bad of [Number.NaN, Number.POSITIVE_INFINITY]) writeSplitRatio(bad, { setItem: (_k, v) => void written.push(v) });
  assert.deepEqual(written, []);
});

test("a storage that throws on write is not an error", () => {
  assert.doesNotThrow(() =>
    writeSplitRatio(0.4, {
      setItem: () => {
        throw new Error("full");
      },
    }),
  );
});

test("what is written is read back", () => {
  const data = new Map<string, string>();
  const store = { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v) };
  writeSplitRatio(0.62, store);
  assert.equal(readSplitRatio(store), 0.62);
});
