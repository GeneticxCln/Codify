/**
 * Arming a recording, used rather than read.
 *
 * `canArmTrace` was written, given a rule, given a test — and never called by
 * anything. `setGoalTrace` in `api.ts` was the same: a client for
 * `PUT /goals/{id}/trace`, which the engine implements and refuses past the start
 * of a run with `trace_locked`. Between them, a capability the engine fully
 * supports had no path to the user: the only way to record a run was the command
 * bar's toggle, applied when the goal was created.
 *
 * That matters because arming is a decision people make late. You write the
 * prompt, the plan comes back, and *then* you decide you want the receipt. The
 * window the engine leaves open is exactly that window — the goal is PLANNING —
 * and nothing in the UI offered it.
 *
 * The rule itself is tested in traceSummary.test.ts. What is held here is the
 * wiring, and it used to be held by matching `TracePanel.tsx`, `ChatTimeline.tsx`
 * and `App.tsx` as text — which passes with every control wired to nothing. This
 * mounts the app against a fake engine that answers like the real one (arming
 * only while the goal is PLANNING, `trace_locked` otherwise), opens a thread, and
 * presses the controls: the control exists, it is offered exactly when the engine
 * would accept it, it sends what the engine expects, and a refusal is shown
 * rather than swallowed.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { canArmTrace } from "../src/traceSummary.ts";
import type { AppContext, EngineCall } from "./appHarness.ts";

const { withApp, conversation } = await import("./appHarness.ts");

/** Mount the app with one thread holding one goal, the thread open. */
async function withGoal(
  goal: { status: string; trace?: boolean },
  body: (ctx: AppContext) => Promise<void>,
): Promise<void> {
  await withApp(
    {
      conversations: [conversation({ id: "c1", title: "Thread" })],
      goals: [{ id: "g1", conversation_id: "c1", title: "Add a limit", ...goal }],
    },
    async (ctx) => {
      const row = ctx.dom.container.querySelector('[data-thread-row="true"]');
      assert.ok(row, "the panel did not list the thread");
      await ctx.dom.click(row);
      await ctx.settle();
      await body(ctx);
    },
  );
}

const control = (ctx: AppContext, name: string): HTMLElement | null =>
  ctx.dom.container.querySelector(`button[aria-label="${name}"]`);

const toggles = (engine: EngineCall[]): EngineCall[] =>
  engine.filter((c) => c.method === "PUT" && c.path === "/goals/g1/trace");

test("the arming rule is still the one the engine enforces", () => {
  // Guard the wiring assertions below against a rule change that leaves them
  // testing nothing: a goal that has started must never be armable.
  assert.equal(canArmTrace("PLANNING"), true);
  for (const status of ["PENDING", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]) {
    assert.equal(
      canArmTrace(status),
      false,
      `${status} must not be armable — the engine refuses with trace_locked`,
    );
  }
});

test("a goal that has not started can be armed from its card, and disarmed again", async () => {
  await withGoal({ status: "PLANNING" }, async (ctx) => {
    const { dom, engine, settle } = ctx;
    // The button is what makes the panel openable, and the panel is the only place
    // the control lives — so a button gated on "already recording" hides the
    // feature from exactly the goals it is for.
    const open = control(ctx, "Show recording");
    assert.ok(open, "a goal that can still be armed offers no recording control");
    await dom.click(open);
    await settle();

    const start = control(ctx, "Start recording");
    assert.ok(start, "the recording panel offers no way to start recording");
    await dom.click(start);
    await settle();
    assert.equal(toggles(engine).length, 1, "starting a recording sent nothing");
    assert.deepEqual(toggles(engine)[0].body, { enabled: true });
    assert.ok(control(ctx, "Stop recording"), "the panel does not offer to stop once it started");
    assert.ok(control(ctx, "Start recording") === null, "the panel still offers to start after it started");

    await dom.click(control(ctx, "Stop recording") as HTMLElement);
    await settle();
    assert.equal(toggles(engine).length, 2);
    assert.deepEqual(toggles(engine)[1].body, { enabled: false });
    assert.ok(control(ctx, "Start recording"), "the panel did not go back to offering to start");
  });
});

test("a run that has started or finished, and was not recorded, shows no recording control", async () => {
  // The reason the control is not on every card: an empty panel on every goal
  // teaches people to ignore it. It is offered where the engine would accept it
  // and nowhere else, so a control that could only earn `trace_locked` is absent.
  for (const status of ["PENDING", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]) {
    await withGoal({ status }, async (ctx) => {
      assert.ok(
        control(ctx, "Show recording") === null,
        `a ${status} goal that was never recorded offers a recording control`,
      );
    });
  }
});

test("a finished run that was recorded can still be inspected and switched off", async () => {
  // Arming is the missing half; disarming is the one that has to stay available
  // after the run begins, because turning a recording off is never refused.
  await withGoal({ status: "COMPLETED", trace: true }, async (ctx) => {
    const { dom, engine, settle } = ctx;
    const open = control(ctx, "Show recording");
    assert.ok(open, "a recorded run cannot be inspected once it has finished");
    await dom.click(open);
    await settle();
    assert.ok(control(ctx, "Start recording") === null, "a finished run offers to start recording");
    const stop = control(ctx, "Stop recording");
    assert.ok(stop, "a recorded run cannot be switched off");
    await dom.click(stop);
    await settle();
    assert.deepEqual(toggles(engine).map((c) => c.body), [{ enabled: false }]);
  });
});

test("the engine's refusal is surfaced rather than swallowed", async () => {
  // The goal moves on between the card being drawn and the button being pressed:
  // the engine answers `trace_locked`, and a control that caught that and did
  // nothing would be a lie about what happened.
  await withGoal({ status: "PLANNING" }, async (ctx) => {
    const { dom, engine, settle, engineGoal } = ctx;
    await dom.click(control(ctx, "Show recording") as HTMLElement);
    await settle();
    const started = engineGoal("g1");
    assert.ok(started, "the fake engine lost the goal");
    started.status = "RUNNING";

    await dom.click(control(ctx, "Start recording") as HTMLElement);
    await settle();

    assert.equal(toggles(engine).length, 1);
    assert.match(
      dom.text(),
      /recording can only start before the run does/,
      "the engine refused and the panel said nothing",
    );
    // Not shown as armed. (Whether "Start" is still offered depends on the panel having re-read
    // the goal and seen it running, which is correct either way and not what this is about.)
    assert.ok(control(ctx, "Stop recording") === null, "a refused arming was shown as armed");
  });
});
