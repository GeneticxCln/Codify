/**
 * Undo for custom colours.
 *
 * The history is four functions over an array, and it is easy to write the
 * version that looks right. The claim worth testing is the one that is not
 * obvious from the shape: **a drag is one entry, and undoing it restores the
 * state from before the gesture rather than from before its last frame.**
 *
 * A colour well is a native picker and a drag across it fires a change event
 * per step, so the sequence below — sixty commits to one variable, none of them
 * distinguishable from a user mashing the keyboard — is the *normal* case and
 * not a stress test. An implementation that records one entry per commit
 * satisfies every other assertion in this file and is unusable: the user presses
 * Undo and the colour does not move, because the last entry is the frame before
 * this one, and the two look identical.
 *
 * Everything here is pure and takes its clock as an argument, so the
 * coalescing window is tested by writing numbers rather than by waiting.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const {
  COALESCE_MS,
  HISTORY_LIMIT,
  canUndo,
  record,
  undo,
} = await import("../src/tintHistory.ts");
const { loadTints, saveTints, applyTintedTheme } = await import("../src/tint.ts");
const { themeById } = await import("../src/appearance.ts");

import type { TintStore } from "../src/tint.ts";
import type { HistoryEntry } from "../src/tintHistory.ts";

/** One pick of `--reagent` on Toxic Lab, at `at` milliseconds. */
const pick = (at: number, store: TintStore = {}): HistoryEntry => ({
  store,
  label: "Bubble body on Toxic Lab",
  burst: "toxic-lab:--reagent",
  at,
});

/** A change with no burst key: a reset, or an import. */
const once = (at: number, store: TintStore = {}): HistoryEntry => ({
  store,
  label: "Reset all colours on Toxic Lab",
  at,
});

/** A one-colour store, so an assertion can name the colour it expects back. */
const with1 = (hex: string): TintStore => ({ "toxic-lab": { "--reagent": hex } });

function fakeStorage(initial: Record<string, string> = {}) {
  const items = new Map<string, string>(Object.entries(initial));
  return {
    getItem: (k: string) => items.get(k) ?? null,
    setItem: (k: string, v: string) => void items.set(k, v),
    removeItem: (k: string) => void items.delete(k),
    items,
  };
}

function fakeStyle() {
  const props = new Map<string, string>();
  return {
    props,
    setProperty: (n: string, v: string) => void props.set(n, v),
    removeProperty: (n: string) => void props.delete(n),
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// The gesture
// ─────────────────────────────────────────────────────────────────────────────

test("sixty changes to one colour well are one undo step", () => {
  // The case the whole module exists for. A drag across the picker is a stream
  // of these and a user thinks of it as one action.
  let history: ReturnType<typeof record> = [];
  const start = with1("#9dff3c");
  for (let i = 0; i < 60; i++) {
    history = record(history, pick(i * 10, start));
  }
  assert.equal(history.length, 1, "a drag became sixty undo steps");
  assert.ok(canUndo(history));
});

test("undoing a drag restores the state from before the drag, not the frame before", () => {
  // The failure this is here to prevent, and it passes every other test: each
  // entry holds a *store*, and the tempting implementation is to overwrite the
  // whole entry on each frame — which would make the last entry hold the second
  // to last colour. Undo would then move the colour by one frame and the user
  // would press it again, and again, sixty times.
  const before = with1("#9dff3c");
  const after = with1("#ce2c43");
  let history: ReturnType<typeof record> = [];
  history = record(history, pick(0, before));
  history = record(history, pick(20, after));
  history = record(history, pick(40, after));
  history = record(history, pick(60, after));

  const step = undo(history);
  assert.ok(step, "there was something to undo");
  assert.deepEqual(step.store, before, "undo restored a frame of the drag, not the state before it");
  assert.equal(step.label, "Bubble body on Toxic Lab", "and it does not say what it undid");
});

test("the collapse keeps the earlier entry's store and drops the intermediate frames", () => {
  let history: ReturnType<typeof record> = [];
  history = record(history, pick(0, with1("#9dff3c")));
  history = record(history, pick(30, with1("#ce2c43")));
  assert.equal(history.length, 1);
  assert.deepEqual(history[0]!.store, with1("#9dff3c"), "the entry holds the frame, not the gesture");
  assert.equal(history[0]!.at, 30, "the clock advanced, so the next frame is still a coalesce");
});

test("two deliberate picks a moment apart stay two entries", () => {
  // The other failure: coalesce on the burst key alone and every pick of the
  // same variable ever made becomes one entry, so a user who picks crimson,
  // dislikes it, picks blue and then dislikes that has one button to press for
  // three mistakes.
  let history: ReturnType<typeof record> = [];
  history = record(history, pick(0, with1("#9dff3c")));
  history = record(history, pick(COALESCE_MS + 1, with1("#0000ff")));
  assert.equal(history.length, 2, "a deliberate second pick was swallowed by the first");
  // An entry holds the state from *before* its change, so the first undo lands
  // on the colour the second pick replaced — not on the original.
  const first = undo(history);
  assert.deepEqual(first?.store, with1("#0000ff"), "the first undo did not undo the second pick");
  assert.deepEqual(undo(first!.history)?.store, with1("#9dff3c"), "and the second did not undo the first");
});

test("picks of different variables never merge, however fast", () => {
  let history: ReturnType<typeof record> = [];
  history = record(history, pick(0, with1("#9dff3c")));
  history = record(history, { ...pick(1, with1("#0000ff")), burst: "toxic-lab:--reagent-skin" });
  assert.equal(history.length, 2, "two different colours became one entry");
});

test("a change with no burst key never coalesces with anything", () => {
  // A reset and an import are not gestures that produce a stream of themselves,
  // and merging two of them would make a second reset a no-op to press.
  let history: ReturnType<typeof record> = [];
  history = record(history, once(0));
  history = record(history, once(1));
  assert.equal(history.length, 2);
  history = record(history, once(2));
  assert.equal(history.length, 3);
  // And one without a key does not absorb a pick that happens to follow it.
  history = record(history, pick(3, with1("#9dff3c")));
  assert.equal(history.length, 4);
});

// ─────────────────────────────────────────────────────────────────────────────
// The stack
// ─────────────────────────────────────────────────────────────────────────────

test("the stack is bounded, and it drops the oldest rather than the newest", () => {
  // Dropping the wrong end makes editing *stop working* after the limit, which
  // is a far worse failure than losing the oldest twenty-five steps: the user
  // is still making changes and they are simply not undoable, with nothing on
  // screen to say so.
  let history: ReturnType<typeof record> = [];
  for (let i = 0; i < HISTORY_LIMIT + 10; i++) {
    history = record(history, once(i * 10_000));
  }
  assert.equal(history.length, HISTORY_LIMIT);
  assert.equal(
    history[0]!.at,
    10 * 10_000,
    "the oldest ten entries survived, so the newest were dropped instead",
  );
  assert.equal(
    history[HISTORY_LIMIT - 1]!.at,
    (HISTORY_LIMIT + 9) * 10_000,
    "the most recent change is not undoable, which is the failure to avoid",
  );
});

test("undo walks backwards and then reports that there is nothing left", () => {
  let history: ReturnType<typeof record> = [];
  history = record(history, once(0, with1("#111111")));
  history = record(history, once(10_000, with1("#222222")));
  history = record(history, once(20_000, with1("#333333")));

  const first = undo(history);
  assert.deepEqual(first?.store, with1("#333333"), "the first undo did not take back the last change");
  const second = undo(first!.history);
  assert.deepEqual(second?.store, with1("#222222"), "and the second skipped a step");
  const third = undo(second!.history);
  assert.deepEqual(third?.store, with1("#111111"), "the third did not reach the oldest entry");
  assert.equal(canUndo(third!.history), false, "still offering to undo after the stack emptied");
  assert.equal(undo(third!.history), null, "and returning a step with nothing in it");
  assert.equal(canUndo([]), false);
});

test("an empty stack is safe to press", () => {
  // The button is disabled, but a disabled button is a UI state and not a
  // guarantee: a keyboard shortcut, a stale render, or a click that lands
  // between the press and the re-render all reach here.
  assert.equal(undo([]), null);
  assert.deepEqual(undo([])?.store, undefined, "and it must not be an empty object that wipes the store");
});

// ─────────────────────────────────────────────────────────────────────────────
// Back into the app
// ─────────────────────────────────────────────────────────────────────────────

test("a restored store has to be persisted *and* worn, not just held in React", () => {
  // The end-to-end claim. `undo` returns a store; if the caller only puts it in
  // component state, the window keeps painting the colour the user is trying to
  // undo, and the next restart brings it back. Both writes, every time.
  const storage = fakeStorage();
  const original = with1("#9dff3c");
  const edited = with1("#8b1e2d");

  // First that the *edited* store is visible as an edit at all, so the
  // assertions below are about a tint that exists rather than about the theme's
  // own colour being written back.
  const editedStyle = fakeStyle();
  saveTints(storage, edited);
  applyTintedTheme("toxic-lab", editedStyle, storage);
  const wearing = editedStyle.props.get("--reagent") as string;
  assert.notEqual(
    wearing,
    themeById("toxic-lab").tokens["--reagent"],
    "the edit is indistinguishable from the theme, so nothing below can fail",
  );

  // Now the undo. The entry holds the state from before the pick, which here is
  // the theme's own colour — so restoring it means *wearing the theme again*,
  // and that is a different assertion from "wrote a hex back".
  let history: ReturnType<typeof record> = [];
  history = record(history, pick(0, original));
  const step = undo(history);
  assert.ok(step);

  saveTints(storage, step.store);
  assert.deepEqual(
    loadTints(storage),
    original,
    "the store came back in memory but never reached storage, so a restart resurrects it",
  );
  assert.equal(
    storage.items.get("codify.tints"),
    JSON.stringify(original),
    "and storage holds the store, not a tombstone — the edited theme is gone entirely",
  );

  const style = fakeStyle();
  applyTintedTheme("toxic-lab", style, storage);
  assert.notEqual(style.props.get("--reagent"), wearing, "the undo changed the store but not the screen");
  assert.equal(style.props.get("--reagent"), themeById("toxic-lab").tokens["--reagent"]);
});

test("undoing past the tintable set restores the theme's own palette, not a blank one", () => {
  // A snapshot can legitimately hold no entry for a theme — it was untinted
  // before the edit. Restoring that has to mean "wearing the theme's own
  // colours", and `applyTintedTheme` is what decides it. An undo that wrote
  // `{}` over the document root would leave a theme with no background at all.
  const storage = fakeStorage();
  saveTints(storage, {});
  const style = fakeStyle();
  applyTintedTheme("toxic-lab", style, storage);
  const theme = themeById("toxic-lab");
  assert.equal(style.props.get("--codify-bg"), theme.tokens["--codify-bg"]);
  assert.ok(style.props.get("--reagent"), "the theme's own weather colour is still there");
});
