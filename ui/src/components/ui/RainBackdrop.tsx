import React from "react";
import { MatrixRain } from "./MatrixRain";
import { useTheme } from "../../hooks/useTheme";

/**
 * The window's weather: rain behind the whole app, edge to edge.
 *
 * **Where it is mounted is the whole design.** It used to live inside the idle
 * hero's empty state — a `max-w-2xl mx-auto` column — which is why it appeared
 * down the middle of the window and vanished the moment a message existed: the
 * transcript replaced the branch that contained it. Both were one decision, the
 * mount point, and a mount point inside the transcript cannot be "behind
 * everything, always": the sidebar is a sibling of the transcript, so a
 * backdrop in there is not behind the sidebar by construction, and any state the
 * transcript owns (`messages.length`) is a state that switches the backdrop off.
 *
 * So it is mounted once, in the shell, as a layer the whole app sits on. Nothing
 * about a conversation reaches it, which is what "the rain is the theme, not the
 * emptiness" has to mean if it is to survive the first message.
 *
 * Mounting is still the theme's decision, not this component's: it renders only
 * when the active theme publishes `--cmatrix-rain` (today, OLED CMatrix), so a
 * theme that names no rain variable gets no rain — adding a third rainy theme is
 * a token edit, not a component edit. The glyph shower itself is `MatrixRain`, at
 * its documented bounds: capped canvas, 30 FPS, a single static frame under
 * `prefers-reduced-motion`.
 *
 * `absolute inset-0` inside the app's `relative` root, not `fixed`: the root *is*
 * the viewport box, so this covers the window without inheriting a stacking
 * context from anywhere, and it is immune to the `overflow-hidden` on the shell's
 * columns that a `fixed` layer would not be. `z-0` with the content above it
 * (`relative z-10` on the header and the main row) is the other half of that
 * arrangement: an absolutely positioned box with no `z-index` paints *over* the
 * header's text, because positioned elements are painted after in-flow content.
 * `pointer-events-none` keeps every control clickable, `aria-hidden` keeps a
 * screen reader out of half-width katakana.
 *
 * The vignette is much lighter than the hero's used to need. It existed to make
 * one centred heading readable over moving glyphs; across the whole window it
 * would dim precisely the two sides the rain is for, so it now only takes the
 * last few percent at the very corners.
 */
export interface RainBackdropProps {
  /**
   * Whether the agent is working, which makes the rain fall faster.
   *
   * The same flag the other themes' weather gets. The rain is the one backdrop
   * that does not live in `WeatherBackdrop`'s table — it predates it — so it
   * takes the prop itself rather than inheriting it. Both paths end at the same
   * `nextRate`, so the rain and the grid answer "working" identically.
   */
  active?: boolean;
}

export const RainBackdrop: React.FC<RainBackdropProps> = ({ active }) => {
  const theme = useTheme();
  const rains = Boolean(theme.tokens["--cmatrix-rain"]);
  if (!rains) return null;
  return (
    // `data-backdrop` and `data-active`: which layer this is, and whether the shell told it the
    // agent is working. Same attributes as `WeatherBackdrop`, for the same reason.
    <div
      aria-hidden="true"
      data-backdrop="rain"
      data-active={active ? "true" : "false"}
      className="pointer-events-none absolute inset-0 z-0 overflow-hidden"
    >
      {/* 1024 rather than the hero's 720: the cap is on the longer edge in CSS
          pixels and this box is now the window, not a column, so the old cap
          would stretch a 1920px span from a 1440px canvas and the glyphs would
          read as blocks. Still capped — the cost of a full-bleed canvas is
          quadratic and the cap is what keeps it off the CPU budget. The cap
          bounds cost, not speed: a faster effect is not a busier one. */}
      {/* The same edge fade every other atmosphere gets — see
          `WeatherBackdrop` and `.codify-atmosphere-canvas` in `index.css`. The
          rain predates that table and has always been mounted separately, so it
          would otherwise have been the one canvas in the app that stopped dead
          against the header. */}
      <MatrixRain
        className="codify-atmosphere-canvas absolute inset-0 h-full w-full"
        maxDimension={1024}
        active={active}
      />
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(ellipse at center, transparent 0%, rgba(0,0,0,0.32) 92%)",
        }}
      />
    </div>
  );
};
