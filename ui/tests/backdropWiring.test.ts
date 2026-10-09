/**
 * What the mounted App does with its animated backdrops.
 *
 * `atmosphere`, `rainBackdrop`, `stateReactiveWeather` and `motionPreference`
 * each proved a claim about `App.tsx` — where a backdrop is mounted, what it sits
 * under, what feeds it, who may stop it — by reading the file as text. These run
 * the app instead: they pick a theme, look at what is on screen, count the
 * animation frames the backdrops ask for, and let the shell announce that the
 * window is starving.
 *
 * What is not here is what only text can say (a stylesheet rule, a palette in a
 * document); that lives in `sourceRules.test.ts`.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext } from "./appHarness.ts";

const { withApp } = await import("./appHarness.ts");
const { CODIFY_DARK, CMATRIX_OLED, ASCII_RAIN, CYBERPUNK_NEON } = await import("../src/appearance.ts");
const { themesWithWeather } = await import("../src/components/ui/WeatherBackdrop.tsx");
const { MOTION_KEY, MOTION_EVENT } = await import("../src/motionPreference.ts");

const VIEWPORT = { width: 900, height: 600 };
const THEME_KEY = "codify.theme";
const WEATHER = [...themesWithWeather()];
const RAIN = [CMATRIX_OLED.id, ASCII_RAIN.id];

const canvases = (root: HTMLElement): HTMLCanvasElement[] => [...root.querySelectorAll("canvas")] as HTMLCanvasElement[];
const composer = (root: HTMLElement) => root.querySelector("textarea") as HTMLTextAreaElement;

/** The animation frames requested over this long, by whatever is looping. */
async function framesRequested(dom: AppContext["dom"], ms: number): Promise<number> {
  const win = dom.window as unknown as { requestAnimationFrame: (cb: FrameRequestCallback) => number };
  const g = globalThis as unknown as { requestAnimationFrame: typeof win.requestAnimationFrame };
  const real = win.requestAnimationFrame.bind(win);
  const realGlobal = g.requestAnimationFrame;
  let count = 0;
  const counting = (cb: FrameRequestCallback): number => {
    count++;
    return real(cb);
  };
  win.requestAnimationFrame = counting;
  g.requestAnimationFrame = counting;
  try {
    // Inside `act`: the app is live for the whole wait, and what it sets state on lands during it.
    await dom.act(() => new Promise((r) => setTimeout(r, ms)));
  } finally {
    win.requestAnimationFrame = real;
    g.requestAnimationFrame = realGlobal;
  }
  return count;
}

const emitStarved = (ctx: AppContext, measuredMs: number) => ctx.emit(MOTION_EVENT, measuredMs);
const banner = (root: HTMLElement) => [...root.querySelectorAll("button")].find((b) => b.textContent === "Animate anyway");
/**
 * Whether the banner is up, as a boolean.
 *
 * Never `assert.equal(banner(root), undefined)`: when that fails, node prints a
 * diff of the two values, and the value is a jsdom element, whose object graph is
 * the whole window. Serialising it takes gigabytes and a few seconds, which is
 * how a failing assertion here took the developer's desktop down.
 */
const bannerUp = (root: HTMLElement): boolean => banner(root) !== undefined;
/** The box a backdrop's canvas is drawn in: the nearest hidden ancestor, not the canvas itself. */
const backdropBox = (canvas: HTMLCanvasElement): HTMLElement | null =>
  canvas.parentElement?.closest<HTMLElement>('[aria-hidden="true"]') ?? null;

// ── where a backdrop is mounted ──────────────────────────────────────────

test("a theme with no weather has no backdrop at all", async () => {
  await withApp({ viewport: VIEWPORT, localStorage: { [THEME_KEY]: CODIFY_DARK.id } }, async ({ dom, settle }) => {
    await settle();
    assert.equal(canvases(dom.container).length, 0, "a motionless theme drew a canvas");
  });
});

test("every weather theme draws one full-bleed, inert backdrop, and the two rain themes draw one rain, not two", async () => {
  // The rain themes are not in the weather table on purpose: if the shell also
  // drew weather for them, the monochrome theme would run two raindrops.
  for (const id of [...WEATHER, ...RAIN]) {
    await withApp({ viewport: VIEWPORT, localStorage: { [THEME_KEY]: id } }, async ({ dom, settle }) => {
      await settle();
      const found = canvases(dom.container);
      assert.equal(found.length, 1, `${id} drew ${found.length} backdrops`);
      const box = backdropBox(found[0]);
      assert.ok(box, `${id}'s backdrop is not hidden from assistive technology`);
      const classes = [...box.classList];
      assert.ok(classes.includes("absolute") && classes.includes("inset-0"), `${id}'s backdrop is not full-bleed: ${classes.join(" ")}`);
      assert.ok(!classes.some((c) => c.startsWith("max-w-") || c === "mx-auto"), `${id}'s backdrop is a centred column again`);
      assert.ok(classes.includes("pointer-events-none"), `${id}'s backdrop would eat every click`);
    });
  }
});

test("the backdrop outlives the first message, because the shell mounts it and the transcript does not", async () => {
  // The bug that shipped once: the rain lived in the branch the transcript renders
  // only while it is empty, so the first message removed it.
  for (const id of [CMATRIX_OLED.id, CYBERPUNK_NEON.id]) {
    await withApp({ viewport: VIEWPORT, localStorage: { [THEME_KEY]: id } }, async ({ dom, settle }) => {
      await settle();
      assert.equal(canvases(dom.container).length, 1);
      await dom.fill(composer(dom.container), "hello");
      await dom.press(composer(dom.container), "Enter");
      await settle();
      assert.ok(dom.container.textContent?.includes("hello"), "the message never reached the transcript");
      assert.equal(canvases(dom.container).length, 1, `${id}'s backdrop went away with the first message`);
    });
  }
});

test("the backdrop sits under the header and the main row, which are lifted above it", async () => {
  // An absolutely positioned box with no z-index paints *after* in-flow content,
  // so the header's text would be covered by rain unless both it and the main row
  // are positioned and lifted.
  await withApp({ viewport: VIEWPORT, localStorage: { [THEME_KEY]: CMATRIX_OLED.id } }, async ({ dom, settle }) => {
    await settle();
    const backdrop = backdropBox(canvases(dom.container)[0]) as HTMLElement;
    assert.ok(backdrop.classList.contains("z-0"), "the backdrop declares no stacking position");
    const header = dom.container.querySelector("header") as HTMLElement;
    const main = dom.container.querySelector("main") as HTMLElement;
    assert.ok(header?.classList.contains("z-10"), "the header is no longer lifted above the rain");
    assert.ok(main?.classList.contains("z-10"), "the main row is no longer lifted above the rain, so glyphs land on the transcript");
    // And in the tree, ahead of the chrome: under it, not inside it.
    assert.ok(
      backdrop.compareDocumentPosition(header) & dom.window.Node.DOCUMENT_POSITION_FOLLOWING,
      "the backdrop is mounted after the header",
    );
  });
});

test("the top bar and the sidebar are glass, or the rain stops at the middle of the window", async () => {
  await withApp({ viewport: VIEWPORT, localStorage: { [THEME_KEY]: CMATRIX_OLED.id } }, async ({ dom, settle }) => {
    await settle();
    assert.ok((dom.container.querySelector("header") as HTMLElement).classList.contains("bg-codify-chrome"), "the top bar is opaque again");
    const chrome = [...dom.container.querySelectorAll(".bg-codify-chrome")];
    assert.ok(chrome.length >= 2, "the sidebar is opaque again, so the rain never reaches the left edge");
  });
});

// ── who may stop it ──────────────────────────────────────────────────────

test("the loops run when motion is allowed and are never armed when it is reduced, in both loops", async () => {
  // The trap in any speed change: a user who asked for no motion gets 1.8x of
  // nothing, which is still motion. The rain has its own loop and the weather
  // effects share another, so both are asked.
  for (const id of [CMATRIX_OLED.id, CYBERPUNK_NEON.id]) {
    await withApp({ viewport: VIEWPORT, localStorage: { [THEME_KEY]: id } }, async ({ dom, settle }) => {
      await settle();
      assert.ok((await framesRequested(dom, 300)) > 0, `${id} does not animate when it may`);
    });
    await withApp({ viewport: VIEWPORT, localStorage: { [THEME_KEY]: id, [MOTION_KEY]: "reduced" } }, async ({ dom, settle }) => {
      await settle();
      assert.equal(await framesRequested(dom, 300), 0, `${id} animates although the user asked for no motion`);
    });
  }
});

test("the user's 'allowed' outranks the system preference; 'reduced' outranks a system that allows motion", async () => {
  await withApp(
    { viewport: VIEWPORT, localStorage: { [THEME_KEY]: CMATRIX_OLED.id, [MOTION_KEY]: "allowed" } },
    async ({ dom, settle }) => {
      dom.window.matchMedia = ((q: string) => ({ matches: /reduce/.test(q), media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })) as never;
      await settle();
      assert.ok((await framesRequested(dom, 300)) > 0);
    },
  );
});

test("a reduced-motion backdrop still gets its one still frame, not an empty canvas", async () => {
  await withApp({ viewport: VIEWPORT, localStorage: { [THEME_KEY]: CMATRIX_OLED.id } }, async (ctx) => {
    await ctx.settle();
    const canvas = canvases(ctx.dom.container)[0];
    const real = canvas.getContext.bind(canvas);
    let drawn = 0;
    canvas.getContext = ((kind: string) => {
      const context = real(kind as "2d") as unknown as Record<string, unknown>;
      return new Proxy(context, {
        get(target, prop) {
          const value = target[prop as string];
          if (typeof value !== "function") return value;
          return (...args: unknown[]) => {
            if (/^(fill|stroke|draw|clear)/.test(String(prop))) drawn++;
            return (value as (...a: unknown[]) => unknown).apply(target, args);
          };
        },
      });
    }) as never;
    // The shell's verdict arrives mid-run: the loop is re-armed as a single frame.
    await emitStarved(ctx, 90);
    await ctx.settle();
    assert.ok(drawn > 0, "the motionless theme got an empty canvas");
    assert.equal(await framesRequested(ctx.dom, 300), 0, "the loop kept running after the verdict");
  });
});

// ── the shell's verdict ──────────────────────────────────────────────────

test("a starving window: the shell's verdict stops the backdrops and the banner names the measurement", async () => {
  await withApp({ viewport: VIEWPORT, localStorage: { [THEME_KEY]: CMATRIX_OLED.id } }, async (ctx) => {
    await ctx.settle();
    assert.equal(bannerUp(ctx.dom.container), false, "the banner is up before the shell said anything");
    assert.ok((await framesRequested(ctx.dom, 300)) > 0);
    await emitStarved(ctx, 88);
    await ctx.settle();
    assert.match(ctx.dom.text(), /~88% of a core/, "the banner does not say what was measured");
    assert.equal(await framesRequested(ctx.dom, 300), 0, "a verdict that arrives mid-run did not stop the animation on the machine it was about");
  });
});

test("'Animate anyway' hands the choice back: it is remembered, and the animation resumes", async () => {
  // Relief a person cannot see and reverse is not a setting, it is a takeover.
  await withApp({ viewport: VIEWPORT, localStorage: { [THEME_KEY]: CMATRIX_OLED.id } }, async (ctx) => {
    await ctx.settle();
    await emitStarved(ctx, 90);
    await ctx.settle();
    await ctx.dom.click(banner(ctx.dom.container)!);
    await ctx.settle();
    assert.equal(ctx.dom.window.localStorage.getItem(MOTION_KEY), "allowed", "the choice was not written down");
    assert.equal(bannerUp(ctx.dom.container), false, "the banner stayed after it was answered");
    assert.ok((await framesRequested(ctx.dom, 300)) > 0, "the animation did not resume");
  });
});

test("a verdict left by an earlier boot does not survive into this one, but the user's choice does", async () => {
  // The verdict is a diagnosis of one boot: persisting it would turn "your machine
  // cannot afford this today" into "your machine can never have this".
  const verdict = JSON.stringify({ at: 1, measuredMs: 95 });
  await withApp(
    { viewport: VIEWPORT, localStorage: { [THEME_KEY]: CMATRIX_OLED.id, [MOTION_KEY]: "allowed" }, sessionStorage: { [MOTION_EVENT]: verdict } },
    async ({ dom, settle }) => {
      await settle();
      assert.equal(bannerUp(dom.container), false, "a stale verdict put the banner up at boot");
      assert.equal(dom.window.sessionStorage.getItem(MOTION_EVENT), null, "the verdict was kept for the next reload");
      assert.equal(dom.window.localStorage.getItem(MOTION_KEY), "allowed", "a boot forgot the user's choice");
      assert.ok((await framesRequested(dom, 300)) > 0);
    },
  );
});

// ── what feeds it ────────────────────────────────────────────────────────

test("a run starting does not tear the backdrop down and rebuild it", async () => {
  // The agent working changes how fast the weather moves, never what is rendered:
  // if `active` were a dependency of the loop's effect, every turn would re-arm it,
  // and for an effect with drift that is a visible jump.
  for (const id of [CMATRIX_OLED.id, CYBERPUNK_NEON.id]) {
    await withApp({ viewport: VIEWPORT, localStorage: { [THEME_KEY]: id } }, async ({ dom, settle }) => {
      await settle();
      const canvas = canvases(dom.container)[0];
      const real = canvas.getContext.bind(canvas);
      let rebuilt = 0;
      canvas.getContext = ((kind: string) => {
        rebuilt++;
        return real(kind as "2d");
      }) as never;
      await dom.fill(composer(dom.container), "go");
      await dom.press(composer(dom.container), "Enter");
      await settle();
      assert.ok(canvases(dom.container)[0] === canvas, `${id}'s canvas was replaced when a run started`);
      assert.equal(rebuilt, 0, `${id}'s loop was torn down and re-armed when a run started`);
    });
  }
});
