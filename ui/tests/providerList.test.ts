import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Which providers the settings screen offers is the engine's decision, not that
// screen's.
//
// `engine/models.py` `BUILTIN_PROVIDERS` is the one place the list is defined and
// `GET /settings/providers` serves it — so `api.ts` grew a `fetchProviders` client
// and nothing ever called it. Meanwhile two components carried their own copy of
// five slugs. The copy was not cosmetic: `AgentConfigCard` asks "is this a custom
// provider?" to decide whether to show a protocol picker and a base-URL box, and
// the answer for a provider the engine ships must be no. With `openrouter` and
// `groq` missing from the copy, both were treated as custom endpoints — a form
// asking the user for a protocol and an endpoint the engine already knew, on a
// provider that was otherwise working fine.
//
// So this asserts the *absence* of the copy, in the direction that matters. A
// component cannot list providers, because a component cannot know when the engine
// gains one. The prop being required is the part that actually holds: TypeScript
// then refuses to build a `ProviderSelect` that was not handed the list, which is
// what stops the duplicate from quietly returning.
//
// The engine is parsed as text, not imported, for the reason `designTokens.test.ts`
// gives: a test that can fail for a reason unrelated to drift is a test that gets
// disabled.

const HERE = dirname(fileURLToPath(import.meta.url));
const UI_DIR = join(HERE, "..");
const COMPONENTS = join(UI_DIR, "src", "components");
const MODELS_PY = join(UI_DIR, "..", "engine", "models.py");

const modelsPy = readFileSync(MODELS_PY, "utf8");

/** The slugs in `BUILTIN_PROVIDERS`, read from the engine's own source. */
function engineBuiltinSlugs(): string[] {
  const start = modelsPy.indexOf("BUILTIN_PROVIDERS: dict");
  assert.notEqual(start, -1, "engine/models.py no longer declares BUILTIN_PROVIDERS");
  const body = modelsPy.slice(start);
  const end = body.indexOf("\n}\n");
  const table = body.slice(0, end === -1 ? body.length : end);
  return [...table.matchAll(/^ {4}"([a-z0-9_]+)": \{/gm)].map((m) => m[1]);
}

/** A slug array literal written out in a component, e.g. `["a", "b"]`. */

test("the engine's provider list is readable, and is not empty", () => {
  const slugs = engineBuiltinSlugs();
  // Guards this test from passing vacuously: if the parse ever stops matching,
  // every "no hardcoded list" assertion below would hold for the wrong reason.
  assert.ok(slugs.length >= 5, `parsed only ${slugs.length} builtin slug(s) from the engine`);
  assert.ok(
    slugs.includes("anthropic") && slugs.includes("ollama"),
    `the engine's own builtins did not parse: ${JSON.stringify(slugs)}`,
  );
});

test("no component hardcodes a list of provider slugs", () => {
  // The known slugs are what a stale copy is made of. A literal made of them is
  // the copy; the `<option value="anthropic">` protocol menu is a different thing
  // (it names a protocol, once) and is deliberately not matched.
  const slugs = engineBuiltinSlugs();
  const pattern = new RegExp(
    `\\[\\s*(?:"(?:${slugs.join("|")})"\\s*,?\\s*){2,}\\]`,
    "g",
  );

  const offenders: string[] = [];
  for (const file of ["ProviderSelect.tsx", "AgentConfigCard.tsx", "SettingsPanel.tsx"]) {
    const text = readFileSync(join(COMPONENTS, file), "utf8");
    for (const m of text.matchAll(pattern)) {
      offenders.push(`${file}: ${m[0].replace(/\s+/g, " ")}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    "a component is carrying its own copy of BUILTIN_PROVIDERS; the engine owns " +
      "that list and GET /settings/providers serves it",
  );
});

test("ProviderSelect requires the list instead of defaulting to one", () => {
  const text = readFileSync(join(COMPONENTS, "ProviderSelect.tsx"), "utf8");
  assert.match(
    text,
    /builtins:\s*string\[\];/,
    "the `builtins` prop must be required — a default is a second copy waiting to " +
      "go stale, and it is what let openrouter and groq disappear from this screen",
  );
  assert.doesNotMatch(
    text,
    /builtins\s*=\s*\[/,
    "ProviderSelect must not supply a default provider list",
  );
  // And the whole point of the list: every shipped provider gets an option.
  assert.match(text, /builtins\.map\(/, "the engine's list must be what renders the options");
});

test("the provider list the screen renders is the one the engine served", () => {
  const panel = readFileSync(join(COMPONENTS, "SettingsPanel.tsx"), "utf8");
  assert.match(
    panel,
    /fetchProviders\(\)/,
    "the panel must read the engine's provider list rather than assume one",
  );
  assert.match(
    panel,
    /catalog\.builtins\.map\(\(b\)\s*=>\s*b\.slug\)/,
    "the slugs handed to the cards must come from the engine's response",
  );
  // A provider the engine ships must not be reachable only through the custom box.
  const card = readFileSync(join(COMPONENTS, "AgentConfigCard.tsx"), "utf8");
  assert.match(
    card,
    /isCustomProvider\s*=\s*!builtinProviders\.includes\(/,
    "custom-vs-builtin must be decided from the engine's list",
  );
  assert.doesNotMatch(
    card,
    /new Set\(\s*"(?:anthropic|openai)"/,
    "AgentConfigCard must not carry its own copy of BUILTIN_PROVIDERS",
  );
});

// Guards the stale-copy bug from coming back through the fallback path: the
// endpoint box is what a user fills in to *repair* a mis-set provider, so it is
// the field most likely to be shown to someone who did not need to touch it.
test("the endpoint box is still gated on the engine's list", () => {
  const card = readFileSync(join(COMPONENTS, "AgentConfigCard.tsx"), "utf8");
  assert.match(card, /isOllama \|\| isCustomProvider/);
  assert.match(card, /fallbackCustom/);
});
