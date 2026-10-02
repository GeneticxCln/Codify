/**
 * The palette, pressed.
 *
 * `commandPalette.test.ts` proves two things about this overlay: the *model*
 * (what a query matches, where the selection wraps) and the *markup* (a
 * combobox over a listbox, one selected option, the group headings). Both are
 * right, and neither can tell you whether a keystroke does anything — because
 * `renderToStaticMarkup` runs no effects and no events. A palette with a
 * perfect list and a `onKeyDown` wired to nothing renders exactly the same
 * markup as a working one.
 *
 * The claims here are the ones `CommandPalette.tsx` makes in its own comments
 * and cannot demonstrate in a screenshot:
 *
 * - focus is *borrowed* on open and *returned* on close;
 * - Escape and Tab stop at the input, so one keystroke cannot also close the
 *   dialog behind the overlay;
 * - a mousedown inside the panel does not blur the input to `<body>`, which is
 *   the same reason Escape has to stop there;
 * - the backdrop closes and the panel does not.
 *
 * Each is a statement about a second listener, not about what is on screen, so
 * each is a test rather than a rendering.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
import type { Dom } from "./dom.ts";

import { buildPaletteItems, type PaletteItem, type PaletteSources } from "../src/commandPalette.ts";
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

/** Eight rows: two tabs, one thread, five settings pages. */
const allItems = (): PaletteItem[] =>
  buildPaletteItems(
    sources({
      tabs: [tab({ id: "tab-1", title: "Refactor the parser" }), tab({ id: "tab-2", title: "build watch" })],
      activeId: "tab-1",
      conversations: [convo({ id: "c9", title: "Table stakes" })],
    }),
  );

const sources = (over: Partial<PaletteSources> = {}): PaletteSources => ({
  tabs: [],
  activeId: null,
  conversations: [],
  ...over,
});

const React = (await import("react")).default;
const h = React.createElement;

/** What the palette reported: which item was chosen, and how often it closed. */
interface Report {
  selected: string[];
  closed: number;
}

/**
 * Mount the palette, with a control beside it to steal and return focus from.
 *
 * The control is not decoration. "Focus comes back on close" is only a claim
 * about *somebody else's* element, so there has to be one, and Ctrl+K is pressed
 * from somewhere in a real window rather than from nothing.
 */
async function withPalette(
  options: { open?: boolean; items?: PaletteItem[] },
  body: (dom: Dom, io: Report) => Promise<void>,
): Promise<Report> {
  const report: Report = { selected: [], closed: 0 };
  await withDom(async (dom) => {
    // Imported inside the DOM: `react-dom/client` reads `document` while it is
    // being imported, and a hoisted static import would get here with none.
    const { CommandPalette } = await import("../src/components/CommandPalette.tsx");
    await dom.render(
      h(
        "div",
        null,
        h("button", { id: "composer", type: "button" }, "Ask"),
        h(CommandPalette, {
          open: options.open ?? true,
          items: options.items ?? allItems(),
          onClose: () => {
            report.closed += 1;
          },
          onSelect: (item: PaletteItem) => {
            report.selected.push(item.id);
          },
        }),
      ),
    );
    await body(dom, report);
  });
  return report;
}

const options = (dom: Dom): HTMLElement[] =>
  [...dom.container.querySelectorAll('[role="option"]')] as HTMLElement[];

const selectedTitle = (dom: Dom): string =>
  options(dom).find((o) => o.getAttribute("aria-selected") === "true")?.textContent ?? "";

/** The rows, in the order the palette is showing them. */
const rowTitles = (dom: Dom): string[] =>
  options(dom).map((o) => (o.querySelector("span")?.textContent ?? "").trim());

// ─────────────────────────────────────────────────────────────────────────────
// Typing
// ─────────────────────────────────────────────────────────────────────────────

test("typing narrows the list, and the count follows it", async () => {
  await withPalette({}, async (dom) => {
    const input = dom.byLabel("Command palette") as HTMLInputElement;
    assert.equal(rowTitles(dom).length, 8, "nothing was shown to begin with");

    await dom.fill(input, "refactor");
    assert.deepEqual(rowTitles(dom), ["Refactor the parser"]);
    assert.match(dom.text(), /1 of 8/, "the footer still claims the whole list");

    // Two tokens, AND not OR: the thread called "Table stakes" does not contain
    // "refactor", so naming both finds nothing.
    await dom.fill(input, "refactor parser stakes");
    assert.deepEqual(rowTitles(dom), [], "AND, not OR — and the empty list says so");
    assert.match(dom.text(), /Nothing matches/);
    assert.match(dom.text(), /0 of 8/);
  });
});

test("a new list starts at the top, not at the old offset", async () => {
  // The failure this replaces: the user arrows down three rows, types one
  // character, and presses Enter — and the row they open is whichever row now
  // happens to sit at offset 3, which is a different item from the one under
  // the cursor.
  await withPalette({}, async (dom) => {
    const input = dom.byLabel("Command palette") as HTMLInputElement;
    await dom.press(input, "ArrowDown");
    assert.equal(rowTitles(dom)[1], "build watch");

    await dom.fill(input, "stakes");
    assert.equal(rowTitles(dom).length, 1);
    assert.equal(
      dom.container.querySelector('[role="option"][aria-selected="true"]')?.id,
      "palette-option-conversation:c9",
      `the single row is not the selected one: ${selectedTitle(dom)}`,
    );
    assert.match(dom.text(), /1 of 8/);
    assert.equal(
      dom.container.querySelector('[role="combobox"]')?.getAttribute("aria-activedescendant"),
      "palette-option-conversation:c9",
      "the combobox still points at an option from the old list",
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Moving and choosing
// ─────────────────────────────────────────────────────────────────────────────

test("the arrow keys move the selection, and the combobox names it", async () => {
  await withPalette({}, async (dom) => {
    const input = dom.byLabel("Command palette") as HTMLInputElement;
    const box = dom.container.querySelector('[role="combobox"]')!;
    assert.equal(rowTitles(dom)[0], "Refactor the parser");
    assert.equal(box.getAttribute("aria-activedescendant"), "palette-option-tab:tab-1");

    await dom.press(input, "ArrowDown");
    assert.equal(rowTitles(dom)[1], "build watch");
    assert.equal(box.getAttribute("aria-activedescendant"), "palette-option-tab:tab-2");

    await dom.press(input, "ArrowUp");
    assert.equal(box.getAttribute("aria-activedescendant"), "palette-option-tab:tab-1");

    // Up off the top wraps to the end, which is `moveSelection`'s half and the
    // key handler's use of it — the unit test covers the arithmetic, this is
    // the wiring.
    await dom.press(input, "ArrowUp");
    assert.equal(box.getAttribute("aria-activedescendant"), "palette-option-settings:about");
  });
});

test("Enter opens the highlighted row, and only that row", async () => {
  const report = await withPalette({}, async (dom) => {
    const input = dom.byLabel("Command palette") as HTMLInputElement;
    await dom.press(input, "ArrowDown");
    await dom.press(input, "ArrowDown");
    await dom.press(input, "Enter");
  });
  assert.deepEqual(report.selected, ["conversation:c9"]);
  assert.equal(report.closed, 0, "choosing a row is not closing the palette by key");
});

test("Enter opens the top match after a query", async () => {
  const report = await withPalette({}, async (dom) => {
    const input = dom.byLabel("Command palette") as HTMLInputElement;
    await dom.fill(input, "appearance");
    await dom.press(input, "Enter");
  });
  assert.deepEqual(report.selected, ["settings:appearance"]);
});

test("the UI scale is found by searching for it, and About by its name", async () => {
  for (const [query, id] of [["scale", "settings:appearance"], ["about", "settings:about"]] as const) {
    const report = await withPalette({}, async (dom) => {
      const input = dom.byLabel("Command palette") as HTMLInputElement;
      await dom.fill(input, query);
      await dom.press(input, "Enter");
    });
    assert.deepEqual(report.selected, [id], `searching for "${query}" did not land on ${id}`);
  }
});

test("Enter on an empty list chooses nothing rather than the first item", async () => {
  const report = await withPalette({}, async (dom) => {
    const input = dom.byLabel("Command palette") as HTMLInputElement;
    await dom.fill(input, "nothing matches this");
    await dom.press(input, "Enter");
  });
  assert.deepEqual(report.selected, [], "an empty list still opened something");
});

test("clicking a row opens *that* row, not the one under the cursor", async () => {
  // Enter and click are two paths to the same place, and they read the selection
  // from different places: Enter reads the index, click carries the item. A
  // click wired to the index would open the highlighted row instead of the one
  // that was clicked, which is wrong the moment the cursor has moved on.
  const report = await withPalette({}, async (dom) => {
    const rows = options(dom);
    // The cursor is still on row 0; the click is on row 4.
    await dom.click(rows[4]);
  });
  assert.deepEqual(report.selected, ["settings:agents"]);
});

// ─────────────────────────────────────────────────────────────────────────────
// Focus: borrowed, then returned
// ─────────────────────────────────────────────────────────────────────────────

test("opening takes focus; closing gives it back to the composer", async () => {
  await withPalette({ open: false }, async (dom) => {
    const composer = dom.container.querySelector("#composer") as HTMLElement;
    composer.focus();
    assert.ok(dom.window.document.activeElement === composer);

    const { CommandPalette } = await import("../src/components/CommandPalette.tsx");
    const shown = (open: boolean): Promise<void> =>
      dom.render(
        h(
          "div",
          null,
          h("button", { id: "composer", type: "button" }, "Ask"),
          h(CommandPalette, { open, items: allItems(), onClose: () => {}, onSelect: () => {} }),
        ),
      );

    await shown(true);
    const input = dom.byLabel("Command palette");
    assert.ok(dom.window.document.activeElement === input, "the palette opened without taking focus");

    await shown(false);
    assert.ok(dom.window.document.activeElement === dom.container.querySelector("#composer"), "closing the palette left the cursor nowhere");
  });
});

test("reopening starts from an empty query", async () => {
  await withPalette({ open: true }, async (dom) => {
    const input = dom.byLabel("Command palette") as HTMLInputElement;
    await dom.fill(input, "keys");
    assert.equal(rowTitles(dom).length, 1);

    const { CommandPalette } = await import("../src/components/CommandPalette.tsx");
    const shown = (open: boolean): Promise<void> =>
      dom.render(
        h(
          "div",
          null,
          h("button", { id: "composer", type: "button" }, "Ask"),
          h(CommandPalette, { open, items: allItems(), onClose: () => {}, onSelect: () => {} }),
        ),
      );
    await shown(false);
    await shown(true);
    assert.equal((dom.byLabel("Command palette") as HTMLInputElement).value, "");
    assert.equal(rowTitles(dom).length, 8, "the last search survived into the next open");
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Escape, Tab, and the window underneath
// ─────────────────────────────────────────────────────────────────────────────

test("Escape closes the palette and never reaches the window", async () => {
  // The claim in the module docs, and the reason it needs a test: Settings
  // listens for Escape on the window. If this keydown bubbled, one keystroke
  // would close the palette *and* the dialog behind it.
  const report = await withPalette({}, async (dom) => {
    const seen: string[] = [];
    dom.window.addEventListener("keydown", (e) => seen.push((e as KeyboardEvent).key));
    await dom.press(dom.byLabel("Command palette"), "Escape");
    assert.deepEqual(seen, [], "the Escape reached a window-level listener");
  });
  assert.equal(report.closed, 1);
});

test("Tab is swallowed, and the input keeps focus", async () => {
  // Tab behind the overlay would land on controls the backdrop covers, from
  // where Escape would miss this component and hit the window's listeners.
  const report = await withPalette({}, async (dom) => {
    const seen: string[] = [];
    dom.window.addEventListener("keydown", (e) => seen.push((e as KeyboardEvent).key));
    const input = dom.byLabel("Command palette");
    await dom.press(input, "Tab");
    assert.deepEqual(seen, [], "Tab reached a window-level listener");
    assert.ok(dom.window.document.activeElement === input, "Tab walked the focus out of the palette");
  });
  assert.equal(report.closed, 0);
});

// ─────────────────────────────────────────────────────────────────────────────
// The backdrop
// ─────────────────────────────────────────────────────────────────────────────

test("a mousedown on the backdrop closes; one inside the panel does not", async () => {
  // The count after each press, read from inside the callback — the fixture's
  // own report is not in scope out here, because the fixture is still being
  // built when this line runs.
  const counts: number[] = [];
  await withPalette({}, async (dom, report) => {
    // The first `role="presentation"` in the tree is the backdrop; the ones
    // after it are the group headings, which are not clickable.
    const backdrop = dom.container.querySelector('[role="presentation"]')!;
    const panel = dom.container.querySelector('[role="combobox"]')!;

    await dom.mousedown(panel);
    counts.push(report.closed);
    await dom.mousedown(backdrop);
    counts.push(report.closed);
  });
  assert.deepEqual(counts, [0, 1], "the panel closed, or the backdrop did not");
});

test("a closed palette is not in the document at all", async () => {
  await withPalette({ open: false }, async (dom) => {
    assert.ok(dom.container.querySelector('[role="combobox"]') === null);
    assert.equal(dom.text(), "Ask", "a closed palette left its text behind");
  });
});
