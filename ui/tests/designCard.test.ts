/**
 * The design deliverable card, as markup rather than as a return value.
 *
 * `designDeliverable.test.ts` covers what `pinReadiness` *decides*. This covers
 * what the card *puts on screen*, which is the layer where a decision can be
 * computed correctly and then never rendered: the reason a disabled pin is
 * unavailable is a `disabled` attribute plus a sibling sentence, and a card that
 * computes the reason and drops the sentence answers nothing. The pinned state
 * is the same shape — a sentence where a button used to be, and no button at all
 * is how a screen reader learns there is nothing to click.
 *
 * Rendered through `react-dom/server` from the real component in `src/`.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

// Dynamic, and after the hook: a static import is hoisted and resolved before
// `registerTsx()` has run, which fails with ERR_UNKNOWN_FILE_EXTENSION.
const { DesignDeliverableCard } = await import(
  "../src/components/DesignDeliverableCard.tsx"
);

import type { Goal, PlanStep } from "../src/types.ts";
import type { PinOutcome } from "../src/components/DesignDeliverableCard.tsx";

const BODY = "# codify-brand\n\nink #0d1117, accent #2f81f7.\n";

const CONTRACT = {
  mode: "design",
  artifact: "web_prototype",
  direction: "One plain monochrome page: a single banner line, no chrome.",
  design_system: { name: "fake-brand", source: null, origin: null },
  tokens: {
    colors: [
      { name: "ink", value: "#0d1117" },
      { name: "accent", value: "#2f81f7" },
    ],
    typography: [{ name: "body", value: "system-ui, sans-serif" }],
  },
  components: [{ name: "Banner", purpose: "the one greeting line" }],
  acceptance: ["banner.txt exists and holds one greeting line"],
  constraints: ["no new dependencies"],
  design_md: BODY,
};

const step = (over: Partial<PlanStep> = {}): PlanStep => ({
  id: "s1",
  goal_id: "g1",
  ordinal: 0,
  title: "Write DESIGN.md",
  description: "publish the brand contract",
  suggested_paths: ["DESIGN.md"],
  status: "PENDING",
  ...over,
});

const goal = (over: Partial<Goal> = {}): Goal => ({
  id: "g1",
  workspace_id: "w1",
  title: "Draft the brand",
  description: "",
  status: "PENDING",
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

const onPin = async (): Promise<void> => {};

const render = (
  g: Goal | undefined = goal(),
  p: Record<string, any> = CONTRACT,
  outcome: PinOutcome | null = null,
  pinnedPath?: string
): string =>
  renderToStaticMarkup(
    React.createElement(DesignDeliverableCard, {
      payload: p,
      goal: g,
      pinOutcome: outcome,
      pinnedPath,
      onPin,
    })
  );

/** A card with no goal prop at all — see `renderBare` in the knowledge test. */
const renderBare = (p: Record<string, any> = CONTRACT): string =>
  renderToStaticMarkup(
    React.createElement(DesignDeliverableCard, {
      payload: p,
      pinOutcome: null,
      onPin,
    })
  );

/**
 * The pin button's opening tag, or "" when there is no button on the card.
 *
 * The whole assertion layer for this card hangs off getting this right. The
 * button's `className` contains `disabled:opacity-40 disabled:cursor-not-allowed`
 * so that the disabled look follows the attribute — which means a naive
 * `/disabled/` scan reports an *enabled* button as disabled. Everything below
 * therefore asks about `disabled` as a standalone attribute word, never as a
 * substring of the styling.
 */
const pinButton = (markup: string): string => {
  // The pin is found by what it says, not by being the first button: the body has a Rendered/Source
  // toggle of its own, and a position-based match would take that for the pin.
  const open = /<button\b[^>]*>(?=(?:(?!<\/button>)[\s\S])*Pin as brand contract)/.exec(markup);
  return open ? open[0] : "";
};

/** True when the pin is on screen and genuinely not clickable. */
const pinDisabled = (markup: string): boolean =>
  /(?<![-\w])disabled(?![-:\w])/.test(pinButton(markup));

/** True when the pin is on screen at all. */
const hasPin = (markup: string): boolean => pinButton(markup) !== "";

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

// ── the direction, and the vocabulary that came with it ───────────────────

test("the heading names this as the deliverable it is", () => {
  assert.match(text(render()), /Design deliverable/);
  assert.doesNotMatch(text(render()), /Design direction/);
});

test("the locked direction is on screen", () => {
  // It is an input to every step, published so a step can be judged against the
  // contract that shaped it rather than from memory.
  const shown = text(render());
  assert.match(shown, /One plain monochrome page/);
  assert.match(shown, /no chrome/);
});

test("the colour tokens are drawn as swatches, with their values named", () => {
  const markup = render();
  assert.match(markup, /background:\s*#0d1117/, "a token is shown, not just listed");
  const shown = text(markup);
  assert.match(shown, /ink/);
  assert.match(shown, /#0d1117/);
  assert.match(shown, /accent/);
  assert.match(shown, /#2f81f7/);
});

test("type, components, acceptance and constraints each have a row", () => {
  const shown = text(render());
  assert.match(shown, /type:/);
  assert.match(shown, /body system-ui, sans-serif/);
  assert.match(shown, /Banner/);
  assert.match(shown, /the one greeting line/);
  assert.match(shown, /acceptance:/);
  assert.match(shown, /banner\.txt exists/);
  assert.match(shown, /constraints:/);
  assert.match(shown, /no new dependencies/);
});

test("a contract with no tokens does not render an empty token row", () => {
  // `banner.txt exists` with nothing under it would read as a layout bug rather
  // than as a contract that had none.
  const shown = text(render(goal(), { ...CONTRACT, tokens: {} }));
  assert.doesNotMatch(shown, /type:/);
  assert.doesNotMatch(shown, /colors/);
  assert.match(shown, /Design deliverable/, "the rest of the card is unaffected");
});

test("a contract discovered rather than proposed says which file it came from", () => {
  const shown = text(
    render(
      goal(),
      {
        ...CONTRACT,
        design_system: {
          name: "acme",
          source: "docs/BRAND.md",
          origin: "discovered",
        },
      }
    )
  );
  assert.match(shown, /found at/);
  assert.match(shown, /docs\/BRAND\.md/);
  assert.doesNotMatch(shown, /pinned at/);
});

test("a contract obeying a pin says the pin is where it came from", () => {
  // The fourth of the four states, and the one that is the user's own
  // instruction rather than the engine's: this file is binding because they said
  // so, which is a different claim from "we found it".
  const shown = text(
    render(
      goal(),
      {
        ...CONTRACT,
        design_system: { name: "acme", source: "DESIGN.md", origin: "pinned" },
      }
    )
  );
  assert.match(shown, /pinned at/);
  assert.match(shown, /DESIGN\.md/);
});

test("an invented contract does not claim it was found anywhere", () => {
  const shown = text(render());
  assert.match(shown, /proposed, no existing contract/);
});

// ── the pin, in the three states it can be in ─────────────────────────────

test("a reviewed, written deliverable offers the pin, enabled", () => {
  const markup = render(goal({ steps: [step({ status: "COMPLETED" })] }));
  assert.ok(hasPin(markup), "a reviewed deliverable with a free path can be pinned");
  assert.ok(
    !pinDisabled(markup),
    `the pin should be live, but its tag was ${pinButton(markup)}`
  );
  assert.match(text(markup), /Pin as brand contract/);
  assert.match(text(markup), /DESIGN\.md/);
});

test("a draft the critic has not approved says so on the card", () => {
  // The reason is rendered text, not only a `title`: a disabled button does not
  // reliably surface a tooltip, and "why can't I pin this" is the question the
  // card exists to answer.
  const markup = render(goal({ steps: [step({ status: "PENDING" })] }));
  assert.ok(hasPin(markup));
  assert.ok(pinDisabled(markup), "an unreviewed draft must not be pinnable");
  assert.match(text(markup), /waiting on the review/);
  assert.match(text(markup), /the draft, not yet reviewed/);
});

test("the enabled pin carries the styling that makes the disabled one look dead", () => {
  // Guards `pinDisabled` itself. If this class list ever loses its
  // `disabled:` variants the helper above keeps working, but the card would
  // render a live button that is indistinguishable from a dead one.
  assert.match(pinButton(render()), /disabled:opacity-40/);
  assert.match(pinButton(render()), /disabled:cursor-not-allowed/);
});

test("a reviewed dry run is refused, and the refusal names the file", () => {
  const shown = text(
    render(goal({ steps: [step({ status: "COMPLETED" })], dry_run: true }))
  );
  assert.match(shown, /this run proposed DESIGN\.md without writing it/);
  assert.match(shown, /apply the goal, then pin it/);
  assert.match(shown, /the proposed draft — nothing written to disk yet/);
});

test("a workspace that already obeys this file gets a state, not an action", () => {
  // The pin may have been set from the workspace picker, or by an earlier goal,
  // or before this tab reloaded — so the card reads workspace state rather than
  // whether this transcript happened to watch it happen.
  const markup = render(
    goal({ steps: [step({ status: "COMPLETED" })] }),
    CONTRACT,
    null,
    "design.md"
  );
  assert.ok(!hasPin(markup), "there is nothing left to press");
  // The real apostrophe, not `[\s\S]`/`workspace.s`: the codebase writes it
  // literally in the other components, and a loose pattern here would not notice
  // the day it changed to an entity or a straight quote.
  assert.match(
    text(markup),
    /DESIGN\.md is this workspace’s brand contract/
  );
});

test("a different pinned file leaves the pin on offer", () => {
  const markup = render(
    goal({ steps: [step({ status: "COMPLETED" })] }),
    CONTRACT,
    null,
    "BRAND.md"
  );
  assert.match(text(markup), /Pin as brand contract/);
  assert.ok(!pinDisabled(markup), "a different pinned file does not block this one");
  assert.doesNotMatch(text(markup), /is this workspace/);
});

test("the review gate outranks the pin on the card too", () => {
  // "This is the contract" is a claim about the reviewed draft, so an
  // unreviewed goal in a workspace that happens to obey the same path still
  // gets the review reason and not the pinned state.
  const shown = text(render(goal(), CONTRACT, null, "DESIGN.md"));
  assert.match(shown, /waiting on the review/);
  assert.doesNotMatch(shown, /is this workspace/);
});

test("what the last click reported is on the card, both ways", () => {
  const good = render(
    goal({ steps: [step({ status: "COMPLETED" })] }),
    CONTRACT,
    { goalId: "g1", ok: true, message: "pinned as the workspace's contract" }
  );
  assert.match(text(good), /pinned as the workspace.s contract/);
  const bad = render(
    goal({ steps: [step({ status: "COMPLETED" })] }),
    CONTRACT,
    { goalId: "g1", ok: false, message: "DESIGN.md is not readable text" }
  );
  assert.match(text(bad), /DESIGN\.md is not readable text/);
});

test("another goal's click is not reported on this card", () => {
  // A message that cannot collide with anything else on the card, and an
  // assertion about the message rather than about where it sits: the pin row is
  // not the last thing rendered, so "does the text end with this" is not the
  // question — "is this text here at all" is.
  const shown = text(
    render(
      goal({ steps: [step({ status: "COMPLETED" })] }),
      CONTRACT,
      {
        goalId: "some-other-goal",
        ok: true,
        message: "acme is now the brand contract",
      }
    )
  );
  assert.doesNotMatch(shown, /acme is now the brand contract/);
  // And it is the card's own click that would be reported, so the mechanism is
  // live rather than simply broken.
  const own = text(
    render(
      goal({ steps: [step({ status: "COMPLETED" })] }),
      CONTRACT,
      { goalId: "g1", ok: true, message: "acme is now the brand contract" }
    )
  );
  assert.match(own, /acme is now the brand contract/);
});

test("a click that failed is not reported on a card the user cannot act on", () => {
  // The outcome belongs to the *ready* state: a refused pin has a reason, and
  // the engine's own complaint about the last attempt is noise next to it.
  const shown = text(
    render(
      goal({ steps: [step({ status: "PENDING" })] }),
      CONTRACT,
      { goalId: "g1", ok: false, message: "DESIGN.md is not readable text" }
    )
  );
  assert.doesNotMatch(shown, /not readable text/);
  assert.match(shown, /waiting on the review/);
});

// ── the body, and the card's other modes ───────────────────────────────────

test("the body is there to be read, behind a click", () => {
  // Collapsed, unlike the knowledge card: a full brand contract with a token
  // list would otherwise swallow the transcript it is sitting in.
  const markup = render(goal({ steps: [step({ status: "COMPLETED" })] }));
  // `renderToStaticMarkup` emits `class=`, not JSX's `className=`.
  assert.match(markup, /<details class="text-xs text-codify-muted">/);
  assert.doesNotMatch(markup, /<details[^>]*\bopen\b/);
  assert.ok(markup.includes("ink #0d1117"), "the body is rendered, not merely counted");
  assert.match(text(markup), new RegExp(`\\(${BODY.length} chars\\)`));
});

test("a normal goal's contract is an input, so the card has no pin", () => {
  const shown = text(renderBare({ ...CONTRACT, mode: "normal" }));
  assert.match(shown, /Design direction/);
  assert.ok(!hasPin(renderBare({ ...CONTRACT, mode: "normal" })));
  assert.doesNotMatch(shown, /Pin as brand contract/);
  assert.doesNotMatch(shown, /is this workspace/);
});

test("a card with no goal still shows the body it was handed", () => {
  // The `design_contract` event arrives during planning, and a transcript
  // message can carry no goal at all. Only the click needs an id to send, so the
  // authored text is on screen regardless — a document the user cannot read is
  // not a deliverable, and this is the case that used to lose it.
  const markup = renderBare();
  assert.match(text(markup), /Design deliverable/);
  assert.match(text(markup), /DESIGN\.md/);
  assert.ok(markup.includes("ink #0d1117"), "the body itself, not a count");
  assert.match(text(markup), /the draft, not yet reviewed/);
  // No plan and no review, so there is no id to pin and nothing to offer.
  assert.ok(!hasPin(markup));
  for (const junk of ["undefined", "NaN", "[object Object]"]) {
    assert.ok(!markup.includes(junk), `the card should never render ${junk}`);
  }
});

test("a body-less design contract renders no deliverable section at all", () => {
  // The body is the section's reason to exist. A card that drew the divider, the
  // pin and an empty disclosure would be furniture for a file with no text.
  const markup = renderBare({ ...CONTRACT, design_md: "" });
  assert.match(text(markup), /Design deliverable/);
  assert.doesNotMatch(markup, /<details/);
  assert.doesNotMatch(markup, /Pin as brand contract/);
});
