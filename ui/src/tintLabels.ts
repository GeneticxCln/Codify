import type { AppearanceTheme } from "./appearance";
import { STATUS_VARS, SURFACE_VARS, TEXT_VARS, weatherVars } from "./tint";

/** What each customisable variable means, for the Appearance picker's labels. */
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
