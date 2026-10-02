/**
 * The conductor's notes, through the whole App.
 *
 * `todoList.test.ts` holds the rules. What it cannot show is that the card is drawn in the run card, that it
 * follows the stream (the engine republishes the whole list on every change), that it is plain text whatever
 * the model wrote, and that a goal with no notes draws nothing at all.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext } from "./appHarness.ts";

const { withApp, conversation } = await import("./appHarness.ts");

const THREAD = conversation({ id: "c1", title: "Thread" });

const goalRow = () => ({
  id: "g1",
  conversation_id: "c1",
  title: "Cache the build",
  status: "RUNNING",
  mode: "normal",
  version: 6,
  steps: [] as unknown[],
});

const frame = (sequence: number, items: unknown[]) => ({
  id: `ev-g1-${sequence}`,
  goal_id: "g1",
  step_id: null,
  type: "todo_updated",
  payload: { items },
  timestamp: sequence,
  sequence,
});

async function withThread(body: (ctx: AppContext) => Promise<void>): Promise<void> {
  await withApp({ conversations: [THREAD], goals: [goalRow()] as never }, async (ctx) => {
    await ctx.dom.click(ctx.dom.container.querySelector('[data-thread-row="true"]') as Element);
    await ctx.settle();
    await body(ctx);
  });
}

const socket = (ctx: AppContext) => {
  const s = ctx.sockets.find((x) => x.url.endsWith("/ws/goals/g1"));
  if (!s) throw new Error(`the app opened no stream for g1: ${ctx.sockets.map((x) => x.url).join(", ")}`);
  return s;
};

const card = (ctx: AppContext): HTMLElement | null => ctx.dom.container.querySelector("[data-todo-card]");
const rows = (ctx: AppContext): HTMLElement[] => [...ctx.dom.container.querySelectorAll<HTMLElement>("[data-todo-status]")];
const flat = (el: Element | null | undefined): string => (el?.textContent ?? "").replace(/\s+/g, " ").trim();
/** What the row says about the item, apart from the status a screen reader is told. */
const itemText = (row: Element): string => flat(row.lastElementChild);

test("a goal whose conductor kept no notes draws no card", async () => {
  await withThread(async (ctx) => {
    assert.equal(card(ctx) === null, true);
  });
});

test("the notes appear when the engine publishes them, one row each, with their status", async () => {
  await withThread(async (ctx) => {
    await socket(ctx).deliver(
      frame(3, [
        { id: "t1", text: "run the linter", status: "pending" },
        { id: "t2", text: "rename in b.py", status: "doing" },
        { id: "t3", text: "read the parser", status: "done" },
      ]),
    );
    await ctx.settle();

    const shown = card(ctx);
    assert.ok(shown, "the notes were published and not drawn");
    assert.match(flat(shown), /The conductor's notes/);
    assert.deepEqual(
      rows(ctx).map((r) => [r.getAttribute("data-todo-status"), itemText(r)]),
      [
        ["pending", "run the linter"],
        ["doing", "rename in b.py"],
        ["done", "read the parser"],
      ],
    );
    assert.match(flat(rows(ctx)[1]), /^In progress: /, "a screen reader is told the status, not just shown a colour");
  });
});

test("the next snapshot replaces the list rather than adding to it", async () => {
  await withThread(async (ctx) => {
    await socket(ctx).deliver(frame(3, [{ id: "t1", text: "first note", status: "pending" }]));
    await socket(ctx).deliver(
      frame(4, [
        { id: "t1", text: "first note", status: "done" },
        { id: "t2", text: "second note", status: "pending" },
      ]),
    );
    await ctx.settle();

    assert.deepEqual(
      rows(ctx).map((r) => r.getAttribute("data-todo-status")),
      ["done", "pending"],
    );
    assert.equal(rows(ctx).length, 2);
  });
});

test("an item the model filled with markup is text, never an element", async () => {
  await withThread(async (ctx) => {
    const hostile = '<img src=x onerror="window.pwned=1"> <b>bold</b>';
    await socket(ctx).deliver(frame(3, [{ id: "t1", text: hostile, status: "pending" }]));
    await ctx.settle();

    const shown = card(ctx);
    assert.ok(shown);
    assert.equal(shown.querySelector("img") === null, true, "the model's markup became an element");
    assert.equal(shown.querySelector("b") === null, true);
    assert.match(flat(shown), /<img src=x onerror="window.pwned=1"> <b>bold<\/b>/);
  });
});

test("a list of only dropped items draws nothing", async () => {
  await withThread(async (ctx) => {
    await socket(ctx).deliver(frame(3, [{ id: "t1", text: "abandoned", status: "dropped" }]));
    await ctx.settle();

    assert.equal(card(ctx) === null, true);
  });
});
