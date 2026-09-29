import React from "react";
import {
  ANON_FLUID,
  BIOLUMINESCENT_ABYSS,
  CYBER_ORGANISM,
  CYBERPUNK_NEON,
  ELECTRIC_ARC,
  EVENT_HORIZON,
  FESTIVE_NIGHT,
  HUD_TACTICAL,
  LIQUID_MERCURY,
  NEURAL_CONSTELLATION,
  SOLAR_FLARE,
  SOLARIZED_FLARE,
  TOXIC_LAB,
  VECTOR_WIREFRAME,
  WINTER_SNOW,
  type AppearanceTheme,
} from "../../appearance";
import { useTheme } from "../../hooks/useTheme";
import { type AtmosphereCanvasProps } from "../../hooks/useAtmosphereCanvas";
import { AbyssSpores } from "./AbyssSpores";
import { CyberGrid } from "./CyberGrid";
import { HudSweep } from "./HudSweep";
import { NeuralWeb } from "./NeuralWeb";
import { NebulaFlow } from "./NebulaFlow";
import { SnowFall } from "./SnowFall";
import { SolarWind } from "./SolarWind";
import { CyberOrganism } from "./CyberOrganism";
import { EventHorizon } from "./EventHorizon";
import { VectorWire } from "./VectorWire";
import { Nanofluid } from "./Nanofluid";
import { ElectricArc } from "./ElectricArc";
import { ToxicLab } from "./ToxicLab";
import { AnonFluid } from "./AnonFluid";

/**
 * The window's weather, for every theme that has some, in one component.
 *
 * The gate is the theme's variable rather than its id, and that one decision is
 * what makes the eighth animated theme a data entry instead of a component.
 * `appearance.ts` lists the same pairing in `ATMOSPHERE_TRIGGERS`, so the two
 * facts that must agree — a theme publishes a variable, and something watches
 * for it — are checkable against each other rather than being two files'
 * private habits.
 *
 * The mount point is the rain's, and the reasons are the ones written down in
 * `RainBackdrop.tsx`: a backdrop inside the transcript is a centred animation
 * that disappears on the first message, so this is mounted once by the shell
 * as a layer the app sits on, and nothing a conversation does reaches it.
 *
 * **The rain is not in this table, on purpose.** `MatrixRain` and its backdrop
 * predate the shell, and `RainBackdrop` is the one component the whole theme
 * suite reads as *source* — its box, its stacking position and its vignette
 * alpha are asserted against the file itself, so folding it in means editing
 * another thread's tests for no gain a user can see. Two rain themes therefore
 * arrive here by token alone (`cmatrix-oled`, `ascii-rain`), both rendered by
 * `RainBackdrop`, and this file handles the other five. That is one legacy
 * outlier rather than a mechanism per theme, and it is the honest trade: the
 * clean version is a one-line change to a test nobody asked me to touch.
 */
interface Atmosphere {
  /** The variable whose presence in the theme means "this theme wants weather". */
  trigger: string;
  /** The effect. Every canvas takes the same props; see `AtmosphereCanvasProps`. */
  Canvas: React.FC<AtmosphereCanvasProps>;
  /**
   * A CSS class layered over the canvas, for a theme whose look needs
   * something CSS is better at than a canvas — currently the CRT veil, which
   * has to be able to answer `prefers-reduced-motion` from `index.css` and
   * cannot from JavaScript.
   */
  overlayClass?: string;
  /**
   * The corner darkening, as a CSS background. `null` means none, which is the
   * right answer for an effect that puts its own structure in the corners —
   * a vignette over the HUD's brackets dims the only thing that theme draws.
   */
  vignette: string | null;
}

/** Theme id → what its weather is. This table is the whole extension point. */
const ATMOSPHERES: Readonly<Record<string, Atmosphere>> = {
  [CYBERPUNK_NEON.id]: {
    trigger: "--cyber-cyan",
    Canvas: CyberGrid,
    overlayClass: "cyber-veil",
    vignette: "radial-gradient(ellipse at center, transparent 42%, rgba(4, 1, 12, 0.62) 100%)",
  },
  [NEURAL_CONSTELLATION.id]: {
    trigger: "--neural-node",
    Canvas: NeuralWeb,
    // No vignette: the web is spread across the whole window and a corner
    // darkening would read as four grey patches, not as depth.
    vignette: null,
  },
  [HUD_TACTICAL.id]: {
    trigger: "--hud-amber",
    Canvas: HudSweep,
    // None, for the reason above: the brackets *are* the corners.
    vignette: null,
  },
  [BIOLUMINESCENT_ABYSS.id]: {
    trigger: "--abyss-spore",
    Canvas: AbyssSpores,
    vignette: null,
  },
  [SOLAR_FLARE.id]: {
    trigger: "--flare-indigo",
    Canvas: NebulaFlow,
    vignette: null,
  },
  // Two entries, one painter. The split is here rather than inside `SnowFall`
  // only because the *vignette* genuinely differs — a cool corner darkening for
  // a blue-black night, a warm one for a green-black one — and the vignette is
  // a CSS background this component already owns. The lights are not mentioned
  // here at all: a theme that publishes a glow colour gets them, and that is
  // decided by `SnowFall` reading its own variables.
  [WINTER_SNOW.id]: {
    trigger: "--snow-flake",
    Canvas: SnowFall,
    vignette:
      "radial-gradient(ellipse at center, transparent 46%, rgba(3, 8, 18, 0.58) 100%)",
  },
  [FESTIVE_NIGHT.id]: {
    trigger: "--snow-flake",
    Canvas: SnowFall,
    vignette:
      "radial-gradient(ellipse at center, transparent 46%, rgba(10, 16, 9, 0.55) 100%)",
  },
  // Five more, and every one of them a canvas that confines itself to the
  // margins — which is why each vignette is the *coolest* thing here: the
  // darkening exists to keep the eye in the middle of the window, and four of
  // the five effects only exist near the edges.
  [SOLARIZED_FLARE.id]: {
    trigger: "--sun-core",
    Canvas: SolarWind,
    // Very slight, and warmer than the others: this is a CRT, and a CRT's
    // corners are darker than its middle because of the tube, not the theme.
    vignette:
      "radial-gradient(ellipse at center, transparent 40%, rgba(26, 14, 0, 0.5) 100%)",
  },
  [CYBER_ORGANISM.id]: {
    trigger: "--organ-node",
    Canvas: CyberOrganism,
    vignette:
      "radial-gradient(ellipse at center, transparent 44%, rgba(2, 12, 28, 0.55) 100%)",
  },
  [EVENT_HORIZON.id]: {
    trigger: "--void-arc",
    Canvas: EventHorizon,
    // The faintest here, and the reason is the effect rather than the palette:
    // the orbital disc already reads as depth, and a heavy vignette over it
    // turns the arcs into four grey corners.
    vignette:
      "radial-gradient(ellipse at center, transparent 58%, rgba(0, 0, 0, 0.34) 100%)",
  },
  [VECTOR_WIREFRAME.id]: {
    trigger: "--vector-line",
    Canvas: VectorWire,
    vignette: null,
  },
  [LIQUID_MERCURY.id]: {
    trigger: "--fluid-crest",
    Canvas: Nanofluid,
    // The one metal theme, so the corners go *cool* rather than dark: a steel
    // edge is a highlight, not a shadow.
    vignette:
      "linear-gradient(90deg, rgba(51, 65, 85, 0.5) 0%, transparent 22%, transparent 78%, rgba(51, 65, 85, 0.5) 100%)",
  },
  // Three more margins, and the vignettes are the same shape as the five above
  // them for the same reason: each of these effects exists only near the edges,
  // so the corner darkening is what keeps the eye in the middle of the window.
  [ELECTRIC_ARC.id]: {
    trigger: "--arc-core",
    Canvas: ElectricArc,
    // Deeper than most, because this theme's brightest colour is a near-white
    // and a white discharge that survives into the transcript is the worst
    // legibility failure in the app.
    vignette:
      "radial-gradient(ellipse at center, transparent 40%, rgba(3, 3, 20, 0.6) 100%)",
  },
  [TOXIC_LAB.id]: {
    trigger: "--reagent",
    Canvas: ToxicLab,
    // A fume hood is dark at the corners because that is where the sash is,
    // and because a green wash across the whole window would read as a filter
    // over the content rather than as air in a room.
    vignette:
      "radial-gradient(ellipse at center, transparent 42%, rgba(4, 12, 6, 0.58) 100%)",
  },
  [ANON_FLUID.id]: {
    trigger: "--fluid-bit",
    Canvas: AnonFluid,
    // The faintest of the three, and deliberately so: a terminal's black is
    // already the darkest thing in the app, and darkening the corners of a
    // black window only makes the gutters harder to see.
    vignette:
      "radial-gradient(ellipse at center, transparent 52%, rgba(0, 10, 5, 0.36) 100%)",
  },
};

export interface WeatherBackdropProps {
  /**
   * Whether the agent is working. Every effect here reads it, and the clock
   * uses it to run the effect faster while a turn is in flight.
   *
   * This prop used to carry a different contract, and the difference is worth
   * keeping in the record: it said the flag "changes nothing about how fast
   * anything moves, which is the line between an effect that reports state and
   * one that is decoration." That was the right worry attached to the wrong
   * test. §7's distinction is not about *how* motion changes — a streaming caret
   * blinks at a constant rate and reports something perfectly well — it is
   * about whether the motion carries information the user did not already have.
   * A rate that tracks the run's state does. A rate that wobbles on a timer, or
   * a breath nobody can learn, would not, and the two look identical in code.
   */
  active?: boolean;
}

export const WeatherBackdrop: React.FC<WeatherBackdropProps> = ({ active }) => {
  const theme = useTheme();
  const atmosphere = ATMOSPHERES[theme.id];
  // Two gates, and both are needed: the table says what this theme's weather
  // would be, and the trigger says whether the theme actually asked for it. A
  // table entry alone would mount a canvas for a theme that published no
  // variable, which is the failure `MANAGED_VARS` exists to prevent elsewhere.
  if (!atmosphere || !theme.tokens[atmosphere.trigger as keyof typeof theme.tokens]) {
    return null;
  }
  const { Canvas, overlayClass, vignette } = atmosphere;
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 z-0 overflow-hidden">
      {/* 1024, like the rain: the cap is on the longer edge in CSS pixels and
          this box is the window, not a column. A perspective grid is cheaper
          than a glyph shower, but the cap is not about this frame's cost — it
          is about what happens on a 4K panel the user has maximised. */}
      {/* `codify-atmosphere-canvas` is the edge fade, and it is passed here
          rather than added inside each of the ~20 painters: every one of them
          renders the same `<canvas className={`pointer-events-none ${className}`}>`
          and threading a class through that one call site is what keeps a new
          effect from shipping without the fade. The class itself is in
          `index.css` — one mask definition, not twenty inline styles. */}
      <Canvas
        className="codify-atmosphere-canvas absolute inset-0 h-full w-full"
        maxDimension={1024}
        active={active}
      />
      {overlayClass && <div className={`${overlayClass} absolute inset-x-0 top-0`} />}
      {vignette && <div className="absolute inset-0" style={{ background: vignette }} />}
    </div>
  );
};

/** The themes this file knows how to give weather to, for the tests. */
export function themesWithWeather(): ReadonlyArray<string> {
  return Object.keys(ATMOSPHERES);
}

/** The trigger a theme is keyed on here, or `undefined` if it has no weather. */
export function triggerFor(theme: AppearanceTheme): string | undefined {
  return ATMOSPHERES[theme.id]?.trigger;
}

/**
 * The effect a theme's settings tile should draw, for the Appearance pane.
 *
 * The pane asks this rather than keeping its own `theme.id === …` chain, so the
 * tile and the backdrop can never disagree about what a theme looks like. The
 * two rain themes are the exception the pane still handles itself, because they
 * are not in this table — see the note on the table above.
 */
export function effectFor(themeId: string): React.FC<AtmosphereCanvasProps> | undefined {
  return ATMOSPHERES[themeId]?.Canvas;
};
