/**
 * The drawers' layout rule, pure: when the left panel gives way to a drawer.
 *
 * The rule is in rem because everything it compares is, and the UI scale moves the root font size: a
 * threshold in px would be right at one scale and wrong at the next. `drawerLayout.test.ts` mounts the
 * App to prove the rule is what the layout actually does.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { sidebarYields, nextDrawer, closeDrawer, SIDEBAR_REM, DRAWER_REM, MIN_CENTRE_REM } = await import("../src/drawers.ts");

test("with no drawer open the panel never yields, however narrow the window", () => {
  assert.equal(sidebarYields(300, 16, null), false);
  assert.equal(sidebarYields(100000, 16, null), false);
});

test("what cannot be measured never hides anything", () => {
  for (const [main, root] of [[0, 20], [-5, 20], [Number.NaN, 20], [1200, 0], [1200, Number.NaN], [1200, -1]] as const) {
    assert.equal(sidebarYields(main, root, "stats"), false, `${main}/${root} hid the panel`);
  }
});

test("the threshold is the panel, the drawer and the centre's floor, in rem", () => {
  assert.equal(SIDEBAR_REM, 15);
  assert.deepEqual({ ...DRAWER_REM }, { stats: 28, notifications: 20, history: 20 });
  assert.equal(MIN_CENTRE_REM, 30);
  for (const drawer of ["stats", "history"] as const) {
    const need = SIDEBAR_REM + DRAWER_REM[drawer] + MIN_CENTRE_REM;
    for (const root of [16, 18, 20, 24, 28]) {
      assert.equal(sidebarYields(need * root - 1, root, drawer), true, `${drawer} @${root}px: just under the need still fits?`);
      assert.equal(sidebarYields(need * root, root, drawer), false, `${drawer} @${root}px: exactly the need yields`);
      assert.equal(sidebarYields(need * root + 1, root, drawer), false);
    }
  }
});

test("the same window yields at a bigger UI scale and not at a smaller one", () => {
  // 1280px wide with the Stats drawer: 80rem at 100%, 64rem at 125%, 45rem at 175%.
  assert.equal(sidebarYields(1280, 16, "stats"), false, "100%");
  assert.equal(sidebarYields(1280, 20, "stats"), true, "125%");
  assert.equal(sidebarYields(1280, 28, "stats"), true, "175%");
});

test("each drawer asks for its own width", () => {
  // Between the two thresholds the narrower History drawer fits beside the panel and Stats does not.
  const root = 20;
  const width = (SIDEBAR_REM + DRAWER_REM.history + MIN_CENTRE_REM + 2) * root;
  assert.equal(sidebarYields(width, root, "history"), false);
  assert.equal(sidebarYields(width, root, "stats"), true);
});

test("pressing a drawer's button opens it, switches to it, or closes it", () => {
  assert.equal(nextDrawer(null, "stats"), "stats");
  assert.equal(nextDrawer("stats", "stats"), null);
  assert.equal(nextDrawer("stats", "history"), "history");
  assert.equal(nextDrawer("history", "stats"), "stats");
  // Three drawers, one at a time: each press lands on exactly the one pressed.
  for (const from of [null, "stats", "notifications", "history"] as const) {
    for (const to of ["stats", "notifications", "history"] as const) {
      assert.equal(nextDrawer(from, to), from === to ? null : to, `${from} -> ${to}`);
    }
  }
});

test("a drawer's close button closes that drawer and nothing else", () => {
  assert.equal(closeDrawer("stats", "stats"), null);
  assert.equal(closeDrawer("history", "stats"), "history", "closing Stats closed History");
  assert.equal(closeDrawer(null, "stats"), null);
});
