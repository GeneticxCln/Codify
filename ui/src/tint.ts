import { clampForContrast, contrastRatio } from "./contrast";

// The lightness clamp lives in `contrast.ts` now, because `appearance.ts` needs it to derive the status
// inks and cannot import this module (this one imports it). Re-exported so `tint.test.ts`, and anything
// else that asked tint.ts for it, keeps one place to look.
export { clampForContrast };
import {
  MANAGED_VARS,
  applyTheme,
  themeById,
  type AppearanceTheme,
  type CssStyleTarget,
  type ManagedVar,
} from "./appearance";

/**
 * Custom colours, and the guard that makes them safe.
 *
 * A tint is a per-theme override of a handful of colour variables: the theme's
 * `accent`, and the two or three weather variables its own painter reads. The
 * physics do not change — the same 30 FPS, the same capped canvas, the same
 * gutters — because nothing here touches a painter's arithmetic. It only
 * rewrites the custom properties the painter resolves every frame.
 *
 * **Why a guard, and not a free colour picker.** A free picker is a bug report.
 * Every colour in this app is painted on a surface, and
 * `ui/tests/contrast.test.ts` holds every pair in every theme to WCAG AA at its
 * own size and weight. A user who picks a deep crimson for the accent of a
 * `#12271a` raised surface gets about 1.4:1 and a transcript they cannot read,
 * and no amount of "it was the user's choice" makes that acceptable. So a
 * proposed colour is *clamped* — its hue and saturation are kept and its
 * lightness is moved until the pairs pass — and when even a fully light or
 * fully dark version of it cannot pass, the theme's own value is kept instead.
 * A tint that cannot be made readable does not get applied.
 *
 * **Why `accent` and the weather variables, and not more.** `primary`,
 * `secondary` and `muted` are the surfaces everything else is measured against;
 * letting a user move them turns a contrast guard into a suggestion. And
 * `danger` and `warning` are held by a separate rule that no picker may touch:
 * they stay on the warm arc so that a user who has learned that orange means
 * "needs attention" in one theme recognises it in the next.
 */

/** `var -> #rrggbb`, for one theme. Absent means "the theme's own value". */
export type TintMap = Readonly<Record<string, string>>;

/** Every theme's tints: `themeId -> (var -> hex)`. */
export type TintStore = Readonly<Record<string, TintMap>>;

export const TINT_STORAGE_KEY = "codify.tints";

/** The surface tokens a colour has to stay legible on, worst case first. */
const TEXT_SURFACES = ["--codify-raised", "--codify-surface", "--codify-bg"] as const;
const ART_SURFACES = ["--codify-bg", "--codify-raised"] as const;

/**
 * AA for body text. The app is full of `text-xs` captions, and WCAG 1.4.3
 * drops to 3:1 only for 18pt regular or 14pt **bold** — so 4.5 is the floor for
 * anything a caption can be drawn in.
 */
export const TEXT_FLOOR = 4.5;

/**
 * Non-text contrast, which is 3:1 in WCAG 1.4.11. A weather variable paints
 * particles and lines, never a glyph, so it is held to the graphic floor —
 * the same reasoning `contrast.test.ts` uses to exempt large text.
 */
export const ART_FLOOR = 3;

/** What each paintable variable means, for the picker's label. */
export const TINT_ROLES: Readonly<Record<string, string>> = {
  "--codify-accent": "Interface accent",
  "--cmatrix-rain": "Rain",
  "--cmatrix-text": "Rain highlight",
  "--cyber-cyan": "Horizon cyan",
  "--cyber-magenta": "Sun magenta",
  "--cyber-amber": "Sun amber",
  "--cyber-scan": "Scanline sweep",
  "--neural-web": "Web, faintest",
  "--neural-node": "Node cores",
  "--neural-pulse": "Working pulse",
  "--hud-amber": "Instrumentation",
  "--hud-tick": "Tick ladder",
  "--abyss-deep": "Spore halo",
  "--abyss-spore": "Spore cores",
  "--flare-indigo": "Stream, inner end",
  "--flare-ultraviolet": "Stream, outer end",
  "--snow-flake": "Near flakes",
  "--snow-flake-2": "Far flakes",
  "--snow-glow": "Lights",
  "--snow-glow-2": "Second light",
  "--sun-core": "Flare, wide stroke",
  "--sun-edge": "Flare, bright core",
  "--organ-node": "Node cores",
  "--organ-tendril": "Tendrils",
  "--void-arc": "Near side of the disc",
  "--void-dust": "Outer orbits",
  "--void-beam": "Working beams",
  "--vector-line": "Every edge",
  "--vector-lock": "Locked reticle",
  "--fluid-crest": "Wave highlight",
  "--fluid-trough": "Wave body",
  "--arc-core": "Discharge channel",
  "--arc-fork": "The spurs",
  "--reagent": "Bubble body",
  "--reagent-skin": "Bubble skin",
  "--fluid-bit": "The data",
  "--fluid-cursor": "Boundary cell",
};

/** A `#rrggbb` the picker can actually store. */
export function isTintable(hex: string): boolean {
  return /^#[0-9a-f]{6}$/i.test(hex.trim());
}

/**
 * The variables a theme offers for tinting, in the order the picker shows them.
 *
 * Derived rather than declared, so a new theme gets a picker with no second
 * list to keep in step — but derived from a *narrower* rule than "every managed
 * variable the theme publishes", because that set is the whole palette. Taking
 * it wholesale offered `--codify-border` and `--codify-muted` on the default
 * theme: the first is the hairline the rest of the app is read against, the
 * second is the captions. Both are the kind of colour a user can pick exactly
 * once, by accident, and then live with.
 *
 * So the rule is the weather namespaces, and nothing else: the accent, which
 * every theme has, plus this theme's own painted colours. `MANAGED_VARS` is
 * where the namespaces are declared, which is why the two are named here rather
 * than filtered on a prefix — a new namespace in `appearance.ts` becomes
 * tintable without a second edit, and `--codify-*` can never become tintable
 * by accident.
 */
const NOT_PAINT: ReadonlyArray<string> = [
  // The CMatrix canvas's own backdrop. It is a surface, drawn *behind* the
  // gutter, and a user who could move it would be repainting the window
  // rather than the weather.
  "--cmatrix-bg",
  // The only managed variable that is not a colour: the ASCII rain publishes
  // the glyphs it draws, so a monochrome theme is a token edit rather than a
  // forked component. It has no hex and a colour well cannot hold it.
  "--cmatrix-glyphs",
];

export function tintableVars(theme: AppearanceTheme): string[] {
  const weather = MANAGED_VARS.filter(
    (name) =>
      !name.startsWith("--codify-") &&
      !name.endsWith("-rgb") &&
      !NOT_PAINT.includes(name) &&
      name in theme.tokens,
  );
  return ["--codify-accent", ...weather];
}

/** The contrast floor and surfaces one variable is held to. */
export function ruleFor(name: string, theme: AppearanceTheme): { floor: number; against: string[] } {
  const isText = name.startsWith("--codify-");
  const names = isText ? TEXT_SURFACES : ART_SURFACES;
  return {
    floor: isText ? TEXT_FLOOR : ART_FLOOR,
    against: names
      .filter((s) => typeof theme.tokens[s] === "string")
      .map((s) => theme.tokens[s] as string),
  };
}

/** The outcome of resolving one proposed tint, and why it came out that way. */
export interface ResolvedTint {
  /** What `applyTheme` will actually write. */
  hex: string;
  /** The colour the user proposed, kept so the picker can show both. */
  proposed: string;
  /** True when the guard moved the colour, or refused it and kept the theme's. */
  adjusted: boolean;
  /** The lowest ratio the resolved colour achieves, for the picker's readout. */
  ratio: number;
}

/**
 * Resolve one proposed tint against one theme, or refuse it.
 *
 * The refusal is the interesting branch and it is a real case rather than a
 * guard against the impossible: a user picking a mid-luminance accent for
 * `Liquid Mercury`, whose `--codify-raised` is `#131720` and whose own accent is
 * already near the top of the passing range, has picked a colour that cannot be
 * made legible on that surface at any lightness. Keeping the theme's own value
 * is the only answer that does not ship an unreadable app.
 */
export function resolveTint(
  theme: AppearanceTheme,
  name: string,
  proposed: string,
): ResolvedTint {
  const own = theme.tokens[name as ManagedVar];
  if (typeof own !== "string" || !isTintable(own)) {
    return { hex: "", proposed, adjusted: false, ratio: 0 };
  }
  if (!isTintable(proposed)) {
    return { hex: own, proposed, adjusted: proposed !== own, ratio: 0 };
  }
  const { floor, against } = ruleFor(name, theme);
  const clamped = clampForContrast(proposed, against, floor);
  if (clamped === null) {
    const ratio = against.length ? Math.min(...against.map((bg) => contrastRatio(own, bg))) : floor;
    return { hex: own, proposed, adjusted: true, ratio };
  }
  const ratio = Math.min(...against.map((bg) => contrastRatio(clamped, bg)));
  return { hex: clamped, proposed, adjusted: clamped.toLowerCase() !== proposed.toLowerCase(), ratio };
}

/** Every tint for one theme, resolved, in picker order. */
export function resolveTints(theme: AppearanceTheme, tints: TintMap | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const name of Object.keys(tints ?? {})) {
    if (!tintableVars(theme).includes(name)) continue;
    const proposed = tints![name]!;
    const { hex } = resolveTint(theme, name, proposed);
    if (hex) out[name] = hex;
  }
  return out;
}

/** Read the store, refusing anything that is not shaped like one. */
export function loadTints(
  storage: Pick<Storage, "getItem"> | undefined = tintStorage(),
): TintStore {
  const raw = storage?.getItem(TINT_STORAGE_KEY);
  if (!raw) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
  const out: Record<string, Record<string, string>> = {};
  for (const [themeId, map] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof map !== "object" || map === null || Array.isArray(map)) continue;
    const tints: Record<string, string> = {};
    for (const [name, value] of Object.entries(map as Record<string, unknown>)) {
      // A stored value that is not a hex is dropped rather than clamped: it is
      // corruption, not a choice, and guessing at it is how a bad write becomes
      // a permanently odd palette.
      if (typeof value === "string" && isTintable(value)) tints[name] = value.toLowerCase();
    }
    out[themeId] = tints;
  }
  return out;
}

/** Write the store. An empty map for a theme removes it, rather than storing `{}`. */
export function saveTints(
  storage: Pick<Storage, "setItem" | "removeItem"> | undefined = tintStorage(),
  store: TintStore = {},
): void {
  if (!storage) return;
  const pruned: Record<string, Record<string, string>> = {};
  for (const [themeId, map] of Object.entries(store)) {
    if (Object.keys(map).length > 0) pruned[themeId] = { ...map };
  }
  if (Object.keys(pruned).length === 0) storage.removeItem(TINT_STORAGE_KEY);
  else storage.setItem(TINT_STORAGE_KEY, JSON.stringify(pruned));
}

function tintStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> | undefined {
  return typeof localStorage !== "undefined" ? localStorage : undefined;
}

/**
 * Apply a theme with the user's own colours on top of it.
 *
 * The one call the app makes, and the seam the picker goes through: resolve
 * this theme's stored tints, hand the result to `applyTheme` as overrides, and
 * let that one write every custom property. Nothing here re-implements the
 * apply, so there is no second place a colour can come from.
 *
 * The canvases need no notification. `applyTheme` ends by dispatching the theme
 * change event, and every painter resolves its variables through
 * `useAtmosphereCanvas`'s live `getComputedStyle` read on the frame it draws —
 * so a tint is on screen within one frame, and a running animation is the
 * reason it is free rather than a repaint.
 */
export function applyTintedTheme(
  id: string,
  root?: CssStyleTarget,
  storage: Pick<Storage, "getItem"> | undefined = tintStorage(),
): Array<[string, string]> {
  const theme = themeById(id);
  return applyTheme(theme.id, root, resolveTints(theme, loadTints(storage)[theme.id]));
}
