/**
 * The thread menu's three rules: what it offers, which thread a new thread hangs
 * off, and where it is allowed to sit.
 *
 * All three are invisible to the render harness. A menu opens on an event the
 * static renderer never fires, its position is a number produced from the
 * pointer — so a menu that runs off the bottom of the window renders perfectly in
 * every test and is unusable on the row nearest the bottom of the list — and the
 * parent is decided from which element was under the pointer, which no snapshot
 * of markup records. That is the same reason `tabs.ts` is a pure module: the
 * decisions have to be somewhere `node --test` can reach them without a DOM.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const {
  THREAD_MENU_ITEMS,
  THREAD_MENU_LABEL,
  THREAD_MENU_FALLBACK_SIZE,
  THREAD_MENU_ROW_HEIGHT,
  threadMenuItems,
  newThreadParentId,
  clampMenuPosition,
} = await import("../src/threadMenu.ts");

/** A window with room in it, and a menu with a known size. */
const WINDOW = { x: 0, y: 0, width: 1000, height: 800 };
const MENU = { width: 176, height: 92 };

// ── what it offers ───────────────────────────────────────────────────────

test("New thread is the first item, and it is the only one needing no thread", () => {
  // The gesture is "I want to work on something new", and the item that does
  // that must not be one the pointer travels to. It is also the only item that
  // works on empty panel, which is what lets one menu serve both cases.
  assert.equal(THREAD_MENU_ITEMS[0].id, "new");
  assert.equal(THREAD_MENU_ITEMS[0].label, "New thread");
  assert.equal(THREAD_MENU_ITEMS[0].needsThread, undefined);
  for (const item of THREAD_MENU_ITEMS.slice(1)) {
    assert.equal(item.needsThread, true, `${item.id} does not say it needs a thread`);
  }
});

test("on a thread the menu offers New thread, Rename and Archive, in order", () => {
  assert.deepEqual(
    threadMenuItems(true).map((i) => i.id),
    ["new", "rename", "archive"]
  );
  assert.deepEqual(
    threadMenuItems(true).map((i) => i.label),
    ["New thread", "Rename", "Archive"]
  );
});

test("there is no Open item, because opening is what clicking the row does", () => {
  // The item that was here was `onSelect` — the row's own click handler. On a
  // thread that was already open, `openConversation` only focuses its tab, so
  // the item did nothing at all: a control that looked alive and was not, in
  // the one place the reader was told the menu could act on this thread.
  //
  // The `as string` is the assertion, not a way around it: `"open"` is outside
  // `ThreadMenuItemId` on purpose, and a test that only ever compares members
  // of the union cannot fail. Widening here is what lets a re-added `open` be
  // caught by the checker as well as by this test.
  assert.equal(
    THREAD_MENU_ITEMS.find((i) => (i.id as string) === "open"),
    undefined,
    "Open is back in the menu",
  );
  assert.equal(
    THREAD_MENU_ITEMS.filter((i) => i.label === "Open").length,
    0,
    "a menu item still claims to open the thread",
  );
});

test("on empty panel the menu is New thread and nothing else", () => {
  // Rename and Archive act on a thread that is not there. Offering them anyway
  // is two controls that silently do nothing, which is the one thing this app
  // does not ship.
  assert.deepEqual(
    threadMenuItems(false).map((i) => i.id),
    ["new"]
  );
});

// ── which thread a new thread hangs off ──────────────────────────────────

test("a new thread from a row branches off that row", () => {
  // You pointed at it. The tab that happened to be showing is a coincidence of
  // navigation, and ignoring the row under the pointer would surprise every time
  // the two disagreed.
  assert.equal(
    newThreadParentId("row-1", "showing-1", ["row-1", "showing-1"]),
    "row-1",
  );
});

test("a new thread from empty panel branches off the thread on screen", () => {
  // Empty panel is not the same as nothing: the gesture was made in a chat, and
  // the chat it was made in is the one the new thread belongs to.
  assert.equal(
    newThreadParentId(undefined, "showing-1", ["row-1", "showing-1"]),
    "showing-1",
  );
});

test("with nothing open, a new thread is top-level", () => {
  // There is no chat to branch off, so `undefined` is the honest answer rather
  // than a fallback to some arbitrary thread. This is the empty-panel case with
  // no tab open, and it is an ordinary outcome.
  assert.equal(
    newThreadParentId(undefined, undefined, ["row-1"]),
    undefined,
  );
});

test("a thread from another workspace is never offered as a parent", () => {
  // The defect, exactly as it reached the user. A tab from workspace A stays
  // open when the selector moves to workspace B — two projects at once is
  // legitimate and closing the strip would destroy transcripts — so the active
  // tab names a thread this panel is not showing. Offering it asks the engine
  // for a child in B whose parent is in A, which it refuses with a 422, so a
  // right-click that should have created a thread produced an error banner
  // instead. A top-level thread is the true answer and it creates fine.
  assert.equal(
    newThreadParentId(undefined, "workspace-a-thread", ["b1", "b2"]),
    undefined,
  );
  // And the same holds for a row, even though a row is in the panel by
  // construction — one boundary, not two.
  assert.equal(
    newThreadParentId("workspace-a-thread", undefined, ["b1", "b2"]),
    undefined,
  );
  // With the foreign tab gone from the equation, the panel's own thread is
  // used again, so the guard does not quietly disable the feature.
  assert.equal(
    newThreadParentId(undefined, "b1", ["b1", "b2"]),
    "b1",
  );
});

test("a row wins over the active thread even when the row is the empty string", () => {
  // `||` not `??`: an id is never an empty string, so the two agree on every
  // real value — but the test pins that a falsy row id falls through to the
  // thread on screen rather than producing a top-level thread by accident.
  assert.equal(newThreadParentId("", "showing-1", ["showing-1"]), "showing-1");
  assert.equal(newThreadParentId("", undefined, ["showing-1"]), undefined);
});

test("only Archive is marked as the one that takes something away", () => {
  // Archive hides the thread from the panel. It is not a delete — the engine
  // keeps the runs — but it is the item that removes something from view, so it
  // is the one that may not look identical to the two beside it.
  assert.deepEqual(
    THREAD_MENU_ITEMS.filter((i) => i.caution).map((i) => i.id),
    ["archive"]
  );
});

test("the menu names itself for a screen reader", () => {
  assert.equal(typeof THREAD_MENU_LABEL, "string");
  assert.ok(THREAD_MENU_LABEL.trim().length > 0, "an empty label says nothing");
});

test("the fallback size is big enough to hold the items it sizes for", () => {
  // The fallback only decides the first frame, but a fallback smaller than the
  // real menu would put the real menu outside the clamp it was clamped to — the
  // arithmetic would have been done against a lie.
  const rows = THREAD_MENU_ITEMS.length;
  assert.ok(
    THREAD_MENU_FALLBACK_SIZE.height >= rows * THREAD_MENU_ROW_HEIGHT,
    `${THREAD_MENU_FALLBACK_SIZE.height}px cannot hold ${rows} rows of ${THREAD_MENU_ROW_HEIGHT}px`,
  );
  assert.ok(THREAD_MENU_FALLBACK_SIZE.width > 0);
});

// ── where it sits ────────────────────────────────────────────────────────

test("a right-click in open space puts the menu at the pointer", () => {
  // The common case, and the one that must be untouched: right-click in the
  // middle of the window and the menu is under the cursor.
  assert.deepEqual(clampMenuPosition({ x: 400, y: 300 }, MENU, WINDOW), {
    x: 400,
    y: 300,
  });
});

test("a right-click near the right edge slides the menu left to stay on screen", () => {
  // The failure this prevents: right-clicking the right-hand thread's row — the
  // whole panel is 240px wide, so every right-click is near an edge — put a
  // 176px menu at x=980 in a 1000px window and hung 156px of it outside.
  const at = clampMenuPosition({ x: 980, y: 300 }, MENU, WINDOW);
  assert.equal(at.x, WINDOW.width - MENU.width);
  assert.equal(at.y, 300, "and only the axis that was tight moves");
});

test("a right-click near the bottom slides the menu up", () => {
  // Same bug on the last thread in the list, which is the one nearest the foot
  // of the window. Without the y clamp the bottom third of the menu — Archive,
  // the item that removes the thread — is under the taskbar.
  const at = clampMenuPosition({ x: 400, y: 790 }, MENU, WINDOW);
  assert.equal(at.y, WINDOW.height - MENU.height);
  assert.equal(at.x, 400);
});

test("a corner right-click slides on both axes at once", () => {
  const at = clampMenuPosition({ x: 999, y: 799 }, MENU, WINDOW);
  assert.deepEqual(at, {
    x: WINDOW.width - MENU.width,
    y: WINDOW.height - MENU.height,
  });
});

test("a bounds narrower than the menu pins to the near edge", () => {
  // The case the arithmetic would get wrong by being clever: slack is negative,
  // so `bounds.x + slack` is negative and the menu is positioned off the left
  // of the screen to "fit". Nothing fits, so the near edge is the only answer
  // that is still on screen.
  const tight = { x: 0, y: 0, width: 100, height: 50 };
  assert.deepEqual(clampMenuPosition({ x: 40, y: 30 }, MENU, tight), {
    x: 0,
    y: 0,
  });
});

test("a negative pointer is clamped to zero, not trusted", () => {
  // Not reachable by a mouse, and that is the point: the function's contract is
  // "inside these bounds", and a coordinate outside them must not survive it.
  const at = clampMenuPosition({ x: -50, y: -50 }, MENU, WINDOW);
  assert.deepEqual(at, { x: 0, y: 0 });
});

test("bounds that do not start at the origin are honoured", () => {
  // The window is the usual bounds, but the function takes bounds and not
  // "the viewport", so it is tested against bounds that are not the viewport.
  const panel = { x: 200, y: 100, width: 400, height: 300 };
  assert.deepEqual(clampMenuPosition({ x: 590, y: 390 }, MENU, panel), {
    x: 200 + 400 - MENU.width,
    y: 100 + 300 - MENU.height,
  });
  assert.deepEqual(clampMenuPosition({ x: 150, y: 50 }, MENU, panel), {
    x: 200,
    y: 100,
  });
});

test("clamping is idempotent, because it is applied to a measured size too", () => {
  // `ThreadMenu` clamps once with the fallback size and again after it has
  // measured itself. If clamping a clamped point moved it, the menu would walk
  // across the screen on the second pass.
  const once = clampMenuPosition({ x: 980, y: 790 }, MENU, WINDOW);
  assert.deepEqual(clampMenuPosition(once, MENU, WINDOW), once);
});
