/**
 * The first message of a brand-new thread is shown once (audit of 2026-09-29, M6).
 *
 * Server: one goal. UI: two message bubbles; after a reload, one. `handleSendMessage` creates the
 * thread and opens its tab, which fires hydration — a read of the thread's turns — and the turn it
 * has just dispatched already exists on the engine. The optimistic message only learns its goal after
 * two awaits, and hydration dedupes by goal id (`threadHydration.turnsNeedingHydration`), so a read
 * that lands in that window sees a turn nothing on screen claims and draws it a second time.
 *
 * The window is a matter of latency, and an instant fake engine never opens it: the send's own read
 * of the goal always wins. So the test holds that one response until hydration has fetched the turn,
 * which is the interleaving a real engine produces and the one measured in Chromium.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { EngineCall } from "./appHarness.ts";

const { withApp, conversation } = await import("./appHarness.ts");

const PROMPT = "QUOKKA-77 fresh thread prompt";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const composer = (root: HTMLElement): HTMLTextAreaElement => {
  const box = root.querySelector("textarea");
  assert.ok(box, "the composer is missing");
  return box as HTMLTextAreaElement;
};

/** The user's message bubbles that carry the prompt: what the transcript shows the person. */
const bubbles = (root: HTMLElement, text: string): number =>
  [...root.querySelectorAll("div")].filter(
    (d) => d.className.toString().includes("bg-codify-raised") && d.children.length === 0 && (d.textContent ?? "").includes(text),
  ).length;

/**
 * Hold the *first* read of a goal (the one `handleSendMessage` makes after dispatching) until the
 * thread's own hydration has asked for that goal's events, or a moment has passed with no such ask.
 */
function holdTheSendsGoalRead(): { hook: (call: EngineCall) => Promise<void>; hydrationFetchedTheTurn: () => boolean } {
  let held = false;
  let sawEvents = false;
  let resolveEvents: () => void = () => {};
  const events = new Promise<void>((resolve) => {
    resolveEvents = resolve;
  });
  return {
    hydrationFetchedTheTurn: () => sawEvents,
    hook: async (call) => {
      if (call.method !== "GET") return;
      if (/^\/goals\/goal-\d+\/events$/.test(call.path)) {
        sawEvents = true;
        resolveEvents();
        return;
      }
      if (/^\/goals\/goal-\d+$/.test(call.path) && !held) {
        held = true;
        await Promise.race([events, sleep(500)]);
        // Long enough for hydration to write what it fetched before this response is released.
        await sleep(60);
      }
    },
  };
}

test("the first message of a brand-new thread is one bubble, not two", async () => {
  const held = holdTheSendsGoalRead();
  await withApp({ beforeRespond: held.hook }, async ({ dom, engine, settle }) => {
    await dom.fill(composer(dom.container), PROMPT);
    await dom.press(composer(dom.container), "Enter");
    await settle();
    await sleep(700);
    await settle();

    assert.equal(engine.filter((c) => c.method === "POST" && c.path.endsWith("/turns")).length, 1, "one turn was dispatched");
    assert.equal(bubbles(dom.container, PROMPT), 1, "the first message of a new thread is drawn more than once");
  });
});

test("a thread this window just created has nothing to read back, so it is not read", async () => {
  await withApp({}, async ({ dom, engine, settle }) => {
    await dom.fill(composer(dom.container), PROMPT);
    await dom.press(composer(dom.container), "Enter");
    await settle();

    const reads = engine.filter((c) => c.method === "GET" && /^\/conversations\/c-new-\d+\/turns$/.test(c.path));
    assert.equal(reads.length, 0, "a thread that did not exist a moment ago was hydrated");
  });
});

test("a thread that already has history is still read back when it is opened", async () => {
  // The control: marking a *new* thread as hydrated must not switch hydration off for the threads
  // that have something to load.
  const old = conversation({ id: "c-old", title: "Earlier work" });
  const stored = {
    version: 1,
    layout: { tabs: [{ key: "k_old", kind: "chat", conversationId: "c-old", workspaceId: "ws-a" }], activeIndex: 0 },
    pendingRemovals: [],
    pendingWrites: [],
  };
  await withApp({ conversations: [old], storedTabs: stored }, async ({ engine, settle }) => {
    await settle();
    const reads = engine.filter((c) => c.method === "GET" && c.path === "/conversations/c-old/turns");
    assert.equal(reads.length, 1, "an existing thread was not read back when its tab opened");
  });
});

test("a message sent while the thread is still being read back is not drawn twice either", async () => {
  // The other half of the same race, for a thread that *exists*: its tab has opened and its read
  // is still in flight when the user sends. That read is answered after the turn exists on the
  // engine, so it lists it — and only the message's own claim on its goal id keeps it from being
  // drawn again. Nothing about the thread being new protects this one.
  const old = conversation({ id: "c-old", title: "Earlier work" });
  const stored = {
    version: 1,
    layout: { tabs: [{ key: "k_old", kind: "chat", conversationId: "c-old", workspaceId: "ws-a" }], activeIndex: 0 },
    pendingRemovals: [],
    pendingWrites: [],
  };
  let releaseThreadRead: () => void = () => {};
  const threadRead = new Promise<void>((resolve) => {
    releaseThreadRead = resolve;
  });
  let releaseSendsGoalRead: () => void = () => {};
  const sendsGoalRead = new Promise<void>((resolve) => {
    releaseSendsGoalRead = resolve;
  });
  let heldGoalRead = false;
  const hook = async (call: EngineCall): Promise<void> => {
    if (call.method === "GET" && call.path === "/conversations/c-old/turns") await threadRead;
    if (call.method === "GET" && /^\/goals\/goal-\d+$/.test(call.path) && !heldGoalRead) {
      heldGoalRead = true;
      await sendsGoalRead;
    }
  };
  await withApp({ conversations: [old], storedTabs: stored, beforeRespond: hook }, async ({ dom, engine, settle }) => {
    await settle();
    await dom.fill(composer(dom.container), PROMPT);
    await dom.press(composer(dom.container), "Enter");
    // The turn is dispatched; the engine now holds it; the send's own read of it is held.
    for (let i = 0; i < 100 && !engine.some((c) => c.method === "POST" && c.path === "/conversations/c-old/turns"); i++) await sleep(10);
    await sleep(80);
    releaseThreadRead(); // the thread's read now lists a turn the screen is already showing
    await sleep(150);
    releaseSendsGoalRead();
    await settle();
    await sleep(200);
    await settle();

    assert.equal(bubbles(dom.container, PROMPT), 1, "a turn sent during a read of its thread was drawn twice");
  });
});
