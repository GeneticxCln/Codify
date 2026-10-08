/**
 * The command bar's model picker, mounted: what it shows, what a send posts, and what the toolbar holds.
 *
 * The picker used to be a decoration (the engine recorded the choice and ran the turn on whatever Settings named),
 * and the bar carried three toggles a turn never reads. These are the claims about the real App, checked by
 * clicking the controls and reading what the app asked the engine to do:
 *
 * - **What is shown is what runs.** A model Settings gives the conversation is shown and *nothing is sent for it*
 *   (so no existing install is rerouted by a UI default); a pick is shown and sent; choosing the configured model
 *   again clears the pick; with nothing configured the old guess (a local model) is shown and sent, because
 *   nothing else would answer.
 * - **A pick survives the catalog refreshing under it**, including one typed in by hand, which no catalog lists.
 * - **The rows say how much a model can hold**, and for an Ollama model what it is actually asked for.
 * - **The toolbar holds only what a turn reads.** Parallel, Design and Knowledge are gone.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { EngineCall } from "./appHarness.ts";

const { withApp, conversation } = await import("./appHarness.ts");

const HOSTED = {
  id: "gpt-x", name: "gpt-x", provider: "openai", protocol: "openai_compat",
  description: "", supports_chat: true, context_tokens: 128000,
};
const LOCAL = {
  id: "qwen3:8b", name: "qwen3:8b", provider: "ollama", protocol: "ollama",
  description: "", supports_chat: true, context_tokens: 131072,
};
// The same id served by two providers: a pick, a default and a badge are all about the *pair*.
const SHARED_OPENAI = {
  id: "shared", name: "shared", provider: "openai", protocol: "openai_compat",
  description: "served by openai", supports_chat: true, context_tokens: 128000,
};
const SHARED_OLLAMA = {
  id: "shared", name: "shared", provider: "ollama", protocol: "ollama",
  description: "served locally", supports_chat: true, context_tokens: 131072,
};
const UNREPORTED = {
  id: "mystery", name: "mystery", provider: "openai", protocol: "openai_compat",
  description: "", supports_chat: true,
};

const CONDUCTOR = { provider: "openai", model: "gpt-x", source: "scribe", num_ctx: 8192 };

/** The engine's `GET /models`, with the conductor block this test wants (or none, as an older engine answers). */
const models = (conductor: unknown, list: unknown[] = [HOSTED, LOCAL, UNREPORTED]) => ({
  "GET /models": { body: { models: list, providers: [], fetched_at: 0, cached: false, conductor } },
});

const THREAD = "c-open";
const stripOn = (conversationId: string) => ({
  version: 1,
  layout: { tabs: [{ key: "k_test_1", kind: "chat", conversationId, workspaceId: "ws-a" }], activeIndex: 0 },
  pendingRemovals: [],
  pendingWrites: [],
});

const open = (conductor: unknown, list?: unknown[]) => ({
  conversations: [conversation({ id: THREAD, title: "A thread" })],
  storedTabs: stripOn(THREAD),
  answers: models(conductor, list),
});

const posts = (engine: EngineCall[], path: string) => engine.filter((c) => c.method === "POST" && c.path === path);

type Ctx = Parameters<Parameters<typeof withApp>[1]>[0];

const composer = (ctx: Ctx): HTMLTextAreaElement => {
  const box = ctx.dom.container.querySelector('textarea[aria-label="Chat prompt"]');
  assert.ok(box, "the composer is missing");
  return box as HTMLTextAreaElement;
};

/** The model button in the bar: the only one whose tooltip says what the model is for. */
const trigger = (ctx: Ctx): HTMLButtonElement => {
  const button = ctx.dom.container.querySelector('button[title*="Answers this conversation"]');
  assert.ok(button, "the bar has no model picker");
  return button as HTMLButtonElement;
};

const shown = (ctx: Ctx): string => (trigger(ctx).textContent ?? "").trim();

async function openMenu(ctx: Ctx): Promise<void> {
  await ctx.dom.click(trigger(ctx));
  await ctx.settle();
}

/** A row in the open menu, found by the id its tooltip starts with. */
const row = (ctx: Ctx, id: string): HTMLButtonElement => {
  const found = [...ctx.dom.container.querySelectorAll("button")].find((b) => (b.getAttribute("title") ?? "").startsWith(`${id}\n`) || b.getAttribute("title") === id);
  assert.ok(found, `no row for ${id} in the model menu`);
  return found as HTMLButtonElement;
};

/** A row whose description (the second line of its tooltip) says which copy of a shared id it is. */
const rowDescribed = (ctx: Ctx, description: string): HTMLButtonElement => {
  const found = [...ctx.dom.container.querySelectorAll("button")].find((b) => (b.getAttribute("title") ?? "").endsWith(`\n${description}`));
  assert.ok(found, `no row described "${description}" in the model menu`);
  return found as HTMLButtonElement;
};

async function send(ctx: Ctx, text = "what does the parser do?"): Promise<Record<string, unknown> | null | undefined> {
  await ctx.dom.fill(composer(ctx), text);
  await ctx.dom.press(composer(ctx), "Enter");
  await ctx.settle();
  const made = posts(ctx.engine, `/conversations/${THREAD}/turns`);
  assert.equal(made.length, 1, "the turn was not sent");
  return made[0].body;
}

// ── what is shown, and what a send posts ────────────────────────────────────────────────────────────

test("with a model configured and nothing picked, the bar shows it and a send names no model", async () => {
  await withApp(open(CONDUCTOR), async (ctx) => {
    assert.match(shown(ctx), /gpt-x/, "the bar should show the model Settings gives the conversation");
    const body = await send(ctx);
    assert.deepEqual(body, { prompt: "what does the parser do?" }, "a configured model must not be re-sent as if it were a pick");
  });
});

test("a pick is shown and posted, and wins over the configured model", async () => {
  await withApp(open(CONDUCTOR), async (ctx) => {
    await openMenu(ctx);
    await ctx.dom.click(row(ctx, "qwen3:8b"));
    await ctx.settle();
    assert.match(shown(ctx), /qwen3:8b/, "the pick is not on screen");
    const body = await send(ctx);
    assert.equal(body?.provider, "ollama");
    assert.equal(body?.model, "qwen3:8b");
  });
});

test("choosing the configured model again clears the pick: the turn is sent with no model", async () => {
  await withApp(open(CONDUCTOR), async (ctx) => {
    await openMenu(ctx);
    await ctx.dom.click(row(ctx, "qwen3:8b"));
    await ctx.settle();
    await openMenu(ctx);
    await ctx.dom.click(row(ctx, "gpt-x"));
    await ctx.settle();
    assert.match(shown(ctx), /gpt-x/);
    const body = await send(ctx);
    assert.equal(body?.provider, undefined, "choosing the default left a pick behind");
    assert.equal(body?.model, undefined);
  });
});

test("an engine that reports no conductor keeps the old behaviour: a local model is shown, and sent", async () => {
  await withApp(open(undefined), async (ctx) => {
    assert.match(shown(ctx), /qwen3:8b/, "nothing configured should show the local model, as it always did");
    const body = await send(ctx);
    assert.equal(body?.provider, "ollama", "with nothing configured the shown model is the only one that can answer");
    assert.equal(body?.model, "qwen3:8b");
  });
});

test("a configured model the catalog does not list is still what the bar shows", async () => {
  await withApp(open({ ...CONDUCTOR, provider: "groq", model: "not-listed" }), async (ctx) => {
    assert.match(shown(ctx), /not-listed/);
    const body = await send(ctx);
    assert.equal(body?.model, undefined, "a configured model was sent as a pick");
  });
});

// ── a pick survives the catalog refreshing under it ─────────────────────────────────────────────────

test("a model typed in by hand survives a refresh and is the one a send names", async () => {
  await withApp(open(CONDUCTOR), async (ctx) => {
    await openMenu(ctx);
    const field = ctx.dom.container.querySelector('input[aria-label="Custom model in provider/model form"]') as HTMLInputElement;
    assert.ok(field, "the custom model field is missing");
    await ctx.dom.fill(field, "openai/gpt-brand-new");
    const set = [...ctx.dom.container.querySelectorAll("button[type=submit]")].find((b) => b.textContent?.trim() === "Set");
    assert.ok(set, "the custom model form has no Set button");
    await ctx.dom.click(set as HTMLElement);
    await ctx.settle();
    assert.match(shown(ctx), /openai\/gpt-brand-new/);

    // The catalog is read again (the Refresh in the menu), and no catalog lists that model.
    await openMenu(ctx);
    const refresh = [...ctx.dom.container.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Refresh");
    assert.ok(refresh, "the menu has no Refresh");
    await ctx.dom.click(refresh as HTMLElement);
    await ctx.settle();
    assert.ok(ctx.engine.filter((c) => c.method === "GET" && c.path === "/models").length >= 2, "the catalog was not read again");
    assert.match(shown(ctx), /openai\/gpt-brand-new/, "the refresh swapped the model on screen for another");

    const body = await send(ctx);
    assert.equal(body?.provider, "openai");
    assert.equal(body?.model, "gpt-brand-new");
  });
});

test("a listed model that the refreshed catalog no longer has is dropped, and the turn runs where Settings says", async () => {
  // The catalog lists both until the pick is made; every read after that has lost the local model (its server stopped).
  let stopped = false;
  const shrinking = () => ({
    body: { models: stopped ? [HOSTED] : [HOSTED, LOCAL], providers: [], fetched_at: 0, cached: false, conductor: CONDUCTOR },
  });
  await withApp({ ...open(CONDUCTOR), answers: { "GET /models": shrinking } }, async (ctx) => {
    await openMenu(ctx);
    await ctx.dom.click(row(ctx, "qwen3:8b"));
    await ctx.settle();
    assert.match(shown(ctx), /qwen3:8b/);

    stopped = true;
    const before = ctx.engine.filter((c) => c.method === "GET" && c.path === "/models").length;
    await openMenu(ctx);
    const refresh = [...ctx.dom.container.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Refresh");
    assert.ok(refresh, "the menu has no Refresh");
    await ctx.dom.click(refresh as HTMLElement);
    await ctx.settle();
    assert.ok(ctx.engine.filter((c) => c.method === "GET" && c.path === "/models").length > before, "the catalog was not read again");

    assert.match(shown(ctx), /gpt-x/, "a pick the catalog lost should fall back to the configured model on screen");
    const body = await send(ctx);
    assert.equal(body?.model, undefined, "a model that is gone was still sent");
  });
});

test("with no model available a send says so and posts nothing", async () => {
  await withApp(open(undefined, []), async (ctx) => {
    await ctx.dom.fill(composer(ctx), "what does the parser do?");
    await ctx.dom.press(composer(ctx), "Enter");
    await ctx.settle();
    assert.equal(posts(ctx.engine, `/conversations/${THREAD}/turns`).length, 0, "a turn was sent with no model to answer it");
    assert.match(ctx.dom.container.textContent ?? "", /No model available/);
  });
});

// ── what the rows say ───────────────────────────────────────────────────────────────────────────────

test("an id two providers serve: only the configured provider's copy is the default", async () => {
  await withApp(open({ ...CONDUCTOR, model: "shared" }, [SHARED_OPENAI, SHARED_OLLAMA]), async (ctx) => {
    await openMenu(ctx);
    assert.match(rowDescribed(ctx, "served by openai").textContent ?? "", /default/);
    assert.doesNotMatch(rowDescribed(ctx, "served locally").textContent ?? "", /default/, "the other provider's copy was marked as the default");
  });
});

test("the menu marks the configured model as the default, and only that one", async () => {
  await withApp(open(CONDUCTOR), async (ctx) => {
    await openMenu(ctx);
    assert.match(row(ctx, "gpt-x").textContent ?? "", /default/);
    assert.doesNotMatch(row(ctx, "qwen3:8b").textContent ?? "", /default/);
    assert.doesNotMatch(row(ctx, "mystery").textContent ?? "", /default/);
  });
});

test("a model's window is on its row: the provider's figure, and for Ollama what it is actually asked for", async () => {
  await withApp(open(CONDUCTOR), async (ctx) => {
    await openMenu(ctx);
    assert.match(row(ctx, "gpt-x").textContent ?? "", /128K ctx/);
    assert.match(row(ctx, "qwen3:8b").textContent ?? "", /8K of 128K/, "an Ollama row should say what num_ctx asks for");
    assert.doesNotMatch(row(ctx, "mystery").textContent ?? "", /ctx|\bK\b|\bof\b/, "a model nobody described has no number");
  });
});

test("an Ollama model on a row that sets no num_ctx says it gets the server's default, not its maximum", async () => {
  await withApp(open({ ...CONDUCTOR, num_ctx: null }), async (ctx) => {
    await openMenu(ctx);
    assert.match(row(ctx, "qwen3:8b").textContent ?? "", /up to 128K/);
  });
});

test("an engine that does not say what num_ctx is claims only the model's own maximum", async () => {
  await withApp(open(undefined), async (ctx) => {
    await openMenu(ctx);
    const text = row(ctx, "qwen3:8b").textContent ?? "";
    assert.match(text, /128K ctx/);
    assert.doesNotMatch(text, /of 128K|up to/);
  });
});

test("the menu says the conversation's model is not the roles' models", async () => {
  await withApp(open(CONDUCTOR), async (ctx) => {
    await openMenu(ctx);
    assert.match(ctx.dom.container.textContent ?? "", /planner, fixer and other roles keep the\s+models set in Settings/);
    assert.match(trigger(ctx).getAttribute("title") ?? "", /keep the models set in Settings/);
  });
});

// ── the toolbar holds only what a turn reads ────────────────────────────────────────────────────────

test("the bar has the folder, model and mode pickers and Record, and nothing a turn ignores", async () => {
  await withApp(open(CONDUCTOR), async (ctx) => {
    const card = composer(ctx).closest("div.rounded-2xl");
    assert.ok(card, "could not find the prompt card");
    const labels = [...card.querySelectorAll("button")].map((b) => (b.textContent ?? "").trim());
    for (const present of ["Record", "Direct Apply"]) {
      assert.ok(labels.includes(present), `the bar lost its ${present} control: ${JSON.stringify(labels)}`);
    }
    for (const gone of ["Parallel", "Design", "Knowledge"]) {
      assert.ok(!labels.includes(gone), `the bar still has ${gone}, which a turn does not read`);
    }
    assert.ok(card.querySelector('[role="radiogroup"]') === null, "the deliverable group is still in the bar");
    assert.ok(card.querySelector('button[title^="Run independent steps"]') === null);
  });
});
