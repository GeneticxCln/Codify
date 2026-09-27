import React from "react";
import {
  BIOLUMINESCENT_ABYSS,
  CYBERPUNK_NEON,
  HUD_TACTICAL,
  NEURAL_CONSTELLATION,
  SOLAR_FLARE,
  type AppearanceTheme,
} from "../../appearance";
import { useTheme } from "../../hooks/useTheme";
import { type AtmosphereCanvasProps } from "../../hooks/useAtmosphereCanvas";
import { AbyssSpores } from "./AbyssSpores";
import { CyberGrid } from "./CyberGrid";
import { HudSweep } from "./HudSweep";
import { NeuralWeb } from "./NeuralWeb";
import { NebulaFlow } from "./NebulaFlow";

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
      <Canvas className="absolute inset-0 h-full w-full" maxDimension={1024} active={active} />
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
