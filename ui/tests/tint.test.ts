/**
 * Custom colours: the guard, the store, and the one claim that makes it work.
 *
 * The feature is small — a hex in a custom property — and almost all of its
 * risk is not in the code but in the two promises around it. `useAtmosphereCanvas`
 * documents that "a theme applied while a canvas is running restyles it on the
 * next frame", and for most of this file's life that sentence was **false**:
 * every painter read its colours once, when the painter was *created*, and
 * cached the result. A tint feature built on that would have looked perfect in
 * a still image and done nothing at all in the only situation a person
 * customises a theme in — while the window is open and the canvas is running.
 * So the last test here drives a real painter across two frames with the
 * variables changing in between, and that is the one that earns the file.
 *
 * The rest guards the thing that would otherwise be a bug report: a free colour
 * picker with no clamp. Every colour in this app is painted on a surface, and
 * `contrast.test.ts` holds every pair in every theme to WCAG AA. A user who
 * picks a deep crimson for the accent of a near-black raised surface gets an
 * unreadable transcript, and "it was their choice" is not a mitigation. So a
 * proposal is clamped — hue and saturation kept, lightness moved until the
 * pairs pass — and a proposal that cannot pass *at any lightness* is refused
 * and the theme's own value is kept.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";

// `tint.ts` imports `./contrast` and `./appearance` extensionlessly, the way
// vite and tsc both resolve them, so node needs the loader's `resolve` hook
// before it can follow them. A *static* import is hoisted and resolved before
// this line runs, which is why every one of these is a dynamic import instead —
// the same reason `tabs.test.ts` does it, and the reason top-level `await` is
// doing real work here rather than decoration.
registerTsx();

const { contrastRatio } = await import("../src/contrast.ts");
const { applyTheme, MANAGED_VARS, THEMES, themeById } = await import("../src/appearance.ts");
const {
  ART_FLOOR,
  TINT_ROLES,
  TEXT_FLOOR,
  TINT_STORAGE_KEY,
  applyTintedTheme,
  clampForContrast,
  isTintable,
  loadTints,
  resolveTint,
  resolveTints,
  ruleFor,
  saveTints,
  tintableVars,
} = await import("../src/tint.ts");
const { createElectricArcPainter } = await import("../src/components/ui/ElectricArc.tsx");
const { createToxicLabPainter } = await import("../src/components/ui/ToxicLab.tsx");
const React = (await import("react")).default;
const { renderToStaticMarkup } = await import("react-dom/server");
const { AppearancePane } = await import("../src/components/AppearancePane.tsx");

import type { ManagedVar } from "../src/appearance.ts";import type {
  AtmospherePainter,
  AtmosphereSize,
  AtmosphereTick,
} from "../src/hooks/useAtmosphereCanvas.ts";

/** A `read`/`setProperty` pair standing in for `document.documentElement.style`. */
function fakeStorage(initial: Record<string, string> = {}): {
  getItem: (k: string) => string | null;
  setItem: (k: string, v: string) => void;
  removeItem: (k: string) => void;
  items: Map<string, string>;
} {
  const items = new Map<string, string>(Object.entries(initial));
  return {
    getItem: (k) => items.get(k) ?? null,
    setItem: (k, v) => void items.set(k, v),
    removeItem: (k) => void items.delete(k),
    items,
  };
}

function fakeStyle(): { props: Map<string, string>; setProperty: (n: string, v: string) => void; removeProperty: (n: string) => void } {
  const props = new Map<string, string>();
  return {
    props,
    setProperty: (n, v) => void props.set(n, v),
    removeProperty: (n) => void props.delete(n),
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// The clamp
// ─────────────────────────────────────────────────────────────────────────────

test("a colour that already passes is returned untouched", () => {
  // The common case, and the one that must not be slowed down or rounded: a
  // user who picks the theme's own colour back gets the theme's own colour.
  assert.equal(clampForContrast("#e6edf3", ["#000000"], TEXT_FLOOR), "#e6edf3");
});

test("the clamp moves lightness and leaves hue and saturation alone", () => {
  // The property that makes a tint a *tint* rather than a replacement: a user
  // who asks for crimson gets a crimson that is legible, never a colour that
  // has quietly drifted toward a different hue to get there.
  const against = ["#0d1117", "#161b22", "#1c2128"];
  const clamped = clampForContrast("#3b0a12", against, TEXT_FLOOR);
  assert.ok(clamped, "a near-black red should still be liftable to AA");
  assert.notEqual(clamped, "#3b0a12", "the proposal failed, so it must have moved");

  const hueOf = (hex: string): number => {
    const n = parseInt(hex.slice(1), 16);
    const r = ((n >> 16) & 255) / 255;
    const g = ((n >> 8) & 255) / 255;
    const b = (n & 255) / 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const d = max - min;
    if (d === 0) return 0;
    let h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
    h *= 60;
    return h < 0 ? h + 360 : h;
  };
  const hueGap = Math.abs(hueOf(clamped) - hueOf("#3b0a12"));
  const circular = Math.min(hueGap, 360 - hueGap);
  assert.ok(
    circular < 6,
    `clamping drifted the hue by ${circular.toFixed(1)}°, so this is a different colour`,
  );

  // And the result actually passes, which is the whole point of moving it.
  for (const bg of against) {
    assert.ok(
      contrastRatio(clamped, bg) >= TEXT_FLOOR,
      `clamped ${clamped} is only ${contrastRatio(clamped, bg).toFixed(2)}:1 on ${bg}`,
    );
  }
});

test("a colour already brighter than every surface is lightened, not darkened", () => {
  // The mirror of the case above, and the one that made "compare against the
  // brightest surface" the wrong fix. Contrast against a surface you are *above*
  // improves by getting further above it, so a colour at luminance 0.066 on
  // Toxic Lab's `#050a06` and `#12271a` has to go lighter — telling it to darken
  // walks it to black, never passes, and returns the last thing it tried:
  // `#fefbfb`, from a rule that had a passing test against the *other* half of
  // this same mistake.
  const toxic = themeById("toxic-lab");
  const raised = toxic.tokens["--codify-raised"] as string;
  const clamped = clampForContrast("#8b1e2d", [toxic.tokens["--codify-bg"] as string, raised], ART_FLOOR);
  assert.ok(clamped, "a mid crimson on two near-black panels is liftable");
  assert.notEqual(clamped, "#fefbfb", "it came back as the near-white of a failed sweep");
  // A red, and still a red: the hue and saturation are the user's and only the
  // lightness moved.
  const r = Number.parseInt(clamped.slice(1, 3), 16);
  const b = Number.parseInt(clamped.slice(5, 7), 16);
  assert.ok(r > b, `${clamped} is not a red — the clamp changed the colour, not just its lightness`);
  assert.ok(contrastRatio(clamped, raised) >= ART_FLOOR);
});

test("the clamp takes the smaller change when both directions have room", () => {
  // On a dark surface lightening nearly always has room, so the choice is
  // usually made. But a colour already near the top of the passing range has
  // room downward too, and pushing a near-white request further toward white is
  // a worse answer than taking it down a few steps — and a needless one, because
  // the colour the user picked was already legible in the other direction.
  const dark = ["#0d1117", "#161b22"];
  const lifted = clampForContrast("#3b0a12", dark, TEXT_FLOOR) as string;
  assert.ok(lifted, "a near-black red is liftable on dark panels");
  // It went up, because going down from luminance 0.012 could never clear 4.5:1
  // against `#161b22` at any lightness — black is 1.0:1 on both.
  const red = Number.parseInt(lifted.slice(1, 3), 16);
  const blue = Number.parseInt(lifted.slice(5, 7), 16);
  assert.ok(red > blue, `${lifted} went the wrong way`);
  // And a colour that only clears going *down* takes the down.
  const dimmed = clampForContrast("#fefbfb", dark, TEXT_FLOOR);
  assert.equal(dimmed, "#fefbfb", "it already passes, so nothing moved");
});

test("a colour that cannot pass at any lightness is refused, not approximated", () => {
  // The refusal branch, on a surface pair no theme in the app has — all
  // nineteen are dark, so a colour is always above them all and can always be
  // lightened out. It is pinned anyway, and on a synthetic pair, because the
  // failure it guards is the worst one here: a sweep that runs out has to
  // answer "no", and a sweep that returns its last candidate instead ships a
  // colour that failed the check it was run through wearing the appearance of
  // one that did not.
  //
  // A colour that sits *between* a near-black and a near-white panel is the
  // honest impossible case: 4.5:1 from #0d1117 needs relative luminance ≥
  // 0.200, and 4.5:1 from #f0f6fc needs ≤ 0.167, and there is no overlap.
  const impossible = ["#0d1117", "#f0f6fc"];
  assert.equal(clampForContrast("#808080", impossible, TEXT_FLOOR), null);
  assert.equal(clampForContrast("#3b0a12", impossible, TEXT_FLOOR), null, "and not only for greys");
  // And the sweep's own endpoints each fail one of the two, so there is
  // nothing between them worth returning either.
  assert.ok(contrastRatio("#000000", impossible[0]!) < TEXT_FLOOR, "black is not far from the dark panel");
  assert.ok(contrastRatio("#ffffff", impossible[1]!) < TEXT_FLOOR, "and white is not far from the light one");
});

test("a colour already past the darkest surface is lightened, not darkened", () => {
  // The first half of the same mistake, pinned separately so both halves have a
  // name. A deep crimson on three dark panels is *lighter* than all three, and
  // the answer is to get further from them — which means lighter. Told to
  // darken, the sweep reaches black (1.0:1) without ever passing and hands back
  // whatever it last tried.
  const clamped = clampForContrast("#3b0a12", ["#0d1117", "#161b22", "#1c2128"], TEXT_FLOOR);
  assert.ok(clamped, "a dark red on three dark panels is liftable");
  assert.notEqual(clamped, "#fefbfb", "it came back as the near-white of a failed sweep");
  // And it is a red: the hue is the user's, only the lightness moved.
  const red = Number.parseInt((clamped as string).slice(1, 3), 16);
  const blue = Number.parseInt((clamped as string).slice(5, 7), 16);
  assert.ok(red > blue, `${clamped} is not a red`);
  assert.ok(
    contrastRatio(clamped as string, "#1c2128") >= TEXT_FLOOR,
    `lifted to ${clamped} but still only ${contrastRatio(clamped as string, "#1c2128").toFixed(2)}:1`,
  );
});

test("every real theme surface can rescue every hue, so the sweep always terminates", () => {
  // The other half of the refusal story, and the reason the branch above is
  // unreachable today: all nineteen themes are dark, so lightening always wins.
  // Asserted as a property rather than assumed, because a future light theme
  // would turn the refusal branch from unreachable into the common case, and
  // the failure it produces is a refused tint — the theme's own colour, with
  // an explanation — rather than an unreadable one.
  for (const theme of THEMES) {
    const raised = theme.tokens["--codify-raised"];
    if (typeof raised !== "string") continue;
    const lifted = clampForContrast("#808080", [raised], TEXT_FLOOR);
    assert.ok(lifted, `${theme.id}'s raised surface cannot rescue a mid grey`);
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// What a tint is allowed to touch
// ─────────────────────────────────────────────────────────────────────────────

test("the picker offers the accent plus that theme's own weather, and nothing else", () => {
  for (const theme of THEMES) {
    const vars = tintableVars(theme);
    assert.equal(vars[0], "--codify-accent", `${theme.id} does not lead with the accent`);
    assert.ok(vars.length >= 1, `${theme.id} offers nothing at all`);
    for (const name of vars) {
      assert.ok(!name.endsWith("-rgb"), `${theme.id} offers the derived ${name}`);
      assert.ok(
        theme.tokens[name as ManagedVar] !== undefined,
        `${theme.id} offers ${name}, which it does not publish`,
      );
      assert.ok(
        !["--codify-bg", "--codify-surface", "--codify-raised"].includes(name),
        `${theme.id} offers the surface ${name}, which would repaint the whole app`,
      );
      assert.ok(
        !["--codify-primary", "--codify-secondary", "--codify-muted"].includes(name),
        `${theme.id} offers ${name}, which the whole app measures its colours against`,
      );
      assert.ok(
        !["--codify-danger", "--codify-warning", "--codify-success", "--codify-info", "--codify-neutral"].includes(
          name,
        ),
        `${theme.id} offers a status tone, and status colours carry meaning no picker may move`,
      );
      assert.ok(
        TINT_ROLES[name],
        `${theme.id} offers ${name} and the picker has no label for it`,
      );
    }
  }
});

test("every theme is tintable and no two themes offer an unlabelled colour", () => {
  const seen = new Set<string>();
  for (const theme of THEMES) {
    for (const name of tintableVars(theme)) {
      seen.add(name);
      assert.ok(TINT_ROLES[name], `no label for ${name}, offered by ${theme.id}`);
    }
  }
  // The labels are the difference between a picker and a hex editor, so a name
  // in `TINT_ROLES` that no theme offers is a label for a colour that cannot be
  // chosen — a rot entry that reads as coverage in the table. Two of them were
  // here: `--cmatrix-glow` and `--cmatrix-glow-2`, a pair the `MANAGED_VARS`
  // list anticipated for a future rain theme that never arrived.
  for (const name of Object.keys(TINT_ROLES)) {
    assert.ok(seen.has(name), `TINT_ROLES documents ${name}, which no theme offers`);
  }
  // And a theme with no atmosphere has nothing to recolour but its accent,
  // which is the honest answer rather than a picker offering to tint variables
  // it does not publish. The default theme is one; `toxic-lab` is not.
  assert.equal(tintableVars(themeById("codify-dark")).length, 1);
  assert.ok(tintableVars(themeById("toxic-lab")).length >= 3);
});

test("the floor is the one WCAG asks for, chosen per kind of colour", () => {
  // Text and art are not the same question. A weather variable paints a line or
  // a particle and is held to the 1.4.11 graphic floor; `--codify-accent` ends
  // up on a label, and labels are text. Holding both to 4.5 would refuse half
  // the art a user can legitimately want; holding both to 3 would ship
  // unreadable chrome.
  const theme = themeById("toxic-lab");
  assert.equal(ruleFor("--codify-accent", theme).floor, TEXT_FLOOR);
  assert.equal(ruleFor("--reagent", theme).floor, ART_FLOOR);
  assert.equal(ruleFor("--reagent", theme).against.length, 2, "art is judged on two surfaces");
  assert.equal(ruleFor("--codify-accent", theme).against.length, 3, "text is judged on all three");
});

// ─────────────────────────────────────────────────────────────────────────────
// Resolution
// ─────────────────────────────────────────────────────────────────────────────

test("a refused tint keeps the theme's own value rather than the unreadable one", () => {
  const theme = themeById("liquid-mercury");
  // Mid-grey, asked to be the accent of a theme whose own accent is already
  // near the top of the passing range. Whatever the guard does here, the value
  // that ships has to be one the app can actually read.
  const resolved = resolveTint(theme, "--codify-accent", "#808080");
  const own = theme.tokens["--codify-accent"];
  const shipped = resolved.hex === own ? own : resolved.hex;
  for (const bg of ruleFor("--codify-accent", theme).against) {
    assert.ok(
      contrastRatio(shipped, bg) >= TEXT_FLOOR,
      `tint shipped ${shipped} at ${contrastRatio(shipped, bg).toFixed(2)}:1 on ${bg}`,
    );
  }
  if (resolved.adjusted) {
    assert.equal(resolved.proposed, "#808080", "the proposal is kept so the picker can say what it refused");
    assert.ok(resolved.ratio >= TEXT_FLOOR, "a refusal still has to report a passing ratio");
  }
});

test("every resolved tint in every theme clears the floor it is held to", () => {
  // The sweep. Not one hand-picked theme, because the property is not "a
  // crimson can be made legible" — it is "no proposal, in any theme, can be
  // made illegible". Anything that holds only for the themes somebody thought
  // about is a guard with a sample size.
  for (const theme of THEMES) {
    for (const name of tintableVars(theme)) {
      for (const proposal of ["#808080", "#ff00ff", "#00ff66", "#000000", "#ffffff", "#3b0a12"]) {
        const resolved = resolveTint(theme, name, proposal);
        const { floor, against } = ruleFor(name, theme);
        for (const bg of against) {
          assert.ok(
            contrastRatio(resolved.hex, bg) >= floor,
            `${theme.id} ${name} took ${proposal} and shipped ${resolved.hex}, ` +
              `which is ${contrastRatio(resolved.hex, bg).toFixed(2)}:1 on ${bg} (floor ${floor})`,
          );
        }
      }
    }
  }
});

test("a resolved tint never repaints a surface", () => {
  // The whole "backgrounds are off-limits" decision, asserted rather than
  // asserted-in-a-comment: whatever a tint resolves to, the three surfaces are
  // still the theme's own values. A tint that moved `--codify-bg` would
  // repaint the transcript and the gutter at once, and the clamp would be
  // guarding colours on a ground that has already moved.
  const storage = fakeStorage();
  const style = fakeStyle();
  const tints: Record<string, string> = {};
  for (const name of tintableVars(themeById("toxic-lab"))) tints[name] = "#ff00ff";
  saveTints(storage, { "toxic-lab": tints });
  applyTintedTheme("toxic-lab", style, storage);
  const theme = themeById("toxic-lab");
  for (const surface of ["--codify-bg", "--codify-surface", "--codify-raised"] as const) {
    assert.equal(
      style.props.get(surface),
      theme.tokens[surface],
      `a tint repainted ${surface}`,
    );
  }
  assert.equal(style.props.get("--reagent"), "#ff00ff", "the weather variable did not take the tint");
});

// ─────────────────────────────────────────────────────────────────────────────
// Storage
// ─────────────────────────────────────────────────────────────────────────────

test("loadTints drops everything that is not shaped like a store", () => {
  // A hand-edited or half-written key is corruption, not a choice, and guessing
  // at it is how one bad write becomes a permanently odd palette.
  assert.deepEqual(loadTints(fakeStorage({ [TINT_STORAGE_KEY]: "{" })), {});
  assert.deepEqual(loadTints(fakeStorage({ [TINT_STORAGE_KEY]: "[1,2]" })), {});
  assert.deepEqual(loadTints(fakeStorage({ [TINT_STORAGE_KEY]: '"a string"' })), {});
  assert.deepEqual(loadTints(fakeStorage({ [TINT_STORAGE_KEY]: "null" })), {});
  assert.deepEqual(loadTints(fakeStorage()), {}, "absent key is an empty store, not a throw");
  assert.deepEqual(
    loadTints(fakeStorage({ [TINT_STORAGE_KEY]: JSON.stringify({ t: { a: "#ff0000" } }) })),
    { t: { a: "#ff0000" } },
  );
});

test("loadTints keeps the good values of a partly bad map", () => {
  const raw = JSON.stringify({
    "toxic-lab": {
      "--reagent": "#00ff88",
      "--reagent-skin": "chartreuse", // not a hex
      "--neural-web": null, // not even a string
      "--arc-core": 42, // a number that would stringify to a plausible hex
    },
    "vector-wire": "not a map", // dropped whole, not spread into strings
  });
  const store = loadTints(fakeStorage({ [TINT_STORAGE_KEY]: raw }));
  assert.deepEqual(store, { "toxic-lab": { "--reagent": "#00ff88" } });
  assert.ok(
    !("vector-wireframe" in store) && !("vector-wire" in store),
    "a theme entry that was not a map survived as an empty one",
  );
});

test("isTintable is the only door, and it is narrow", () => {
  for (const good of ["#000000", "#ffffff", "#AbCdEf"]) {
    assert.ok(isTintable(good), `${good} is a colour`);
  }
  for (const bad of ["#fff", "red", "rgb(1 2 3)", "var(--x)", "#12345g", "", "#0000000"]) {
    assert.ok(!isTintable(bad), `${bad} is not a colour`);
  }
});

test("saveTints prunes emptied themes and removes the key rather than storing {}", () => {
  // A store full of `{}` is indistinguishable from a store full of choices to
  // anyone reading `localStorage`, and it grows without ever being meaningful.
  const storage = fakeStorage();
  saveTints(storage, { a: { "--codify-accent": "#ff0000" }, b: {} });
  assert.deepEqual(JSON.parse(storage.items.get(TINT_STORAGE_KEY)!), { a: { "--codify-accent": "#ff0000" } });

  saveTints(storage, { a: {}, b: {} });
  assert.equal(storage.items.has(TINT_STORAGE_KEY), false, "an all-empty store left the key behind");
});

test("resolveTints ignores a tint for a variable this theme does not offer", () => {
  // A theme id is the only key, so a tint stored for `--reagent` under
  // `toxic-lab` must not follow the user to `vector-wireframe` — where that
  // variable means nothing and `--vector-line` does.
  const theme = themeById("vector-wireframe");
  const resolved = resolveTints(theme, { "--reagent": "#00ff88", "--vector-line": "#00ff88" });
  assert.equal(resolved["--reagent"], undefined);
  assert.equal(resolved["--vector-line"], "#00ff88");
});

// ─────────────────────────────────────────────────────────────────────────────
// The apply
// ─────────────────────────────────────────────────────────────────────────────

test("an override is merged before the -rgb triplet is derived, not after", () => {
  // If the hex were written second, `rgb(var(--arc-core-rgb) / 0.4)` — which is
  // how the utility overrides in index.css rebuild these colours with an alpha
  // — would keep resolving to the theme's original hue while the canvas painted
  // the user's. Two different colours in one component, and a screenshot would
  // not show it.
  const style = fakeStyle();
  applyTheme("toxic-lab", style, { "--reagent": "#00ff88" });
  assert.equal(style.props.get("--reagent"), "#00ff88");
  assert.equal(style.props.get("--reagent-rgb"), "0 255 136", "the triplet is the old colour's");
  assert.notEqual(
    style.props.get("--reagent-rgb"),
    style.props.get("--reagent"),
    "sanity: the triplet is channels, not a hex",
  );
});

test("switching away from a tinted theme clears the tint", () => {
  // `applyTheme` clears every managed variable first — that is the whole
  // difference between switching and layering. A tint that survived the switch
  // would leave a `toxic-lab` green in `vector-wireframe`, which publishes no
  // such variable and so could never be cleared again.
  const style = fakeStyle();
  applyTheme("toxic-lab", style, { "--reagent": "#00ff88" });
  assert.ok(MANAGED_VARS.includes("--reagent" as ManagedVar), "sanity: the variable is managed");
  applyTheme("vector-wireframe", style);
  assert.equal(style.props.get("--reagent"), undefined, "a tint outlived the theme that set it");
});

test("the boot path applies stored tints, not just the palette", () => {
  const storage = fakeStorage({
    [TINT_STORAGE_KEY]: JSON.stringify({ "vector-wireframe": { "--vector-line": "#00ff88" } }),
  });
  const style = fakeStyle();
  applyTintedTheme("vector-wireframe", style, storage);
  assert.equal(style.props.get("--vector-line"), "#00ff88");
  assert.equal(
    style.props.get("--codify-bg"),
    themeById("vector-wireframe").tokens["--codify-bg"],
    "the palette still applies underneath the tint",
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// The claim the whole feature rests on
// ─────────────────────────────────────────────────────────────────────────────

const SIZE: AtmosphereSize = { width: 1440, height: 900 };

interface StyleMark {
  op: "fill" | "stroke" | "fillRect";
  style: string;
}

/**
 * A context that records the *colour* of every mark, which is the one thing
 * `painter.test.ts` deliberately ignores — it asks whether a painter painted,
 * this asks what colour it painted in.
 */
function stylingContext(): { ctx: CanvasRenderingContext2D; marks: StyleMark[]; reset: () => void } {
  const marks: StyleMark[] = [];
  let path = 0;
  const ctx = {
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    lineCap: "butt" as CanvasLineCap,
    lineJoin: "miter" as CanvasLineJoin,
    globalAlpha: 1,
    save() {},
    restore() {},
    clip() {},
    beginPath() { path = 0; },
    closePath() {},
    moveTo() { path += 1; },
    lineTo() { path += 1; },
    quadraticCurveTo() { path += 1; },
    bezierCurveTo() { path += 1; },
    arc() { path += 1; },
    clearRect() {},
    fillRect() { marks.push({ op: "fillRect", style: String(ctx.fillStyle) }); },
    fill() { if (path) marks.push({ op: "fill", style: String(ctx.fillStyle) }); },
    stroke() { if (path) marks.push({ op: "stroke", style: String(ctx.strokeStyle) }); },
    fillText() { path += 1; },
    createLinearGradient() { return { addColorStop() {} }; },
    createRadialGradient() { return { addColorStop() {} }; },
    measureText() { return { width: 0 }; },
  };
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    marks,
    reset: () => { marks.length = 0; },
  };
}

/** A tick the painters accept; the frame number is the only part any of them reads. */
const at = (frame: number): AtmosphereTick => ({ frame, seconds: frame / 30, size: SIZE });

test("a painter re-reads its variables every frame, so a tint repaints a running canvas", () => {
  // The regression this whole file exists for. Every painter used to resolve
  // its colours once, when the painter was *created*, and cache them in a
  // closure — which made `useAtmosphereCanvas`'s "a theme applied while a
  // canvas is running restyles it on the next frame" a false sentence, and made
  // the tint picker a control that visibly did nothing to the one thing it
  // exists to change.
  //
  // The value handed to the painter is a hex, because that is what a painter
  // reads: `channelsOf` calls `hexChannels`, which throws on anything else, so
  // a channel string here would fail in `read` rather than test anything. The
  // assertion is therefore on the `rgba(...)` the painter built from it, which
  // is one step further along and cannot pass by coincidence.
  const cases: ReadonlyArray<{
    name: string;
    create: (read: (n: string) => string, active: boolean) => AtmospherePainter;
    variable: string;
  }> = [
    { name: "ElectricArc", create: createElectricArcPainter, variable: "--arc-core" },
    { name: "ToxicLab", create: createToxicLabPainter, variable: "--reagent" },
  ];

  for (const { name, create, variable } of cases) {
    let current = "#ff0000";
    const read = (asked: string): string => (asked === variable ? current : "#808080");

    const painter = create(read, true);
    const first = stylingContext();
    painter.draw(first.ctx, SIZE, at(0));
    assert.ok(
      first.marks.some((m) => m.style.includes("255, 0, 0")),
      `${name} painted in the wrong colour on its static frame: ` +
        `${first.marks.map((m) => m.style).join(" | ") || "(nothing)"}`,
    );

    // The variable changes under a painter that is already running — which is
    // the situation a user is in when they drag a colour in the picker.
    current = "#00ff88";
    for (let frame = 1; frame <= 8; frame += 1) {
      painter.step?.(1 / 30, at(frame));
    }
    const second = stylingContext();
    painter.draw(second.ctx, SIZE, at(9));
    assert.ok(second.marks.length > 0, `${name} painted nothing after the variable changed`);
    assert.ok(
      second.marks.some((m) => m.style.includes("0, 255, 136")),
      `${name} never repainted in the new value of ${variable}: it drew ` +
        `${[...new Set(second.marks.map((m) => m.style))].join(" | ") || "(nothing)"}`,
    );
  }
});

test("a painter's static frame also reads live variables", () => {
  // The one-frame case matters more than it looks: under
  // `prefers-reduced-motion` the hook calls `draw` exactly once and never arms
  // the loop. A painter that resolved its colours in its *constructor* would
  // still show the old palette there forever, and a reduced-motion user would
  // be the one person a tint never reached.
  let current = "#808080";
  const read = (asked: string): string => (asked === "--reagent" ? current : "#808080");
  const painter = createToxicLabPainter(read, true);
  current = "#00ff88";
  const frame = stylingContext();
  painter.draw(frame.ctx, SIZE, at(0));
  assert.ok(
    frame.marks.some((m) => m.style.includes("0, 255, 136")),
    `the static frame painted ${frame.marks.map((m) => m.style).join(" | ") || "(nothing)"}`,
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// The pane
// ─────────────────────────────────────────────────────────────────────────────

const pane = (): string => renderToStaticMarkup(React.createElement(AppearancePane));

/** The store's key, named once so a test cannot mistype it and pass anyway. */
const STORAGE = "codify.tints";

test("the picker is on screen for the theme you are wearing, and only that one", () => {
  // It appears for the *selected* theme, never for a hovered one. Two reasons,
  // and the second is the one that decides it: storing a palette for a theme
  // you are only pointing at would let a stray mouse-cross write to disk, and
  // the colour being dragged would have no effect on the app — which is
  // showing a different theme. A preview you cannot customise is honest; a
  // picker that silently does nothing is not.
  localStorage.setItem("codify.theme", "toxic-lab");
  const out = pane();
  assert.match(out, /Custom colours/, "the active theme has no picker");
  const wells = out.match(/type="color"/g) ?? [];
  assert.equal(
    wells.length,
    tintableVars(themeById("toxic-lab")).length,
    "the picker did not offer one well per tintable variable",
  );

  // Every well is a bare input, so every one needs an accessible name; an
  // unnamed colour well is a control a screen reader announces as "colour
  // picker" with nothing said about what it colours.
  for (const match of out.matchAll(/<input[^>]*type="color"[^>]*>/g)) {
    assert.match(
      match[0],
      /aria-label="[^"]+"/,
      `a colour well has no label: ${match[0].slice(0, 140)}`,
    );
  }
  // And nothing is described-but-not-offered: an earlier version listed the
  // editable colours in a sentence and drew wells only for the edited ones,
  // which named three controls the user could not touch.
  for (const name of tintableVars(themeById("toxic-lab"))) {
    assert.ok(
      out.includes(TINT_ROLES[name] as string),
      `${name} is offered as editable but its row is not on screen`,
    );
  }
});

test("a tint the guard refused is shown as refused, with the theme's own value", () => {
  // The picker's honesty test. If it rendered only the proposal, a user who
  // picked something unusable would see a swatch the screen is not wearing, and
  // the note would be the only warning about something invisible.
  localStorage.setItem("codify.theme", "toxic-lab");
  localStorage.setItem(
    TINT_STORAGE_KEY,
    JSON.stringify({ "toxic-lab": { "--codify-accent": "#808080" } }),
  );
  const out = pane();
  assert.ok(out.includes("was #808080"), "the pane did not say what the picker's value was");
  // Only the row that was actually tinted offers a Reset; the untouched ones
  // have nothing to undo, and a Reset beside every row would imply otherwise.
  const resets = out.match(/aria-label="Reset [^"]+"/g) ?? [];
  assert.equal(resets.length, 1, `expected one Reset button, found ${resets.length}`);
  localStorage.removeItem(TINT_STORAGE_KEY);
});

test("Undo is there, and it says there is nothing to undo rather than just sitting there", () => {
  // The empty state is the only one a static render can reach, because the
  // history is React state and starts empty. It is worth the assertion anyway:
  // a permanently-enabled Undo that does nothing is worse than no button, and
  // a *disabled* button named only "Undo" tells a screen reader nothing about
  // why.
  localStorage.setItem("codify.theme", "toxic-lab");
  localStorage.removeItem(STORAGE);
  const empty = pane();
  assert.match(
    empty,
    /aria-label="Nothing to undo"/,
    "the Undo button does not say it has nothing to undo",
  );
  assert.match(
    empty,
    /aria-label="Nothing to undo"[^>]*disabled|disabled[^>]*aria-label="Nothing to undo"/,
    "and it is not disabled",
  );

  // With something to reset, the two buttons sit side by side and must stay two
  // distinct things. Conflating them is how "undo my last pick" turns into
  // "throw away all my colours", so this is worth a look at the markup for.
  localStorage.setItem(STORAGE, JSON.stringify({ "toxic-lab": { "--reagent": "#8b1e2d" } }));
  const tinted = pane();
  assert.ok(tinted.includes("Reset all"), "the per-theme reset is gone once there is something to reset");
  assert.ok(
    !tinted.includes('aria-label="Reset all"'),
    "and Reset all now claims the accessible name Undo should have",
  );
  // Reset is named by its own text rather than an aria-label, so it is reachable
  // by its visible wording; Undo needs the label because "Undo" alone says
  // nothing about *what* it would take back.
  assert.match(tinted, /Nothing to undo/, "the Undo button lost its name with a non-empty history");
  localStorage.removeItem(STORAGE);
});

test("the detail panel prints the colour you are wearing, the rows print the theme's", () => {
  // Found by looking at the panel rather than by reading it. With a tint on
  // Toxic Lab's `--reagent`, the hex line said `#9dff3c` and the colour well
  // two lines below said `#ce2c43`, with the canvas painting the second and
  // nothing on screen saying which of the two the question was about.
  //
  // The rows keep the stock values: their job is telling two dark themes apart
  // at a glance, and the stock palette is the stable identity. The detail
  // prints the resolved ones, because its job is telling you what you are
  // looking at — and the redundancy is the point: the same hex in the line and
  // in the well is what makes either of them believable.
  localStorage.setItem("codify.theme", "toxic-lab");
  localStorage.setItem(STORAGE, JSON.stringify({ "toxic-lab": { "--reagent": "#8b1e2d" } }));
  const out = pane();
  const theme = themeById("toxic-lab");
  const stock = theme.tokens["--reagent"] as string;
  const resolved = resolveTints(theme, { "--reagent": "#8b1e2d" })["--reagent"] as string;
  assert.notEqual(resolved, stock, "sanity: the tint has to change something for this to mean anything");
  assert.ok(
    out.includes(stock),
    "the rows lost the theme's own hexes, which is how two dark themes are told apart",
  );
  assert.ok(out.includes(resolved), "the detail panel is not showing the resolved colour");
  assert.ok(
    out.split(resolved).length - 1 >= 2,
    "the resolved colour appears once; the hex line and the well should agree",
  );
  localStorage.removeItem(STORAGE);
});

test("a theme with no weather offers its accent alone, and nothing to reset", () => {
  // The default theme is the one a new user sees first, so a picker that
  // rendered three empty rows there would be the first impression of the
  // feature. It has no painter, so it has no weather to recolour.
  localStorage.setItem("codify.theme", "codify-dark");
  const out = pane();
  assert.equal(
    (out.match(/type="color"/g) ?? []).length,
    1,
    "codify-dark publishes no weather, so it should offer exactly its accent",
  );
  assert.doesNotMatch(out, /Reset all/, "and nothing is tinted, so there is nothing to reset");
});
