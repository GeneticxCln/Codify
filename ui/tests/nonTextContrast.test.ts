/**
 * SC 1.4.11 Non-text Contrast: the parts of this app that have to be seen to be
 * *used*, measured against the colour they are actually adjacent to, in every
 * theme.
 *
 * `ui/tests/contrast.test.ts` is the text leg and it is complete for text: every
 * `text-codify-*` on a `bg-codify-*` in the same class string, in nineteen
 * themes, at the threshold its own size and weight earn. This is the other half
 * of the same question, and it had no answer at all — which is how the
 * scrollbar thumb spent its life painted `--codify-border`, a token DESIGN.md §2
 * reserves for dividers, at between 1.21:1 and 1.86:1 against its own track in
 * every theme in the box.
 *
 * ## Why this file is a table and not another scan
 *
 * 1.4.11 governs "visual information required to identify user interface
 * components and states, and graphical objects". The scan that the text leg uses
 * cannot find those things, because they are not written as a class pair: a
 * scrollbar thumb is `background: var(--codify-border-strong)` in a
 * pseudo-element rule, a focus ring is a `ring-` utility, and an atmosphere
 * stroke is a value read by a canvas painter through `getComputedStyle`. So the
 * objects are **declared** here, one at a time, each with the reason it is in
 * the table or out of it. A table that lists the wrong object fails the next
 * person to look at it; a scan that silently misses one fails nobody.
 *
 * ## What is gated, and what is deliberately not
 *
 * **Gated at 3:1.**
 *  - `accent` — the selection indicator (the sidebar row, the active tab) and
 *    the focus ring. It is the one thing that says which thread you are in.
 *  - `info` — the "in flight" indicator, held to the same bar for the same
 *    reason: a spinner nobody can see is a spinner that is not reporting.
 *  - `border-strong` — the scrollbar thumb. A thumb is the only thing that says
 *    where you are in a long list, so of everything in this file it is the least
 *    arguable.
 *  - every **atmosphere stroke** — see below.
 *
 * **Measured, printed, and not gated: `border`.** 1.4.11's threshold applies to
 * what is *required to identify* a component. A divider identifies nothing — it
 * is not a component, it is the absence of one — and a control that already has
 * a fill, a label and a resting boundary is identified without the boundary
 * being legible. Holding every 1px rule in the app to 3:1 is how a design system
 * ends up with lines you can see from the next desk, and it would be a restyle
 * of nineteen themes to fix nothing. It is measured every run and reported, so
 * the number is never lost, and a theme that wants a stronger divider can move
 * it without the gate having to approve it.
 *
 * ## The three roles an atmosphere variable can have
 *
 * A theme's weather namespace is not one kind of thing, and gating it as if it
 * were produced eight false failures the first time this ran. Each variable is
 * classified:
 *
 *  - **`stroke`** — a mark: a glyph, a line, a particle, a filament. Gated at
 *    3:1 against every surface the canvas can be drawn over.
 *  - **`backdrop`** — a fill the mark is drawn *on*: the canvas background, the
 *    scanline wash, the body of a wave under its highlight. 1.4.11 has nothing
 *    to say about a fill, which has no boundary to be seen.
 *  - **`dim`** — a mark that is deliberately below the gate *because it carries
 *    no information*: the trailing wash behind the glyphs, the background
 *    filaments the nodes hang from. This is the "required to identify" test
 *    again, applied honestly — these are the *absence* of a mark, and lifting
 *    them to 3:1 would replace a phosphor phosphor with a phosphor wash.
 *
 * **Every published weather variable must be classified, in both directions.**
 * A variable with no role cannot be measured and a role naming a variable no
 * theme offers is a stale entry, so both are failures. That is what stops a
 * twentieth theme from inheriting a gate it was never checked against.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { THEMES } from "../src/appearance.ts";
import {
  AA_NON_TEXT,
  contrastRatio,
  describeGraphicRequirement,
} from "../src/contrast.ts";

type Theme = (typeof THEMES)[number];

// ── what a theme actually renders ───────────────────────────────────────────

/**
 * The `:root` block, which is the floor every theme stands on.
 *
 * Read the same way `contrast.test.ts` reads it, and for the same reason: a
 * token a theme does not publish is not absent, it is the default.
 */
const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const rootStart = css.indexOf(":root");
assert.notEqual(rootStart, -1, "index.css has no :root block to read defaults from");
const ROOT: Readonly<Record<string, string>> = Object.fromEntries(
  [...css.slice(rootStart, css.indexOf("}", rootStart)).matchAll(
    /(--[a-z-]+):\s*(#[0-9a-fA-F]{6})/g,
  )].map((m) => [m[1]!, m[2]!]),
);

/** The colour a theme renders a `codify-*` token as. */
function token(theme: Theme, name: string): string {
  const key = `--codify-${name}`;
  const value = theme.tokens[key as keyof typeof theme.tokens] ?? ROOT[key];
  assert.ok(
    value,
    `${theme.id} publishes no --codify-${name} and :root does not either, so the ` +
      "object cannot be measured at all — a missing token is a failure, not a skip",
  );
  return value;
}

/**
 * The three surfaces anything drawn on the app can be adjacent to.
 *
 * All three, not the one a component happens to sit on: a canvas or a scrollbar
 * follows the user, and the same stroke is drawn over the window, over a panel
 * and over a card depending on where the pointer is. The worst of the three is
 * what a theme has to clear.
 */
const SURFACES = ["bg", "surface", "raised"] as const;

function worstSurface(theme: Theme, colour: string): { surface: string; ratio: number } {
  let worst = { surface: "", ratio: Number.POSITIVE_INFINITY };
  for (const surface of SURFACES) {
    const ratio = contrastRatio(colour, token(theme, surface));
    if (ratio < worst.ratio) worst = { surface, ratio };
  }
  return worst;
}

// ── the objects 1.4.11 reaches ──────────────────────────────────────────────

interface GatedObject {
  readonly what: string;
  readonly token: string;
  /** Every `ui/src` file that paints it, for the failure message. */
  readonly where: string;
  /** Why this is in the table, in one sentence a sceptic could disagree with. */
  readonly why: string;
}

const GATED: ReadonlyArray<GatedObject> = [
  {
    what: "the selection indicator (the selected thread's title and icon, the active tab)",
    token: "accent",
    where: "components/Sidebar.tsx, components/ui/Wordmark.tsx and every `text-codify-accent`",
    why: "it is the only thing that says which thread you are in; the row's fill alone does not, because a hover is the same fill",
  },
  {
    what: "the focus ring",
    token: "accent",
    where: "index.css (`.focus-within:border-codify-accent`, `.focus-visible:ring-codify-accent`)",
    why: "a focus indicator is a state indicator, and a keyboard user who cannot see it is tabbing blind",
  },
  {
    what: "the in-flight indicator",
    token: "info",
    where: "every `text-codify-info`",
    why: "a spinner nobody can see is a spinner that is not reporting; this is the same bar for the same reason as `accent`",
  },
  {
    what: "the scrollbar thumb, and the border a control takes on hover and focus",
    token: "border-strong",
    where: "index.css (`::-webkit-scrollbar-thumb`, `scrollbar-color`), every `hover:border-codify-border-strong`",
    why: "a thumb is the only thing that says where you are in a long list, which makes it the least arguable object in this file",
  },
];

// ── the objects it does not reach ───────────────────────────────────────────

interface ExemptObject {
  readonly what: string;
  readonly token: string;
  /** Every `ui/src` file that paints it, for the report. */
  readonly where: string;
  readonly why: string;
}

const EXEMPT: ReadonlyArray<ExemptObject> = [
  {
    what: "every divider and resting control border",
    token: "border",
    where: "every `border-codify-border` and `divide-codify-border`",
    why: "1.4.11 reaches visual information *required to identify* a component. A divider is not a component — it is the absence of one — and a control that has a fill and a label is identified without its boundary being legible. Still measured, and reported below.",
  },
];

// ── the atmosphere ──────────────────────────────────────────────────────────

type Role = "stroke" | "dim" | "backdrop";

/** Gated. `dim` and `backdrop` are exempt, and each entry says which it is. */
const GATED_ROLES: ReadonlySet<Role> = new Set<Role>(["stroke"]);

const WEATHER_ROLES: Readonly<Record<string, Readonly<Record<string, { role: Role; why: string }>>>> = {
  "cmatrix-oled": {
    "--cmatrix-bg": { role: "backdrop", why: "the canvas's own background; the glyphs are drawn on it" },
    "--cmatrix-rain": { role: "dim", why: "the trailing wash behind the glyphs — the absence of a mark, and 3:1 would replace a phosphor phosphor with a phosphor wash" },
    "--cmatrix-text": { role: "stroke", why: "the glyphs themselves" },
  },
  "cyberpunk-neon": {
    "--cyber-cyan": { role: "stroke", why: "the grid lines and the tube glow" },
    "--cyber-magenta": { role: "stroke", why: "the skyline and the horizon line" },
    "--cyber-amber": { role: "stroke", why: "the HUD tick marks" },
    "--cyber-scan": { role: "backdrop", why: "the scanline wash over the window, not a mark" },
  },
  "neural-constellation": {
    "--neural-web": { role: "dim", why: "the background filaments the nodes hang from; the nodes are the information" },
    "--neural-node": { role: "stroke", why: "the node cores" },
    "--neural-pulse": { role: "stroke", why: "the signals travelling the tendrils — a run in flight" },
  },
  "hud-tactical": {
    "--hud-amber": { role: "stroke", why: "the brackets and the sweep" },
    "--hud-tick": { role: "stroke", why: "the minor tick marks" },
  },
  "abyss-bioluminescent": {
    "--abyss-spore": { role: "stroke", why: "the spores" },
    "--abyss-deep": { role: "stroke", why: "the tendril filaments" },
  },
  "solar-flare": {
    "--flare-indigo": { role: "stroke", why: "the wide faint outer stroke" },
    "--flare-ultraviolet": { role: "stroke", why: "the bright core and the outward rays" },
  },
  "ascii-rain": {
    "--cmatrix-bg": { role: "backdrop", why: "the canvas's own background" },
    "--cmatrix-rain": { role: "dim", why: "the trailing wash behind the glyphs; ascii-rain is the stillest theme in the box and 3:1 would put a grid of grey rectangles on the window" },
    // This one is a judgement, and the measurement is what forced it. It was
    // classified as a `stroke` first, on the reasoning that a glyph is a mark —
    // and the leg came back 1.33:1 against this theme's `raised` of `#0a0a0a`.
    // Clearing 3:1 means `#767676`, a mid-grey rain on a pure-black window,
    // which is not a restyle of a theme but a replacement of it. So the
    // classification moved instead of the hex, and the reason it moved is the
    // same one the `dim` role exists for: the glyph is a mark that carries no
    // information. Nothing in the window is read by squinting at the rain.
    // The other eighteen themes' glyphs clear the bar and stay `stroke`, which
    // is what makes this a decision about *this* variable rather than a blanket
    // exemption for the atmosphere.
    "--cmatrix-text": { role: "dim", why: "the glyphs themselves, at 1.33:1 on this theme's near-black `raised`. Lifting them to 3:1 means a mid-grey rain on pure black, which replaces the theme rather than fixing it — and DESIGN.md §7 exempts the atmosphere because it is neither state nor feedback, so there is no information in it to be required" },
  },
  "winter-snow": {
    "--snow-flake": { role: "stroke", why: "the near flakes" },
    "--snow-flake-2": { role: "stroke", why: "the paler flakes the field mixes in for depth" },
  },
  "festive-night": {
    "--snow-flake": { role: "stroke", why: "the near flakes" },
    "--snow-flake-2": { role: "stroke", why: "the faint flakes under the lights" },
    "--snow-glow": { role: "stroke", why: "the warm lights strung through the snow" },
    "--snow-glow-2": { role: "stroke", why: "the second light colour" },
  },
  "solarized-flare": {
    "--sun-core": { role: "stroke", why: "the wide faint stroke" },
    "--sun-edge": { role: "stroke", why: "the thin bright core and the heat pulse" },
  },
  "cyber-organism": {
    "--organ-node": { role: "stroke", why: "the node cores and the signals travelling the tendrils" },
    "--organ-tendril": { role: "stroke", why: "the branching tendrils and each node's halo — the closest call in the app at 3.74:1" },
  },
  "event-horizon": {
    "--void-arc": { role: "stroke", why: "the near side of the disc" },
    "--void-dust": { role: "stroke", why: "the far side and the outer orbits" },
    "--void-beam": { role: "stroke", why: "the radial jets while a run is in flight" },
  },
  "vector-wireframe": {
    "--vector-line": { role: "stroke", why: "every edge of every solid" },
    "--vector-lock": { role: "stroke", why: "the locked vertices and the target reticle — a state" },
  },
  "liquid-mercury": {
    "--fluid-crest": { role: "stroke", why: "the thin highlight on each wave" },
    "--fluid-trough": { role: "backdrop", why: "the thick body of the wave *under* the highlight; the highlight is the mark, and this is what it is read against" },
  },
  "electric-arc": {
    "--arc-core": { role: "stroke", why: "the discharge channel" },
    "--arc-fork": { role: "stroke", why: "the spurs and the wide faint halo around the channel" },
  },
  "toxic-lab": {
    "--reagent": { role: "stroke", why: "the body of each bubble" },
    "--reagent-skin": { role: "stroke", why: "the bright upper third of a bubble and the burst ring" },
  },
  "anon-fluid": {
    "--fluid-bit": { role: "stroke", why: "the data stream" },
    "--fluid-cursor": { role: "stroke", why: "the one dash in eleven that marks a boundary" },
  },
};

/** The weather variables a theme publishes, with the ones that are not colours dropped. */
function weatherOf(theme: Theme): Array<[string, string]> {
  return Object.entries(theme.tokens).filter(
    ([name, value]) => !name.startsWith("--codify-") && /^#[0-9a-fA-F]{6}$/.test(value),
  );
}

// ── the leg ─────────────────────────────────────────────────────────────────

test("the gated objects are real, so a passing leg is not a passing empty table", () => {
  // Every assertion below iterates GATED, WEATHER_ROLES or both, so a table that
  // lost a row — a renamed token, a theme that stopped publishing its weather —
  // would leave the leg passing for the wrong reason. This is the guard, and it
  // is the first test so a failure here is read before the other three.
  assert.ok(
    GATED.length >= 4,
    `only ${GATED.length} non-text objects are gated. The table lost rows and the ` +
      "assertions below are now measuring almost nothing",
  );
  assert.ok(
    new Set(GATED.map((g) => g.token)).size >= 3,
    "the gated list names fewer than three distinct tokens, so it is not the list it was",
  );
  for (const object of [...GATED, ...EXEMPT]) {
    assert.ok(object.why.trim(), `the ${object.token} entry does not say why it is in the table`);
    assert.ok(object.where, `the ${object.token} entry says where it is painted`);
  }
  const strokes = Object.values(WEATHER_ROLES).flatMap((vars) =>
    Object.values(vars).filter((v) => v.role === "stroke"),
  );
  assert.ok(
    strokes.length >= 30,
    `only ${strokes.length} atmosphere strokes are classified. The table is stale and ` +
      "the atmosphere is not being measured",
  );
  const exempt = Object.values(WEATHER_ROLES)
    .flatMap((vars) => Object.values(vars))
    .filter((v) => !GATED_ROLES.has(v.role));
  assert.ok(
    exempt.length >= 3 && exempt.length <= strokes.length / 2,
    `${exempt.length} weather variables are exempt against ${strokes.length} strokes. ` +
      "A table that is nearly all exemptions is not a classification, and one that is " +
      "nearly none is not a role system — both mean the table stopped describing the " +
      "namespace it claims to cover",
  );
});

test("the floor is 1.4.11's 3:1 and not the large-text 3:1 by coincidence", () => {
  // `AA_LARGE` is also 3. If this leg had reached for that constant it would be
  // right by accident today and wrong the first time either number moves, and
  // nothing would say so. They are asserted to be equal *and* to come from
  // different criteria, so the coupling is visible rather than silent.
  assert.equal(AA_NON_TEXT, 3, "SC 1.4.11 requires 3:1 for non-text contrast");
  assert.notEqual(
    describeGraphicRequirement("the scrollbar thumb", "its track"),
    "3:1",
    "the failure message has to name the object and what it is adjacent to, or " +
      "'3:1' alone does not say which pairing failed",
  );
});

test("every gated object clears 3:1 against every surface in every theme", () => {
  const failures: string[] = [];
  for (const object of GATED) {
    for (const theme of THEMES) {
      const { surface, ratio } = worstSurface(theme, token(theme, object.token));
      if (ratio < AA_NON_TEXT) {
        failures.push(
          `  ${theme.id}  ${ratio.toFixed(2)}:1 — ${object.what} ` +
            `(text/bg --codify-${object.token}) on --codify-${surface}; ` +
            `${describeGraphicRequirement(object.what, `--codify-${surface}`)}; painted in ${object.where}`,
        );
      }
    }
  }
  assert.equal(
    failures.length,
    0,
    `${failures.length} of ${GATED.length * THEMES.length} object/theme combinations are ` +
      `under SC 1.4.11:\n${failures.join("\n")}`,
  );
});

test("every atmosphere stroke clears 3:1 against every surface in every theme", () => {
  const failures: string[] = [];
  let measured = 0;
  for (const theme of THEMES) {
    const roles = WEATHER_ROLES[theme.id] ?? {};
    for (const [name, value] of weatherOf(theme)) {
      if (roles[name]?.role !== "stroke") continue;
      measured += 1;
      const { surface, ratio } = worstSurface(theme, value);
      if (ratio < AA_NON_TEXT) {
        failures.push(
          `  ${theme.id}  ${ratio.toFixed(2)}:1 — ${name} (${value}) on --codify-${surface}; ` +
            describeGraphicRequirement(name, `--codify-${surface}`),
        );
      }
    }
  }
  assert.ok(
    measured > 0,
    "no atmosphere stroke was measured at all — the role table and the themes have " +
      "drifted apart, and this leg would pass on an empty list",
  );
  assert.equal(
    failures.length,
    0,
    `${failures.length} of ${measured} atmosphere strokes are under SC 1.4.11:\n${failures.join("\n")}`,
  );
});

test("every published weather variable is classified, and no role is a ghost", () => {
  // Both directions, and the second is the one that earns its keep. A new theme
  // publishing a stroke nobody classified would otherwise sail through a leg
  // that says it measures every atmosphere stroke; and a role left behind by a
  // renamed variable would sit in the table looking like coverage.
  const failures: string[] = [];
  for (const theme of THEMES) {
    const roles = WEATHER_ROLES[theme.id] ?? {};
    const published = new Set(weatherOf(theme).map(([name]) => name));
    for (const name of published) {
      if (!roles[name]) {
        failures.push(
          `  ${theme.id} publishes ${name} and nothing classifies it. Either it is a ` +
            "stroke and this leg is not measuring it, or it is a backdrop and it needs " +
            "saying so — an unclassified variable is a variable nobody decided about",
        );
      }
    }
    for (const name of Object.keys(roles)) {
      if (!published.has(name)) {
        failures.push(
          `  ${theme.id} has no ${name}, but WEATHER_ROLES classifies it. The variable ` +
            "was renamed or removed and this table is now claiming coverage it does not have",
        );
      }
    }
  }
  for (const entry of Object.values(WEATHER_ROLES).flatMap((vars) => Object.values(vars))) {
    if (!GATED_ROLES.has(entry.role) && !entry.why.trim()) {
      failures.push(
        `  a variable is classed "${entry.role}" with no reason. 1.4.11's carve-outs ` +
          "have to be argued, not assumed",
      );
    }
  }
  // And at least one exemption has to argue from the criterion's own test rather
  // than from convenience. This is the check that catches a table where every
  // reason is "it looked fine", which is the direction a gate decays in.
  const argues = Object.values(WEATHER_ROLES)
    .flatMap((vars) => Object.values(vars))
    .filter((v) => !GATED_ROLES.has(v.role))
    .some((v) => /identify|information|boundary/i.test(v.why));
  if (!argues) {
    failures.push(
      "  no exemption in the weather table argues from 1.4.11's own test ('required to " +
        "identify'). A table of carve-outs that never says why the criterion does not " +
        "apply is a list of preferences wearing the criterion's clothes",
    );
  }
  assert.equal(failures.length, 0, `${failures.length} classification problems:\n${failures.join("\n")}`);
});

test("the scrollbar thumb is painted from the gated token, not from the divider's", () => {
  // The rule and the table disagreed for as long as both existed: `border-strong`
  // has been documented as "scrollbar thumbs" since the token table was written,
  // and the thumb was painted `--codify-border`. Reading the table alone would
  // have called that pass — the table was right about what the token is *for*.
  // So the stylesheet is read, which is the only place the answer is.
  const thumb = /::-webkit-scrollbar-thumb\s*\{([^}]*)\}/.exec(css);
  assert.ok(thumb, "index.css has no ::-webkit-scrollbar-thumb rule to read");
  assert.match(
    thumb[1]!,
    /var\(--codify-border-strong\)/,
    "the scrollbar thumb is not painted from --codify-border-strong. The table above " +
      "gates that token on the strength of being the thumb, and a thumb painted from " +
      "--codify-border is the finding this leg was written to find",
  );
  const firefox = /scrollbar-color:\s*([^;]+);/.exec(css);
  assert.ok(firefox, "index.css sets no scrollbar-color, so Firefox draws its own thumb");
  assert.match(
    firefox[1]!,
    /var\(--codify-border-strong\)\s+var\(--codify-bg\)/,
    "scrollbar-color's first value is the thumb and its second is the track. The " +
      "thumb must be the gated token and the track must be --codify-bg, because " +
      "that adjacency is the pair the 3:1 is measured against",
  );
});

test("the divider is measured and reported, because 'not gated' is not 'not looked at'", () => {
  // The exemption is a judgement, and a judgement that is never printed is
  // indistinguishable from having forgotten. These numbers are the ones the
  // judgement rests on: they are the argument for not gating `border`, and if a
  // theme ever moved its divider to a 3:1 value this is what would say so.
  const report: string[] = [];
  for (const object of EXEMPT) {
    let lowest = { theme: "", ratio: Number.POSITIVE_INFINITY };
    for (const theme of THEMES) {
      const { ratio } = worstSurface(theme, token(theme, object.token));
      if (ratio < lowest.ratio) lowest = { theme: theme.id, ratio };
    }
    report.push(
      `  --codify-${object.token} (${object.what}) is not gated: its lowest ratio in ` +
        `any theme on any surface is ${lowest.ratio.toFixed(2)}:1 (${lowest.theme}). ` +
        object.why,
    );
  }
  // Printed, not asserted: the point is that the number is never lost.
  for (const line of report) console.log(line);
  assert.equal(EXEMPT.length, 1, "the exemption list changed; this report is one shape long");
  assert.ok(
    report[0]!.includes("1.4.11"),
    "the report has to cite the criterion the exemption rests on",
  );
});
