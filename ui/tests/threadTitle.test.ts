/**
 * What a thread is called, and where that name comes from.
 *
 * The tab-per-thread change made this load-bearing. A tab per thread with no
 * name per thread is several tabs all labelled "New chat", and a user who opens
 * one cannot tell it from the others — which reads, from the outside, exactly
 * like the click doing nothing. So the rule that a thread's first prompt names
 * it is a correctness rule now, not a nicety, and it is tested like one.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const {
  UNTITLED_THREAD_TITLE,
  THREAD_TITLE_LIMIT,
  threadTitleFromPrompt,
} = await import("../src/threadTitle.ts");

test("a thread with no name is called the same thing everywhere", () => {
  // One constant, three surfaces: the tab strip, the side panel, and this
  // fallback. Two literals called "New chat" is a tab and the row above it
  // disagreeing about what a thread is called, and the user cannot tell which
  // of them is lying.
  assert.equal(UNTITLED_THREAD_TITLE, "New chat");
});

test("a short prompt is its own name", () => {
  assert.equal(
    threadTitleFromPrompt("Add an index"),
    "Add an index"
  );
});

test("a pasted prompt is collapsed onto one line", () => {
  // A prompt pasted with newlines would otherwise put four words on four lines
  // of a one-line tab, and the tab is 224px wide.
  assert.equal(
    threadTitleFromPrompt("Add an index\n\nto the goals table\n  for ordering"),
    "Add an index to the goals table for ordering"
  );
  assert.equal(
    threadTitleFromPrompt("   Fix    the\t flaky test   "),
    "Fix the flaky test"
  );
});

test("a long prompt is cut at a word, not mid-word", () => {
  // The whole reason this function exists rather than a `.slice()`: a name
  // ending in "so every row h" is a name you read twice.
  const prompt =
    "Add a migration for the users table so every row has a workspace id";
  const title = threadTitleFromPrompt(prompt);
  assert.ok(title.length <= THREAD_TITLE_LIMIT, `${title.length} chars`);
  assert.ok(!title.endsWith(" "), "and it does not end in a space");
  // Every word in the name is a whole word of the prompt.
  const words = prompt.split(" ");
  for (const word of title.split(" ")) {
    assert.ok(words.includes(word), `"${word}" is not a word in the prompt`);
  }
  assert.ok(prompt.startsWith(title), "and it is a prefix, not a rearrangement");
});

test("a prompt with no word boundary in range is cut hard", () => {
  // A base64 blob or a stack trace: there is no space to cut at, and refusing
  // to cut would put an unbounded string in a tab.
  const blob = "x".repeat(200);
  const title = threadTitleFromPrompt(blob);
  assert.equal(title.length, THREAD_TITLE_LIMIT);
});

test("an empty prompt falls back rather than naming a thread nothing", () => {
  // `""` is not a name, and this value is about to be shown in a tab and a row.
  assert.equal(threadTitleFromPrompt(""), UNTITLED_THREAD_TITLE);
  assert.equal(threadTitleFromPrompt("   \n\t "), UNTITLED_THREAD_TITLE);
});

test("a name at exactly the limit is left whole", () => {
  // An off-by-one here truncates a title that fitted, which is a name changing
  // for no reason between one send and the next.
  const exact = "a".repeat(THREAD_TITLE_LIMIT);
  assert.equal(threadTitleFromPrompt(exact), exact);
  const over = "a".repeat(THREAD_TITLE_LIMIT + 1);
  assert.equal(threadTitleFromPrompt(over).length, THREAD_TITLE_LIMIT);
});

test("the limit is a parameter, so a caller can ask for less", () => {
  // The rule is "cut at a word", and a caller with a narrower place to put the
  // name needs the same rule at a different width rather than its own slicing.
  assert.equal(threadTitleFromPrompt("Add an index to the goals table", 12), "Add an index");
});
