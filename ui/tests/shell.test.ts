/**
 * The shell's two new surfaces, as markup.
 *
 * `tabs.test.ts` covers the arithmetic. This covers what a person sees: that an
 * empty sidebar says what it is for rather than being a blank column, that the
 * thread you are on is distinguishable from the ones you are not, that a tab's
 * kind is legible to a screen reader and not only by its icon.
 *
 * Rendered through `react-dom/server` from the real components in `src/`, via
 * the loader in `tsxLoader.ts`.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { Sidebar } = await import("../src/components/Sidebar.tsx");
const { TabBar } = await import("../src/components/TabBar.tsx");
const { ThreadMenu } = await import("../src/components/ThreadMenu.tsx");

import type { Conversation, Workspace } from "../src/types.ts";
import type { Tab } from "../src/tabs.ts";

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

const noop = (): void => {};

const sidebar = (
  conversations: Conversation[],
  activeConversationId?: string
): string =>
  renderToStaticMarkup(
    React.createElement(Sidebar, {
      conversations,
      activeConversationId,
      onNewChat: noop,
      onNewThread: noop,
      onSelect: noop,
      onRename: noop,
      onArchive: noop,
    })
  );

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

// ── the sidebar ─────────────────────────────────────────────────────────

test("the sidebar always offers a new chat", () => {
  const markup = sidebar([]);
  assertClean(markup, "an empty sidebar");
  assert.match(text(markup), /New chat/);
});

test("an empty sidebar says what it is for", () => {
  // A blank column next to a New chat button is a control with no explanation.
  const shown = text(sidebar([]));
  assert.match(shown, /No conversations yet/);
  assert.match(shown, /stays here between sessions/);
});

test("a thread list is legible and names the active one", () => {
  const markup = sidebar(
    [convo(), convo({ id: "c2", title: "Add an index" })],
    "c2"
  );
  assertClean(markup, "a thread list");
  const shown = text(markup);
  assert.match(shown, /Refactor the parser/);
  assert.match(shown, /Add an index/);
  // The active row is the one carrying the selected treatment, not merely
  // present — otherwise "which thread am I on" is unanswerable.
  assert.match(markup, /bg-codify-raised border-codify-border/);
});

test("an unnamed thread gets a placeholder rather than an empty row", () => {
  // The engine stores no title until a turn gives it one, on purpose. The
  // placeholder is the panel's to supply.
  const shown = text(sidebar([convo({ title: "" })]));
  assert.match(shown, /New chat/);
  assert.doesNotMatch(shown, /undefined/);
});

test("an archived thread says so instead of pretending to be live", () => {
  const shown = text(sidebar([convo({ archived: true })]));
  assert.match(shown, /archived/);
});

test("a thread can be renamed and archived from the panel", () => {
  const markup = sidebar([convo()]);
  assert.match(markup, /aria-label="Rename conversation"/);
  assert.match(markup, /aria-label="Archive conversation"/);
  // Archive is not delete: the panel's action hides a thread and its label says
  // that, because the engine keeps the runs either way.
  assert.doesNotMatch(markup, /aria-label="Delete/);
});

// ── the tab strip ───────────────────────────────────────────────────────

const tab = (over: Partial<Tab> = {}): Tab => ({
  id: "t1",
  kind: "chat",
  title: "Refactor the parser",
  ...over,
});

const tabbar = (
  tabs: Tab[],
  activeId: string | null,
  busy: string[] = [],
  workspaces: Workspace[] = [],
): string =>
  renderToStaticMarkup(
    React.createElement(TabBar, {
      tabs,
      activeId,
      onFocus: noop,
      onClose: noop,
      busyTabIds: busy,
      workspaces,
    }),
  );

const ws = (over: Partial<Workspace> = {}): Workspace => ({
  id: "w1",
  name: "Codify",
  root_path: "/home/quinton/Projects/Codify",
  design_contract_path: "",
  created_at: 0,
  ...over,
});

test("a new tab is called by its folder, not by the placeholder", () => {
  // "New chat" names nothing — not the thread, not where it is, not which of
  // three identical tabs you are looking at. For a thread that has not named
  // itself, the folder is the only real fact there is.
  const shown = tabbar([tab({ title: "New chat", workspaceId: "w1" })], "t1", [], [ws()]);
  const shownText = shown.replace(/<[^>]+>/g, " ");
  assert.match(shownText, /Codify/, "a new tab does not show its folder");
  assert.doesNotMatch(
    shownText,
    /New chat/,
    `a new tab still says "New chat": ${shownText}`,
  );
  // One label, not two. A tab carrying the folder *and* the placeholder is two
  // names for one thing, and the placeholder is the one that means nothing.
  assert.equal(
    (shownText.match(/Codify/g) || []).length,
    1,
    `the folder is printed more than once: ${shownText}`,
  );
});

test("a named thread keeps its own name, and the folder stays out of the tab", () => {
  // Once a thread has a name, that is what it is called. The folder is still
  // known — it is in the tooltip and the accessible name — it is just not worth
  // spending strip width on.
  const shown = tabbar([tab({ title: "Refactor the parser", workspaceId: "w1" })], "t1", [], [ws()]);
  const shownText = shown.replace(/<[^>]+>/g, " ");
  assert.match(shownText, /Refactor the parser/);
  assert.doesNotMatch(
    shownText,
    /Codify/,
    `the folder is crowding a named tab: ${shownText}`,
  );
  // The exact path is the tooltip and the accessible name — where a path
  // belongs, asked once rather than paid for by every tab.
  assert.match(shown, /title="\/home\/quinton\/Projects\/Codify — Refactor the parser"/);
  assert.match(shown, /Conversation: Refactor the parser in \/home\/quinton\/Projects\/Codify/);
});

test("a tab with no recorded folder falls back rather than guessing one", () => {
  // Naming a row from the composer's selection would claim a directory the
  // thread is not in. No folder recorded means the generic placeholder, which
  // is honest.
  const shown = tabbar([tab({ title: "New chat" })], "t1");
  assert.match(shown.replace(/<[^>]+>/g, " "), /New chat/);
  assert.doesNotMatch(shown, /Codify|undefined/);
});

test("a tab strip is a tablist, not a row of buttons", () => {
  const markup = tabbar([tab()], "t1");
  assert.match(markup, /role="tablist"/);
  assert.match(markup, /role="tab"/);
  assert.match(markup, /aria-selected="true"/);
});

test("each tab's kind is named, not only iconned", () => {
  // An icon is a shape, and a screen reader gets nothing from it. The kind is
  // in the accessible name, which is what makes a terminal tab reachable rather
  // than a mystery glyph.
  const markup = tabbar(
    [
      tab({ id: "c1", kind: "chat", title: "Refactor the parser" }),
      tab({ id: "t1", kind: "terminal", title: "zsh" }),
      tab({ id: "b1", kind: "browser", title: "docs.rs" }),
    ],
    "t1"
  );
  assertClean(markup, "a three-kind strip");
  assert.match(markup, /aria-label="Conversation: Refactor the parser"/);
  assert.match(markup, /aria-label="Terminal: zsh"/);
  assert.match(markup, /aria-label="Browser: docs\.rs"/);
});

test("the active tab is the selected one", () => {
  const markup = tabbar(
    [tab({ id: "c1", title: "one" }), tab({ id: "c2", title: "two" })],
    "c2"
  );
  assert.match(
    markup,
    /aria-selected="true"[^>]*aria-label="Conversation: two"/
  );
  assert.doesNotMatch(
    markup,
    /aria-selected="true"[^>]*aria-label="Conversation: one"/
  );
});

test("only a tab with a live run shows the busy dot", () => {
  // The strip is the only place that knows a tab is busy when it is not the one
  // on screen. A dot on every tab would be decoration; a dot on none is a
  // background run that looks finished.
  const busy = tabbar(
    [tab({ id: "c1", title: "one" }), tab({ id: "c2", title: "two" })],
    "c1",
    ["c2"]
  );
  const calm = tabbar(
    [tab({ id: "c1", title: "one" }), tab({ id: "c2", title: "two" })],
    "c1"
  );
  // Exactly one hidden dot in the busy strip, none in the calm one.
  assert.equal((busy.match(/bg-blue-400 flex-shrink-0/g) ?? []).length, 1);
  assert.equal((calm.match(/bg-blue-400 flex-shrink-0/g) ?? []).length, 0);
});

test("every tab can be closed, and the button names the one it closes", () => {
  // Not `close`: two tabs labelled "Close" are two identical controls, and
  // which one gets closed is the entire question.
  const markup = tabbar(
    [tab({ id: "c1", title: "one" }), tab({ id: "c2", title: "two" })],
    "c1"
  );
  assert.match(markup, /aria-label="Close conversation: one"/);
  assert.match(markup, /aria-label="Close conversation: two"/);
});

test("an empty strip renders as an empty strip, not as nothing", () => {
  // The shell is open on nothing, which is a state the user can act on. A
  // missing tablist here would leave no landmark at all.
  const markup = tabbar([], null);
  assertClean(markup, "an empty strip");
  assert.match(markup, /role="tablist"/);
});

// ── the three shell badges ───────────────────────────────────────────────
//
// Browser, Terminal and Settings moved here from the header, where three
// labelled buttons sat in a row of things that have nothing to do with each
// other. They are at the foot of the panel, named, and spread across its width.
//
// Three claims, three tests, because each has failed in a different way:
// *that they are named* — an icon with no name is a control nobody using a
// screen reader can find;
// *that they are at the foot* — a badge in the top row beside "New chat" is a
// different layout that passes every "is it rendered?" test there is, which is
// exactly what I shipped first;
// *that they are spread* — three glyphs huddled in one corner of a 240px
// column read as a toolbar someone dropped there.

const sidebarWithBadges = (over: Record<string, unknown> = {}): string =>
  renderToStaticMarkup(
    React.createElement(Sidebar, {
      conversations: [],
      onNewChat: noop,
      onNewThread: noop,
      onSelect: noop,
      onRename: noop,
      onArchive: noop,
      onOpenBrowser: noop,
      onOpenTerminal: noop,
      onOpenSettings: noop,
      ...over,
    })
  );

test("the three shell surfaces are named, not left as glyphs", () => {
  const markup = sidebarWithBadges();
  // Visible text, not an `aria-label` on an icon: the name is the point now, and
  // a name only a screen reader can reach is not a name.
  const words = text(markup);
  assert.match(words, /Browser/);
  assert.match(words, /Terminal/);
  assert.match(words, /Settings/);
  // And the longer sentence still has somewhere to live.
  assert.match(markup, /title="Browser — the page opens in its own window"/);
  assert.match(markup, /title="Terminal — a shell in this workspace"/);
  assert.match(markup, /title="Keys &amp; Endpoints"/);
  assertClean(markup, "the panel with its three named badges");
});

test("the badges are spread across the foot, each in its own share", () => {
  // A layout claim, so a class is the only honest place to pin it.
  //
  // `flex-1` per child is what does the work, and it is the only version whose
  // gaps stay even whichever two of the three are present — a fixed `gap` would
  // leave the last one hard against the right edge whenever one is omitted.
  // There is deliberately no `justify-between`: with `flex-1` filling the row
  // there is no free space for it to distribute, so it would be a class that
  // claims to do this job and does nothing. This test is what caught that
  // sentence being written before the class was.
  const markup = sidebarWithBadges();
  const container = markup.match(/<div class="([^"]*border-t[^"]*)"/);
  assert.ok(container, "the foot row is no longer a ruled-off row at the bottom");
  assert.doesNotMatch(
    container[1],
    /justify-between/,
    "the foot row claims to spread itself with justify-between, which is a " +
      "no-op once each child is flex-1",
  );
  const flexOnes = (markup.match(/flex-1 min-w-0 flex-col/g) ?? []).length;
  assert.equal(
    flexOnes,
    3,
    `only ${flexOnes} of the three badges take an equal share of the width`,
  );
});

test("the badges cannot paint outside the panel, whatever a name is called", () => {
  // This is the bug the running app showed and the markup could not: laid out
  // inline, the three came to 241px inside a 223px row, and Settings' right edge
  // sat at 249px in a 240px panel. `Button` carries `whitespace-nowrap`, so a
  // squeezed label cannot wrap — it overflows instead, which is the failure its
  // own comment warns about. No render assertion catches a width, so the
  // *structure* that prevents it is pinned here instead.
  //
  // The icon above the name is the part that makes it fit: a cell is then as
  // wide as its longest word rather than that word plus a 12px icon and a 6px
  // gap.
  const markup = sidebarWithBadges();
  const row = markup.match(/<div class="([^"]*border-t[^"]*)"/);
  assert.ok(row, "the foot row is no longer a ruled-off row at the bottom");
  assert.match(
    row[1],
    /overflow-hidden/,
    `the foot row can paint outside the panel: class="${row[1]}"`,
  );
  const cells = markup.match(/<button[^>]*class="[^"]*flex-1 min-w-0 flex-col[^"]*"/g) ?? [];
  assert.equal(cells.length, 3, "the three badges no longer stack icon over name");
  for (const cell of cells) {
    assert.match(cell, /min-w-0/, "a badge can be squeezed past its own name");
  }
  // And the name itself truncates rather than pushing the cell wider.
  assert.equal(
    (markup.match(/class="text-2xs truncate"/g) ?? []).length,
    3,
    "a badge's name is not truncated, so a longer one would escape the panel",
  );
});

test("the badges are below the threads, not above them", () => {
  const markup = sidebarWithBadges({
    conversations: [convo({ id: "c1", title: "one" })],
  });
  // Every anchor is a `title` or an `aria-label`. The first version of this test
  // anchored the thread on the title "one" and matched
  // `focus-visible:outline-none` in a class attribute 750 characters earlier —
  // a bare word in a class list is not an anchor.
  const newChat = markup.indexOf("New chat");
  const thread = markup.indexOf('aria-label="Archive conversation"');
  const badge = markup.indexOf('title="Browser — the page opens in its own window"');
  assert.ok(newChat >= 0 && thread >= 0 && badge >= 0, "a landmark went missing");
  assert.ok(newChat < thread, "New chat is no longer at the top of the panel");
  assert.ok(
    thread < badge,
    "the badges are above the thread list; they belong at the foot of the panel",
  );
  // And the foot means the foot: nothing is rendered below them. The `&amp;` is
  // decoded because this is raw markup — comparing against a bare `&` fails on
  // an escaping detail and says nothing about the layout.
  const lastTitle = ([...markup.matchAll(/title="([^"]+)"/g)].pop()?.[1] ?? "")
    .replace(/&amp;/g, "&");
  assert.equal(
    lastTitle,
    "Keys & Endpoints",
    `the last thing in the panel is "${lastTitle}", so the badges are not at ` +
      "the foot",
  );
});

test("New chat is on its own full-width row", () => {
  // It was narrowed to make room for the badges in a first attempt. Sharing a
  // row with them is the thing being undone, so the row goes back to being the
  // panel's one primary action on its own line.
  const markup = sidebarWithBadges();
  assert.match(text(markup), /New chat/);
  const row = markup.slice(
    markup.indexOf("New chat"),
    markup.indexOf('title="Browser'),
  );
  assert.doesNotMatch(row, /title="Browser|title="Terminal|title="Keys/);
});

test("a surface the shell does not offer is absent, not disabled", () => {
  // A greyed-out badge still advertises something that cannot be used, which is
  // the thing this change is trying to stop. Omitting the handler omits the
  // control.
  const markup = sidebarWithBadges({ onOpenTerminal: undefined });
  assert.match(text(markup), /Browser/);
  assert.doesNotMatch(text(markup), /Terminal/);
  // Two of three still each take a share, rather than the row closing up.
  assert.equal((markup.match(/flex-1 min-w-0 flex-col/g) ?? []).length, 2);
  const bare = sidebarWithBadges({
    onOpenBrowser: undefined,
    onOpenTerminal: undefined,
    onOpenSettings: undefined,
  });
  assert.doesNotMatch(bare, /title="Browser|title="Terminal|title="Keys/);
  assertClean(bare, "the panel with no badges");
});

test("the header no longer owns the three, and the panel is given them", () => {
  // `App.tsx` is not mounted by anything in this suite, so the move itself is
  // only checkable by reading it. Both halves matter: leaving the buttons in
  // the header would give a user two ways to do one thing, and passing the
  // handlers without removing the buttons would be exactly that.
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  // Comments are stripped before anything is matched. The header's own prose
  // contains the literal text "`<Button>`" — the connection pill's comment says
  // why it is *not* a Button — so a freeze that greps raw source matches a
  // sentence about a control and calls it a control.
  const withoutComments = (src: string): string =>
    src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  const header = withoutComments(
    app.split("<header")[1]?.split("</header>")[0] ?? ""
  );
  assert.ok(header.trim(), "App.tsx no longer has a header in the expected shape");
  // Browser and Terminal moved outright, so the handlers must not be reachable
  // from the header at all.
  assert.doesNotMatch(
    header,
    /handleNewBrowserTab|handleOpenTerminal/,
    "the header still opens a browser tab or a terminal; they are on the side " +
      "panel now",
  );
  // Settings is different, and deliberately: the connection pill has always
  // opened it on click, so "the header must not mention setIsSettingsOpen"
  // would be forbidding a control that should stay. What moved is the labelled
  // *button*, and the header has no `Button` left in it at all — the pill is a
  // raw `<button>`.
  assert.doesNotMatch(
    header,
    /<Button/,
    "the header still has a labelled button; Browser, Terminal and Settings " +
      "are on the side panel now",
  );
  // The folder went the same way. One label in the header claimed the
  // composer's workspace described every tab at once, and with two tabs in two
  // folders it was wrong for one of them — so it is the tab that says which
  // folder it is in now.
  assert.doesNotMatch(
    header,
    /root_path|FolderGit2|selectedWs\.name/,
    "the header still names the current folder; the tab does that per tab now",
  );
  const sidebar = app.split("<Sidebar")[1]?.split("/>")[0];
  assert.ok(sidebar, "App.tsx no longer renders <Sidebar> in the expected shape");
  assert.match(sidebar, /onOpenBrowser=\{handleNewBrowserTab\}/);
  assert.match(sidebar, /onOpenTerminal=\{\(\) => void handleOpenTerminal\(\)\}/);
  assert.match(sidebar, /onOpenSettings=\{\(\) => setIsSettingsOpen\(true\)\}/);
});

// ── the thread right-click menu ──────────────────────────────────────────
//
// One right-click, one menu, everywhere in the thread panel. On a thread it
// carries that thread's actions; on empty panel it carries only "New thread",
// which is everything that can be done there.
//
// The menu is rendered on its own because the sidebar only renders it once a
// `contextmenu` event has fired, and `renderToStaticMarkup` fires nothing. Two
// halves therefore: what the menu *is* (this file, as markup) and where it
// *goes* (`threadMenu.test.ts`, as arithmetic). The wiring in `Sidebar.tsx` is
// read off disk, for the reason `invokeArgs.test.ts` gives — an event handler is
// not something a static render can see.

/** A viewport with room in it. `renderToStaticMarkup` has no `window`. */
const VIEWPORT = { x: 0, y: 0, width: 1000, height: 800 };

const menu = (over: Partial<React.ComponentProps<typeof ThreadMenu>> = {}) =>
  renderToStaticMarkup(
    React.createElement(ThreadMenu, {
      title: "Refactor the parser",
      parentTitle: "Refactor the parser",
      x: 100,
      y: 100,
      bounds: VIEWPORT,
      onNewThread: noop,
      onRename: noop,
      onArchive: noop,
      onClose: noop,
      ...over,
    })
  );

test("the thread menu is a menu, and says which thread it is for", () => {
  const markup = menu();
  assertClean(markup, "the thread menu");
  assert.match(markup, /role="menu"/);
  assert.match(markup, /aria-label="Conversation actions"/);
  assert.match(text(markup), /Refactor the parser/);
});

test("the thread menu leads with New thread, then the thread's own actions", () => {
  // The order is a decision. "New thread" first because a right-click in this
  // panel is the gesture for starting something, and the item that does it
  // should not be one the pointer has to travel to.
  const shown = text(menu());
  assert.match(menu(), /role="menuitem"/);
  const order = ["New thread", "Rename", "Archive"].map((l) => shown.indexOf(l));
  for (const at of order) assert.ok(at >= 0, `the menu is missing an item: ${shown}`);
  assert.deepEqual(order, [...order].sort((a, b) => a - b), `out of order: ${shown}`);
  // Nothing that looks like a delete. Archive hides a thread; the engine keeps
  // every run either way, and a menu offering Delete would be a lie.
  assert.doesNotMatch(shown, /Delete/);
});

test("a menu item's label is on the left, not floating in the middle", () => {
  // The defect, in the shape it had: `Button`'s base is `inline-flex items-center
  // justify-center`, and the menu overrode it with a `justify-start` *class*.
  // Both utilities reached the stylesheet and the later stylesheet rule won —
  // `justify-center` — so every menu item's label sat centred in a 176px row
  // with a third of the width empty on the right. A class string cannot decide
  // this; the primitive has to emit exactly one of the two.
  const button = readFileSync(
    new URL("../src/components/ui/Button.tsx", import.meta.url),
    "utf8",
  );
  assert.match(
    button,
    /align === "start" \? "justify-start" : "justify-center"/,
    "Button can emit two competing justify-* utilities again",
  );
  assert.match(
    button,
    /weight === "normal" \? "font-normal" : "font-semibold"/,
    "Button can emit two competing font-weight utilities again",
  );
  // Comments do not emit a stylesheet rule, so they are stripped before counting
  // — the comment explaining this bug names both utilities and would otherwise
  // read as two more of them.
  const code = button.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  const justifyCount = (code.match(/justify-(start|center)/g) || []).length;
  assert.equal(
    justifyCount,
    2,
    `Button's code has ${justifyCount} justify-* utilities; the two-branch emitter must be the only pair`,
  );
  // And the menu asks for the left-aligned one.
  const menu = readFileSync(
    new URL("../src/components/ThreadMenu.tsx", import.meta.url),
    "utf8",
  );
  assert.match(menu, /align="start"/, "menu items are not left-aligned");
  assert.doesNotMatch(
    menu,
    /justify-start/,
    "the menu is fighting alignment with a class again",
  );
});

test("the menu is compact: no oversized floor, no heavy weight", () => {
  // The two things that made it look like a dialog rather than a context menu:
  // a 176px minimum width holding three short words, and `font-semibold` from
  // the button primitive making every item shout. Both are in the markup, so
  // both are read off it.
  const menu = readFileSync(
    new URL("../src/components/ThreadMenu.tsx", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(menu, /min-w-44/, "the menu is back to a 176px floor");
  // An explicit width, not a floor plus shrink-to-fit: the items are `w-full`,
  // and a percentage width in a self-sizing container is a feedback loop that
  // stretched the menu to 255px to hold three words.
  assert.match(menu, /w-36/, "the menu has no fixed compact width");
  assert.doesNotMatch(
    menu,
    /min-w-\d+/,
    "the menu is sizing itself from its own percentage-width items again",
  );
  // Weight is asked for as a prop for the same reason alignment is: a
  // `font-normal` class lost to the primitive's `font-semibold` on stylesheet
  // order, and every menu item came out bold.
  assert.match(menu, /weight="normal"/, "menu items still carry the button's bold weight");
  assert.doesNotMatch(menu, /font-normal/, "the menu is fighting weight with a class again");
  assert.doesNotMatch(menu, /shadow-xl/, "the menu still has a dialog's shadow");
});

test("the menu names the thread a New thread would be on", () => {
  // Said *in the menu*, before the click. A new empty tab that turns out to be
  // unrelated is a mistake the reader only discovers once it is made.
  const shown = text(menu());
  assert.match(
    shown,
    /on “Refactor the parser”/,
    `the menu does not say what New thread is on: ${shown}`,
  );
});

test("with nothing on screen, the menu claims no parent", () => {
  // There is no chat to branch off, and a menu that named one anyway would be
  // promising a link the created thread does not have.
  const shown = text(menu({ title: undefined, parentTitle: undefined }));
  assert.doesNotMatch(shown, /on “/);
  assert.match(shown, /New thread/);
});

test("on empty panel the menu is New thread alone", () => {
  // Rename and Archive act on a thread that is not there. Offering them anyway
  // is two controls that silently do nothing.
  const markup = menu({ title: undefined, parentTitle: undefined });
  const shown = text(markup);
  assert.match(shown, /New thread/);
  for (const absent of ["Rename", "Archive"]) {
    assert.ok(!shown.includes(absent), `${absent} is offered with no thread to act on`);
  }
});

test("the thread menu is placed where it was asked for", () => {
  // The numbers themselves are `clampMenuPosition`'s business and are tested in
  // `threadMenu.test.ts`. What is checked here is that the component *uses*
  // them: a menu that ignored its x/y would render identically in every other
  // test in this file and open in the top-left corner of the app.
  assert.match(menu({ x: 321, y: 654 }), /left:\s*321px/);
  assert.match(menu({ x: 321, y: 654 }), /top:\s*654px/);
  const corner = menu({ x: 9999, y: 9999 });
  assert.match(corner, /left:\s*\d+px/);
  assert.doesNotMatch(corner, /left:\s*-|top:\s*-|left:\s*9999/);
});

test("a right-click anywhere in the panel opens the menu, and the panel owns it", () => {
  // Read off the source: `renderToStaticMarkup` drops event handlers, so a
  // panel with no right-click path renders exactly what a correct one renders.
  const src = readFileSync(
    new URL("../src/components/Sidebar.tsx", import.meta.url),
    "utf8"
  );
  assert.match(src, /onContextMenu=\{onPanelContextMenu\}/);
  const handler = src.slice(
    src.indexOf("const onPanelContextMenu"),
    src.indexOf("const onPanelContextMenu") + 700
  );
  assert.match(handler, /event\.preventDefault\(\);/, "the webview's own menu is not suppressed");
  assert.match(handler, /setMenu\(/, "a right-click does not open the menu");
  // One handler, not one per row: a row used to handle it separately, which is
  // two code paths for one behaviour and the reason a right-click on a thread
  // could do nothing while the same click beside it worked.
  const row = src.slice(
    src.indexOf("const SidebarRow"),
    src.indexOf("export const Sidebar")
  );
  assert.ok(row.trim(), "SidebarRow is gone; the assertions below would pass for the wrong reason");
  assert.doesNotMatch(
    row,
    /onContextMenu/,
    "a row handles the right-click itself instead of the panel"
  );
});

test("a right-click on a row opens the menu with that row's thread", () => {
  const src = readFileSync(
    new URL("../src/components/Sidebar.tsx", import.meta.url),
    "utf8"
  );
  // The row is found by the attributes it carries, so the panel needs no
  // per-row wiring to know which thread was clicked.
  assert.match(src, /data-thread-row="true"/, "rows are not marked");
  assert.match(
    src,
    /data-thread-id=\{conversation\.id\}/,
    "a row does not say which thread it is"
  );
  const handler = src.slice(
    src.indexOf("const onPanelContextMenu"),
    src.indexOf("const onPanelContextMenu") + 700
  );
  assert.match(handler, /closest\("\[data-thread-row\]"\)/);
  assert.match(handler, /getAttribute\("data-thread-id"\)/);
  // And the menu acts on that thread. A menu that showed the first thread in
  // the list would archive it while the user is looking at another.
  assert.match(
    src,
    /conversations\.find\(\(c\) => c\.id === menu\.conversationId\)/,
    "the menu no longer resolves the thread it was opened on"
  );
  for (const action of ["onRename", "onArchive"]) {
    assert.match(src, new RegExp(`${action}\\)?target\\.id|${action}\\(target\\.id`));
  }
});

test("the menu's New thread makes a thread ON the chat, not a new chat", () => {
  // The failure this pins, in the exact shape it had: a menu item labelled
  // "New thread" wired to `onNewChat` — the *button's* handler. That creates a
  // conversation with no parent, which is a brand-new chat wearing the name of
  // a thread, and the only symptom is an unrelated empty tab opening.
  const sidebar = readFileSync(
    new URL("../src/components/Sidebar.tsx", import.meta.url),
    "utf8"
  );
  assert.doesNotMatch(
    sidebar,
    /onNewThread=\{onNewChat\}/,
    "the menu's New thread is wired to the new-chat button again",
  );
  // The parent is resolved once and used for both the label the reader sees and
  // the id the thread is created with, so the menu cannot say one and do another.
  assert.match(
    sidebar,
    /newThreadParentId\(\s*menu\.conversationId,\s*activeConversationId,\s*conversations\.map\(\(c\) => c\.id\),?\s*\)/,
    "the menu no longer decides which thread a new thread hangs off, or has " +
      "stopped passing the panel so a tab from another workspace can be " +
      "offered as a parent (the engine refuses that with a 422)",
  );
  assert.match(
    sidebar,
    /onNewThread=\{\(\) => onNewThread\(parentId\)\}/,
    "the new thread is not created on the resolved parent",
  );
  // And the shell hands the panel a real handler that passes a parent through,
  // rather than the panel reaching for a new chat.
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(
    app,
    /onNewThread=\{\(parentId\) => void handleNewThread\(parentId\)\}/,
    "the shell does not hand the panel a parent-aware new-thread action",
  );
  assert.match(
    app,
    /createConversation\(\s*selectedWs\.id,\s*"",\s*parentConversationId,?\s*\)/,
    "the new thread is not created with the parent the menu resolved",
  );
});

test("a thread that has a parent says so in the panel", () => {
  // The column is the whole difference between a thread and a chat, so the row
  // has to show it — otherwise the reader has to open the thread to find out
  // where it came from.
  const src = readFileSync(
    new URL("../src/components/Sidebar.tsx", import.meta.url),
    "utf8"
  );
  assert.match(
    src,
    /conversation\.parent_id/,
    "the panel ignores parent_id entirely",
  );
  assert.match(src, /parentTitle\?: string/, "a row is never told its parent");
  // The menu names the parent before the click, not after the new tab is open.
  const menu = readFileSync(
    new URL("../src/components/ThreadMenu.tsx", import.meta.url),
    "utf8",
  );
  assert.match(
    menu,
    /parentTitle\?: string/,
    "the menu cannot say which thread the new thread will be on",
  );
});

// ── a thread's first prompt is its name ──────────────────────────────────
//
// Read off `App.tsx` rather than rendered: this is a hook inside an
// 1800-line component, and `renderToStaticMarkup` neither runs effects nor
// fires a send. What is pinned is the *decision* — a thread with no name is
// named from the prompt that starts it — and that decision is the whole bug:
// without it every tab in a per-thread strip reads "New chat" and opening one
// is indistinguishable from opening nothing.

test("a turn names the thread it is starting, and only an unnamed one", () => {
  const src = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  // The guard. Without it, a thread the user has already renamed would be
  // overwritten by its first prompt on every later turn.
  assert.match(
    src,
    /if \(named && !named\.title\.trim\(\)\)/,
    "the turn no longer checks whether the thread already has a name"
  );
  assert.match(
    src,
    /nameThread\(threadId, threadTitleFromPrompt\(promptText\)\)/,
    "the turn does not name the thread from the prompt"
  );
  // And the two updates that make the name visible: the list and the tab. A
  // name the engine accepted but the strip never learned about is the same
  // invisible-tab problem one layer down.
  assert.match(
    src,
    /const tab = tabForConversation\(prev, conversationId\);\s*return tab \? renameTab\(prev, tab\.id, updated\.title\) : prev;/,
    "renaming a thread does not move its tab"
  );
  assert.match(
    src,
    /setConversations\(\(prev\) =>\s*prev\.map\(\(c\) => \(c\.id === updated\.id \? updated : c\)\),\s*\);/,
    "naming a thread does not update the panel"
  );
});

test("a thread started by a prompt is named the same way as one that is not", () => {
  // Two paths create a thread: the panel's "New chat" button (no name, named
  // later by its first turn) and a prompt typed with no tab open. If they name
  // threads differently, the same question produces two different names
  // depending on which button the user happened to press first.
  const src = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(
    src,
    /createConversation\(wsToUse\.id, threadTitleFromPrompt\(promptText\)\)/,
    "a thread created from a prompt is not named by the shared rule"
  );
  assert.doesNotMatch(
    src,
    /createConversation\([^)]*promptText\.slice/,
    "a thread created from a prompt is still named by a raw slice"
  );
});

test("no surface invents its own name for an unnamed thread", () => {
  // The one-string rule. `UNTITLED_THREAD_TITLE` is imported by the tab strip
  // and the panel and defined once; a second literal "New chat" in either is a
  // tab and a row that can disagree about the same thread.
  for (const file of ["../src/tabs.ts", "../src/components/Sidebar.tsx"]) {
    const src = readFileSync(new URL(file, import.meta.url), "utf8");
    const withoutComments = src
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");
    const literals = withoutComments.match(/"New chat"/g) ?? [];
    assert.equal(
      literals.length,
      0,
      `${file} hard-codes "New chat" instead of using UNTITLED_THREAD_TITLE`
    );
  }
});
