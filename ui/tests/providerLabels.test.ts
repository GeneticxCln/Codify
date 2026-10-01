/**
 * How a provider and a protocol are read on screen, and that the slug stays the identifier.
 *
 * The labels are display only. These tests hold the two halves of that: the words people read are the
 * brands' own spellings (and a slug nobody listed is made readable and no more), and nothing about a
 * provider's identity is decided by the table.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { providerLabel, protocolLabel, PROTOCOL_LABELS } from "../src/providerLabels.ts";

test("the providers the app ships are written as their brands write them", () => {
  assert.deepEqual(
    ["openai", "openrouter", "deepseek", "nvidia", "groq", "google", "anthropic", "ollama"].map(providerLabel),
    ["OpenAI", "OpenRouter", "DeepSeek", "NVIDIA", "Groq", "Google", "Anthropic", "Ollama"],
  );
});

test("a slug is matched however it is cased or padded, since the engine lowercases", () => {
  assert.equal(providerLabel("OpenAI"), "OpenAI");
  assert.equal(providerLabel("  NVIDIA "), "NVIDIA");
  assert.equal(providerLabel("OPENROUTER"), "OpenRouter");
});

test("a provider nobody listed is shown as written, title-cased on its separators", () => {
  assert.equal(providerLabel("my-gateway"), "My Gateway");
  assert.equal(providerLabel("together_ai"), "Together Ai");
  assert.equal(providerLabel("local llm"), "Local Llm");
  assert.equal(providerLabel("togetherai"), "Togetherai");
  assert.equal(providerLabel("a--b__c"), "A B C");
});

test("an unset provider is not a name", () => {
  assert.equal(providerLabel(""), "");
  assert.equal(providerLabel("   "), "");
});

test("the table is a spelling guide and never a catalogue: no unknown slug is refused or invented", () => {
  // Whatever the engine reports gets a label; nothing here decides which providers exist.
  for (const slug of ["x", "acme-corp", "123", "ünïcode"]) {
    assert.ok(providerLabel(slug).length > 0, `${slug} got no label`);
  }
});

test("a protocol has a word for a row and the word plus its endpoint for the choice list", () => {
  assert.equal(protocolLabel("openai_compat"), "OpenAI-compatible");
  assert.equal(protocolLabel("openai_compat", "long"), "OpenAI Compatible (chat/completions)");
  assert.equal(protocolLabel("anthropic", "long"), "Anthropic (v1/messages)");
  assert.equal(protocolLabel("google"), "Google Gemini");
  assert.equal(protocolLabel("ollama", "long"), "Ollama (api/generate)");
});

test("a protocol tag the table does not know is shown as the engine sent it", () => {
  assert.equal(protocolLabel("some_new_protocol"), "some_new_protocol");
  assert.equal(protocolLabel("", "long"), "");
});

test("every protocol the engine can send has a label", () => {
  assert.deepEqual(Object.keys(PROTOCOL_LABELS).sort(), ["anthropic", "google", "ollama", "openai_compat"]);
});
