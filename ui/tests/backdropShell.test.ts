/**
 * Where the shell puts the weather, and what it tells it.
 *
 * The rain's two original failures were one decision — where `RainBackdrop` is mounted — and neither
 * was visible to a test that looked at the component alone: rendered by itself the backdrop is a canvas
 * in a full-size box, exactly as it should be. What was wrong was its *position in the tree*. These
 * tests used to read `App.tsx` and `ChatTimeline.tsx` as text for that position (`<RainBackdrop />`
 * appears once, before `<header`, and the transcript's source does not contain the word), which holds
 * the spelling and not the thing: a layer moved into the transcript by a wrapper component, or mounted
 * twice by a conditional, reads the same.
 *
 * This mounts the whole App against the fake engine and looks at the layer the user would have under
 * their windows: that there is one, that it is under the chrome and outside the transcript, that it is
 * the same element after the first message exists (so nothing in a conversation remounts it), and that
 * it is told the agent is working when — and because — a turn is in flight.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { ASCII_RAIN, CMATRIX_OLED, CODIFY_DARK, CYBERPUNK_NEON, STILL, THEMES } from "../src/appearance.ts";
import type { AppContext } from "./appHarness.ts";

const { withApp } = await import("./appHarness.ts");
const { themesWithWeather } = await import("../src/components/ui/WeatherBackdrop.tsx");

const PROMPT = "QUOKKA-31 add a limit";

const layers = (root: ParentNode): HTMLElement[] => [...root.querySelectorAll<HTMLElement>("[data-backdrop]")];

const composer = (root: HTMLElement): HTMLTextAreaElement => {
  const box = root.querySelector("textarea");
  assert.ok(box, "the composer is missing");
  return box;
};

/** Stop is on screen exactly while a goal is in flight. */
const stopButton = (root: HTMLElement): Element | null => root.querySelector('button[title^="Stop this goal"]');

/** Mount the app under a stored theme and let it settle. */
async function underTheme(themeId: string, body: (ctx: AppContext) => Promise<void>): Promise<void> {
  await withApp({ localStorage: { "codify.theme": themeId } }, async (ctx) => {
    await ctx.settle();
    await body(ctx);
  });
}

async function sendAPrompt({ dom, settle }: AppContext): Promise<void> {
  await dom.fill(composer(dom.container), PROMPT);
  await dom.press(composer(dom.container), "Enter");
  await settle();
}

for (const [label, themeId, kind] of [
  ["the rain", CMATRIX_OLED.id, "rain"],
  ["the weather", CYBERPUNK_NEON.id, "weather"],
] as const) {
  test(`${label} is mounted once, under the chrome and outside the transcript`, async () => {
    await underTheme(themeId, async ({ dom }) => {
      const found = layers(dom.container);
      assert.equal(found.length, 1, `a ${kind} theme mounted ${found.length} backdrops`);
      const layer = found[0];
      assert.equal(layer.dataset.backdrop, kind);

      // A child of the app's own root, which is the window's box: `absolute inset-0` inside it is
      // the window, and a mount anywhere deeper is a column.
      const root = dom.container.firstElementChild;
      assert.ok(root, "the app rendered nothing");
      assert.ok(layer.parentElement === root, "the backdrop is not a direct child of the app's root");

      const header = root.querySelector("header");
      const main = root.querySelector("main");
      assert.ok(header && main, "the header or the main row is missing");
      assert.ok(
        layer.compareDocumentPosition(header) & dom.window.Node.DOCUMENT_POSITION_FOLLOWING,
        "the backdrop comes after the header, so it is over the chrome and not under it",
      );
      assert.ok(!main.contains(layer), "the backdrop is inside the main row, with the transcript");
      assert.deepEqual(layers(main), [], "a backdrop is mounted inside the main row");
    });
  });

  test(`${label} sits under the content, and the chrome is glass over it`, async () => {
    // An absolutely positioned box with no z-index paints *after* in-flow content, so a bare
    // backdrop would cover the header's text: the layer is at z-0 and the two things above it are
    // positioned and lifted. jsdom has no stacking contexts to measure, so this reads the classes
    // the rendered elements carry — the rendered tree, not the source that produced it.
    await underTheme(themeId, async ({ dom }) => {
      const layer = layers(dom.container)[0];
      assert.ok(layer.classList.contains("z-0"), "the backdrop declares no stacking position");
      assert.ok(layer.classList.contains("pointer-events-none"), "the backdrop would eat every click");
      assert.equal(layer.getAttribute("aria-hidden"), "true");
      const root = dom.container.firstElementChild as HTMLElement;
      const header = root.querySelector("header") as HTMLElement;
      const main = root.querySelector("main") as HTMLElement;
      assert.ok(header.classList.contains("z-10"), "the header is not lifted above the backdrop");
      assert.ok(main.classList.contains("z-10"), "the main row is not lifted above the backdrop");

      // The two full-bleed surfaces the weather is behind. Opaque, they hide it completely and the
      // user sees a centred animation with clean edges, which is the reported symptom.
      const sidebar = root.querySelector("nav") as HTMLElement;
      assert.ok(header.classList.contains("bg-codify-chrome"), "the top bar is opaque, so the weather stops below it");
      assert.ok(sidebar.classList.contains("bg-codify-chrome"), "the sidebar is opaque, so the weather never reaches the left edge");
    });
  });

  test(`${label} survives the first message, and is told the agent is working while it runs`, async () => {
    await underTheme(themeId, async (ctx) => {
      const { dom } = ctx;
      const before = layers(dom.container)[0];
      assert.equal(before.dataset.active, "false", "an idle app told the weather the agent was working");
      assert.ok(stopButton(dom.container) === null, "Stop is on screen with nothing running");

      await sendAPrompt(ctx);

      // The transcript replaced the branch that used to hold the rain, so the first message removed
      // it. The layer is now the same element, not merely an equal one.
      const after = layers(dom.container);
      assert.equal(after.length, 1, "a message changed how many backdrops there are");
      assert.ok(after[0] === before, "the first message remounted the backdrop");

      // One flag, from the goal's own status: it comes on with Stop, because both are derived from
      // the same thing and there is no second piece of state to disagree.
      assert.ok(stopButton(dom.container), "a turn is in flight and Stop is not offered");
      assert.equal(after[0].dataset.active, "true", "a turn is in flight and the weather was not told");
    });
  });
}

test("every theme mounts the layer it asked for, and no other", async () => {
  // Gated on the theme's own variable, not on an id: the rain themes publish one, the weather
  // themes publish another, and a theme with neither (`codify-dark`, `still`) has no canvas and not
  // even an empty div. A theme in the wrong list would mount the wrong layer, or two.
  const weather = new Set(themesWithWeather());
  const rain = new Set([CMATRIX_OLED.id, ASCII_RAIN.id]);
  assert.ok(weather.size >= 10 && rain.size === 2, "the lists of weather themes could not be read");
  for (const theme of THEMES) {
    const expected = rain.has(theme.id) ? ["rain"] : weather.has(theme.id) ? ["weather"] : [];
    await underTheme(theme.id, async ({ dom }) => {
      assert.deepEqual(
        layers(dom.container).map((l) => l.dataset.backdrop),
        expected,
        `${theme.id} mounted the wrong backdrop`,
      );
      if (expected.length === 0) {
        assert.ok(dom.container.querySelector("canvas") === null, `${theme.id} has no weather and drew a canvas`);
      }
    });
  }
  // The two themes a reader would check first.
  assert.ok(!weather.has(CODIFY_DARK.id) && !weather.has(STILL.id));
});
