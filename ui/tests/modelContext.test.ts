/**
 * What a model row says about memory, and what it must never say.
 *
 * A number next to a model's name is a claim about whether a prompt fits. Two different facts feed it and the
 * badge must not blur them: what the provider says the model *can* hold, and, for an Ollama model, what Codify
 * actually *asks* it to hold (`num_ctx`), which is usually far less. Unknown stays unknown: no number from a
 * name, none where nothing was reported.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { contextBadge, formatTokens, modelBadges } from "../src/modelSignals.ts";
import type { ModelOption } from "../src/types.ts";

const model = (over: Partial<ModelOption> = {}): ModelOption => ({
  id: "m",
  name: "m",
  provider: "openai",
  protocol: "openai_compat",
  description: "",
  ...over,
});

const ollama = (over: Partial<ModelOption> = {}): ModelOption =>
  model({ provider: "ollama", protocol: "ollama", ...over });

test("a token count reads the way it does on a model card", () => {
  const table: Array<[number, string]> = [
    [131072, "128K"], // a power of two: the trained window
    [128000, "128K"], // the provider's round number, not "125K"
    [200000, "200K"],
    [8192, "8K"],
    [4096, "4K"],
    [32768, "32K"],
    [65536, "64K"],
    [262144, "256K"],
    [40960, "41K"], // not a power of two: decimal
    [4500, "4.5K"],
    [1000, "1K"],
    [1024, "1K"],
    [999, "999"],
    [1048576, "1M"],
    [1000000, "1M"],
    [1500000, "1.5M"],
    [2097152, "2M"],
  ];
  for (const [n, want] of table) assert.equal(formatTokens(n), want, String(n));
});

test("a num_ctx of zero is no window: it reads as unset and never as '0 ctx'", () => {
  const badge = contextBadge(ollama({ context_tokens: 131072 }), 0);
  assert.equal(badge?.label, "up to 128K");
  assert.equal(contextBadge(ollama(), 0), null, "nothing is known, so nothing is claimed");
});

test("what is not a count is not formatted", () => {
  for (const n of [0, -1, -4096, NaN, Infinity]) assert.equal(formatTokens(n), "", String(n));
});

test("a hosted model shows the window its provider reports, with the exact figure in the title", () => {
  const badge = contextBadge(model({ context_tokens: 131072 }));
  assert.equal(badge?.label, "128K ctx");
  assert.match(badge?.title ?? "", /131,072 tokens/);
});

test("nothing reported means no badge, whatever the model is called", () => {
  assert.equal(contextBadge(model({ id: "gpt-4-128k", name: "gpt-4-128k" })), null);
  for (const bad of [null, undefined, 0, -5, NaN]) {
    assert.equal(contextBadge(model({ context_tokens: bad as number | null | undefined })), null, String(bad));
  }
});

test("a hosted model ignores num_ctx, which only Ollama is asked for", () => {
  const badge = contextBadge(model({ context_tokens: 200000 }), 8192);
  assert.equal(badge?.label, "200K ctx");
});

test("an Ollama row on a surface that does not know num_ctx claims only the model's own maximum", () => {
  const badge = contextBadge(ollama({ context_tokens: 131072 }), undefined);
  assert.equal(badge?.label, "128K ctx");
  assert.doesNotMatch(badge?.title ?? "", /Codify asks|num_ctx|default/, "it said something about a window it does not know");
});

test("an Ollama model asked for less than it supports says both numbers", () => {
  const badge = contextBadge(ollama({ context_tokens: 131072 }), 8192);
  assert.equal(badge?.label, "8K of 128K");
  assert.match(badge?.title ?? "", /8,192/);
  assert.match(badge?.title ?? "", /131,072/);
  assert.match(badge?.title ?? "", /num_ctx/);
});

test("an Ollama model asked for as much as it supports, or more, is simply its full window", () => {
  assert.equal(contextBadge(ollama({ context_tokens: 8192 }), 8192)?.label, "8K ctx");
  assert.equal(contextBadge(ollama({ context_tokens: 8192 }), 32768)?.label, "8K ctx", "a window larger than the model's is not claimed");
});

test("an Ollama model asked for a window, with no maximum reported, shows what it is asked for", () => {
  const badge = contextBadge(ollama(), 12288);
  assert.equal(badge?.label, "12K ctx");
  assert.match(badge?.title ?? "", /not reported/);
});

test("an Ollama model with no num_ctx set says it gets the server's default, not its maximum", () => {
  const badge = contextBadge(ollama({ context_tokens: 131072 }), null);
  assert.equal(badge?.label, "up to 128K");
  assert.match(badge?.title ?? "", /Ollama uses its own default/);
});

test("an Ollama model with nothing known about it has no badge", () => {
  assert.equal(contextBadge(ollama(), null), null);
  assert.equal(contextBadge(ollama(), undefined), null);
});

test("the row's badges carry the window and the default flag, and the old ones are unchanged", () => {
  const m = ollama({ context_tokens: 131072, supports_chat: false });
  const plain = modelBadges(m);
  assert.deepEqual(plain.context, { label: "128K ctx", title: plain.context?.title ?? "" });
  assert.equal(plain.isDefault, false);
  assert.equal(plain.notChat, true);
  assert.equal(plain.roles, "");
  const here = modelBadges(m, undefined, 3, { numCtx: 8192, isDefault: true });
  assert.equal(here.context?.label, "8K of 128K");
  assert.equal(here.isDefault, true);
});
