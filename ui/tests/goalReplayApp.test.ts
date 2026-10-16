/**
 * A goal's stream replays its whole log from 0 on every connect. What the window does about an event it has
 * already acted on: nothing. Through the whole App, with the engine on the other end of recorded sockets.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext } from "./appHarness.ts";

const { withApp, conversation } = await import("./appHarness.ts");

const THREAD = conversation({ id: "c1", title: "Thread" });
const goalRow = (over: Record<string, unknown> = {}) => ({
  id: "g1", conversation_id: "c1", title: "Cache the build", status: "PLANNING", plan_only: true, mode: "normal", steps: [] as unknown[], version: 3, ...over,
});
const ev = (type: string, payload: Record<string, unknown>, sequence: number) => ({
  id: `ev-g1-${sequence}`, goal_id: "g1", step_id: null, type, payload, timestamp: sequence, sequence,
});
const LOG = [
  ev("goal_status", { status: "PLANNING" }, 1),
  ev("plan_updated", {}, 2),
  ev("step_status", { status: "PENDING" }, 3),
  ev("step_status", { status: "PENDING" }, 4),
  ev("goal_status", { status: "PENDING" }, 5),
];
const goalSockets = (ctx: AppContext) => ctx.sockets.filter((s) => s.url.endsWith("/ws/goals/g1"));
const goalGets = (ctx: AppContext): number => ctx.engine.filter((c) => c.method === "GET" && c.path === "/goals/g1").length;
const wait = (ctx: AppContext, ms: number): Promise<void> => ctx.act(() => new Promise<void>((r) => setTimeout(r, ms)));
const bell = (ctx: AppContext): HTMLElement =>
  [...(ctx.dom.container.querySelector("header") as HTMLElement).querySelectorAll("button")].find((b) => /^Notifications/.test(b.getAttribute("aria-label") ?? "")) as HTMLElement;
const badge = (ctx: AppContext): string => (bell(ctx).textContent ?? "").replace(/Notifications/, "").trim();

async function withWatchedGoal(body: (ctx: AppContext) => Promise<void>): Promise<void> {
  await withApp({ conversations: [THREAD], goals: [goalRow()] as never }, async (ctx) => {
    await ctx.dom.click(ctx.dom.container.querySelector('[data-thread-row="true"]') as Element);
    await ctx.settle();
    Object.assign(ctx.engineGoal("g1")!, { status: "PENDING", version: 4 });
    await body(ctx);
  });
}

/** Drop the goal's socket and wait out the real 1 s backoff for the one that replaces it. */
async function reconnect(ctx: AppContext, from: number): Promise<ReturnType<typeof goalSockets>[number]> {
  await goalSockets(ctx)[from - 1]!.drop(1006);
  const deadline = Date.now() + 6000;
  while (goalSockets(ctx).length < from + 1 && Date.now() < deadline) await wait(ctx, 50);
  const next = goalSockets(ctx)[from]!;
  assert.ok(next, "the stream did not reconnect");
  await next.open();
  return next;
}

test("the first connect refreshes the goal once per state event, as it always did", async () => {
  await withWatchedGoal(async (ctx) => {
    const base = goalGets(ctx);
    const s1 = goalSockets(ctx)[0]!;
    await s1.open();
    for (const e of LOG) await s1.deliver(e);
    await ctx.settle();
    assert.equal(goalGets(ctx) - base, LOG.length);
  });
});

test("a reconnect that replays events already seen asks the engine for nothing", async () => {
  await withWatchedGoal(async (ctx) => {
    const s1 = goalSockets(ctx)[0]!;
    await s1.open();
    for (const e of LOG) await s1.deliver(e);
    await ctx.settle();
    const seen = goalGets(ctx);
    const s2 = await reconnect(ctx, 1);
    for (const e of LOG) await s2.deliver(e);
    await ctx.settle();
    assert.equal(goalGets(ctx), seen, "replayed events fetched the goal again");
  });
});

test("an event that arrives after a replay is new, and refreshes the goal once", async () => {
  await withWatchedGoal(async (ctx) => {
    const s1 = goalSockets(ctx)[0]!;
    await s1.open();
    for (const e of LOG.slice(0, 3)) await s1.deliver(e);
    await ctx.settle();
    const seen = goalGets(ctx);
    const s2 = await reconnect(ctx, 1);
    // The gap: events 4 and 5 happened while the window had no connection, and the replay brings them with the rest.
    for (const e of LOG) await s2.deliver(e);
    await s2.deliver(ev("goal_status", { status: "RUNNING" }, 6));
    await ctx.settle();
    assert.equal(goalGets(ctx) - seen, 3, "exactly the events past the last one seen (4, 5, 6) should refresh");
  });
});

test("a replayed plan does not raise a notification the person already cleared", async () => {
  await withWatchedGoal(async (ctx) => {
    const s1 = goalSockets(ctx)[0]!;
    await s1.open();
    await s1.deliver(ev("goal_status", { status: "PENDING" }, 5));
    await ctx.settle();
    assert.equal(badge(ctx), "1");
    await ctx.dom.click(bell(ctx));
    await ctx.dom.click(ctx.dom.byLabel("Clear notifications"));
    await ctx.dom.click(bell(ctx));
    assert.equal(badge(ctx), "");
    const s2 = await reconnect(ctx, 1);
    await s2.deliver(ev("goal_status", { status: "PENDING" }, 5));
    await ctx.settle();
    assert.equal(badge(ctx), "", "a replayed event re-announced a plan that was already seen and cleared");
  });
});

test("a refresh that failed is tried again by the replay", async () => {
  await withWatchedGoal(async (ctx) => {
    const realFetch = globalThis.fetch;
    let failing = true;
    globalThis.fetch = (async (input: unknown, init?: unknown) => {
      const url = String(input instanceof Request ? input.url : input);
      if (failing && /\/goals\/g1$/.test(new URL(url, "http://x").pathname)) throw new TypeError("network down");
      return (realFetch as (a: unknown, b?: unknown) => Promise<Response>)(input, init);
    }) as typeof fetch;
    try {
      const s1 = goalSockets(ctx)[0]!;
      await s1.open();
      await s1.deliver(ev("goal_status", { status: "PENDING" }, 5));
      await ctx.settle();
      assert.equal(badge(ctx), "", "the refresh failed, so there is no plan to announce yet");
      failing = false;
      const s2 = await reconnect(ctx, 1);
      await s2.deliver(ev("goal_status", { status: "PENDING" }, 5));
      await ctx.settle();
      assert.equal(badge(ctx), "1", "the replay did not retry the refresh that failed");
    } finally {
      globalThis.fetch = realFetch;
    }
  });
});

/**
 * Stand between the window and the engine for `GET /goals/g1`. `before` runs with the number of this attempt
 * (1 for the first) and may hold the answer or throw to fail it; every attempt is counted, failed or not.
 */
function interceptGoalReads(before: (attempt: number) => Promise<void> | void): { attempts: () => number; restore: () => void } {
  const realFetch = globalThis.fetch;
  let attempts = 0;
  globalThis.fetch = (async (input: unknown, init?: unknown) => {
    const url = new URL(String(input instanceof Request ? input.url : input), "http://x");
    const method = (input instanceof Request ? input.method : ((init as RequestInit | undefined)?.method ?? "GET")).toUpperCase();
    if (method === "GET" && url.pathname === "/goals/g1") {
      attempts += 1;
      await before(attempts);
    }
    return (realFetch as (a: unknown, b?: unknown) => Promise<Response>)(input, init);
  }) as typeof fetch;
  return { attempts: () => attempts, restore: () => void (globalThis.fetch = realFetch) };
}

for (const timing of ["before", "after"] as const) {
  test(`a refresh that failed ${timing} a later event was applied is still tried again by the replay, and only that one`, async () => {
    await withWatchedGoal(async (ctx) => {
      let fail!: () => void;
      const down = new Promise<void>((_, reject) => {
        fail = () => reject(new TypeError("network down"));
      });
      // The first read (event 5's) fails: at once, or only after event 6 has been applied and answered.
      const spy = interceptGoalReads((n) => {
        if (n !== 1) return;
        if (timing === "before") throw new TypeError("network down");
        return down;
      });
      try {
        const s1 = goalSockets(ctx)[0]!;
        await s1.open();
        await s1.deliver(ev("goal_status", { status: "PENDING" }, 5));
        await s1.deliver(ev("step_status", { status: "PENDING" }, 6));
        await ctx.settle();
        if (timing === "after") {
          fail();
          await ctx.settle();
        }
        assert.equal(spy.attempts(), 2);
        assert.equal(badge(ctx), "", "the refresh for 5 failed, so there is no plan to announce yet");

        const s2 = await reconnect(ctx, 1);
        await s2.deliver(ev("goal_status", { status: "PENDING" }, 5));
        await s2.deliver(ev("step_status", { status: "PENDING" }, 6));
        await ctx.settle();
        assert.equal(spy.attempts(), 3, "the replay should ask once more, for 5, and not for 6 which went through");
        assert.equal(badge(ctx), "1", "the replay did not retry the event whose refresh failed");
      } finally {
        spy.restore();
      }
    });
  });
}

test("a refresh that failed is tried again once: a later replay finds it done", async () => {
  await withWatchedGoal(async (ctx) => {
    const spy = interceptGoalReads((n) => {
      if (n === 1) throw new TypeError("network down");
    });
    try {
      const s1 = goalSockets(ctx)[0]!;
      await s1.open();
      await s1.deliver(ev("goal_status", { status: "PENDING" }, 5));
      await ctx.settle();
      const s2 = await reconnect(ctx, 1);
      await s2.deliver(ev("goal_status", { status: "PENDING" }, 5));
      await ctx.settle();
      assert.equal(badge(ctx), "1");
      await ctx.dom.click(bell(ctx));
      await ctx.dom.click(ctx.dom.byLabel("Clear notifications"));
      await ctx.dom.click(bell(ctx));
      const s3 = await reconnect(ctx, 2);
      await s3.deliver(ev("goal_status", { status: "PENDING" }, 5));
      await ctx.settle();
      // Counting reads would race the 1.5 s poll of a goal still shown as PLANNING; what the person sees does not.
      assert.equal(badge(ctx), "", "an event whose retry succeeded was announced again");
    } finally {
      spy.restore();
    }
  });
});

test("a floor given to a new stream is history: Apply replays the log without refreshing or announcing it", async () => {
  const log = [...LOG.slice(0, 4), ev("goal_status", { status: "COMPLETED" }, 5)];
  const dryRun = goalRow({ status: "COMPLETED", dry_run: true, plan_only: false, workspace_id: "ws-a" });
  await withApp({ conversations: [THREAD], goals: [dryRun] as never, goalEvents: { g1: log } as never }, async (ctx) => {
    await ctx.dom.click(ctx.dom.container.querySelector('[data-thread-row="true"]') as Element);
    await ctx.settle();
    const apply = [...ctx.dom.container.querySelectorAll("button")].find((b) => /Apply these\s+changes/.test(b.textContent ?? ""));
    assert.ok(apply, "a finished dry run offers no Apply");
    await ctx.dom.click(apply);
    await ctx.settle();
    const sockets = goalSockets(ctx);
    assert.equal(sockets.length, 1, "Apply did not open a stream for the run");
    const s = sockets[0]!;
    await s.open();
    const base = goalGets(ctx);
    // The engine replays the whole log, and the part of it the card already shows is not news.
    for (const e of log) await s.deliver(e);
    await ctx.settle();
    assert.equal(goalGets(ctx), base, "the replayed history refreshed the goal");
    assert.equal(badge(ctx), "", "the replayed history raised a notification");
    await s.deliver(ev("goal_status", { status: "RUNNING" }, 6));
    await ctx.settle();
    assert.equal(goalGets(ctx) - base, 1, "the first event past the floor should refresh exactly once");
  });
});

test("an old stream's late failure does not make the next stream treat history as new", async () => {
  const dryRun = goalRow({ status: "RUNNING", dry_run: true, plan_only: false, workspace_id: "ws-a" });
  await withApp({ conversations: [THREAD], goals: [dryRun] as never }, async (ctx) => {
    await ctx.dom.click(ctx.dom.container.querySelector('[data-thread-row="true"]') as Element);
    await ctx.settle();
    let fail!: () => void;
    const down = new Promise<void>((_, reject) => {
      fail = () => reject(new TypeError("network down"));
    });
    // The read the terminal event's own handler makes is held; the one the stream makes on closing is not.
    const spy = interceptGoalReads((n) => (n === 1 ? down : undefined));
    try {
      Object.assign(ctx.engineGoal("g1")!, { status: "COMPLETED", dry_run: true, version: 4 });
      const s1 = goalSockets(ctx)[0]!;
      await s1.open();
      await s1.deliver(ev("goal_status", { status: "COMPLETED" }, 5));
      await ctx.settle();
      const apply = [...ctx.dom.container.querySelectorAll("button")].find((b) => /Apply these\s+changes/.test(b.textContent ?? ""));
      assert.ok(apply, "the finished dry run offers no Apply");
      await ctx.dom.click(apply);
      await ctx.settle();
      const s2 = goalSockets(ctx)[1]!;
      assert.ok(s2, "Apply did not open a second stream");
      await s2.open();
      fail();
      await ctx.settle();
      const base = spy.attempts();
      await s2.deliver(ev("goal_status", { status: "COMPLETED" }, 5));
      await ctx.settle();
      assert.equal(spy.attempts(), base, "the old stream's failure made the new stream's replay count as news");
    } finally {
      spy.restore();
    }
  });
});
