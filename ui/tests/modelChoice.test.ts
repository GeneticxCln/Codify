/**
 * Which model a turn runs on, what the command bar shows, and what it sends.
 *
 * The properties, in the order a wrong answer would hurt: **what is shown is what runs**; an install that
 * configured a model is not rerouted by a UI default nobody chose; an install with nothing configured still
 * gets a model that answers; a pick survives the catalog refreshing under it, including one typed by hand.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  CUSTOM_MODEL_DESCRIPTION,
  configuredModel,
  isCustomPick,
  keepPick,
  pickFromMenu,
  preferLocal,
  resolveModelChoice,
} from "../src/modelChoice.ts";
import type { ConductorModel, ModelOption } from "../src/types.ts";

const m = (provider: string, id: string, over: Partial<ModelOption> = {}): ModelOption => ({
  id,
  name: id,
  provider,
  protocol: provider === "ollama" ? "ollama" : "openai_compat",
  description: "",
  ...over,
});

const LOCAL = m("ollama", "qwen3:8b", { context_tokens: 40960 });
const HOSTED = m("openai", "gpt-x", { context_tokens: 128000 });
const OTHER = m("groq", "gpt-x"); // the same id on another provider
const CATALOG = [HOSTED, LOCAL, OTHER];

const conductor = (over: Partial<ConductorModel> = {}): ConductorModel => ({
  provider: "openai",
  model: "gpt-x",
  source: "scribe",
  num_ctx: null,
  ...over,
});

test("nothing picked and a model configured: the bar shows it and sends nothing", () => {
  const choice = resolveModelChoice({ catalog: CATALOG, picked: undefined, conductor: conductor() });
  assert.equal(choice.shown, HOSTED, "it should be the catalog's own row, with its window");
  assert.equal(choice.override, undefined, "a configured model must not be re-sent as if it were a pick");
  assert.equal(choice.configured, HOSTED);
});

test("a configured model the catalog does not list is still shown, as what it is", () => {
  const choice = resolveModelChoice({ catalog: [LOCAL], picked: undefined, conductor: conductor({ provider: "groq", model: "gone" }) });
  assert.deepEqual(
    { provider: choice.shown?.provider, id: choice.shown?.id },
    { provider: "groq", id: "gone" },
  );
  assert.match(choice.shown?.description ?? "", /not in the discovered list/);
  assert.equal(choice.override, undefined);
});

test("a pick is shown and sent, and wins over the configured model", () => {
  const choice = resolveModelChoice({ catalog: CATALOG, picked: LOCAL, conductor: conductor() });
  assert.equal(choice.shown, LOCAL);
  assert.equal(choice.override, LOCAL);
  assert.equal(choice.configured, HOSTED);
});

test("nothing picked and nothing configured: the old guess is shown and sent, because nothing else would answer", () => {
  for (const none of [null, undefined, conductor({ provider: "", model: "" }), conductor({ model: "" }), conductor({ provider: "" })]) {
    const choice = resolveModelChoice({ catalog: CATALOG, picked: undefined, conductor: none });
    assert.equal(choice.shown, LOCAL, JSON.stringify(none));
    assert.equal(choice.override, LOCAL, JSON.stringify(none));
    assert.equal(choice.configured, undefined);
  }
});

test("with no models at all there is nothing to show and nothing to send", () => {
  const choice = resolveModelChoice({ catalog: [], picked: undefined, conductor: null });
  assert.equal(choice.shown, undefined);
  assert.equal(choice.override, undefined);
});

test("the old guess prefers a local model and falls back to the first", () => {
  assert.equal(preferLocal([HOSTED, LOCAL]), LOCAL);
  assert.equal(preferLocal([HOSTED, OTHER]), HOSTED);
  assert.equal(preferLocal([]), undefined);
});

test("choosing the configured model from the menu clears the pick, and anything else is one", () => {
  assert.equal(pickFromMenu(HOSTED, HOSTED), undefined);
  assert.equal(pickFromMenu(LOCAL, HOSTED), LOCAL);
  // Ids repeat across providers: the same id on another provider is a different model, and a pick.
  assert.equal(pickFromMenu(OTHER, HOSTED), OTHER);
  assert.equal(pickFromMenu(LOCAL, undefined), LOCAL, "with nothing configured every choice is a pick");
});

test("a pick survives a refresh while the catalog lists it, and is dropped when its model is gone", () => {
  assert.equal(keepPick(LOCAL, CATALOG), LOCAL);
  assert.equal(keepPick(LOCAL, [HOSTED]), undefined);
  assert.equal(keepPick(m("other-provider", "qwen3:8b"), CATALOG), undefined, "same id, different provider");
  assert.equal(keepPick(undefined, CATALOG), undefined);
});

test("a model typed in by hand is never dropped for not being in a catalog", () => {
  const typed = m("openai", "gpt-brand-new", { description: CUSTOM_MODEL_DESCRIPTION });
  assert.equal(isCustomPick(typed), true);
  assert.equal(isCustomPick(LOCAL), false);
  assert.equal(keepPick(typed, CATALOG), typed, "the next turn would have gone to a model other than the one on screen");
  assert.equal(keepPick(typed, []), typed);
});

test("configuredModel reads a provider and a model, and half of one is nothing", () => {
  assert.equal(configuredModel(CATALOG, conductor({ provider: "ollama", model: "qwen3:8b" })), LOCAL);
  assert.equal(configuredModel(CATALOG, conductor({ provider: "", model: "x" })), undefined);
  assert.equal(configuredModel(CATALOG, null), undefined);
});

test("an id two providers serve is matched on the pair: the configured model is the one that was configured", () => {
  const served = [m("ollama", "shared"), m("openai", "shared")];
  const got = configuredModel(served, conductor({ provider: "openai", model: "shared" }));
  assert.equal(got, served[1], "a model with the right id on the wrong provider was shown as the one that runs");
  // And choosing the *other* provider's copy is a pick, not a return to the default.
  assert.equal(pickFromMenu(served[0], served[1]), served[0]);
});
