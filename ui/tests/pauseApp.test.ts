/**
 * A paused goal says why, through the whole App.
 *
 * `pauseReason.test.ts` holds the rules. What it cannot show is that the card draws them above the button that
 * resumes the goal, that the same event becomes one notification, and that the cases which must stay quiet do:
 * the person's own Pause, a Start that clears it, and a pause the stream replayed after the goal had moved on.
 * The engine is on the other end of recorded sockets: a thread opens with a goal in flight, the goal is paused
 * behind the app's back, and the frames the engine would push are delivered.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext } from "./appHarness.ts";

const { withApp, conversation } = await import("./appHarness.ts");

const STORAGE_KEY = "CODIFY_NOTIFICATIONS";
const THREAD = conversation({ id: "c1", title: "Thread" });

const goalRow = (over: Record<string, unknown> = {}) => ({
  id: "g1",
  conversation_id: "c1",
  title: "Cache the build",
  status: "RUNNING",
  mode: "normal",
  version: 6,
  steps: [] as unknown[],
  ...over,
});

const frame = (sequence: number, payload: Record<string, unknown>, type = "goal_status", stepId: string | null = null) => ({
  id: `ev-g1-${sequence}`,
  goal_id: "g1",
  step_id: stepId,
  type,
  payload,
  timestamp: sequence,
  sequence,
});

const BUDGET = {
  status: "PAUSED",
  version: 7,
  reason_code: "conductor_budget",
  reason: "The conductor used all the calls it was given on this step before finishing it. Press Start to let it carry on from where it stopped.",
};

async function withThread(goals: Array<Record<string, unknown>>, body: (ctx: AppContext) => Promise<void>): Promise<void> {
  await withApp({ conversations: [THREAD], goals: goals as never }, async (ctx) => {
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

/** The engine pauses (or resumes) the goal: the record changes first, then the status event arrives. */
async function moveGoal(
  ctx: AppContext,
  status: string,
  version: number,
  payload: Record<string, unknown>,
  sequence: number,
  stepId: string | null = null,
): Promise<void> {
  Object.assign(ctx.engineGoal("g1")!, { status, version, updated_at: 100 + version });
  await socket(ctx).deliver(frame(sequence, payload, "goal_status", stepId));
  await ctx.settle();
}

const banner = (ctx: AppContext): HTMLElement | null => ctx.dom.container.querySelector("[data-pause-code]");
const startButton = (ctx: AppContext): HTMLElement | undefined =>
  [...ctx.dom.container.querySelectorAll("button")].find((b) => /^\s*Start\s*$/.test(b.textContent ?? "")) as HTMLElement | undefined;
const stored = (ctx: AppContext): Array<Record<string, unknown>> => JSON.parse(ctx.dom.window.localStorage.getItem(STORAGE_KEY) ?? "[]");
const flat = (el: Element | null | undefined): string => (el?.textContent ?? "").replace(/\s+/g, " ").trim();

test("an engine pause draws its heading and the engine's sentence above Start, and the Start button is there", async () => {
  await withThread([goalRow()], async (ctx) => {
    assert.equal(banner(ctx) === null, true, "a running goal shows no pause");

    await moveGoal(ctx, "PAUSED", 7, BUDGET, 5);

    const shown = banner(ctx);
    assert.ok(shown, "the paused goal drew no reason");
    assert.equal(shown.getAttribute("data-pause-code"), "conductor_budget");
    assert.equal(shown.getAttribute("role"), "status");
    assert.match(flat(shown), /The conductor ran out of calls/);
    assert.match(flat(shown), /Press Start to let it carry on from where it stopped\./, "the sentence ends in what to do");
    const start = startButton(ctx);
    assert.ok(start, "the goal can still be started");
    // Above: the banner comes before the buttons in the document.
    assert.ok(shown.compareDocumentPosition(start) & 4 /* DOCUMENT_POSITION_FOLLOWING */, "the reason is not above Start");
  });
});

test("the pause is one notification that opens the goal, and a second delivery of it is the same one", async () => {
  await withThread([goalRow()], async (ctx) => {
    await moveGoal(ctx, "PAUSED", 7, BUDGET, 5);
    await socket(ctx).deliver(frame(5, BUDGET));
    await ctx.settle();

    const list = stored(ctx);
    assert.equal(list.length, 1, JSON.stringify(list));
    assert.equal(list[0].kind, "goal");
    assert.equal(list[0].tone, "warning");
    assert.equal(list[0].title, "The conductor ran out of calls");
    assert.match(String(list[0].detail), /Cache the build/);
    assert.deepEqual(list[0].target, { kind: "goal", goalId: "g1" });
  });
});

test("Start clears it: the next status event has no reason, so the banner goes", async () => {
  await withThread([goalRow()], async (ctx) => {
    await moveGoal(ctx, "PAUSED", 7, BUDGET, 5);
    assert.ok(banner(ctx));

    await moveGoal(ctx, "RUNNING", 8, { status: "RUNNING", version: 8 }, 6);

    assert.equal(banner(ctx) === null, true, "a resumed goal still shows why it was paused");
  });
});

test("the person's own Pause shows no reason and raises no notification", async () => {
  await withThread([goalRow()], async (ctx) => {
    await moveGoal(ctx, "PAUSED", 7, { status: "PAUSED", version: 7 }, 5);

    assert.equal(banner(ctx) === null, true, "the person pressed Pause; they know why");
    assert.ok(startButton(ctx), "a paused goal can still be started");
    assert.equal(stored(ctx).length, 0);
  });
});

test("a pause the stream replayed after the goal moved on is not announced", async () => {
  await withThread([goalRow()], async (ctx) => {
    // The goal is at version 9 now; the frame is the engine replaying the pause it made at version 7.
    await moveGoal(ctx, "PAUSED", 9, BUDGET, 5);

    assert.equal(stored(ctx).length, 0, JSON.stringify(stored(ctx)));
  });
});

test("a critic's pause quotes the critic's reasons from the step's notes", async () => {
  await withThread([goalRow()], async (ctx) => {
    await socket(ctx).deliver(frame(4, { status: "IN_PROGRESS", review_notes: "a.py: no docstring\nb.py: bad name" }, "step_status", "s1"));
    await moveGoal(
      ctx,
      "PAUSED",
      7,
      {
        status: "PAUSED",
        version: 7,
        reason_code: "critic_rejected",
        reason: "The critic asked for changes and stopped the run. Its reasons are on the step. Press Start to carry on, or edit the plan first.",
      },
      5,
      // The engine's critic pause names the step it is about (`_pause(goal, step.id, ...)`).
      "s1",
    );

    const shown = banner(ctx);
    assert.ok(shown);
    assert.match(flat(shown), /The critic asked for changes/);
    assert.match(flat(shown), /The critic's reasons/);
    assert.match(flat(shown), /a\.py: no docstring/);
    assert.match(flat(shown), /b\.py: bad name/);
  });
});

test("a code this build does not know leaves the card as it was and says nothing", async () => {
  await withThread([goalRow()], async (ctx) => {
    await moveGoal(ctx, "PAUSED", 7, { ...BUDGET, reason_code: "something_new" }, 5);

    assert.equal(banner(ctx) === null, true);
    assert.equal(stored(ctx).length, 0);
    assert.ok(startButton(ctx));
  });
});
