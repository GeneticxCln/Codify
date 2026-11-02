/**
 * The suggestions inbox in the whole App (docs/09 §10.22): what a person sees on the bell, what a press sends, and,
 * above all, what is never sent unless they press.
 *
 * The engine's detectors never act, and the window's reading of them must not either. The only way an offer becomes a
 * turn is a person pressing Send to chat, which sends the offer's words through the ordinary composer door
 * (`POST /conversations/{id}/turns`) and then tells the engine which turn that was. These mount the App against the
 * recording fake engine and look at what it was asked.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext, AppOptions, EngineAnswer } from "./appHarness.ts";
import type { Suggestion } from "../src/types.ts";

const { withApp } = await import("./appHarness.ts");

const WORKSPACES = [
  { id: "ws-a", name: "Alpha", root_path: "/tmp/e2e-alpha" },
  { id: "ws-b", name: "Beta", root_path: "/tmp/e2e-beta" },
];

const offer = (over: Partial<Suggestion> = {}): Suggestion => ({
  id: "s1",
  workspace_id: "ws-a",
  kind: "paused_goal",
  title: "“Add the banner” has been paused for 7 h",
  detail: "Waiting for you to press Start.",
  evidence: [],
  prompt: "My goal “Add the banner” has been paused. Do not change anything.",
  status: "open",
  snoozed_until: null,
  goal_id: null,
  created_at: 1,
  updated_at: 1,
  ...over,
});

/** A fake inbox that behaves like the engine's: acting on an offer takes it out of the list. */
function inbox(initial: Suggestion[]): NonNullable<AppOptions["answers"]> {
  let open = [...initial];
  const ok = (id: string): EngineAnswer => ({ body: initial.find((s) => s.id === id) ?? {} });
  const answers: NonNullable<AppOptions["answers"]> = {
    "GET /suggestions": () => ({ body: { suggestions: open, snoozed: 0 } }),
    "POST /suggestions/scan": () => ({ body: { created: 0 } }),
  };
  for (const s of initial) {
    const gone = (): EngineAnswer => {
      open = open.filter((o) => o.id !== s.id);
      return ok(s.id);
    };
    answers[`POST /suggestions/${s.id}/snooze`] = gone;
    answers[`POST /suggestions/${s.id}/dismiss`] = gone;
    answers[`POST /suggestions/${s.id}/accept`] = gone;
  }
  return answers;
}

const bell = (ctx: AppContext): HTMLElement => {
  const found = [...ctx.dom.container.querySelectorAll("header button")].find((b) =>
    /^Notifications/.test(b.getAttribute("aria-label") ?? ""),
  );
  if (!found) throw new Error("the header has no Notifications button");
  return found as HTMLElement;
};
const drawer = (ctx: AppContext): Element | null => ctx.dom.container.querySelector('aside[aria-label="Notifications"]');
const posts = (ctx: AppContext, pattern: RegExp) =>
  ctx.engine.filter((c) => c.method === "POST" && pattern.test(c.path));

async function withInbox(
  suggestions: Suggestion[],
  body: (ctx: AppContext) => Promise<void>,
): Promise<void> {
  await withApp({ workspaces: WORKSPACES, answers: inbox(suggestions) }, async (ctx) => {
    await ctx.settle();
    await body(ctx);
  });
}

test("the bell counts what is waiting in the inbox and says so, and an empty inbox adds nothing", async () => {
  await withInbox([offer(), offer({ id: "s2", title: "Another" })], async (ctx) => {
    assert.equal(bell(ctx).getAttribute("aria-label"), "Notifications, 2 suggestions");
    assert.match(bell(ctx).textContent ?? "", /2/);
  });
  await withInbox([], async (ctx) => {
    assert.equal(bell(ctx).getAttribute("aria-label"), "Notifications");
  });
});

test("nobody pressed anything: the inbox is read, and no turn, goal or acceptance is sent", async () => {
  await withInbox([offer()], async (ctx) => {
    await ctx.dom.click(bell(ctx));
    await ctx.settle();
    assert.match(drawer(ctx)?.textContent ?? "", /has been paused for 7 h/, "the offer is on screen");
    assert.deepEqual(posts(ctx, /^\/conversations\/[^/]+\/turns$/), [], "a turn was sent without a press");
    assert.deepEqual(posts(ctx, /^\/goals/), [], "a goal was started without a press");
    assert.deepEqual(posts(ctx, /^\/suggestions\/s1\//), [], "an offer was acted on without a press");
  });
});

test("Send to chat sends the offer's words as the person's own turn, then records which turn it became", async () => {
  await withInbox([offer()], async (ctx) => {
    await ctx.dom.click(bell(ctx));
    await ctx.settle();
    await ctx.dom.click(ctx.dom.byButton("Send to chat"));
    await ctx.settle();

    const turns = posts(ctx, /^\/conversations\/[^/]+\/turns$/);
    assert.equal(turns.length, 1, "exactly one turn, through the one door");
    assert.equal(turns[0].body?.prompt, offer().prompt, "the offer's own words");
    for (const flag of ["mode", "dry_run", "plan_only", "parallel", "agent_config"]) {
      assert.ok(!(flag in (turns[0].body ?? {})), `the turn carried ${flag}: the client chose what the turn becomes`);
    }
    const accepted = posts(ctx, /^\/suggestions\/s1\/accept$/);
    assert.equal(accepted.length, 1);
    assert.match(String(accepted[0].body?.goal_id), /^goal-/, "it names the goal the turn made");
    assert.deepEqual(posts(ctx, /^\/goals/), [], "and starts nothing else");
  });
});

test("an offer about another project cannot be sent from this one", async () => {
  await withInbox([offer({ workspace_id: "ws-b" })], async (ctx) => {
    await ctx.dom.click(bell(ctx));
    await ctx.settle();
    const send = ctx.dom.byButton("Send to chat");
    assert.equal(send.hasAttribute("disabled"), true);
    assert.match(drawer(ctx)?.textContent ?? "", /different project/);
    assert.match(drawer(ctx)?.textContent ?? "", /Beta/, "it says which project it is about");
    await ctx.dom.click(send);
    await ctx.settle();
    assert.deepEqual(posts(ctx, /\/turns$/), [], "a disabled button sent a turn into the wrong project");
  });
});

test("Snooze and Dismiss change the one offer and start nothing", async () => {
  await withInbox([offer(), offer({ id: "s2", title: "Another one" })], async (ctx) => {
    await ctx.dom.click(bell(ctx));
    await ctx.settle();
    await ctx.dom.click(ctx.dom.container.querySelector('[aria-label^="Snooze 24 hours: “Add the banner”"]') as Element);
    await ctx.settle();
    await ctx.dom.click(ctx.dom.container.querySelector('[aria-label="Dismiss: Another one"]') as Element);
    await ctx.settle();

    assert.deepEqual(posts(ctx, /^\/suggestions\/s1\/snooze$/).map((c) => c.body), [{ hours: 24 }]);
    assert.equal(posts(ctx, /^\/suggestions\/s2\/dismiss$/).length, 1);
    assert.deepEqual(posts(ctx, /\/turns$/), [], "putting an offer away sent a turn");
    assert.deepEqual(posts(ctx, /^\/goals/), []);
    assert.doesNotMatch(drawer(ctx)?.textContent ?? "", /Another one/, "a dismissed offer leaves the list");
    assert.equal(bell(ctx).getAttribute("aria-label"), "Notifications", "and the bell follows");
  });
});

test("an engine that answers the inbox with nonsense shows an empty inbox, not a broken window", async () => {
  await withApp(
    { workspaces: WORKSPACES, answers: { "GET /suggestions": () => ({ body: { not: "a list" } }) } },
    async (ctx) => {
      await ctx.settle();
      assert.equal(bell(ctx).getAttribute("aria-label"), "Notifications");
      await ctx.dom.click(bell(ctx));
      await ctx.settle();
      assert.match(drawer(ctx)?.textContent ?? "", /Nothing to suggest/);
    },
  );
});
