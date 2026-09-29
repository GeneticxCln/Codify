/**
 * The motion store: who decided this window animates, and for how long.
 *
 * **The failure this holds.** On a machine whose WebKitGTK composites in
 * software, a full-window 30 FPS backdrop costs about a core *in the shell* —
 * the page never sees a slow frame, so the page cannot fix it and a frozen
 * window cannot reach a settings pane. The shell announces; this store is how
 * the page reacts, and these tests are the contract between the two halves:
 *
 * - **Two facts, two lifetimes.** The user's choice is a preference
 *   (`localStorage`); the shell's verdict is a diagnosis of one boot
 *   (`sessionStorage`). Persisting the diagnosis would turn "your machine
 *   cannot afford this today" into "your machine can never have this".
 * - **The ranking.** The user's own word wins in both directions, the verdict
 *   outranks the system preference, and `auto` falls through to the system.
 * - **The wiring.** The event the shell emits is the event this store listens
 *   for, and the two render loops honor the store — nothing in the two type
 *   systems spans that gap, so these tests read the committed files.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  applyShellStarvation,
  clearShellStarvation,
  motionAllowed,
  MOTION_EVENT,
  MOTION_KEY,
  readMotion,
  rearmVerdictForBoot,
  VERDICT_SURVIVES_RELOAD,
  writeSetting,
} from "../src/motionPreference.ts";

/** A tiny map-backed Storage, so tests can read what a call wrote. */
function mapStore(): Storage {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
  } as unknown as Storage;
}

test("the user's word outranks everything, in both directions", () => {
  const verdict = { at: 1, measuredMs: 90 };
  assert.equal(motionAllowed({ setting: "allowed", shellStarved: verdict }, false), true);
  assert.equal(
    motionAllowed({ setting: "allowed", shellStarved: verdict }, true),
    true,
    "an explicit 'allowed' outranks even the system preference",
  );
  assert.equal(motionAllowed({ setting: "reduced", shellStarved: null }, false), false);
  assert.equal(motionAllowed({ setting: "reduced", shellStarved: null }, true), false);
});

test("auto falls through: the verdict first, then the system preference", () => {
  const verdict = { at: 1, measuredMs: 90 };
  assert.equal(motionAllowed({ setting: "auto", shellStarved: verdict }, false), false);
  assert.equal(motionAllowed({ setting: "auto", shellStarved: null }, false), true);
  assert.equal(motionAllowed({ setting: "auto", shellStarved: null }, true), false);
});

test("a verdict written by the shell is readable until cleared, and clearing is the user's undo", () => {
  const s = mapStore();
  const withVerdict = applyShellStarvation({ at: 5, measuredMs: 88 }, { session: s });
  assert.deepEqual(readMotion({ session: s }).shellStarved, { at: 5, measuredMs: 88 });
  assert.equal(motionAllowed(withVerdict, false), false);

  const undone = clearShellStarvation({ session: s });
  assert.equal(undone.shellStarved, null);
  assert.equal(motionAllowed(undone, false), true, "undoing the verdict restores motion");
});

test("the verdict dies with the session; the choice does not", () => {
  // The literal claim of the lifetime split: the user's setting is written to
  // the long-lived store, the verdict to the session one, and re-arming a boot
  // empties only the diagnosis. A test that asserted this from strings would
  // not hold the lifetime at all — it holds because the two fakes are distinct
  // stores and each call names the one it must touch.
  const user = mapStore();
  const session = mapStore();
  writeSetting("reduced", { user, session });
  applyShellStarvation({ at: 1, measuredMs: 90 }, { user, session });
  rearmVerdictForBoot({ user, session });

  const after = readMotion({ user, session });
  assert.equal(after.setting, "reduced", "a boot must not forget the user's choice");
  assert.equal(after.shellStarved, null, "a boot must not remember the shell's verdict");
  assert.equal(VERDICT_SURVIVES_RELOAD, true, "the session store outlives a reload by design");
});

test("garbage in the verdict slot is no verdict", () => {
  const session = mapStore();
  session.setItem(MOTION_EVENT, "{not json");
  assert.equal(readMotion({ session }).shellStarved, null);
  session.setItem(MOTION_EVENT, JSON.stringify({ at: "x" }));
  assert.equal(readMotion({ session }).shellStarved, null);
});

test("an unknown spelling of the setting reads as auto", () => {
  const user = mapStore();
  user.setItem(MOTION_KEY, "sometimes");
  assert.equal(readMotion({ user }).setting, "auto");
});

test("the loops honor the store, and App folds the shell's verdict in", () => {
  // Both render loops must consult the store at effect time, and App must be
  // the listener that turns the shell's event into store state. The hooks read
  // the store *inside* their effects, so the source is the only place this can
  // be asserted — and the same argument as canvasRecovery's wiring test: a rule
  // wired into one loop is a rule the other silently lacks.
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(app, /listenShellEvent<[^>]+>\(MOTION_EVENT/);
  assert.match(app, /applyShellStarvation\(/);
  assert.match(app, /rearmVerdictForBoot\(\)/, "a stale verdict must not survive a boot");
  assert.match(app, /writeSetting\("allowed"\)/, "the banner must hand back the choice");

  for (const file of ["hooks/useAtmosphereCanvas.ts", "components/ui/MatrixRain.tsx"]) {
    const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
    assert.match(
      source,
      /useMotionAllowed\(\)/,
      `${file} does not consult the motion store`,
    );
    // The explicit preview prop still wins — a preview that cannot animate is
    // not a preview — so the store consult sits behind an `animated` check.
    // (`atmosphereMotion.test.ts` is the one that proves which way it plays.)
    assert.match(source, /animated === undefined/, `${file} lost the preview escape hatch`);
  }
});

test("the shell emits the event this store names", () => {
  // Nothing spans the Rust/TypeScript type systems, so this reads both
  // committed sources: the Rust constant and the TS constant must agree.
  const rust = readFileSync(new URL("../../src-tauri/src/lib.rs", import.meta.url), "utf8");
  const ts = readFileSync(new URL("../src/motionPreference.ts", import.meta.url), "utf8");
  const match = rust.match(/RENDER_STARVED_EVENT: &str = "([^"]+)"/);
  assert.ok(match, "the Rust event constant moved or was renamed");
  assert.ok(
    ts.includes(`"${match![1]}"`),
    "the two ends of the starvation contract disagree",
  );
});
