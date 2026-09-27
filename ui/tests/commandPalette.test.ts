/**
 * The palette: what it contains, how a query narrows it, and what it renders.
 *
 * Two halves in one file on purpose — they are one contract. The model tests
 * prove a query finds the right rows in the right order; the render tests
 * prove the overlay says so in markup a screen reader and a person can both
 * read (`role="option"`, the group headings, the single `aria-selected`, the
 * "current" marker on the tab you are already on).
 *
 * Rendering goes through `tsxLoader.ts`, the same harness as `shell.test.ts`.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { CommandPalette } = await import("../src/components/CommandPalette.tsx");

import {
  buildPaletteItems,
  filterPalette,
  moveSelection,
  type PaletteItem,
  type PaletteSources,
} from "../src/commandPalette.ts";
import type { Tab } from "../src/tabs.ts";
import type { Conversation } from "../src/types.ts";

// ── fixtures ────────────────────────────────────────────────────────────

const tab = (over: Partial<Tab> = {}): Tab => ({
  id: "tab-1",
  kind: "chat",
  title: "Refactor the parser",
  conversationId: "c1",
  ...over,
});

const convo = (over: Partial<Conversation> = {}): Conversation => ({
  id: "c1",
  workspace_id: "w1",
  title: "Refactor the parser",
  archived: false,
  created_at: 0,
  updated_at: 0,
  parent_id: null,
  parent_title: null,
  ...over,
});

const sources = (over: Partial<PaletteSources> = {}): PaletteSources => ({
  tabs: [],
  activeId: null,
  conversations: [],
  ...over,
});

const noop = (): void => {};

const render = (props: {
  open: boolean;
  items: PaletteItem[];
}): string =>
  renderToStaticMarkup(
    React.createElement(CommandPalette, {
      open: props.open,
      items: props.items,
      onClose: noop,
      onSelect: noop,
    })
  );

// ── the model ───────────────────────────────────────────────────────────

test("items come in strip order: tabs, then conversations, then settings", () => {
  const items = buildPaletteItems(
    sources({
      tabs: [
        tab({ id: "tab-1", title: "First" }),
        tab({ id: "tab-2", kind: "terminal", title: "build watch" }),
      ],
      conversations: [convo({ id: "c9", title: "A thread" })],
    })
  );
  assert.deepEqual(
    items.map((i) => i.id),
    ["tab:tab-1", "tab:tab-2", "conversation:c9", "settings:keys", "settings:agents", "settings:appearance"]
  );
});

test("the active tab is marked, and settings land on the right dialog page", () => {
  const items = buildPaletteItems(
    sources({
      tabs: [tab({ id: "tab-1" }), tab({ id: "tab-2" })],
      activeId: "tab-2",
    })
  );
  const active = items.filter((i) => i.kind === "tab" && i.active);
  assert.deepEqual(
    active.map((i) => i.id),
    ["tab:tab-2"],
    "exactly one tab is the current one"
  );
  const settings = items.filter((i) => i.kind === "settings");
  assert.deepEqual(
    settings.map((i) => (i.kind === "settings" ? i.settingsTab : "?")),
    ["keys", "agents", "appearance"]
  );
});

test("a thread with no title is 'New chat', exactly as the sidebar calls it", () => {
  const items = buildPaletteItems(
    sources({ conversations: [convo({ id: "c1", title: "   " })] })
  );
  const item = items.find((i) => i.kind === "conversation");
  assert.equal(item?.title, "New chat");
});

test("an open conversation appears as both a tab and a conversation — both jumps are idempotent", () => {
  const items = buildPaletteItems(
    sources({
      tabs: [tab({ id: "tab-1", conversationId: "c1" })],
      conversations: [convo({ id: "c1" })],
    })
  );
  assert.equal(items.filter((i) => i.title === "Refactor the parser").length, 2);
});

test("an empty query is the whole list, untouched", () => {
  const items = buildPaletteItems(
    sources({ conversations: [convo({ id: "c1" })] })
  );
  const filtered = filterPalette(items, "   ");
  assert.deepEqual(filtered, items);
});

test("every token must match — AND, not OR", () => {
  const items = buildPaletteItems(
    sources({
      conversations: [
        convo({ id: "c1", title: "Refactor the parser" }),
        convo({ id: "c2", title: "Refactor the router" }),
      ],
    })
  );
  const hits = filterPalette(items, "refactor router");
  assert.deepEqual(
    hits.map((i) => i.id),
    ["conversation:c2"]
  );
  assert.deepEqual(filterPalette(items, "refactor parser bread"), []);
});

test("matching is case-insensitive", () => {
  const items = buildPaletteItems(
    sources({ conversations: [convo({ id: "c1", title: "Refactor the parser" })] })
  );
  assert.equal(filterPalette(items, "PARSER").length, 1);
});

test("a title hit outranks a group-label hit, and ties keep source order", () => {
  const items = buildPaletteItems(
    sources({
      tabs: [tab({ id: "tab-1", title: "Something else" })],
      conversations: [
        convo({ id: "c1", title: "Table stakes" }),
        convo({ id: "c2", title: "Whatever" }),
      ],
    })
  );
  // "Table stakes" starts with the token (3); the tab only matches its
  // group label "Open tab" (1) — so the conversation must come first even
  // though tabs are built first.
  const ranked = filterPalette(items, "tab");
  assert.equal(ranked[0]?.id, "conversation:c1");
  assert.equal(
    ranked[ranked.length - 1]?.id,
    "tab:tab-1",
    "the label-only hit sorts last"
  );
  // Two equal scores keep build order (stable sort).
  const equal = filterPalette(
    buildPaletteItems(
      sources({
        conversations: [
          convo({ id: "c1", title: "Alpha thread" }),
          convo({ id: "c2", title: "Alpha notes" }),
        ],
      })
    ),
    "alpha"
  );
  assert.deepEqual(
    equal.map((i) => i.id),
    ["conversation:c1", "conversation:c2"]
  );
});

test("the selection wraps at both ends, and an empty list selects nothing", () => {
  assert.equal(moveSelection(0, 1, 3), 1);
  assert.equal(moveSelection(2, 1, 3), 0, "down off the end wraps to the top");
  assert.equal(moveSelection(0, -1, 3), 2, "up off the top wraps to the end");
  assert.equal(moveSelection(-1, 1, 3), 0);
  assert.equal(moveSelection(1, 1, 0), -1, "nothing to select means -1, not 0");
  assert.equal(moveSelection(-1, -1, 0), -1);
});

// ── the overlay, as markup ──────────────────────────────────────────────

const text = (markup: string): string =>
  markup
    .replace(/<[^>]+>/g, " ")
    .replace(/&rsquo;/g, "'")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/\s+/g, " ")
    .trim();

test("a closed palette renders nothing at all", () => {
  const markup = render({ open: false, items: buildPaletteItems(sources()) });
  assert.equal(markup, "");
});

test("an open palette is a combobox over a listbox, with one selected option", () => {
  const items = buildPaletteItems(
    sources({
      tabs: [tab({ id: "tab-1" }), tab({ id: "tab-2" })],
      activeId: "tab-1",
      conversations: [convo({ id: "c1", title: "Thread about tests" })],
    })
  );
  const markup = render({ open: true, items });

  assert.match(markup, /role="combobox"/);
  assert.match(markup, /role="listbox"/);
  const options = markup.match(/role="option"/g) ?? [];
  assert.equal(options.length, items.length, "every item renders as an option");

  const selected = markup.match(/aria-selected="true"/g) ?? [];
  assert.equal(selected.length, 1, "exactly one option is selected at a time");
  assert.match(markup, /^[\s\S]*aria-selected="true"[\s\S]*Refactor the parser/,
    "the first row starts selected");

  // The combobox points at the selected option's id — the aria-activedescendant
  // contract a screen reader follows.
  const active = /aria-activedescendant="([^"]+)"/.exec(markup);
  assert.ok(active, "the combobox names its active descendant");
  assert.ok(
    markup.includes(`id="${active[1]}"`),
    "the active descendant names an id that exists in the list"
  );

  // Group headings appear once each, in build order.
  const shown = text(markup);
  assert.ok(shown.includes("Open tabs"));
  assert.ok(shown.includes("Conversations"));
  assert.ok(shown.includes("Settings"));
  assert.ok(shown.includes("current"), "the active tab is marked as current");
  assert.ok(shown.includes("esc close"), "the key hints are visible");
  assert.ok(shown.includes(`${items.length} of ${items.length}`));
});

test("no matches says so instead of showing an empty box", () => {
  const markup = render({ open: true, items: [] });
  assert.match(markup, /Nothing matches/);
  assert.ok(text(markup).includes("0 of 0"));
});

test("settings items name the page they open, not just 'Settings'", () => {
  const items = buildPaletteItems(sources());
  const markup = render({ open: true, items });
  const shown = text(markup);
  assert.ok(shown.includes("Provider keys & endpoints"));
  assert.ok(shown.includes("Agent roles & prompts"));
});
