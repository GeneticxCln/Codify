/**
 * The conductor card's arithmetic: what a stored pair means, and when a save
 * would store something the engine ignores.
 *
 * The conductor reads `conductor_provider` / `conductor_model` out of
 * `engine_settings` and treats a provider without a model as "not configured",
 * borrowing the scribe's row instead. A card that saved a half pair would
 * therefore be a setting a user could change and watch nothing happen, so the
 * refusal lives in this module and is pinned here rather than in a `.tsx` the
 * suite can only reach through a loader hook.
 *
 * The `borrowed` cases matter as much as the `configured` one. A fresh install
 * has no scribe model either, and a card that said "runs on the scribe's model"
 * with nothing after it would be claiming a configuration that does not exist —
 * the conductor simply is not there.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { canSaveChoice, conductorFallback, conductorStatus, needsClear } from "../src/conductorSettings.ts";
import type { ConductorChoice } from "../src/conductorSettings.ts";

const pair = (provider: string, model: string): ConductorChoice => ({ provider, model });

test("a provider and a model are the only thing that configures the conductor", () => {
  const status = conductorStatus(pair("openai", "gpt-4o"), "scribe-model");
  assert.equal(status.kind, "configured");
  assert.match(status.text, /openai · gpt-4o/);
  // The sub-agents are not touched by this card, and the text should not imply
  // they are.
  assert.match(status.text, /sub-agents keep their own models/);
});

test("half a pair is stored and ignored, and says so", () => {
  for (const half of [pair("openai", ""), pair("", "gpt-4o"), pair("openai", "   ")]) {
    const status = conductorStatus(half, "scribe-model");
    assert.equal(status.kind, "incomplete", JSON.stringify(half));
    assert.match(status.text, /not a configuration/);
    assert.match(status.text, /scribe's model/);
  }
});

test("empty means borrowed, and the borrowed model is named", () => {
  const status = conductorStatus(pair("", ""), "scribe-model");
  assert.equal(status.kind, "borrowed");
  assert.match(status.text, /scribe-model/);
  assert.match(status.text, /stronger one/);
});

test("borrowed with no scribe model says there is no conductor, not that it runs", () => {
  // The failure this prevents: a card reading "the conductor borrows the
  // scribe's model" on an install where the scribe has none, so a user hunting
  // for a bug in the conductor is sent to a role card instead.
  for (const missing of [undefined, "", "   "]) {
    const status = conductorStatus(pair("", ""), missing);
    assert.equal(status.kind, "borrowed");
    assert.match(status.text, /no conductor on this install/);
  }
});

test("whitespace is not a choice", () => {
  assert.equal(conductorStatus(pair("  ", "\t"), "scribe-model").kind, "borrowed");
});

test("Save is offered only for a whole pair", () => {
  assert.equal(canSaveChoice(pair("openai", "gpt-4o")).ok, true);
  assert.equal(canSaveChoice(pair("", "")).ok, false);

  // Each refusal names the way out, because a disabled button with no reason is
  // the thing this card exists to avoid.
  const noModel = canSaveChoice(pair("openai", ""));
  assert.equal(noModel.ok, false);
  assert.match(noModel.text, /Choose a model/);
  const noProvider = canSaveChoice(pair("", "gpt-4o"));
  assert.equal(noProvider.ok, false);
  assert.match(noProvider.text, /Choose a provider/);
});

test("clearing is offered for a pair the user has begun to empty", () => {
  const stored = pair("openai", "gpt-4o");
  assert.equal(needsClear(stored, pair("", "")), true);
  assert.equal(needsClear(stored, pair("openai", "gpt-4o")), false);
  assert.equal(needsClear(stored, pair("openai", "gpt-4o-mini")), false);
  // A draft that is merely half full is a refused save, not a clear: offering
  // "use the scribe's model" there would be offering a button that does
  // something the user did not ask for.
  assert.equal(needsClear(stored, pair("openai", "")), false);
});

test("a half pair already in storage is not offered as something to undo", () => {
  // Undoing it would produce the same inert state it is already in.
  const half = pair("", "gpt-4o");
  assert.equal(needsClear(half, pair("", "")), false);
  assert.equal(needsClear(half, half), false);
});

test("an own pair takes its own fallback", () => {
  const { source, text } = conductorFallback(
    pair("openai", "gpt-4o"),
    pair("ollama", "qwen3:8b"),
    "scribe-model"
  );
  assert.equal(source, "own");
  assert.match(text, /ollama · qwen3:8b/);
  // The distinction that makes a fallback worth having at all.
  assert.match(text, /the call is retried, not the whole turn/);
});

test("a borrowing conductor takes the scribe's fallback, not the pair below", () => {
  // The precedence rule the executor implements. Reading the two sources as one
  // chain would mean a field a user fills in has no effect at all.
  const { source, text } = conductorFallback(
    pair("", ""),
    pair("ollama", "qwen3:8b"),
    "scribe-backup"
  );
  assert.equal(source, "borrowed");
  assert.match(text, /scribe-backup/);
  assert.match(text, /not used while the conductor borrows/);
});

test("no fallback anywhere says so plainly rather than implying safety", () => {
  const borrowed = conductorFallback(pair("", ""), pair("", ""), "");
  assert.equal(borrowed.source, "none");
  assert.match(borrowed.text, /a dead model ends the turn/);

  const own = conductorFallback(pair("openai", "gpt-4o"), pair("", ""), "scribe-backup");
  assert.equal(own.source, "none");
  assert.match(own.text, /after the moves it already made/);
});

test("the scribe fallback is not consulted for a conductor with its own model", () => {
  // The source of the fallback follows the primary. A conductor on its own pair
  // with no fallback of its own has none, even though the scribe has one — which
  // is the same rule the executor applies in `_conductor_targets`.
  const { source } = conductorFallback(pair("openai", "gpt-4o"), pair("", ""), "scribe-backup");
  assert.equal(source, "none");
});

test("half a fallback pair is not a fallback", () => {
  for (const half of [pair("ollama", ""), pair("", "qwen3:8b")]) {
    const { source } = conductorFallback(pair("openai", "gpt-4o"), half, "");
    assert.equal(source, "none", JSON.stringify(half));
  }
});
