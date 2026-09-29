/**
 * The two Ollama-only fields on a role card, and what each one claims to do.
 *
 * Both exist because Ollama does something silently. `num_ctx` truncates a
 * prompt that does not fit its 4096 default and returns a degraded answer
 * rather than an error; `keep_alive` unloads a model five minutes after its
 * last request, so a role used every ten minutes pays a full reload each time.
 * Neither failure announces itself, which is why they are per-role fields at
 * all rather than a global default nobody thinks to look for.
 *
 * The contract pinned here is behavioural, not prose:
 *
 *  - **Both fields are gated on `protocol === "ollama"`.** A window or a
 *    residency control on an OpenAI-compatible provider is not a setting that
 *    does nothing — it is a setting that *looks* like it is doing something,
 *    which is worse, because the user changes it and the behaviour is
 *    identical.
 *
 *  - **An emptied field saves as `null`, not as absent.** The engine treats
 *    these as clearable; sending "no change" instead of "back to the default"
 *    is why a user who deletes the value can see it come straight back.
 *
 *  - **`keep_alive`'s wording says it is about the gap *between* calls.** It
 *    does not make a call faster. A field that implies otherwise gets set
 *    expecting a latency win that never arrives, and then gets blamed.
 *
 * The suite runs with no DOM, so this is the source-as-contract style
 * `staleAuthBanner.test.ts` uses.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const CARD_SRC = readFileSync(
  new URL("../src/components/AgentConfigCard.tsx", import.meta.url),
  "utf8",
);
const TYPES_SRC = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");

/** The JSX for one field, from its guard to the end of its block. */
function ollamaOnlyBlock(label: string): string {
  // Plain substring, not `>Label</label>`: JSX puts the text on its own line
  // with indentation inside the element, so the tight form never matches and a
  // matcher that quietly finds nothing is worse than no matcher.
  const at = CARD_SRC.indexOf(label);
  assert.notEqual(at, -1, `no "${label}" label in the card`);
  // Back up to the `{active.protocol === "ollama" && (` that gates it, so the
  // test reads the field *with* its guard rather than trusting a comment.
  const guard = CARD_SRC.lastIndexOf('{active.protocol === "ollama" && (', at);
  assert.notEqual(guard, -1, `"${label}" is not gated on the ollama protocol`);
  // Whitespace collapsed, so a sentence can be asserted on as a sentence. JSX
  // wraps prose at the indentation, and a matcher that has to know where the
  // author happened to break the line fails the next time they reflow the text
  // without changing a word of it.
  return CARD_SRC.slice(guard, guard + 1600).replace(/\s+/g, " ");
}

test("both Ollama-only fields are gated on the protocol", () => {
  for (const label of ["Context Window", "Keep Alive"]) {
    const block = ollamaOnlyBlock(label);
    assert.match(
      block,
      /active\.protocol === "ollama"/,
      `${label} would be shown on providers that have no equivalent field`,
    );
  }
});

test("each field saves as null when emptied, so a clear is a clear", () => {
  // Absent means "no change" to the merge in AgentRegistryService; null means
  // "back to Ollama's own default". A user who deletes the value must get the
  // second one, and the difference is invisible until the value comes back.
  assert.match(
    CARD_SRC,
    /num_ctx: active\.num_ctx \?\? null/,
    "an emptied window must arrive as null",
  );
  assert.match(
    CARD_SRC,
    /keep_alive: active\.keep_alive\?\.trim\(\) \|\| null/,
    "an emptied keep-alive must arrive as null, and a blank is not a value",
  );
});

test("the keep-alive field says it is about the gap between calls", () => {
  const block = ollamaOnlyBlock("Keep Alive");
  assert.match(
    block,
    /does not make a call faster/i,
    "the field must not imply a per-call speedup, which it does not deliver",
  );
  assert.match(block, /reloaded between goals/i, "or that it addresses the between-goal reload");
});

test("the keep-alive field shows the server's own window when unset", () => {
  const block = ollamaOnlyBlock("Keep Alive");
  assert.match(
    block,
    /server default \(5m\)/,
    "an empty field must state what is actually in force, not read as blank",
  );
});

test("both fields are typed on the config and on the patch", () => {
  // Two interfaces, two declarations. Adding one to only the config type
  // type-checks the card and then fails at runtime the moment a save is
  // attempted, which is the worst place to find out.
  const occurrences = TYPES_SRC.match(/keep_alive\?: string \| null;/g) ?? [];
  assert.equal(occurrences.length, 2, "keep_alive must be on AgentConfig and AgentConfigPatch");
});
