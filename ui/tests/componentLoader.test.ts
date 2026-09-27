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

const { Badge } = await import("../src/components/ui/Badge.tsx");
const { Button } = await import("../src/components/ui/Button.tsx");
const { Field } = await import("../src/components/ui/Field.tsx");
const { IconButton } = await import("../src/components/ui/IconButton.tsx");
const { Panel } = await import("../src/components/ui/Panel.tsx");
const { Toggle } = await import("../src/components/ui/Toggle.tsx");
const { ChatTimeline } = await import("../src/components/ChatTimeline.tsx");
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
    markup: /class="[^"]*bg-amber-950\/40/,
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
    markup: /aria-pressed="true"[^>]*class="[^"]*bg-cyan-600\/20/,
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
      onQuickPrompt: noop,
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

test("an empty transcript invites a goal rather than rendering a void", () => {
  // The composer is not this component's — it lives above the timeline — so what
  // the transcript owes an empty state is the question and something to click.
  const markup = timeline([]);
  assertClean(markup, "an empty transcript");
  const shown = text(markup);
  assert.match(shown, /What would you like to build or fix\?/);
  assert.match(markup, /<button/);
  // Three starter prompts, each a real button.
  assert.ok(
    shown.includes("Find failing tests"),
    `expected the starter prompts, got: ${shown.slice(0, 200)}`
  );
  const buttons = [...markup.matchAll(/<button\b/g)].length;
  assert.ok(
    buttons >= 3,
    `expected at least the three starter prompts, found ${buttons} buttons`
  );
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
