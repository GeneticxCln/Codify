/**
 * A budget on tests that read the app's source as text.
 *
 * A test that greps `App.tsx` for `onClick={handleX}` proves the words are there,
 * not that pressing the control does anything; every one of the bugs found in
 * the 2026-09-29 audit (a motion flag read the wrong way round, a layer that ate
 * every click, queues nothing ever filled, a stale dispatch-table literal) had a
 * source-text test that was green throughout. `appHarness.ts` mounts the whole
 * App against a recording fake engine and shell, so "does it work" no longer
 * needs a regex.
 *
 * This does not ban them: some rules really are about text (a palette with no
 * hard-coded hex, the one spelling of a placeholder, a Rust constant matching a
 * TypeScript one). It stops the pile growing by accident. Adding a file to it is
 * a decision, made by raising SOURCE_TEXT_TEST_BUDGET in the same change with a
 * reason; converting one to a behavioural test lowers the number to match.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

/** Test files that read something under `src/` as text, as of this change. */
const SOURCE_TEXT_TEST_BUDGET = 32;

test("the number of tests that read the app's source as text has not grown", () => {
  const dir = path.dirname(fileURLToPath(import.meta.url));
  const readers = readdirSync(dir)
    .filter((f) => f.endsWith(".test.ts") && f !== path.basename(fileURLToPath(import.meta.url)))
    .filter((f) => {
      const text = readFileSync(path.join(dir, f), "utf8");
      return /readFileSync\(/.test(text) && /\.\.\/src\/|"\.\.", "src"|'\.\.', 'src'/.test(text);
    });
  assert.ok(
    readers.length <= SOURCE_TEXT_TEST_BUDGET,
    `${readers.length} test files read src/ as text, over the budget of ${SOURCE_TEXT_TEST_BUDGET}. ` +
      "Test the behaviour instead: mount the App with tests/appHarness.ts (see appWiring.test.ts) " +
      "and check what the app asked the engine or shell to do. If the rule really is about text, " +
      "raise the budget in this file with the reason.",
  );
  assert.ok(
    readers.length >= SOURCE_TEXT_TEST_BUDGET - 5,
    `only ${readers.length} test files read src/ as text against a budget of ${SOURCE_TEXT_TEST_BUDGET}: ` +
      "lower SOURCE_TEXT_TEST_BUDGET to match, so the ratchet keeps tightening.",
  );
});
