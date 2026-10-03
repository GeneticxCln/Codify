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
    [
      "tab:tab-1", "tab:tab-2", "conversation:c9",
      "settings:keys", "settings:agents", "settings:audio", "settings:appearance", "settings:about",
      // Opening a machine is always on offer, and sits after settings and before anything about a split.
      "action:new-machine", "action:new-machine-network",
    ]
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
    ["keys", "agents", "audio", "appearance", "about"]
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

// ── split actions ───────────────────────────────────────────────────────────

const chatTab = (id: string): Tab => ({ id, kind: "chat", title: `Chat ${id}`, conversationId: id });
const termTab = (id: string): Tab => ({ id, kind: "terminal", title: `Shell ${id}` });
const pageTab = (id: string): Tab => ({ id, kind: "browser", title: `Page ${id}`, url: "https://example.com" });

const actionOf = (item: PaletteItem) => (item.kind === "action" ? item.action : null);
/** What the palette offers about the split: every action except opening a machine, which is not about the split. */
const actionItems = (sources: PaletteSources): PaletteItem[] =>
  buildPaletteItems(sources).filter((i) => i.kind === "action" && actionOf(i)?.type !== "new-machine");

test("with no word about splits, the palette has no split actions: what it listed before is what it lists", () => {
  const sources: PaletteSources = { tabs: [chatTab("c"), termTab("t")], activeId: "c", conversations: [] };
  assert.deepEqual(actionItems(sources), []);
});

test("while a split is showing the palette offers to close it, and nothing else about splits", () => {
  const items = actionItems({ tabs: [chatTab("c"), termTab("t")], activeId: "c", conversations: [], split: { showing: true } });
  assert.deepEqual(items.map(actionOf), [{ type: "close-split" }]);
  assert.equal(items[0].title, "Close split");
});

test("with no split showing it offers a new terminal beside this tab, and each tab that can sit beside it", () => {
  const items = actionItems({
    tabs: [chatTab("c"), termTab("t1"), chatTab("c2"), pageTab("p"), termTab("t2")],
    activeId: "c",
    conversations: [],
    split: { showing: false },
  });
  assert.deepEqual(
    items.map(actionOf),
    [
      { type: "split-new-terminal" },
      { type: "show-beside", tabId: "t1" },
      { type: "show-beside", tabId: "p" },
      { type: "show-beside", tabId: "t2" },
    ],
    "a chat beside a chat was offered, or a page was left out",
  );
  assert.deepEqual(
    items.map((i) => i.title),
    ["Split: new terminal beside this one", "Show beside: Shell t1", "Show beside: Page p", "Show beside: Shell t2"],
  );
});

test("from a terminal, another terminal and a chat can sit beside it", () => {
  const items = actionItems({
    tabs: [termTab("t"), chatTab("c"), termTab("t2")],
    activeId: "t",
    conversations: [],
    split: { showing: false },
  });
  assert.deepEqual(items.map(actionOf), [
    { type: "split-new-terminal" },
    { type: "show-beside", tabId: "c" },
    { type: "show-beside", tabId: "t2" },
  ]);
});

test("from a browser page, a new terminal and each tab but another page can sit beside it", () => {
  const items = actionItems({
    tabs: [pageTab("p"), termTab("t"), pageTab("q"), chatTab("c")],
    activeId: "p",
    conversations: [],
    split: { showing: false },
  });
  assert.deepEqual(items.map(actionOf), [
    { type: "split-new-terminal" },
    { type: "show-beside", tabId: "t" },
    { type: "show-beside", tabId: "c" },
  ]);
});

test("nothing open has no split to offer", () => {
  assert.deepEqual(actionItems({ tabs: [], activeId: null, conversations: [], split: { showing: false } }), []);
});

test("the split actions come last, under their own heading, and have unique ids", () => {
  const items = buildPaletteItems({
    tabs: [chatTab("c"), termTab("t")],
    activeId: "c",
    conversations: [convo({ id: "c" })],
    split: { showing: false },
  });
  const kinds = items.map((i) => i.kind);
  assert.deepEqual([...new Set(kinds)], ["tab", "conversation", "settings", "action"], "no files were offered, so no file group");
  assert.equal(new Set(items.map((i) => i.id)).size, items.length, "two items share an id");
  assert.ok(actionItems({ tabs: [chatTab("c"), termTab("t")], activeId: "c", conversations: [], split: { showing: false } }).every((i) => i.label === "Split"));
  const lastMachine = items.map((i) => actionOf(i)?.type).lastIndexOf("new-machine");
  const firstSplit = items.findIndex((i) => ["split-new-terminal", "show-beside", "close-split"].includes(actionOf(i)?.type ?? ""));
  assert.ok(lastMachine < firstSplit, "a machine row came after a split row");
});

// ── opening a machine ───────────────────────────────────────────────────────

const machineItems = (sources: PaletteSources): PaletteItem[] =>
  buildPaletteItems(sources).filter((i) => actionOf(i)?.type === "new-machine");

test("opening a machine is always offered: with nothing open, with a split showing, and with no word about splits at all", () => {
  for (const sources of [
    { tabs: [], activeId: null, conversations: [] } as PaletteSources,
    { tabs: [chatTab("c")], activeId: "c", conversations: [], split: { showing: true } } as PaletteSources,
    { tabs: [chatTab("c")], activeId: "c", conversations: [] } as PaletteSources,
  ]) {
    assert.equal(machineItems(sources).length, 2);
  }
});

test("the network is a row of its own, so it cannot be had by pressing Enter on the first", () => {
  const [plain, withNetwork] = machineItems({ tabs: [], activeId: null, conversations: [] });

  assert.deepEqual(actionOf(plain), { type: "new-machine", network: false });
  assert.deepEqual(actionOf(withNetwork), { type: "new-machine", network: true });
  assert.match(plain.title, /no network/i);
  assert.doesNotMatch(plain.title, /with network/i);
  assert.match(withNetwork.title, /with network/i, "the row that gives a machine a network does not say so");
  assert.notEqual(plain.id, withNetwork.id);
});

test("the machine rows are under their own heading, and not under the split's", () => {
  const items = machineItems({ tabs: [], activeId: null, conversations: [] });

  assert.ok(items.every((i) => i.label === "Machine"), "a heading that said Split sat over a row that is not about the split");
  const markup = renderToStaticMarkup(
    React.createElement(CommandPalette, { open: true, items: buildPaletteItems({ tabs: [], activeId: null, conversations: [] }), onClose: noop, onSelect: noop }),
  );
  assert.match(markup, />Machine</);
});

test("typing 'machine' finds both rows, and 'network' finds the one that has one", () => {
  const items = buildPaletteItems({ tabs: [chatTab("c")], activeId: "c", conversations: [], split: { showing: false } });

  assert.deepEqual(filterPalette(items, "machine").map((i) => actionOf(i)), [
    { type: "new-machine", network: false },
    { type: "new-machine", network: true },
  ]);
  assert.deepEqual(filterPalette(items, "with network").map((i) => actionOf(i)), [{ type: "new-machine", network: true }]);
});

test("typing 'split' finds the split actions", () => {
  const items = buildPaletteItems({ tabs: [chatTab("c"), termTab("t")], activeId: "c", conversations: [], split: { showing: false } });
  const found = filterPalette(items, "split");
  assert.ok(found.length >= 2 && found.every((i) => i.kind === "action"), found.map((i) => i.title).join(" | "));
  const closing = filterPalette(
    buildPaletteItems({ tabs: [chatTab("c"), termTab("t")], activeId: "c", conversations: [], split: { showing: true } }),
    "close",
  );
  assert.deepEqual(closing.map((i) => i.title), ["Close split"]);
});

test("the palette draws the split group under its own heading", () => {
  const items = buildPaletteItems({ tabs: [chatTab("c"), termTab("t")], activeId: "c", conversations: [], split: { showing: true } });
  const markup = renderToStaticMarkup(
    React.createElement(CommandPalette, { open: true, items, onClose: () => {}, onSelect: () => {} }),
  );
  assert.match(markup, />Split</);
  assert.match(markup, /Close split/);
});


// ── files: "Open file…" ────────────────────────────────────────────────────

const FILES = [
  "README.md",
  "src/app/main.py",
  "src/app/models.py",
  "src/util/main_helpers.py",
  "tests/test_main.py",
  "docs/main.md",
];

const withFiles = (over: Partial<PaletteSources> = {}): PaletteItem[] =>
  buildPaletteItems(sources({ tabs: [tab()], activeId: "tab-1", conversations: [convo()], files: FILES, ...over }));

test("files are offered as 'Open file' rows named by their path, after everything else", () => {
  const items = withFiles();
  const files = items.filter((i) => i.kind === "file");

  assert.deepEqual(files.map((i) => (i.kind === "file" ? i.path : "")), FILES);
  assert.ok(files.every((i) => i.label === "Open file" && i.title === (i.kind === "file" ? i.path : "")));
  assert.equal(items.findIndex((i) => i.kind === "file"), items.length - files.length, "files come last");
  assert.equal(new Set(items.map((i) => i.id)).size, items.length, "two items share an id");
});

test("a palette given no files has none", () => {
  assert.equal(buildPaletteItems(sources({ tabs: [tab()], conversations: [convo()] })).some((i) => i.kind === "file"), false);
  assert.equal(withFiles({ files: [] }).some((i) => i.kind === "file"), false);
});

test("an empty query lists no files: thousands of rows are a search, not a menu", () => {
  const shown = filterPalette(withFiles(), "");

  assert.equal(shown.some((i) => i.kind === "file"), false);
  assert.equal(shown.length, withFiles().length - FILES.length, "everything else is still there");
});

test("a query finds files by any part of the path, and every token must match", () => {
  const titles = (q: string): string[] => filterPalette(withFiles(), q).filter((i) => i.kind === "file").map((i) => i.title);

  assert.deepEqual(titles("models"), ["src/app/models.py"]);
  assert.deepEqual(titles("app main"), ["src/app/main.py"]);
  assert.deepEqual(titles("zzz"), []);
});

test("a file whose name starts with the query beats one that only contains it somewhere in its path", () => {
  const titles = filterPalette(withFiles(), "main").filter((i) => i.kind === "file").map((i) => i.title);

  assert.equal(titles[0], "src/app/main.py", titles.join(" | "));
  assert.ok(titles.indexOf("src/app/main.py") < titles.indexOf("docs/main.md") || titles.indexOf("docs/main.md") < titles.indexOf("src/util/main_helpers.py"));
  assert.equal(titles.at(-1), "tests/test_main.py", "the file that only contains the word, deep in its name, comes last");
});

test("only the best thirty files are shown, and nothing else is cut", () => {
  const many = Array.from({ length: 200 }, (_, i) => `src/gen/file${i}.ts`);
  const shown = filterPalette(withFiles({ files: many }), "file");

  assert.equal(shown.filter((i) => i.kind === "file").length, 30);

  const everything = filterPalette(withFiles({ files: many }), "tab");
  assert.equal(everything.filter((i) => i.kind === "tab").length, 1, "a tab is never dropped to make room for files");
});

test("a tab that matches less well than thirty files is still shown after them", () => {
  const many = Array.from({ length: 60 }, (_, i) => `src/gen/file${i}.ts`);
  const items = buildPaletteItems(
    sources({
      tabs: [tab({ id: "tab-9", title: "my file notes", conversationId: undefined })],
      activeId: "tab-9",
      conversations: [convo({ id: "c9", title: "about a file" })],
      files: many,
    }),
  );

  const shown = filterPalette(items, "file");

  assert.equal(shown.filter((i) => i.kind === "file").length, 30);
  assert.equal(shown.some((i) => i.kind === "tab"), true, "the tab was dropped to make room for files");
  assert.equal(shown.some((i) => i.kind === "conversation"), true, "the thread was dropped to make room for files");
});

test("the palette draws the file group under its own heading, once there is a query", async () => {
  const { withDom } = await import("./dom.ts");
  await withDom(async (dom) => {
    await dom.render(
      React.createElement(CommandPalette, { open: true, items: withFiles(), onClose: () => {}, onSelect: () => {} }),
    );
    const input = dom.byLabel("Command palette") as HTMLInputElement;

    assert.equal(dom.container.textContent?.includes("src/app/models.py"), false, "files are not listed before a query");
    await dom.fill(input, "models");

    assert.match(dom.container.textContent ?? "", /Files/);
    assert.match(dom.container.textContent ?? "", /src\/app\/models\.py/);
    assert.equal(dom.container.querySelectorAll('[role="option"]').length, 1);
  });
});

test("the search box says it opens files", () => {
  const markup = renderToStaticMarkup(
    React.createElement(CommandPalette, { open: true, items: [], onClose: () => {}, onSelect: () => {} }),
  );

  assert.match(markup, /placeholder="[^"]*file/i);
});
