import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// `DESIGN.md` at the repository root and `ui/tailwind.config.js` are two views of
// one decision, and the pairing is load-bearing rather than tidy: the engine
// auto-discovers a root DESIGN.md and makes it the binding design contract for any
// goal run against this repo (docs/04 §4.0a). So a DESIGN.md that disagrees with the
// config is not a documentation nit — it is the contract and the implementation
// disagreeing, which is the exact failure this repository has already shipped once.
//
// Every check below runs in both directions, and the reverse direction is the one
// that earns its keep. A wrong value in DESIGN.md is a document that has lied. An
// *undocumented* token is the quieter failure: a component invents a colour, nothing
// in the contract says what it means, and the six-hues-for-status drift this whole
// exercise existed to fix walks straight back in through the next feature. Neither
// direction is allowed to pass quietly.
//
// The config is parsed as text rather than imported. Importing it would make this
// test's result depend on a bundler-shaped module load, and a test that can fail for
// a reason unrelated to drift is a test that gets disabled.

const HERE = dirname(fileURLToPath(import.meta.url));
const UI_DIR = join(HERE, "..");
const CONFIG_PATH = join(UI_DIR, "tailwind.config.js");
const DESIGN_PATH = join(UI_DIR, "..", "DESIGN.md");

const config = readFileSync(CONFIG_PATH, "utf8");
const design = readFileSync(DESIGN_PATH, "utf8");

/** `| \`name\` | \`value\` | why |` — the shape every token table in DESIGN.md uses. */
const TABLE_ROW = /^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|/gm;

/** Everything from a `### heading` (or `## heading`) up to the next heading. */
function sectionOf(heading: string): string {
  const lines = design.split("\n");
  const start = lines.findIndex((l) => l.trimEnd().startsWith(heading));
  assert.notEqual(
    start,
    -1,
    `DESIGN.md has no section headed ${JSON.stringify(heading)}; the token tables ` +
      "moved and this test no longer knows where to read them",
  );
  const rest = lines.slice(start + 1);
  const end = rest.findIndex((l) => /^#{2,3}\s/.test(l));
  return (end === -1 ? rest : rest.slice(0, end)).join("\n");
}

/** `name -> value` for every token table inside one DESIGN.md section. */
function documentedTokens(heading: string): Map<string, string> {
  const found = new Map<string, string>();
  for (const match of sectionOf(heading).matchAll(TABLE_ROW)) {
    found.set(match[1], match[2]);
  }
  return found;
}

/**
 * The body of one top-level config key, found by matching braces.
 *
 * The first version of this read every `"Npx"` string in the file and called it a
 * radius — which quietly swept in the seven `lineHeight: "14px"` entries nested
 * inside the type ramp, and reported the radius scale as five tokens instead of
 * four. Guessing the block from the shape of a value cannot tell a token from a
 * neighbouring key that happens to share a value shape, so the block is located by
 * its name and its braces instead.
 */
function configBlock(key: string): string {
  const open = config.indexOf(`${key}: {`);
  assert.notEqual(
    open,
    -1,
    `tailwind.config.js has no \`${key}: {\` block. The tokens moved and this test ` +
      "is no longer reading the thing it exists to compare",
  );
  const start = config.indexOf("{", open);
  let depth = 0;
  for (let i = start; i < config.length; i++) {
    if (config[i] === "{") depth++;
    else if (config[i] === "}") {
      depth--;
      if (depth === 0) return config.slice(start + 1, i);
    }
  }
  throw new AssertionError(
    `unbalanced braces after \`${key}: {\` in tailwind.config.js`,
  );
}

/** `name -> value` for every `"name": "value"` pair inside one config block. */
function blockTokens(key: string, value: RegExp, label: string): Map<string, string> {
  const found = new Map<string, string>();
  for (const match of configBlock(key).matchAll(value)) {
    found.set(match[1], match[2]);
  }
  assert.ok(
    found.size > 0,
    `no ${label} parsed out of tailwind.config.js's \`${key}\` block — the value ` +
      "shape changed, so this test would be comparing two empty sets and passing",
  );
  return found;
}

const COLOR = /"?([\w-]+)"?\s*:\s*"(#[0-9a-fA-F]{6})"/g;
const FONT_SIZE = /"?([\w-]+)"?\s*:\s*\["(\d+px)"/g;
const RADIUS = /"?([\w-]+)"?\s*:\s*"(\d+px)"/g;

// The colour sections of DESIGN.md, in the order they appear. All four document
// names inside the same `codify` colour group, so they are compared as one set.
const COLOR_SECTIONS = [
  "### Surfaces",
  "### Text",
  "### Status — five tones, five meanings",
  "### Mode accents — reserved, and each one means one thing",
];

function documentedColors(): Map<string, string> {
  const all = new Map<string, string>();
  for (const heading of COLOR_SECTIONS) {
    for (const [name, value] of documentedTokens(heading)) {
      assert.equal(
        all.get(name),
        undefined,
        `DESIGN.md documents the colour \`${name}\` in two sections; one of them is ` +
          "stale",
      );
      all.set(name, value);
    }
  }
  return all;
}

function assertSameSet(
  documented: Map<string, string>,
  configured: Map<string, string>,
  label: string,
  expectedSize?: number,
): void {
  if (expectedSize !== undefined) {
    assert.equal(
      configured.size,
      expectedSize,
      `${label}: tailwind.config.js defines ${configured.size}, expected ` +
        `${expectedSize}. A token was added or removed without this test being ` +
        "told, so one of the tables below is no longer the whole story",
    );
  }

  for (const [name, value] of documented) {
    assert.ok(
      configured.has(name),
      `DESIGN.md documents the ${label} \`${name}\`, which tailwind.config.js does ` +
        "not define. Either the doc is describing a token that no longer exists, or " +
        "the token was renamed without the doc being updated",
    );
    assert.equal(
      configured.get(name),
      value,
      `DESIGN.md says ${label} \`${name}\` is ${value}, but tailwind.config.js says ` +
        `${configured.get(name)}. The document is binding on goals run against this ` +
        "repo, so a fixer would be held to a value the build does not use",
    );
  }

  // The direction that catches a new component inventing a colour.
  for (const [name, value] of configured) {
    assert.ok(
      documented.has(name),
      `tailwind.config.js defines the ${label} \`${name}\` (${value}), which ` +
        "DESIGN.md does not document. An undocumented token is a decision nobody " +
        "made, and it is how this palette ended up with six hues for one job in the " +
        "first place",
    );
  }
}

test("the colour tokens in DESIGN.md are the colour tokens in the config", () => {
  assertSameSet(
    documentedColors(),
    blockTokens("colors", COLOR, "colour"),
    "colour",
    16, // bg, surface, raised, border, border-strong, primary, secondary, muted,
        // accent, info, success, warning, danger, neutral, design, knowledge
  );
});

test("the CSS custom properties agree with the config too", () => {
  // `index.css` needs the palette for the handful of places CSS rather than a
  // utility does: the body, the scrollbars, the audit-flash keyframes. That is a
  // real third copy, and leaving it unpinned would have moved the drift rather than
  // removed it — the hexes would simply live somewhere a test never looked.
  const css = readFileSync(join(UI_DIR, "src", "index.css"), "utf8");
  const root = css.slice(css.indexOf(":root"), css.indexOf("}"));
  const declared = new Map<string, string>();
  for (const match of root.matchAll(/--codify-([\w-]+)\s*:\s*(#[0-9a-fA-F]{6})/g)) {
    declared.set(match[1], match[2]);
  }
  assert.ok(
    declared.size > 0,
    "index.css has no --codify-* custom properties. Either the palette was removed " +
      "from CSS, or a colour was reintroduced as a bare hex where a property belongs",
  );

  const configured = blockTokens("colors", COLOR, "colour");
  for (const [name, value] of declared) {
    assert.equal(
      configured.get(name),
      value,
      `index.css sets --codify-${name} to ${value}, but tailwind.config.js says ` +
        `${configured.get(name)}. A scrollbar in one colour and a border in another ` +
        "is the kind of drift nobody notices until it is on screen",
    );
  }
  // Every surface/text token the CSS actually uses must be a property, so a bare hex
  // in the body or the scrollbars is a finding rather than a new colour.
  for (const match of css.matchAll(/(?:background-color|color|background)\s*:\s*(#[0-9a-fA-F]{6})/g)) {
    const hex = match[1];
    const owner = [...declared.values()];
    assert.ok(
      owner.includes(hex),
      `index.css sets a colour to the bare hex ${hex}, which is not one of the ` +
        "--codify-* properties. Use the property so the value has one owner",
    );
  }
});

test("the type ramp in DESIGN.md is the type ramp in the config", () => {
  const configured = blockTokens("fontSize", FONT_SIZE, "type step");
  // `2xs` exists only because Tailwind's smallest is already too large for a
  // 10px badge; the rest keep their stock names, at stock names' new sizes.
  assertSameSet(documentedTokens("## 3. Type"), configured, "type step", 7);
  assert.equal(
    configured.get("2xs"),
    "10px",
    "`2xs` is the reason this ramp exists at all — a 10px badge is the single most " +
      "common piece of meta text in this UI, and Tailwind's smallest step is 12px",
  );
});

test("the radius scale in DESIGN.md is the radius scale in the config", () => {
  assertSameSet(
    documentedTokens("## 4. Spacing and radius"),
    blockTokens("borderRadius", RADIUS, "radius"),
    "radius",
    4,
  );
});

test("DESIGN.md keeps the precedence clause the engine depends on", () => {
  // The engine only auto-discovers a DESIGN.md at the workspace *root*. A file that
  // silently moved to ui/ would stop being binding on anything, while still looking
  // like documentation in the right place to whoever opened the folder.
  assert.ok(
    design.includes("Binding for any goal run against this repository"),
    "DESIGN.md no longer states that it is binding. It is only binding because the " +
      "engine finds a root DESIGN.md, so that link has to be stated, not assumed",
  );
  assert.match(
    design,
    /designTokens\.test\.ts/,
    "DESIGN.md does not name the test that keeps it true, so a reader who has to " +
      "change a token has no way to learn that changing it requires changing two files",
  );
});
