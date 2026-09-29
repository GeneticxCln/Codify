/**
 * What the render harness can reach, asserted rather than assumed.
 *
 * The two card tests proved that *an extracted* component can be rendered. This
 * is the claim underneath them: that the loader can load the rest of `src/` —
 * the shared primitives every control is built from, the icon set 18 files draw
 * with, and `ChatTimeline` itself, which is where the two cards actually live
 * and which no test could import until this was found.
 *
 * That gap was invisible as a failure. Nothing errored; the render tests were
 * simply two islands, and every other card was unreachable because `api.ts`
 * reads the boot token while its module evaluates. A harness that silently
 * cannot reach most of the app is a harness whose green means very little, so
 * this file exists to make the reach a checked property.
 *
 * Three things are held here:
 *
 * 1. **Every shared primitive renders.** The list is read off disk, so a new
 *    `components/ui/*.tsx` has to be added to the table below or this fails —
 *    the drift is caught by the file appearing, not by anyone remembering.
 * 2. **`ChatTimeline` renders a real transcript**, with both deliverable cards
 *    visible inside it. That is the composition claim, and it is the reason the
 *    other two files test extracted components at all.
 * 3. **The `localStorage` stub is a working `Storage`**, not a thrower, since
 *    its entire job is to let a module finish loading.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

// The id the empty state's skip link and the composer both name. Imported rather
// than written out, so a test that checks the link cannot pass by agreeing with a
// second copy of the same typo.
const { COMPOSER_ANCHOR_ID } = await import("../src/composerAnchor.ts");

const { Badge } = await import("../src/components/ui/Badge.tsx");
const { Button } = await import("../src/components/ui/Button.tsx");
const { Field } = await import("../src/components/ui/Field.tsx");
const { IconButton } = await import("../src/components/ui/IconButton.tsx");
const { Logo } = await import("../src/components/ui/Logo.tsx");
const { Wordmark } = await import("../src/components/ui/Wordmark.tsx");
const { SnowFall } = await import("../src/components/ui/SnowFall.tsx");
const { SolarWind } = await import("../src/components/ui/SolarWind.tsx");
const { CyberOrganism } = await import("../src/components/ui/CyberOrganism.tsx");
const { EventHorizon } = await import("../src/components/ui/EventHorizon.tsx");
const { VectorWire } = await import("../src/components/ui/VectorWire.tsx");
const { Nanofluid } = await import("../src/components/ui/Nanofluid.tsx");
const { ElectricArc } = await import("../src/components/ui/ElectricArc.tsx");
const { ToxicLab } = await import("../src/components/ui/ToxicLab.tsx");
const { AnonFluid } = await import("../src/components/ui/AnonFluid.tsx");
const { Panel } = await import("../src/components/ui/Panel.tsx");
const { Toggle } = await import("../src/components/ui/Toggle.tsx");
const { ChatTimeline } = await import("../src/components/ChatTimeline.tsx");
const { MatrixRain } = await import("../src/components/ui/MatrixRain.tsx");
const { RainBackdrop } = await import("../src/components/ui/RainBackdrop.tsx");
const { CyberGrid } = await import("../src/components/ui/CyberGrid.tsx");
const { AbyssSpores } = await import("../src/components/ui/AbyssSpores.tsx");
const { HudSweep } = await import("../src/components/ui/HudSweep.tsx");
const { NeuralWeb } = await import("../src/components/ui/NeuralWeb.tsx");
const { NebulaFlow } = await import("../src/components/ui/NebulaFlow.tsx");
const { WeatherBackdrop } = await import("../src/components/ui/WeatherBackdrop.tsx");
const { CMATRIX_OLED, CODIFY_DARK, THEMES } = await import("../src/appearance.ts");
const { Pin } = await import("lucide-react");
const { BookOpen } = await import("lucide-react");

import type { Goal, PlanStep } from "../src/types.ts";

const UI_DIR = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "src",
  "components",
  "ui"
);

/** Markup with tags stripped, so an assertion is about words, not nesting. */
const text = (markup: string): string =>
  markup
    .replace(/<[^>]+>/g, " ")
    .replace(/&rsquo;/g, "'")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ")
    .trim();

const JUNK = ["undefined", "NaN", "[object Object]"];

const assertClean = (markup: string, what: string): void => {
  assert.ok(markup.length > 0, `${what} rendered nothing at all`);
  for (const junk of JUNK) {
    assert.ok(!markup.includes(junk), `${what} rendered ${junk}`);
  }
};

// ── the storage stub, which is what unblocked the rest ────────────────────

test("a module can read localStorage while it loads", () => {
  // The failure this replaces was a `TypeError` from `undefined.getItem` before
  // the first assertion of any test could run.
  assert.equal(typeof localStorage.getItem, "function");
  assert.equal(localStorage.getItem("absent"), null);
});

test("the stub is a working Storage, not a thrower", () => {
  localStorage.setItem("k", "v");
  assert.equal(localStorage.getItem("k"), "v");
  assert.equal(localStorage.length >= 1, true);
  localStorage.setItem("k", "v2");
  assert.equal(localStorage.getItem("k"), "v2", "a second write replaces");
  localStorage.removeItem("k");
  assert.equal(localStorage.getItem("k"), null);
  localStorage.setItem("other", "x");
  localStorage.clear();
  assert.equal(localStorage.length, 0, "clear empties it");
});

// ── the icon set 18 files draw with ───────────────────────────────────────

test("a lucide icon renders as the svg it is", () => {
  const markup = renderToStaticMarkup(React.createElement(Pin));
  assert.match(markup, /^<svg /);
  assert.match(markup, /lucide-pin/);
  // Geometry, not an empty box: a 5271-export package that resolved to nothing
  // drawable would still satisfy the two assertions above.
  assert.ok(
    /<(path|circle|rect|line|polyline)\b/.test(markup),
    `expected an icon with geometry, got: ${markup.slice(0, 120)}`
  );
});

// ── the shared primitives, every one of them ──────────────────────────────

const step = (over: Partial<PlanStep> = {}): PlanStep => ({
  id: "s1",
  goal_id: "g1",
  ordinal: 0,
  title: "Write DESIGN.md",
  description: "publish the contract",
  suggested_paths: ["DESIGN.md"],
  status: "COMPLETED",
  ...over,
});

const goal = (over: Partial<Goal> = {}): Goal => ({
  id: "g1",
  workspace_id: "w1",
  title: "Draft the brand",
  description: "",
  status: "COMPLETED",
  dry_run: false,
  plan_only: false,
  parallel: false,
  mode: "design",
  version: 0,
  created_at: 0,
  updated_at: 0,
  steps: [step()],
  ...over,
});

/**
 * One entry shape for the five effects, because five hand-written entries for
 * the same contract is five places for one of them to be wrong.
 */
const effectEntry = (Component: React.FC<{ animated?: boolean }>) => ({
  render: () =>
    renderToStaticMarkup(React.createElement(Component, { animated: false })),
  // Same contract as the rain: decoration beside real content, untouchable,
  // and no width/height attributes at import — the effect never runs under
  // `renderToStaticMarkup`, which is what keeps a canvas a render.
  expect: /^\s*$/,
  markup: new RegExp(`^<canvas[^>]*aria-hidden="true"[^>]*class="[^"]*pointer-events-none`),
});

/**
 * One entry per shared primitive: how to render it, and what must come out.
 *
 * Two expectations, because "what the user reads" and "what the element *is*"
 * are different claims and a primitive can fail either. `expect` is matched
 * against the tag-stripped text; `markup` against the raw output, which is the
 * only place an attribute survives — and for three of these six the attribute
 * *is* the content, since an icon button's label is what makes it reachable at
 * all and a toggle's state is `aria-pressed` rather than a word.
 */
const PRIMITIVES: Record<
  string,
  { render: () => string; expect: RegExp; markup?: RegExp }
> = {
  "Badge.tsx": {
    render: () =>
      renderToStaticMarkup(
        // `children` is a required prop on these primitives, so it goes in the
        // props object: `createElement`'s variadic children are not checked
        // against `P`, and a call that leaves it out is a call TypeScript
        // should refuse.
        React.createElement(Badge, { tone: "warning", children: "1 contract" })
      ),
    expect: /1 contract/,
    // The tone names a theme variable, not a Tailwind hue. It used to be
    // `bg-amber-950/40`, which is the whole defect: that class compiles to one
    // amber forever, so a warning pill in the OLED app was the same orange it
    // had always been. `ui/src/index.css` carries the class name and the
    // variable, so the name here is what a theme gets to change.
    markup: /class="[^"]*bg-codify-warning\/40[^"]*text-codify-warning/,
  },
  "Button.tsx": {
    render: () =>
      renderToStaticMarkup(
        React.createElement(Button, { tone: "primary" }, "Run")
      ),
    expect: /\bRun\b/,
    // `type="button"` is the default these components set so a button inside a
    // form cannot submit it; it is in the markup and nowhere in the text.
    markup: /<button[^>]*type="button"/,
  },
  "Field.tsx": {
    render: () =>
      renderToStaticMarkup(
        React.createElement(Field, {
          label: "Boot token",
          hint: "from the shell",
          children: React.createElement("input", { readOnly: true }),
        })
      ),
    expect: /Boot token[\s\S]*from the shell/,
    markup: /<label/,
  },
  "IconButton.tsx": {
    render: () =>
      renderToStaticMarkup(
        React.createElement(
          IconButton,
          { label: "Delete goal", title: "Delete goal" },
          React.createElement(BookOpen)
        )
      ),
    // Nothing to read: the whole content is the accessible name.
    expect: /^$/,
    markup: /aria-label="Delete goal"/,
  },
  "Panel.tsx": {
    render: () =>
      renderToStaticMarkup(
        React.createElement(Panel, {
          title: "Settings",
          actions: React.createElement("button", null, "x"),
          children: React.createElement("p", null, "body"),
        })
      ),
    expect: /Settings[\s\S]*body/,
    markup: /<section/,
  },
  "Toggle.tsx": {
    render: () =>
      renderToStaticMarkup(
        React.createElement(Toggle, {
          armed: true,
          tone: "knowledge",
          children: "Knowledge",
        })
      ),
    expect: /Knowledge/,
    markup: /aria-pressed="true"[^>]*class="[^"]*bg-codify-knowledge\/20/,
  },
  "Logo.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(Logo, { size: 24 })),
    // The badge is an inline `<svg>` now, so the two halves of the old contract
    // moved with it. Decorative: an `<img alt="">` said that with an empty alt,
    // an SVG says it with `aria-hidden`. Rounded: the old mark carried
    // `rounded-lg` on the `<img>`, which rounds the *element* — and the tile is
    // a `<rect>` drawn to the full 24×24 box, so the rounding is the rect's own
    // `rx`. A class on the `<svg>` would round nothing that is visible.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*>[\s\S]*<rect[^>]*rx="6"/,
  },
  "SolarWind.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(SolarWind, { animated: false })),
    // aria-hidden for the same reason every other canvas here is: a decorative
    // effect announced as a graphic tells a screen reader nothing it can use.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*class="[^"]*pointer-events-none/,
  },
  "CyberOrganism.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(CyberOrganism, { animated: false })),
    // aria-hidden for the same reason every other canvas here is: a decorative
    // effect announced as a graphic tells a screen reader nothing it can use.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*class="[^"]*pointer-events-none/,
  },
  "EventHorizon.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(EventHorizon, { animated: false })),
    // aria-hidden for the same reason every other canvas here is: a decorative
    // effect announced as a graphic tells a screen reader nothing it can use.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*class="[^"]*pointer-events-none/,
  },
  "VectorWire.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(VectorWire, { animated: false })),
    // aria-hidden for the same reason every other canvas here is: a decorative
    // effect announced as a graphic tells a screen reader nothing it can use.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*class="[^"]*pointer-events-none/,
  },
  "Nanofluid.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(Nanofluid, { animated: false })),
    // aria-hidden for the same reason every other canvas here is: a decorative
    // effect announced as a graphic tells a screen reader nothing it can use.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*class="[^"]*pointer-events-none/,
  },
  "ElectricArc.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(ElectricArc, { animated: false })),
    // aria-hidden for the same reason every other canvas here is: a decorative
    // effect announced as a graphic tells a screen reader nothing it can use.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*class="[^"]*pointer-events-none/,
  },
  "ToxicLab.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(ToxicLab, { animated: false })),
    // aria-hidden for the same reason every other canvas here is: a decorative
    // effect announced as a graphic tells a screen reader nothing it can use.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*class="[^"]*pointer-events-none/,
  },
  "AnonFluid.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(AnonFluid, { animated: false })),
    // aria-hidden for the same reason every other canvas here is: a decorative
    // effect announced as a graphic tells a screen reader nothing it can use.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*class="[^"]*pointer-events-none/,
  },
  "SnowFall.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(SnowFall, { animated: false })),
    // aria-hidden for the same reason the rain is: a canvas of falling snow
    // announced as a graphic, and a screen reader reading nothing useful off a
    // canvas's pixels.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*class="[^"]*pointer-events-none/,
  },
  "Wordmark.tsx": {
    render: () => renderToStaticMarkup(React.createElement(Wordmark)),
    // The name, once. The glyphs are painted twice — the fill and the sweep —
    // so both copies carry `aria-hidden` and the container carries the label.
    // Stripping the tags leaves the two text nodes behind, which is exactly why
    // the two `aria-hidden` spans are the thing being asserted: without them a
    // screen reader says "Codify" twice and the second one is a div.
    expect: /Codify/,
    markup:
      /role="img"[^>]*aria-label="Codify"[\s\S]*aria-hidden="true"[^>]*codify-wordmark-fill[\s\S]*aria-hidden="true"[^>]*codify-wordmark-sweep/,
  },
  "MatrixRain.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(MatrixRain, { animated: false })),
    // aria-hidden: the rain is decoration beside real content, and a screen
    // reader reading half-width katakana is not a service to anyone.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*class="[^"]*pointer-events-none/,
  },
  "RainBackdrop.tsx": {
    // The backdrop exists only under a theme that publishes `--cmatrix-rain`,
    // so this entry drives the mechanism the real app uses: persist the OLED
    // id, render, put the default back for everything below. On the default
    // theme it renders *nothing* — that half is asserted in
    // `appearance.test.ts`.
    render: () => {
      localStorage.setItem("codify.theme", CMATRIX_OLED.id);
      const out = renderToStaticMarkup(React.createElement(RainBackdrop));
      localStorage.setItem("codify.theme", CODIFY_DARK.id);
      return out;
    },
    expect: /^\s*$/,
    markup: /aria-hidden="true"[\s\S]*<canvas/,
  },
  "CyberGrid.tsx": {
    render: () =>
      renderToStaticMarkup(React.createElement(CyberGrid, { animated: false })),
    // Same contract as the rain: decoration beside real content, untouchable,
    // and no width/height attributes at import — the effect never runs under
    // `renderToStaticMarkup`, which is what keeps a canvas a render.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[^>]*class="[^"]*pointer-events-none/,
  },
  "AbyssSpores.tsx": effectEntry(AbyssSpores),
  "HudSweep.tsx": effectEntry(HudSweep),
  "NeuralWeb.tsx": effectEntry(NeuralWeb),
  "NebulaFlow.tsx": effectEntry(NebulaFlow),
  "WeatherBackdrop.tsx": {
    // Gated on the theme's own variable rather than on a theme id, so the
    // harness walks the themes that have weather the way the app would.
    render: () => {
      const themed = THEMES.filter((t) => t.id !== CODIFY_DARK.id);
      const out = themed
        .map((t) => {
          localStorage.setItem("codify.theme", t.id);
          return renderToStaticMarkup(React.createElement(WeatherBackdrop));
        })
        .join("");
      localStorage.setItem("codify.theme", CODIFY_DARK.id);
      return out;
    },
    // The veil is a class, not an inline style: it is the one piece of a theme
    // that has to be able to answer `prefers-reduced-motion` from `index.css`,
    // and an inline background could not.
    expect: /^\s*$/,
    markup: /aria-hidden="true"[\s\S]*<canvas[\s\S]*class="cyber-veil/,
  },
};

test("every shared primitive on disk is rendered here", () => {
  const onDisk = readdirSync(UI_DIR)
    .filter((f) => f.endsWith(".tsx"))
    .sort();
  const covered = Object.keys(PRIMITIVES).sort();
  assert.deepEqual(
    onDisk,
    covered,
    "components/ui/ and this table disagree. A shared primitive with no entry " +
      "here is a primitive whose markup nothing checks — add it, rather than " +
      "quietly leaving the render layer one component short."
  );
});

for (const [name, { render, expect, markup: wantMarkup }] of Object.entries(
  PRIMITIVES
)) {
  test(`${name} renders its own content`, () => {
    const markup = render();
    assertClean(markup, name);
    assert.match(text(markup), expect);
    if (wantMarkup) assert.match(markup, wantMarkup);
  });
}

// ── the whole transcript, which is where the cards actually live ──────────

const noop = (): void => {};
const timeline = (messages: any[], pinned: Record<string, string> = {}): string =>
  renderToStaticMarkup(
    React.createElement(ChatTimeline, {
      messages,
      pinnedContracts: pinned,
      onStartGoal: noop,
      onEnableExecution: noop,
      onApplyGoal: noop,
      onEditStep: () => true,
      onPauseGoal: noop,
      onCancelGoal: noop,
      onSetGoalTrace: noop,
      onDeleteGoal: noop,
      onRetryStep: noop,
      onOpenSettings: noop,
      onImportAudit: noop,
      onPinDesignContract: async () => {},
    })
  );

const designEvent = {
  id: "e1",
  goal_id: "g1",
  type: "design_contract",
  // The transcript keys every event on this, so it is not optional in a
  // fixture: an event without one keys `undefined`, React warns about the whole
  // list, and the warning is the fixture's fault rather than the component's.
  // `sequence` is `NOT NULL` and unique per goal in the schema, and
  // `next_sequence()` always assigns it.
  sequence: 1,
  created_at: 0,
  timestamp: 0,
  payload: {
    mode: "design",
    artifact: "web_prototype",
    direction: "One plain monochrome page.",
    design_md: "# brand\n\nink #0d1117.\n",
  },
};

const knowledgeEvent = {
  ...designEvent,
  id: "e2",
  payload: {
    mode: "knowledge",
    artifact: "document",
    direction: "Record what this repository is.",
    design_md: "# what this is\n\nA dashboard.\n",
  },
};

test("a design goal's transcript draws the design card inside it", () => {
  const markup = timeline([
    {
      id: "m1",
      role: "assistant",
      content: "",
      timestamp: 0,
      goal: goal(),
      events: [designEvent],
    },
  ]);
  assertClean(markup, "a design transcript");
  const shown = text(markup);
  assert.match(shown, /Design deliverable/);
  assert.match(shown, /One plain monochrome page/);
  assert.match(shown, /Pin as brand contract/);
});

test("a knowledge goal's transcript draws the knowledge card instead", () => {
  const markup = timeline([
    {
      id: "m1",
      role: "assistant",
      content: "",
      timestamp: 0,
      goal: goal({ mode: "knowledge" }),
      events: [knowledgeEvent],
    },
  ]);
  assertClean(markup, "a knowledge transcript");
  const shown = text(markup);
  assert.match(shown, /Knowledge deliverable/);
  assert.match(shown, /CODIFY\.md/);
  // And no pin, because nothing in this mode is bindable by a click.
  assert.doesNotMatch(shown, /Pin as brand contract/);
});

test("a workspace that already obeys the file is reported inside the transcript", () => {
  // The pin is workspace state read from `GET /workspaces`, so the card has to
  // be able to say it without having watched the click. This is the wiring the
  // card test asserts in isolation, in the place it is actually used.
  const markup = timeline(
    [
      {
        id: "m1",
        role: "assistant",
        content: "",
        timestamp: 0,
        goal: goal(),
        events: [designEvent],
      },
    ],
    { w1: "design.md" }
  );
  assert.match(text(markup), /is this workspace’s brand contract/);
  assert.doesNotMatch(text(markup), /Pin as brand contract/);
});

test("an empty transcript is the name, and one real action", () => {
  // The composer is not this component's — it lives above the timeline — so what
  // the transcript owes an empty state is the app's name, and nothing that could
  // have been the conversation instead.
  //
  // It used to owe a *question* and a paragraph: "What would you like to build
  // or fix?", then "Select a project folder and your preferred model below.
  // Codify will inspect your codebase, plan atomic steps, propose file diffs,
  // and verify tests automatically." That described a pipeline to someone who
  // had not typed anything yet, and an empty window that explains itself is a
  // window asking to be read before it has been used. The negative assertion
  // below is the point: the instruction is not allowed back.
  //
  // Four starter-prompt pills went next, and the reasoning is the same one a
  // layer deeper. They were four sentences about work this codebase might
  // plausibly be asked to do, offered to someone who had not yet asked for any of
  // it; they turned the empty state into a menu, so the one thing worth reading
  // had to compete with four rows of small type. The negative assertion is what
  // keeps them out, because a suggestion list is exactly the kind of thing that
  // comes back as a "little helpful nudge".
  const markup = timeline([]);
  assertClean(markup, "an empty transcript");
  const shown = text(markup);
  assert.match(shown, /Codify/);
  assert.doesNotMatch(
    shown,
    /What would you like to build or fix|inspect your codebase|plan atomic steps/,
    "the empty transcript is explaining the pipeline again"
  );
  assert.doesNotMatch(
    shown,
    /Add JWT authentication|Find failing tests|Refactor API error|Generate unit tests/,
    "the starter prompts are back; the empty state is a menu again"
  );
  // Exactly one control, and it is the one real thing an empty transcript can
  // do: open an audit report exported earlier. Counted rather than matched,
  // because a new button is the failure this is watching for.
  const buttons = [...markup.matchAll(/<button\b/g)].length;
  assert.equal(
    buttons,
    1,
    `expected only the audit-import button, found ${buttons} buttons: ` +
      [...markup.matchAll(/<button[\s\S]{0,200}?<\/button>/g)]
        .map((m) => text(m[0]).slice(0, 60))
        .join(" | ")
  );
  assert.match(shown, /Import audit report/);
});

test("the empty state offers a way back to the composer", () => {
  // The empty transcript's one control is a file picker, which is a dead end for
  // a keyboard user who tabbed into the middle of the window. The composer is
  // auto-focused on mount, so they land on it the first time — but that is a
  // one-shot, and nothing brings focus back after it. This is the control that
  // does.
  //
  // A skip link is two halves that have to agree: the link's `href` and the
  // element it names. Renaming one without the other produces a link that
  // silently does nothing, which is why both ends read the same constant rather
  // than a literal — and why this test checks the constant, not the string.
  const markup = timeline([]);

  const link = /<a[^>]*href="#([^"]+)"[^>]*>([^<]*)<\/a>/.exec(markup);
  assert.ok(
    link,
    "the empty transcript renders no skip link, so a keyboard user has one " +
      "control and it opens a file dialog. Expected an <a href> naming the composer.",
  );
  assert.equal(
    link[1],
    COMPOSER_ANCHOR_ID,
    `the skip link points at #${link[1]} but the composer is ` +
      `#${COMPOSER_ANCHOR_ID}; a fragment link to an id nothing carries moves ` +
      "focus nowhere and the control is a lie",
  );
  // Named, not "click here": a screen reader reads the text, not the target.
  assert.match(
    link[2],
    /prompt/i,
    `the skip link's text is ${JSON.stringify(link[2])}, which does not name ` +
      "where it goes",
  );
  // Invisible until asked for. `sr-only` with `focus:not-sr-only` is the
  // canonical form, and the empty state's whole argument is that the wordmark is
  // the only thing on it.
  const linkTag = link[0];
  assert.match(linkTag, /sr-only/, "the skip link is visible when it should not be");
  assert.match(
    linkTag,
    /focus:not-sr-only/,
    "the skip link never becomes visible, so a sighted keyboard user cannot " +
      "see where focus is",
  );
  // The other end — the composer carrying that id in the mounted app — is checked in
  // `appWiring.test.ts`.
});

test("rendering a transcript produces no React warning of any kind", () => {
  // The reason this file exists is a claim about the markup, and React is the
  // one authority on whether that markup is well-formed. Its most expensive
  // complaint — a list child with no unique key — is a reconciliation bug rather
  // than a cosmetic one: keyed by index, a transcript that appends an event can
  // move the DOM state of one row onto another.
  //
  // This found the fixtures above, which had been omitting `sequence` and
  // therefore keying every event `undefined`. The component was right and the
  // test was wrong, which is the least interesting way for it to go — the point
  // is that it cannot happen again unnoticed.
  const warnings: string[] = [];
  const original = console.error;
  console.error = (...args: unknown[]): void => {
    warnings.push(args.map(String).join(" "));
  };
  try {
    for (const messages of [
      [],
      [{ id: "m1", role: "user", content: "hi", timestamp: 0 }],
      [
        {
          id: "m1",
          role: "assistant",
          content: "",
          timestamp: 0,
          goal: goal(),
          events: [designEvent, knowledgeEvent],
        },
      ],
    ] as any[]) {
      timeline(messages);
    }
    timeline(
      [
        {
          id: "m1",
          role: "assistant",
          content: "",
          timestamp: 0,
          goal: goal(),
          events: [designEvent],
        },
      ],
      { w1: "design.md" }
    );
  } finally {
    console.error = original;
  }
  assert.deepEqual(
    warnings,
    [],
    `React complained while rendering:\n  ${warnings.join("\n  ")}`
  );
});

