/**
 * Which models count as new, and where that answer comes from.
 *
 * A provider that releases a model puts it in the catalogue, and a list of two
 * hundred is not somewhere a release announces itself — so the panel keeps the
 * record of what the reader was last shown and marks the difference. That record
 * is the only piece of state in this feature, and every interesting failure is a
 * question about it:
 *
 *  * a first visit has nothing to be new *to* (marking 16 models "new" is how a
 *    marker gets ignored);
 *  * a rejected key answers with an empty list, and a baseline that believed it
 *    would announce the whole provider as new one refresh later;
 *  * a baseline that moved with every fetch would make the marker blink out the
 *    moment it appeared, which is the same as having none.
 *
 * `localStorage` is hand-editable, so the reader is tested against values a
 * person, an extension, or an older build could have left behind.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  SEEN_LIMIT,
  emptyIndex,
  loadSeen,
  mergeAll,
  mergeIndex,
  newCountLabel,
  newCountTitle,
  newFirst,
  newModelIds,
  parseIndex,
  readIndex,
  saveSeen,
  writeIndex,
  type SeenIndex,
  type SeenStorage,
} from "../src/modelFreshness.ts";

/** A `localStorage` stand-in, so the tests need no browser. */
function memoryStore(initial: Record<string, string> = {}): SeenStorage & {
  data: Record<string, string>;
} {
  const data: Record<string, string> = { ...initial };
  return {
    data,
    getItem: (key) => (key in data ? data[key] : null),
    setItem: (key, value) => {
      data[key] = value;
    },
  };
}

const OPTION = (id: string) => ({ id });

// ── what counts as new ─────────────────────────────────────────────────────

test("a provider seen for the first time has nothing new in it", () => {
  // Someone opening the panel for the first time has no last visit to be new
  // *to*. Marking every model they have never been shown is how a badge becomes
  // wallpaper — and the first fetch is what puts the provider in the index.
  assert.deepEqual(newModelIds(emptyIndex(), "ollama", ["a", "b"]), []);
  const baseline = mergeIndex(emptyIndex(), "ollama", ["a", "b"]);
  assert.deepEqual(baseline, { ollama: ["a", "b"] });
  assert.deepEqual(newModelIds(baseline, "ollama", ["a", "b"]), []);
});

test("a model the provider did not list last time is new, and only that one", () => {
  const baseline: SeenIndex = { ollama: ["qwen3:8b", "gemma4:12b"] };
  assert.deepEqual(newModelIds(baseline, "ollama", ["qwen3:8b", "newcomer:70b"]), [
    "newcomer:70b",
  ]);
  // Ids repeat across providers, so the comparison is per provider. The same id
  // served by another provider is not a release here.
  assert.deepEqual(newModelIds(baseline, "groq", ["newcomer:70b"]), []);
});

test("a provider with history is unaffected by another provider's release", () => {
  const baseline = mergeAll(emptyIndex(), [
    { provider: "ollama", ok: true, ids: ["a"] },
    { provider: "groq", ok: true, ids: ["x"] },
  ]);
  const next = mergeIndex(baseline, "groq", ["x", "y"]);
  assert.deepEqual(newModelIds(next, "ollama", ["a", "b"]), ["b"]);
  assert.deepEqual(newModelIds(next, "groq", ["x", "y"]), []);
});

test("the baseline keeps first-seen order and does not duplicate", () => {
  const once = mergeIndex(emptyIndex(), "ollama", ["a", "b"]);
  const twice = mergeIndex(once, "ollama", ["b", "a"]);
  assert.deepEqual(twice, { ollama: ["a", "b"] }, "a re-discovery reordered the record");
});

// ── what must never become new ─────────────────────────────────────────────

test("a provider that refused to answer keeps its baseline", () => {
  // A 401 answers with an empty list. Folding that in as "what this provider
  // serves" is how one rejected key turns the provider's entire catalogue into
  // new models on the next refresh — the marker reporting the outage as a
  // release.
  const baseline: SeenIndex = { openai: ["gpt-x", "gpt-y"] };
  const afterFailure = mergeAll(baseline, [{ provider: "openai", ok: false, ids: [] }]);
  assert.deepEqual(afterFailure, baseline, "a failed discovery rewrote the baseline");
  assert.deepEqual(
    newModelIds(afterFailure, "openai", ["gpt-x", "gpt-y", "gpt-z"]),
    ["gpt-z"],
    "the old models are announced as new because the key was rejected",
  );
});

test("a provider that serves nothing at all still counts as answered", () => {
  // The opposite case, and it matters: `ok: true, ids: []` is a fact about the
  // provider (it published nothing), not a failure. It records nothing new and
  // forgets nothing, so a model it adds later is still news.
  const baseline = mergeAll(emptyIndex(), [{ provider: "groq", ok: true, ids: [] }]);
  assert.deepEqual(baseline, { groq: [] });
  assert.deepEqual(newModelIds(baseline, "groq", ["first-ever"]), ["first-ever"]);
});

test("the record cannot grow without bound", () => {
  // A provider serving more models than the cap means the ids seen longest ago
  // are the ones dropped — those are the ones whose "new" badge would matter
  // least. The engine caps a provider at the same number.
  const many = Array.from({ length: SEEN_LIMIT + 5 }, (_, i) => `m${i}`);
  const merged = mergeIndex(emptyIndex(), "openrouter", many);
  assert.equal(merged.openrouter.length, SEEN_LIMIT);
  assert.equal(merged.openrouter[0], "m5", "the oldest ids are the ones dropped");
  assert.equal(merged.openrouter.at(-1), `m${many.length - 1}`);
});

// ── reading and writing the store ──────────────────────────────────────────

test("an unreadable record is no record, not a crash", () => {
  // `localStorage` is editable by the user, by an extension, and by an older
  // build. The cost of getting it wrong is one missing badge; the cost of
  // refusing to open the panel is higher.
  assert.deepEqual(parseIndex(null), {});
  assert.deepEqual(parseIndex(""), {});
  assert.deepEqual(parseIndex("{not json"), {});
  assert.deepEqual(parseIndex('["a"]'), {}, "a list is not a provider map");
  assert.deepEqual(parseIndex('"a string"'), {});
  assert.deepEqual(parseIndex("null"), {});
  assert.deepEqual(parseIndex('{"ollama":"not a list"}'), {});
  // Entries that are not strings are dropped rather than failing the whole read:
  // one bad id is not a reason to forget the other 499.
  assert.deepEqual(parseIndex('{"ollama":["a",7,null,"b"]}'), { ollama: ["a", "b"] });
});

test("a record survives a round trip through storage", () => {
  const store = memoryStore();
  const index = mergeIndex(emptyIndex(), "nvidia", ["meta-llama/llama-3.3-70b"]);
  writeIndex(store, index);
  assert.deepEqual(readIndex(store), index);
  assert.equal(JSON.parse(store.data.CODIFY_SEEN_MODELS).nvidia.length, 1);
});

test("a store that refuses costs the badge, not the panel", () => {
  // Private browsing and a full quota both throw here. The panel still has to
  // open; it just cannot remember anything.
  const hostile: SeenStorage = {
    getItem: () => {
      throw new Error("SecurityError");
    },
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
  };
  assert.deepEqual(readIndex(hostile), {});
  writeIndex(hostile, { ollama: ["a"] });
});

test("outside a browser there is no record and nothing to store", () => {
  // The panel is rendered to a string in the test suite, so this is not
  // defensive decoration: touching `localStorage` at render time is a crash
  // there.
  assert.deepEqual(loadSeen(), {});
  saveSeen({ ollama: ["a"] });
});

// ── where the marker puts the model ────────────────────────────────────────

test("a new model is listed first, and the rest keep the order they arrived in", () => {
  // The engine sorts newest-first only where a provider dates its models, and
  // Ollama dates nothing — so on a long list a release can land in row 170. A
  // badge down there is a badge nobody scrolls to.
  const options = [OPTION("a"), OPTION("b"), OPTION("newcomer"), OPTION("c")];
  assert.deepEqual(
    newFirst(options, ["newcomer"]).map((m) => m.id),
    ["newcomer", "a", "b", "c"],
  );
});

test("a list with nothing new is the list, untouched and copied", () => {
  const options = [OPTION("a"), OPTION("b")];
  const out = newFirst(options, []);
  assert.deepEqual(out, options);
  assert.notEqual(out, options, "the picker got the caller's array back");
  // An id that is not in the list must not empty it or duplicate it.
  assert.deepEqual(
    newFirst(options, ["gone"]).map((m) => m.id),
    ["a", "b"],
  );
});

// ── the words on the badge ─────────────────────────────────────────────────

test("the badge counts in words, and says nothing when there is nothing", () => {
  // A "0 new" pill is a control-shaped lie: it asserts a comparison was made and
  // found empty, which is noise on a row that is already showing a count.
  assert.equal(newCountLabel(0), "");
  assert.equal(newCountLabel(-1), "");
  assert.equal(newCountLabel(1), "1 new");
  assert.equal(newCountLabel(3), "3 new");
  assert.equal(newCountTitle("nvidia", 0), "");
  assert.match(newCountTitle("nvidia", 1), /nvidia did not list when you last looked/);
  assert.match(newCountTitle("nvidia", 3), /3 models nvidia did not list when you last looked/);
  // Not "the last time you were here": the panel re-discovers while it sits
  // open, so the reader can be looking straight at the list as a release lands,
  // and that phrasing is then false in the only sense the reader can check.
  assert.doesNotMatch(newCountTitle("nvidia", 1), /were here/);
});
