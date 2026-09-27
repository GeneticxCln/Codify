/**
 * A provider row, as markup rather than as a return value.
 *
 * `providerSetup.test.ts` covers the arithmetic. This covers what a person
 * actually sees, because the two decisions this row exists for — paste a key,
 * pick a model — are both easy to render *nearly* right: a key field that is not
 * labelled as a key, a picker whose empty state sends a local-server user to add
 * an API key they do not need, or a button that reports a state nobody is in.
 *
 * Rendered through `react-dom/server` from the real component in `src/`, via the
 * loader in `tsxLoader.ts`.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { ProviderRow } = await import("../src/components/ProviderRow.tsx");

import type { AgentRole, ModelOption, ProviderKeyStatus } from "../src/types.ts";

const LLAMA: ModelOption = {
  id: "meta-llama/llama-3.3-70b-instruct",
  name: "Llama 3.3 70B Instruct",
  provider: "nvidia",
  description: "Meta's open-weight flagship",
};

const keyStatus = (over: Partial<ProviderKeyStatus> = {}): ProviderKeyStatus => ({
  provider: "nvidia",
  protocol: "openai_compat",
  base_url: "https://integrate.api.nvidia.com/v1",
  has_key: false,
  needs_key: true,
  storage: "keyring",
  storage_detail: "OS keychain",
  ...over,
} as ProviderKeyStatus);

/** Markup with tags stripped, so an assertion is about words, not nesting. */
const text = (markup: string): string =>
  markup
    .replace(/<[^>]+>/g, " ")
    .replace(/&rsquo;/g, "'")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ")
    .trim();

const render = (over: Record<string, unknown> = {}): string =>
  renderToStaticMarkup(
    React.createElement(ProviderRow, {
      keyStatus: keyStatus(),
      models: [LLAMA],
      roleProviders: [
        { role: "librarian" as AgentRole, provider: "nvidia", model_name: "old-model" },
      ],
      onSaveKey: async () => {},
      onApplyModel: async () => {},
      ...over,
    } as never),
  );

test("the two decisions are on the same row, and both are named", () => {
  // The reason this row exists: the key field and the model picker used to be
  // on different tabs, so you were asked to trust a provider before you could
  // see what it served. If either label goes, the row is two unrelated inputs.
  const shown = text(render());
  assert.match(shown, /API key/i, "the credential field is not labelled");
  assert.match(shown, /Model/i, "the picker is not labelled");
  assert.match(shown, /nvidia/);
});

test("a keyed provider offers a password field, never a plain one", () => {
  // A key typed into a `type="text"` field is a key on screen, in a screenshot,
  // and in anything that reads the DOM. This is the one assertion here that is
  // about a *property* rather than a word, because no amount of visible text
  // catches it.
  const markup = render();
  assert.match(markup, /type="password"/);
  assert.doesNotMatch(
    markup,
    /type="text"[^>]*value="[^"]+"/,
    "a typed value is sitting in a plain text field",
  );
});

test("a local provider is told it needs no key, and shows no key field", () => {
  // ollama ships no credential. Offering an empty password box for it is a
  // control that cannot do anything, which is the thing this app does not ship.
  const markup = render({
    keyStatus: keyStatus({
      provider: "ollama",
      protocol: "ollama",
      base_url: "http://127.0.0.1:11434",
      needs_key: false,
    }),
    models: [],
    roleProviders: [],
  });
  assert.doesNotMatch(markup, /type="password"/);
  assert.match(text(markup), /no key to paste/i);
});

test("a provider nobody uses offers no bulk edit, only a request for a choice", () => {
  // A fresh provider — key pasted, no role on it yet. Nothing may be staged from
  // a static render, so the label here is always the "choose one" one; the
  // no-roles and already-on-it labels need a keystroke first and are asserted
  // in `providerSetup.test.ts` against `applyLabel` directly.
  const shown = text(render({ roleProviders: [], models: [LLAMA] }));
  assert.match(shown, /Choose a model/);
  assert.doesNotMatch(shown, /Apply to \d+ roles?/);
  assert.doesNotMatch(shown, /No roles on this provider/);
});

test("with nothing chosen, the button asks for a choice rather than claiming one", () => {
  // Before this row there was no staged model at all, so the label fell through
  // to "1 role already on it" — a statement about a state nobody is in, on a
  // button that looks like it has already done something.
  const shown = text(render());
  assert.match(shown, /Choose a model/);
  assert.doesNotMatch(shown, /already on it/);
});

test("a stored key reads as stored, without echoing the key", () => {
  // `PUT /settings/keys` never returns the key, and the field must not either:
  // the stored value is a bullet run, not the secret.
  const shown = text(render({ keyStatus: keyStatus({ has_key: true }) }));
  assert.match(shown, /Configured/);
  assert.doesNotMatch(shown, /stored — leave blank to keep\}/, "placeholder text leaked");
});

test("the model picker is present with a browse control, not a bare input", () => {
  // A field with no caret is a field, and a field is what this replaces. The
  // caret is the affordance that says "there is a list behind this".
  const markup = render();
  assert.match(markup, /aria-label="Browse nvidia models"/);
  assert.match(markup, /aria-expanded="false"/);
});

// ── a provider that released something ────────────────────────────────────

const ARRIVAL: ModelOption = {
  id: "meta-llama/llama-4-405b-instruct",
  name: "Llama 4 405B Instruct",
  provider: "nvidia",
  description: "Released this morning",
};

test("a release is announced in the row's header, not only behind the caret", () => {
  // A provider that ships a model puts it in the catalogue, and a list of two
  // hundred is not somewhere a release announces itself. The count has to be on
  // the row, because a badge that only exists after opening a dropdown is a
  // badge nobody opens a dropdown for.
  const markup = render({ models: [LLAMA, ARRIVAL], newIds: [ARRIVAL.id] });
  const shown = text(markup);
  assert.match(shown, /1 new/);
  // And it has to say what "new" is measured against, or it reads as a count of
  // everything the provider serves. "When you last looked" rather than "the last
  // time you were here": the panel re-discovers on its own, so a release can
  // land while the reader is looking right at it.
  assert.match(markup, /nvidia did not list when you last looked/);
});

test("a provider with nothing new says nothing about it", () => {
  // "0 new" is a control-shaped lie: it asserts a comparison happened and found
  // nothing, on a row that is already showing a model count. Silence is the
  // honest form — and it is the form a reader stops noticing.
  const shown = text(render({ models: [LLAMA], newIds: [] }));
  assert.doesNotMatch(shown, /\bnew\b/);
  assert.doesNotMatch(shown, /0 new/);
});

test("a new id the provider no longer serves is not counted as new", () => {
  // The count is of what is on screen, so a stale id in the record cannot inflate
  // it. Otherwise a provider that retired five models reports six new ones.
  const shown = text(render({ models: [LLAMA], newIds: [ARRIVAL.id, "retired:1b"] }));
  assert.doesNotMatch(shown, /\bnew\b/);
});
