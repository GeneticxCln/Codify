/**
 * The Appearance pane, used rather than read.
 *
 * Every other test of this pane either renders it to a string or opens the file
 * and matches a regex against it. The first can only say a sentence is on
 * screen; the second is a test of the *file* — it passed happily while three
 * controls it was written to protect were not reachable by anything.
 *
 * This file presses them. It is the one place in the suite where the claim is
 * "a person can do this", and the difference is the whole reason: a colour well
 * that renders beautifully and records nothing is indistinguishable from one
 * that works, until you drag it and look at what the store says.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
const { TINT_STORAGE_KEY } = await import("../src/tint.ts");
import type { Dom } from "./dom.ts";

const React = (await import("react")).default;
const h = React.createElement;

/** Whatever is in the store, read the way the app reads it. */
const store = (): Record<string, Record<string, string>> =>
  JSON.parse(localStorage.getItem(TINT_STORAGE_KEY) ?? "{}");

/** The document root's own value for a variable — what the app is *wearing*. */
const wearing = (name: string): string =>
  document.documentElement.style.getPropertyValue(name).trim();

/**
 * Mount the pane inside a DOM, after seeding storage.
 *
 * The seeding is why the loader is a parameter: the pane reads storage in a
 * `useState` initialiser at mount, so anything written after mount is too late
 * and a test that forgets will quietly assert about the default theme.
 */
async function withPane(
  seed: Record<string, unknown>,
  body: (dom: Dom) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    for (const [key, value] of Object.entries(seed)) {
      localStorage.setItem(key, typeof value === "string" ? value : JSON.stringify(value));
    }
    // Imported inside the DOM, deliberately. `react-dom/client` and any module
    // graph reaching it read `document` *while being imported*, so a static
    // import at the top of this file would give React no document and the pane
    // would never mount.
    const { AppearancePane } = await import("../src/components/AppearancePane.tsx");
    await dom.render(h(AppearancePane));
    await body(dom);
  });
}

const ON_TOXIC_LAB = { "codify.theme": "toxic-lab" };

// ─────────────────────────────────────────────────────────────────────────────
// The colour wells
// ─────────────────────────────────────────────────────────────────────────────

test("dragging a colour well records the colour, on screen and in storage", async () => {
  await withPane(ON_TOXIC_LAB, async (dom) => {
    const well = dom.byLabel("Bubble body colour") as HTMLInputElement;
    await dom.fill(well, "#8b1e2d");

    // Three places, because the failure this replaces was a control that
    // rendered perfectly and reached none of them.
    assert.equal(
      store()["toxic-lab"]?.["--reagent"],
      "#8b1e2d",
      "the pick never reached storage",
    );
    assert.ok(
      /^#[0-9a-f]{6}$/.test(wearing("--reagent")),
      `the pick never reached the document root, which has ${wearing("--reagent") || "(nothing)"}`,
    );
    // And the row reports the *resolved* colour, which is the guard's output
    // rather than the raw proposal, so a crimson too dark for the theme shows up
    // as a lighter crimson with a note.
    assert.match(dom.text(), /#ce2c43|was #8b1e2d/, "the row did not say what it resolved to");
  });
});

test("a pick is guarded on the way in, not stored raw", async () => {
  // The store keeps the *proposal*; the guard runs at apply. A test that only
  // checked the storage value would pass with the guard removed, and a user
  // would get a transcript they cannot read.
  await withPane(ON_TOXIC_LAB, async (dom) => {
    await dom.fill(dom.byLabel("Bubble body colour") as HTMLInputElement, "#8b1e2d");
    assert.equal(store()["toxic-lab"]?.["--reagent"], "#8b1e2d", "the proposal was resolved on the way in");
    assert.notEqual(
      wearing("--reagent"),
      "#8b1e2d",
      "but the value on screen is the raw proposal, so nothing was clamped",
    );
  });
});

test("per-row Reset takes one colour back and leaves the others", async () => {
  await withPane(ON_TOXIC_LAB, async (dom) => {
    await dom.fill(dom.byLabel("Bubble body colour") as HTMLInputElement, "#8b1e2d");
    await dom.fill(dom.byLabel("Interface accent colour") as HTMLInputElement, "#ff9f43");
    assert.equal(Object.keys(store()["toxic-lab"] ?? {}).length, 2);

    await dom.click(dom.byLabel("Reset Bubble body"));
    const left = store()["toxic-lab"] ?? {};
    assert.equal(left["--reagent"], undefined, "the reset colour is still in the store");
    assert.equal(left["--codify-accent"], "#ff9f43", "and it took the other one with it");
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Undo
// ─────────────────────────────────────────────────────────────────────────────

test("Undo appears after a pick, names it, and puts it back", async () => {
  // The one a static render cannot reach at all: the button does not exist until
  // there is something to undo, and its name is only meaningful once there is.
  await withPane(ON_TOXIC_LAB, async (dom) => {
    assert.equal(
      dom.byLabel("Nothing to undo").hasAttribute("disabled"),
      true,
      "Undo is offered before anything has happened",
    );

    await dom.fill(dom.byLabel("Bubble body colour") as HTMLInputElement, "#8b1e2d");
    const undo = dom.byLabel("Undo Bubble body on Toxic Lab");
    assert.equal(undo.hasAttribute("disabled"), false, "Undo is still disabled after a pick");

    await dom.click(undo);
    assert.equal(store()["toxic-lab"], undefined, "undo left the colour in the store");
    assert.equal(
      dom.byLabel("Nothing to undo").hasAttribute("disabled"),
      true,
      "and left the button armed",
    );
  });
});

test("a burst of picks from one drag is a single Undo, and it goes all the way back", async () => {
  // The claim the whole history module exists for, asserted through the UI: a
  // drag fires a change event per step, and an Undo that took one step back per
  // event would leave the colour almost where it was.
  await withPane(ON_TOXIC_LAB, async (dom) => {
    const well = dom.byLabel("Bubble body colour") as HTMLInputElement;
    for (let i = 0; i < 30; i++) {
      await dom.fill(well, `#${i.toString(16).padStart(2, "0")}3080`);
    }
    // By label, not by text: the button's *visible* text is "Undo", and its name
    // is the `aria-label`. Reading `dom.text()` for this is the exact mistake
    // `byLabel` exists to stop — it would have said the label is missing, which
    // is a different claim from "thirty fills became thirty steps".
    dom.byLabel("Undo Bubble body on Toxic Lab");
    await dom.click(dom.byLabel("Undo Bubble body on Toxic Lab"));
    assert.equal(
      store()["toxic-lab"],
      undefined,
      "undo moved the colour back one frame of the drag instead of before it",
    );
  });
});

test("an import is one Undo, and undoing it puts every theme back", async () => {
  await withPane(ON_TOXIC_LAB, async (dom) => {
    await dom.fill(dom.byLabel("Bubble body colour") as HTMLInputElement, "#8b1e2d");
    const before = JSON.stringify(store());

    await dom.click(dom.byLabel("Import…"));
    await dom.fill(
      dom.container.querySelector("textarea") as HTMLTextAreaElement,
      JSON.stringify({
        kind: "codify-scheme",
        version: 1,
        name: "From a friend",
        themes: [
          { id: "vector-wireframe", label: "Vector Wireframe", tints: { "--vector-line": "#ff2e9a" } },
          { id: "holo-deck-2099", label: "Holo Deck 2099", tints: { "--reagent": "#00ffff" } },
        ],
      }),
    );
    await dom.click(dom.byText("Apply pasted scheme"));

    assert.deepEqual(Object.keys(store()).sort(), ["toxic-lab", "vector-wireframe"]);
    // And the half that could not be applied is named, which is the whole
    // reason an import reports rather than being quiet about it.
    assert.match(dom.text(), /Holo Deck 2099/, "the skipped theme was not named");
    assert.match(dom.text(), /Applied to Vector Wireframe/, "the applied theme was not named");
    // Announced, not merely drawn: a report that is only visible is one a screen reader never says.
    assert.match(
      dom.container.querySelector('[role="status"]')?.textContent ?? "",
      /Applied to Vector Wireframe/,
      "the import's report is not announced as a status",
    );

    await dom.click(dom.byLabel("Undo Import 1 theme"));
    assert.equal(
      JSON.stringify(store()),
      before,
      "undoing an import did not restore what was there before it",
    );
  });
});

test("a scheme can be loaded from a file, and the file input stays usable", async () => {
  // The file half of "a file picker and a paste box". It was untestable before
  // — the input is inside a panel that only exists once opened, and opening it
  // needed a click — and it is the half a person uses when someone sent them a
  // `.json` rather than a block of chat text.
  await withPane(ON_TOXIC_LAB, async (dom) => {
    await dom.click(dom.byLabel("Import…"));
    const input = dom.container.querySelector('input[type="file"]') as HTMLInputElement;
    assert.ok(input, "there is no file input behind the Load button");

    // Reachable by keyboard, which is the claim the `sr-only` class exists for
    // and which a source-reading test could not make: a `display: none` input
    // takes itself out of the tab order, and so does one that is not focusable.
    input.focus();
    assert.ok(dom.window.document.activeElement === input, "the file input cannot be focused");

    await dom.selectFile(input, {
      name: "codify-colours-2026-09-28.json",
      text: JSON.stringify({
        kind: "codify-scheme",
        version: 1,
        name: "From a friend",
        themes: [
          { id: "toxic-lab", label: "Toxic Lab", tints: { "--reagent": "#00b3ff" } },
        ],
      }),
    });

    assert.deepEqual(store()["toxic-lab"], { "--reagent": "#00b3ff" }, "the file was not applied");
    assert.match(dom.text(), /Applied to Toxic Lab/, "and the report did not say so");
  });
});

test("choosing the same file twice applies it twice, not once and then silently not at all", async () => {
  // The input is cleared after every pick so a second `change` fires. Without
  // that, picking the *same* file again does nothing and the user concludes the
  // import is broken — a failure with no error and no message.
  await withPane(ON_TOXIC_LAB, async (dom) => {
    await dom.click(dom.byLabel("Import…"));
    const input = dom.container.querySelector('input[type="file"]') as HTMLInputElement;
    const scheme = JSON.stringify({
      kind: "codify-scheme",
      version: 1,
      name: "x",
      themes: [{ id: "toxic-lab", label: "Toxic Lab", tints: { "--reagent": "#00b3ff" } }],
    });

    await dom.selectFile(input, { name: "a.json", text: scheme });
    assert.equal(store()["toxic-lab"]?.["--reagent"], "#00b3ff");

    // A second, *different* scheme through the same input.
    await dom.selectFile(input, {
      name: "a.json",
      text: JSON.stringify({
        kind: "codify-scheme",
        version: 1,
        name: "x",
        themes: [{ id: "toxic-lab", label: "Toxic Lab", tints: { "--reagent": "#ff4fa3" } }],
      }),
    });
    assert.equal(
      store()["toxic-lab"]?.["--reagent"],
      "#ff4fa3",
      "the second pick of the same input did nothing — the input was not reset",
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// The refusals
// ─────────────────────────────────────────────────────────────────────────────

test("a file that is not a scheme says so, and changes nothing", async () => {
  await withPane(ON_TOXIC_LAB, async (dom) => {
    await dom.fill(dom.byLabel("Bubble body colour") as HTMLInputElement, "#8b1e2d");
    const before = JSON.stringify(store());

    await dom.click(dom.byLabel("Import…"));
    await dom.fill(dom.container.querySelector("textarea") as HTMLTextAreaElement, "{ nope");
    await dom.click(dom.byText("Apply pasted scheme"));

    const alert = dom.container.querySelector('[role="alert"]');
    assert.ok(alert, "a failed import was not announced");
    assert.match(alert.textContent ?? "", /not JSON/i, `got: ${alert.textContent}`);
    assert.equal(JSON.stringify(store()), before, "and a refused import still changed the store");
  });
});

test("a scheme from a newer build is refused with its own sentence", async () => {
  await withPane(ON_TOXIC_LAB, async (dom) => {
    await dom.click(dom.byLabel("Import…"));
    await dom.fill(
      dom.container.querySelector("textarea") as HTMLTextAreaElement,
      JSON.stringify({
        kind: "codify-scheme",
        version: 99,
        themes: [{ id: "toxic-lab", label: "Toxic Lab", tints: { "--reagent": "#00ffff" } }],
      }),
    );
    await dom.click(dom.byText("Apply pasted scheme"));
    assert.match(
      dom.container.querySelector('[role="alert"]')?.textContent ?? "",
      /newer version/i,
      "a file from the future was refused as though it were not a scheme at all",
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// The list
// ─────────────────────────────────────────────────────────────────────────────

test("the radiogroup keeps its promise when a key is pressed, not only when one is matched", async () => {
  // `appearancePane.test.ts` asserts the handler exists by reading the source.
  // This asserts the promise is kept, which is a different claim and the one
  // `role="radiogroup"` actually makes to a person using a keyboard.
  await withPane(ON_TOXIC_LAB, async (dom) => {
    const group = dom.byLabel("Theme");
    assert.equal(
      group.getAttribute("role"),
      "radiogroup",
      "the element the arrow keys are pressed on is not the radiogroup",
    );
    // Inside the Theme group: the UI scale panel above it is a radiogroup too, with its own checked option.
    const checked = (): string | null =>
      group.querySelector('[role="radio"][aria-checked="true"]')?.textContent ?? null;
    assert.match(checked() ?? "", /Toxic Lab/);

    await dom.press(group, "ArrowDown");
    assert.notEqual(checked(), null, "arrowing deselected everything");
    assert.match(checked() ?? "", /Toxic Lab|Anon Fluid/, "and did not move to a neighbour");
    // Arrowing *selects* in a single-choice control, so it commits — which is
    // the pane's stated rule and the thing that makes the key and the click
    // agree.
    assert.notEqual(localStorage.getItem("codify.theme"), "toxic-lab", "the key moved nothing");
  });
});
