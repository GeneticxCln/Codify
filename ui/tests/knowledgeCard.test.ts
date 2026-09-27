/**
 * The knowledge deliverable card, as markup rather than as a return value.
 *
 * `designDeliverable.test.ts` covers what the card *decides*. This covers what
 * it *puts on screen*, which is a different layer: the strings exist, the
 * `designDeliverable.ts` tests pass, and the JSX still drops one on the floor —
 * a body rendered inside a branch that never evaluates, a `path` that prints as
 * `undefined`, a sentence about what makes a file real that never made it into
 * the tree. None of that is visible from the logic module.
 *
 * It renders through `react-dom/server`, so this is real React producing real
 * markup from the real component — the one in `src/`, imported through the
 * loader in `tsxLoader.ts`, not a copy kept here to be testable.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

// Dynamic, and after the hook: a static import is hoisted and resolved before
// `registerTsx()` has run, which fails with ERR_UNKNOWN_FILE_EXTENSION.
const { KnowledgeDeliverableCard } = await import(
  "../src/components/KnowledgeDeliverableCard.tsx"
);

import type { Goal, PlanStep } from "../src/types.ts";

const BODY = "# What this repository is\n\nA KPI dashboard. Tests run with `python3 -m pytest`.\n";

const PRIOR = "# What this repository is\n\nA dashboard. The old renderer is `ui/src/legacy/render.js`.\n";

const step = (over: Partial<PlanStep> = {}): PlanStep => ({
  id: "s1",
  goal_id: "g1",
  ordinal: 0,
  title: "Write CODIFY.md",
  description: "record the workspace knowledge",
  suggested_paths: ["CODIFY.md"],
  status: "PENDING",
  ...over,
});

const goal = (over: Partial<Goal> = {}): Goal => ({
  id: "g1",
  workspace_id: "w1",
  title: "Record what this repository is",
  description: "",
  status: "PENDING",
  dry_run: false,
  plan_only: false,
  parallel: false,
  mode: "knowledge",
  version: 0,
  created_at: 0,
  updated_at: 0,
  steps: [step()],
  ...over,
});

const payload = (over: Record<string, any> = {}) => ({
  mode: "knowledge",
  artifact: "document",
  direction: "Record what this repository is, for the next run.",
  design_md: BODY,
  ...over,
});

const render = (g: Goal | undefined = goal(), p: Record<string, any> = payload()): string =>
  renderToStaticMarkup(
    React.createElement(KnowledgeDeliverableCard, { goal: g, payload: p })
  );

/**
 * A card rendered with no goal prop at all.
 *
 * Not `render(undefined)`: a default parameter fires on `undefined`, so that
 * would quietly hand the fixture's goal in and the test would pass while
 * exercising the case it was written for. Omitting the prop is the only way to
 * say what it means.
 */
const renderBare = (p: Record<string, any> = payload()): string =>
  renderToStaticMarkup(React.createElement(KnowledgeDeliverableCard, { payload: p }));

/** Markup with tags stripped, so an assertion is about words, not nesting. */
const text = (markup: string): string =>
  markup
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ")
    .trim();

// ── the body is the point of the card ─────────────────────────────────────

test("the authored body is on screen, in a preformatted block", () => {
  const markup = render();
  assert.match(markup, /<pre[^>]*>[\s\S]*<\/pre>/, "a document is prose, not markup");
  assert.ok(
    markup.includes("A KPI dashboard"),
    "the body the run authored must be readable, not merely counted"
  );
  // The backticks are content: a document that quotes paths has to show them.
  assert.ok(markup.includes("python3 -m pytest"));
});

test("the heading names what this file is, not what a design file is", () => {
  assert.match(text(render()), /Knowledge deliverable/);
  assert.doesNotMatch(text(render()), /Design direction/);
  assert.doesNotMatch(text(render()), /Design deliverable/);
});

test("the direction is shown, because it is what the body is for", () => {
  assert.match(text(render()), /Record what this repository is, for the next run/);
});

test("the file is named, and its length is countable from the screen", () => {
  const shown = text(render());
  assert.match(shown, /CODIFY\.md/);
  assert.ok(
    shown.includes(`(${BODY.length} chars)`),
    "the card says how much it is holding, so a capped body is visible as capped"
  );
});

test("nothing renders as the word undefined", () => {
  // The failure a card built from loose payload reads produces: one missing
  // field and a literal `undefined` sits in the transcript looking like data.
  for (const markup of [
    renderBare({ mode: "knowledge", design_md: "x" }),
    renderBare({ mode: "knowledge", design_md: BODY }),
  ]) {
    assert.doesNotMatch(markup, /undefined/);
    assert.doesNotMatch(markup, /NaN/);
    assert.doesNotMatch(markup, /\[object Object\]/);
  }
});

test("a card with no goal attached still names CODIFY.md", () => {
  // The bug this file found. A transcript message can carry no goal, and the
  // card was resolving the file from the *goal's* mode — so with none it fell
  // back to the design convention and printed `DESIGN.md` on a card headed
  // "Knowledge deliverable". The one file this mode can never write, named as
  // though it were the deliverable, and no logic test could see it: the logic
  // was correct for every goal it was given.
  const shown = text(renderBare());
  assert.match(shown, /Knowledge deliverable/);
  assert.match(
    shown,
    /CODIFY\.md/,
    "the event's own mode is the one that has to decide, and it is always there"
  );
  assert.doesNotMatch(shown, /DESIGN\.md/);
});

test("the body is open, and the file it replaces is not", () => {
  // The two disclosures make opposite decisions, on purpose. The new document
  // is what the user asked to see, so it is on screen without a click; the old
  // one is context for judging it, and a timeline that opened both would bury
  // the body under the thing it is replacing.
  const markup = render(
    goal({ steps: [step({ status: "COMPLETED" })] }),
    payload({
      revises: {
        path: "CODIFY.md",
        text: PRIOR,
        chars: PRIOR.length,
        truncated: false,
        stale_paths: [],
      },
    })
  );
  const details = [...markup.matchAll(/<details[^>]*>/g)].map((m) => m[0]);
  assert.equal(details.length, 2, "the body and the file it replaces");
  assert.match(details[0], /\bopen\b/, "the body is what the user asked for");
  assert.doesNotMatch(details[1], /\bopen\b/, "the old file is context, not the point");
});

// ── the three review states, in the sentence that stands in for a button ──

test("an unreviewed draft says the file is not on disk yet", () => {
  const shown = text(render(goal({ steps: [step({ status: "PENDING" })] })));
  assert.match(shown, /the draft, not yet reviewed/);
  assert.match(shown, /takes effect when a step writes it/);
  assert.doesNotMatch(shown, /apply this goal/, "nothing was proposed by a dry run to apply");
});

test("a reviewed draft is shown as what it is, and the file takes effect", () => {
  const shown = text(
    render(goal({ steps: [step({ status: "COMPLETED" })] }))
  );
  assert.match(shown, /the deliverable, as written/);
  assert.match(shown, /every later run's librarian now reads CODIFY\.md as a prior/);
  // There is no pin for this mode, so the card must not offer one.
  assert.doesNotMatch(shown, /Pin as brand contract/);
});

test("a reviewed dry run admits the next run is unaffected", () => {
  const shown = text(
    render(goal({ steps: [step({ status: "COMPLETED" })], dry_run: true }))
  );
  assert.match(shown, /the proposed draft — nothing written to disk yet/);
  assert.match(shown, /apply this goal to write CODIFY\.md/);
  assert.match(
    shown,
    /the next run still reads what an earlier run left/,
    "saying the file was written here would be a claim about a file that is not"
  );
});

// ── what the run is rewriting ─────────────────────────────────────────────

test("the file being replaced is shown, not just counted", () => {
  const markup = render(
    goal({ steps: [step({ status: "COMPLETED" })] }),
    payload({
      revises: {
        path: "CODIFY.md",
        text: PRIOR,
        chars: PRIOR.length,
        truncated: false,
        stale_paths: [],
      },
    })
  );
  assert.ok(
    markup.includes("ui/src/legacy/render.js"),
    "the old document is the context for judging the new one"
  );
  assert.match(text(markup), /replacing CODIFY\.md/);
});

test("the old file's dead paths are named, and counted in the summary", () => {
  const shown = text(
    render(
      goal({ steps: [step({ status: "COMPLETED" })] }),
      payload({
        revises: {
          path: "CODIFY.md",
          text: PRIOR,
          chars: PRIOR.length,
          truncated: false,
          stale_paths: ["ui/src/legacy/render.js", "ui/src/board.html"],
        },
      })
    )
  );
  assert.match(shown, /2 path\(s\) it named are already gone/);
  assert.match(shown, /the engine told the drafter to disregard these/);
  assert.ok(shown.includes("ui/src/legacy/render.js, ui/src/board.html"));
});

test("a file with nothing stale does not claim a warning", () => {
  const shown = text(
    render(
      goal({ steps: [step({ status: "COMPLETED" })] }),
      payload({
        revises: {
          path: "CODIFY.md",
          text: PRIOR,
          chars: PRIOR.length,
          truncated: false,
          stale_paths: [],
        },
      })
    )
  );
  assert.doesNotMatch(shown, /already gone/);
  assert.doesNotMatch(shown, /disregard these/);
});

test("a half-read prior is labelled, not shown as if it were the whole file", () => {
  // A truncated prior rendered in full reads as a complete file, which is the
  // failure docs/04 §4.9.1 exists to prevent.
  const shown = text(
    render(
      goal({ steps: [step({ status: "COMPLETED" })] }),
      payload({
        revises: {
          path: "CODIFY.md",
          text: PRIOR,
          chars: 8000,
          truncated: true,
          stale_paths: [],
        },
      })
    )
  );
  assert.match(shown, /shown from the top — the file is longer than the 8000 chars/);
});

test("a workspace with no prior shows no comparison at all", () => {
  // An empty block would be a claim about a file the workspace does not have,
  // with nothing on the other side of the comparison.
  const shown = text(render());
  assert.doesNotMatch(shown, /replacing/);
  assert.doesNotMatch(shown, /disregard these/);
});

// ── the body is the thing; its absence is named as its absence ────────────

test("a body that has not been authored says so, and blames no step", () => {
  // The engine refuses to publish a knowledge contract with no body, so this
  // means the drafter failed or is still answering. Saying "nothing written to
  // disk" would name the write step, which is not the stage that is missing.
  const shown = text(render(goal(), { mode: "knowledge", artifact: "document" }));
  assert.match(shown, /no body authored/);
  assert.doesNotMatch(shown, /nothing written to disk yet/);
  assert.doesNotMatch(shown, /takes effect when a step writes it/);
});
