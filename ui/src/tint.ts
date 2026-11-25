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
 * Deep theme customisation, and the contrast guard that keeps foregrounds legible.
 *
 * Every colour token a theme publishes can be customised per theme:
 * - **Surfaces & borders** (`--codify-bg`, `--codify-surface`, `--codify-raised`, `--codify-border`,
 *   `--codify-border-strong`, and `--cmatrix-bg`) apply verbatim so a person can reshape the entire
 *   palette — pure OLED black, tinted darks, or even light surfaces.
 * - **Text, accent, status tones, and backdrop art** are contrast-checked against the theme's
 *   *effective* surfaces (including any custom surface colours the user chose), moving lightness
 *   while preserving hue and saturation so the UI remains readable.
 *
 * `applyTheme` merges resolved overrides before deriving `-rgb` channel triplets and `--codify-*-ink`
 * badge/button inks, so every utility class, terminal palette, and live canvas repaints together.
 */

/** `var -> #rrggbb`, for one theme. Absent means "the theme's own value". */
export type TintMap = Readonly<Record<string, string>>;

/** Every theme's tints: `themeId -> (var -> hex)`. */
export type TintStore = Readonly<Record<string, TintMap>>;

export const TINT_STORAGE_KEY = "codify.tints";

/** The surface tokens a foreground colour has to stay legible on, worst case first. */
const TEXT_SURFACES = ["--codify-raised", "--codify-surface", "--codify-bg"] as const;
const ART_SURFACES = ["--codify-bg", "--codify-raised"] as const;

/** Surface & structural colour variables that apply verbatim without contrast clamping. */
export const SURFACE_VARS: readonly string[] = [
  "--codify-bg",
  "--codify-surface",
  "--codify-raised",
  "--codify-border",
  "--codify-border-strong",
];

/** Text colour variables. */
export const TEXT_VARS: readonly string[] = [
  "--codify-primary",
  "--codify-secondary",
  "--codify-muted",
];

/** Status tone variables. */
export const STATUS_VARS: readonly string[] = [
  "--codify-info",
  "--codify-success",
  "--codify-warning",
  "--codify-danger",
  "--codify-neutral",
];

/** Background/structural tokens that are never clamped against surfaces. */
const UNCLAMPED_VARS = new Set<string>([...SURFACE_VARS, "--cmatrix-bg"]);

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

/** What each customisable variable means, for the picker's label. */
export const TINT_ROLES: Readonly<Record<string, string>> = {
  "--codify-accent": "Interface accent",
  "--codify-bg": "Window background",
  "--codify-surface": "Panel surface",
  "--codify-raised": "Elevated surface",
  "--codify-border": "Borders",
  "--codify-border-strong": "Strong borders",
  "--codify-primary": "Primary text",
  "--codify-secondary": "Secondary text",
  "--codify-muted": "Muted text",
  "--codify-info": "Info tone",
  "--codify-success": "Success tone",
  "--codify-warning": "Warning tone",
  "--codify-danger": "Danger tone",
  "--codify-neutral": "Neutral badge",
  "--cmatrix-bg": "Rain backdrop",
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

/** Non-colour managed tokens that a colour picker cannot hold. */
const NOT_COLOUR: ReadonlyArray<string> = ["--cmatrix-glyphs"];

function weatherVars(theme: AppearanceTheme): string[] {
  return MANAGED_VARS.filter(
    (name) =>
      !name.startsWith("--codify-") &&
      !name.endsWith("-rgb") &&
      !NOT_COLOUR.includes(name) &&
      name in theme.tokens,
  );
}

/** One titled group of colour tokens for the Appearance customiser. */
export interface TintGroup {
  id: "accent" | "surfaces" | "text" | "status";
  title: string;
  vars: string[];
}

/** The customisable variables of `theme`, grouped for clear navigation. */
export function tintGroups(theme: AppearanceTheme): TintGroup[] {
  const present = (list: readonly string[]): string[] =>
    list.filter((name) => name in theme.tokens);
  const weather = weatherVars(theme);
  const groups: TintGroup[] = [
    {
      id: "accent",
      title: weather.length > 0 ? "Accent & backdrop" : "Accent",
      vars: ["--codify-accent", ...weather],
    },
    { id: "surfaces", title: "Surfaces & borders", vars: present(SURFACE_VARS) },
    { id: "text", title: "Text", vars: present(TEXT_VARS) },
    { id: "status", title: "Status tones", vars: present(STATUS_VARS) },
  ];
  return groups.filter((g) => g.vars.length > 0);
}

/** Every colour variable a theme offers for customisation, in picker order. */
export function tintableVars(theme: AppearanceTheme): string[] {
  return tintGroups(theme).flatMap((g) => g.vars);
}

/** The contrast floor and surfaces one variable is held to. */
export function ruleFor(name: string, theme: AppearanceTheme): { floor: number; against: string[] } {
  if (UNCLAMPED_VARS.has(name)) {
    return { floor: 0, against: [] };
  }
  const isText = name.startsWith("--codify-") && name !== "--codify-neutral";
  const names = isText ? TEXT_SURFACES : ART_SURFACES;
  return {
    floor: isText ? TEXT_FLOOR : ART_FLOOR,
    against: names
      .filter((s) => typeof theme.tokens[s] === "string")
      .map((s) => theme.tokens[s] as string),
  };
}

/** Build a view of `theme` whose surface/structural tokens include any valid user overrides. */
export function themeWithSurfaceTints(
  theme: AppearanceTheme,
  tints: TintMap | undefined,
): AppearanceTheme {
  if (!tints) return theme;
  let changed = false;
  const tokens: Record<string, string> = { ...(theme.tokens as Record<string, string>) };
  for (const name of UNCLAMPED_VARS) {
    const proposed = tints[name];
    if (proposed && name in tokens && isTintable(proposed)) {
      tokens[name] = proposed.trim().toLowerCase();
      changed = true;
    }
  }
  return changed ? { ...theme, tokens: tokens as AppearanceTheme["tokens"] } : theme;
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
 * Resolve one proposed tint against one theme (or a theme with user surface overrides applied).
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
  const clean = proposed.trim().toLowerCase();
  const { floor, against } = ruleFor(name, theme);
  if (against.length === 0 || floor <= 0) {
    return { hex: clean, proposed: clean, adjusted: false, ratio: 0 };
  }
  const clamped = clampForContrast(clean, against, floor);
  if (clamped === null) {
    const ratio = Math.min(...against.map((bg) => contrastRatio(own, bg)));
    return { hex: own, proposed: clean, adjusted: true, ratio };
  }
  const ratio = Math.min(...against.map((bg) => contrastRatio(clamped, bg)));
  return { hex: clamped, proposed: clean, adjusted: clamped.toLowerCase() !== clean, ratio };
}

/**
 * Every tint for one theme, resolved in two passes:
 * 1. Surface & border overrides apply directly, updating the effective surfaces.
 * 2. Foreground, text, status, and backdrop overrides resolve against those effective surfaces.
 */
export function resolveTints(theme: AppearanceTheme, tints: TintMap | undefined): Record<string, string> {
  if (!tints) return {};
  const allowed = new Set(tintableVars(theme));
  const effective = themeWithSurfaceTints(theme, tints);
  const out: Record<string, string> = {};
  for (const name of Object.keys(tints)) {
    if (!allowed.has(name)) continue;
    const proposed = tints[name]!;
    const { hex } = resolveTint(effective, name, proposed);
    if (hex) out[name] = hex;
  }
  return out;
}

/** Read the store, refusing anything that is not shaped like one. */
export function loadTints(
  storage: Pick<Storage, "getItem"> | undefined = tintStorage(),
): TintStore {
  let raw: string | null | undefined;
  try {
    raw = storage?.getItem(TINT_STORAGE_KEY);
  } catch {
    return {};
  }
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
  try {
    if (Object.keys(pruned).length === 0) storage.removeItem(TINT_STORAGE_KEY);
    else storage.setItem(TINT_STORAGE_KEY, JSON.stringify(pruned));
  } catch {
    // Storage may be blocked or full; keep the in-memory theme applied without crashing.
  }
}

function tintStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> | undefined {
  try {
    return typeof localStorage !== "undefined" ? localStorage : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Apply a theme with the user's own colours on top of it.
 */
export function applyTintedTheme(
  id: string,
  root?: CssStyleTarget,
  storage: Pick<Storage, "getItem"> | undefined = tintStorage(),
): Array<[string, string]> {
  const theme = themeById(id);
  return applyTheme(theme.id, root, resolveTints(theme, loadTints(storage)[theme.id]));
}
