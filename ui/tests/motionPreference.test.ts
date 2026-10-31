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
 *   (`sessionStorage`, keyed to the shell's boot id because the store outlived
 *   the process in this webview). Persisting the diagnosis as a preference
 *   would turn "your machine cannot afford this today" into "your machine can
 *   never have this".
 * - **The ranking.** The user's own word wins in both directions, the verdict
 *   outranks the system preference, and `auto` falls through to the system.
 * - **The wiring.** The event the shell emits is the event this store listens
 *   for, and the App turns it into a banner and a verdict. The Rust constant is
 *   the one thing here that only text can reach, so that one test reads
 *   `lib.rs`; the App half is a mounted App receiving the event. The two render
 *   loops honoring the store is held in `atmosphereMotion.test.ts`, which mounts
 *   both.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import {
  applyShellStarvation,
  BOOT_ID_COMMAND,
  BOOT_ID_TIMEOUT_MS,
  clearShellStarvation,
  motionAllowed,
  MOTION_BOOT_KEY,
  MOTION_EVENT,
  MOTION_KEY,
  MOTION_SESSION_KEY,
  readMotion,
  STARVED_BOOT_KEY,
  SOFTWARE_RENDERING_COMMAND,
  starvedThisBoot,
  VERDICT_SURVIVES_RELOAD,
  writeSetting,
  writeSessionSetting,
  within,
} from "../src/motionPreference.ts";

const { withApp } = await import("./appHarness.ts");

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

test("the measurement outlives the verdict that announced it, and dies with the session", () => {
  const session = mapStore();
  applyShellStarvation({ at: 1, measuredMs: 90 }, { session });
  assert.equal(starvedThisBoot({ session }), true);

  clearShellStarvation({ session });
  assert.equal(readMotion({ session }).shellStarved, null, "'Animate anyway' did not withdraw the verdict");
  assert.equal(
    starvedThisBoot({ session }),
    true,
    "keeping the backdrop was read as a claim that the wordmark's gradient is affordable",
  );

  // A reload keeps the same session store; a new launch gets a fresh one.
  assert.equal(
    starvedThisBoot({ session: mapStore() }),
    false,
    "a new launch inherited an earlier window's measurement",
  );
});

test("the verdict survives a reload and dies with the session; the preference does not", () => {
  // The literal claim of the lifetime split: the user's setting is written to
  // the long-lived store, the verdict to the session one, and a new launch gets
  // a fresh session store. A test that asserted this from strings would not hold
  // the lifetime at all — it holds because the two fakes are distinct stores and
  // each call names the one it must touch.
  const user = mapStore();
  const session = mapStore();
  writeSetting("reduced", { user, session });
  applyShellStarvation({ at: 1, measuredMs: 90 }, { user, session });

  const reloaded = readMotion({ user, session });
  assert.equal(reloaded.setting, "reduced", "a reload forgot the user's choice");
  assert.deepEqual(
    reloaded.shellStarved,
    { at: 1, measuredMs: 90 },
    "a reload forgot the shell's verdict, which the shell will not announce again",
  );

  const nextWindow = readMotion({ user, session: mapStore() });
  assert.equal(nextWindow.setting, "reduced", "a new launch forgot the user's choice");
  assert.equal(nextWindow.shellStarved, null, "a new launch remembered the shell's verdict");
  assert.equal(VERDICT_SURVIVES_RELOAD, true, "the session store outlives a reload by design");
});

test("a session fact written by an earlier launch is not this launch's", () => {
  // Measured in the webview: `sessionStorage` outlived the process, so the
  // lifetime had to be written beside the facts. These four keys are what a
  // fresh launch finds when the previous one clicked "Animate anyway" — and
  // reading any of them as current is the original bug.
  const session = mapStore();
  session.setItem(MOTION_BOOT_KEY, "a-previous-launch");
  session.setItem(MOTION_SESSION_KEY, "allowed");
  session.setItem(MOTION_EVENT, JSON.stringify({ at: 1, measuredMs: 90 }));
  session.setItem(STARVED_BOOT_KEY, "1");

  const state = readMotion({ session });
  assert.equal(state.setting, "auto", "last launch's 'Animate anyway' became this launch's setting");
  assert.equal(state.shellStarved, null, "last launch's verdict came back");
  assert.equal(starvedThisBoot({ session }), false, "last launch's measurement lowered this launch's rate");
});

test("a new verdict does not adopt the previous launch's 'Animate anyway'", () => {
  // The collision that was measured: the boot key is shared, so writing this
  // launch's verdict over a previous launch's store made its stale `allowed`
  // current in the same write. The banner said the backdrops were stopped while
  // `motionAllowed` read "allowed" and the canvases kept moving.
  const session = mapStore();
  session.setItem(MOTION_BOOT_KEY, "a-previous-launch");
  session.setItem(MOTION_SESSION_KEY, "allowed");

  const state = applyShellStarvation({ at: 2, measuredMs: 88 }, { session });
  assert.equal(state.setting, "auto", "the previous launch's choice survived into this one");
  assert.deepEqual(state.shellStarved, { at: 2, measuredMs: 88 });
  assert.equal(motionAllowed(state, false), false, "the new verdict did not stop motion");
  assert.equal(session.getItem(MOTION_SESSION_KEY), null, "the stale choice was left in the store");
});

test("a later verdict does not reopen the banner the person already answered", () => {
  // The watchdog can speak after a click that beat it to the store. Re-applying
  // its verdict brought the banner back and stopped a backdrop the person had
  // chosen to keep; the boot flag alone is what the low rate needs.
  const session = mapStore();
  applyShellStarvation({ at: 1, measuredMs: 80 }, { session });
  writeSessionSetting("allowed", { session });
  clearShellStarvation({ session });

  const later = applyShellStarvation({ at: 2, measuredMs: 90 }, { session });
  assert.equal(later.setting, "allowed", "the person's answer was overwritten");
  assert.equal(later.shellStarved, null, "the banner was reopened after it was answered");
  assert.equal(starvedThisBoot({ session }), true, "the measurement was forgotten");
});

test("'Animate anyway' belongs to the window, so a later launch cannot inherit it", () => {
  const user = mapStore();
  const session = mapStore();
  const chosen = writeSessionSetting("allowed", { user, session });
  assert.equal(chosen.setting, "allowed");
  assert.equal(
    session.getItem(MOTION_SESSION_KEY),
    "allowed",
    "the session override was not written to the session store",
  );
  assert.equal(user.getItem(MOTION_KEY), null, "the session override became a preference");

  // The next window starts with a new session store, and the old preference
  // store has nothing that says "animate anyway".
  const nextWindow = mapStore();
  assert.equal(
    readMotion({ user, session: nextWindow }).setting,
    "auto",
    "a new window inherited the previous window's answer",
  );
  // A legacy `allowed` left in localStorage by an earlier build is read as no
  // choice at all; the launch after the click is not the window that clicked.
  const legacy = mapStore();
  legacy.setItem(MOTION_KEY, "allowed");
  assert.equal(readMotion({ user: legacy, session: mapStore() }).setting, "auto");
});

test("writing a preference cannot smuggle in 'allowed'", () => {
  const user = mapStore();
  const session = mapStore();
  writeSetting("allowed", { user, session });
  assert.equal(readMotion({ user, session }).setting, "auto");
});

test("a persistent choice undoes 'Animate anyway', which belongs to the session", () => {
  // The Settings control has to be able to reverse the banner's answer. The
  // session override is read first, so writing only `localStorage` would leave
  // the window animating while the checkbox said it was reduced.
  const user = mapStore();
  const session = mapStore();
  applyShellStarvation({ at: 1, measuredMs: 90 }, { user, session });
  assert.equal(writeSessionSetting("allowed", { user, session }).setting, "allowed");

  const chosen = writeSetting("reduced", { user, session });

  assert.equal(chosen.setting, "reduced");
  assert.equal(user.getItem(MOTION_KEY), "reduced");
  assert.equal(session.getItem(MOTION_SESSION_KEY), null, "the session override survived");
  assert.equal(
    starvedThisBoot({ user, session }),
    true,
    "choosing reduced forgot that this boot was measured slow",
  );
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

const BANNER = /Animated backdrops were stopped/;

test("App folds the shell's verdict in: a banner, and a verdict for this session only", async () => {
  await withApp({}, async ({ dom, emit, settle }) => {
    assert.doesNotMatch(dom.text(), BANNER, "the banner is up before the shell said anything");
    await emit(MOTION_EVENT, 88);
    await settle();
    assert.match(dom.text(), BANNER, "the shell's verdict did not reach the page");
    assert.match(dom.text(), /~88%/, "the banner does not name the measurement");
    assert.equal(readMotion().shellStarved?.measuredMs, 88);
    assert.equal(motionAllowed(readMotion(), false), false, "the verdict did not stop motion");
    // The two lifetimes, checked where they are stored rather than where they are read.
    assert.notEqual(sessionStorage.getItem(MOTION_EVENT), null, "the verdict is not on the session");
    assert.equal(localStorage.getItem(MOTION_EVENT), null, "the verdict was persisted as a preference");
  });
});

test("the motion decision reaches the document, so the CSS animations obey it", async () => {
  await withApp({}, async ({ dom, emit, settle }) => {
    const root = dom.window.document.documentElement;
    await settle();
    assert.equal(root.dataset.motion, "allowed", "the default did not reach the document");
    assert.equal(root.dataset.starved, "false");

    await emit(MOTION_EVENT, 88);
    await settle();
    assert.equal(root.dataset.motion, "reduced", "the verdict did not reach the stylesheet");
    assert.equal(root.dataset.starved, "true");

    await dom.click(dom.byButton("Animate anyway"));
    await settle();
    assert.equal(root.dataset.motion, "allowed", "the person's choice did not reach the stylesheet");
    assert.equal(
      root.dataset.starved,
      "true",
      "the shell's measurement was forgotten when the backdrop was kept",
    );
  });
});

test("'Animate anyway' hands the choice back and withdraws the verdict", async () => {
  await withApp({}, async ({ dom, emit, settle }) => {
    await emit(MOTION_EVENT, 90);
    await settle();
    await dom.click(dom.byButton("Animate anyway"));
    await settle();
    assert.doesNotMatch(dom.text(), BANNER, "the banner outlived the choice that answered it");
    assert.equal(
      sessionStorage.getItem(MOTION_SESSION_KEY),
      "allowed",
      "the session choice was not recorded",
    );
    assert.equal(
      localStorage.getItem(MOTION_KEY),
      null,
      "the session choice became a preference that outlives the window",
    );
    assert.equal(sessionStorage.getItem(MOTION_EVENT), null, "the verdict was not withdrawn");
    assert.equal(motionAllowed(readMotion(), true), true, "an explicit 'allowed' outranks everything");
  });
});

test("a reload keeps the verdict it cannot hear again", async () => {
  // The shell announces at most once per boot. Emptying the verdict slot at
  // mount made a reload look like a fresh machine: it animated at full rate and
  // the banner never came back, because there was no second announcement left
  // to restore either. The session store is the only copy, so the page keeps it.
  const verdict = JSON.stringify({ at: 1, measuredMs: 90 });
  // No boot flag: the verdict alone is proof this boot was measured starving,
  // and the stylesheet must hear it even before the flag's listener exists.
  await withApp(
    { sessionStorage: { [MOTION_EVENT]: verdict } },
    async ({ dom, settle }) => {
      await settle();
      assert.match(dom.text(), BANNER, "a reload lost the verdict it can no longer be told");
      assert.match(dom.text(), /~90%/, "the banner forgot the measurement");
      assert.equal(readMotion().shellStarved?.measuredMs, 90);
      assert.equal(
        motionAllowed(readMotion(), false),
        false,
        "a reload turned the backdrop back on at full rate",
      );
      assert.equal(
        dom.window.document.documentElement.dataset.starved,
        "true",
        "the verdict did not reach the stylesheet that stops the wordmark",
      );
    },
  );
});

test("the shell emits the event this store names", () => {
  // Nothing spans the Rust/TypeScript type systems, so this reads the Rust
  // source: the constant there and the one imported here must agree.
  const rust = readFileSync(new URL("../../src-tauri/src/lib.rs", import.meta.url), "utf8");
  const match = rust.match(/RENDER_STARVED_EVENT: &str = "([^"]+)"/);
  assert.ok(match, "the Rust event constant moved or was renamed");
  assert.equal(match[1], MOTION_EVENT, "the two ends of the starvation contract disagree");
});

test("the shell can be asked directly when the page-load injection loses the race", async () => {
  // The global and the event both come from `on_page_load`, which can land
  // after App's first effect. When they miss, the mount must still learn the
  // shell is on the software rasteriser — otherwise the backdrop animates and
  // the watchdog's ten seconds are the first thing that says no.
  await withApp(
    { shellAnswers: { [SOFTWARE_RENDERING_COMMAND]: true } },
    async ({ dom, settle }) => {
      await settle();
      assert.match(dom.text(), BANNER, "the shell's direct answer did not reach the page");
      assert.match(
        dom.text(),
        /rendered in software/,
        "the banner did not name the software-rendering verdict",
      );
      assert.equal(
        motionAllowed(readMotion(), false),
        false,
        "the direct answer did not stop the animation",
      );
      assert.notEqual(sessionStorage.getItem(MOTION_EVENT), null, "the verdict was not recorded");
      assert.equal(
        dom.window.document.documentElement.dataset.starved,
        "true",
        "the software verdict never reached the stylesheet that stops the wordmark",
      );
    },
  );
});

test("the software-rendering and boot-id commands are the ones Rust defines", () => {
  const rust = readFileSync(new URL("../../src-tauri/src/lib.rs", import.meta.url), "utf8");
  assert.equal(SOFTWARE_RENDERING_COMMAND, "codify_software_rendering");
  assert.ok(
    rust.includes("fn codify_software_rendering"),
    "the UI asks a command the shell no longer defines",
  );
  assert.equal(BOOT_ID_COMMAND, "codify_boot_id");
  assert.ok(
    rust.includes("fn codify_boot_id"),
    "the session facts are scoped to an id the shell no longer defines",
  );
});

test("a boot-id call that never answers cannot hold the first paint", async () => {
  // `main.tsx` applies the palette first, but it still waits for the session id
  // before mounting. The deadline is what turns a hung local IPC into the
  // per-page id rather than a window that never appears.
  assert.ok(
    BOOT_ID_TIMEOUT_MS > 0 && BOOT_ID_TIMEOUT_MS <= 3_000,
    "the boot-id wait needs a short deadline",
  );
  const never = new Promise<number>(() => {});
  const started = Date.now();
  assert.equal(await within(never, 20, 7), 7);
  assert.ok(Date.now() - started < 1_000, "the fallback did not arrive");
});
