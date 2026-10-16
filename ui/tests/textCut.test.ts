/**
 * A cut string never ends on the first half of a character.
 *
 * `slice(0, n)` counts UTF-16 units, so a cut between the two units of an emoji leaves a lone high surrogate. The
 * engine refuses that in a request body (422, `string_unicode`), every time, because the same prompt cuts at the
 * same place: a thread whose first prompt had an emoji at its 60th unit could not be created.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { clipHead } = await import("../src/textCut.ts");
const { threadTitleFromPrompt, THREAD_TITLE_LIMIT } = await import("../src/threadTitle.ts");
const { clipForSpeech } = await import("../src/speech.ts");

const GRIN = "\u{1F600}"; // two UTF-16 units

/** A high surrogate with no low one after it, or a low one with no high one before it. */
function hasLoneSurrogate(text: string): boolean {
  return /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/.test(text);
}

test("a cut that would split an emoji stops one unit short, and a cut after it keeps it", () => {
  const text = "a".repeat(5) + GRIN + "tail";
  assert.equal(clipHead(text, 6), "aaaaa", "the limit lands between the two halves");
  assert.equal(clipHead(text, 7), "aaaaa" + GRIN, "the limit lands after both");
  assert.equal(clipHead(text, 5), "aaaaa");
  assert.equal(clipHead(text, 0), "");
});

test("text within the limit is returned as it is", () => {
  assert.equal(clipHead("short", 60), "short");
  assert.equal(clipHead(GRIN, 2), GRIN);
});

test("a thread named from a prompt with an emoji at the limit is a string the engine accepts", () => {
  // 59 units of one long word (no space to cut back to), then an emoji straddling unit 60.
  const prompt = "x".repeat(THREAD_TITLE_LIMIT - 1) + GRIN + " and then some more words";
  const title = threadTitleFromPrompt(prompt);
  assert.ok(!hasLoneSurrogate(title), JSON.stringify(title));
  assert.ok(title.length <= THREAD_TITLE_LIMIT);
  assert.equal(title, "x".repeat(THREAD_TITLE_LIMIT - 1));
});

test("a title cut back to a space is unaffected", () => {
  // The same rule as before for ordinary text: the cut falls back to the last whole word.
  assert.equal(threadTitleFromPrompt("Add an index to the goals table", 12), "Add an index");
});

test("read-aloud text is not cut inside an emoji", () => {
  const text = "y".repeat(9) + GRIN + "z".repeat(40);
  const clipped = clipForSpeech(text, 10);
  assert.ok(!hasLoneSurrogate(clipped), JSON.stringify(clipped));
});
