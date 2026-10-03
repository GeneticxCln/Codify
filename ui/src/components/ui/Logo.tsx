import React from "react";

/**
 * Codify's mark: the terminal prompt — chevron asks, caret answers.
 *
 * **Why this is SVG now and was a GIF.** It used to be `ui/public/logo.gif`, a
 * pre-rendered animated asset, and a GIF cannot read a CSS variable: the green
 * was in the pixels. That made the badge the one thing on screen that could not
 * match its theme — in OLED it was a slightly wrong green, in Vector Wireframe
 * a saturated forest, in Solarized Flare a brown. A user who chose a palette
 * still got the default brand green in the corner, and nothing in the code said
 * so, because there was no code to say it with.
 *
 * Drawn inline, the mark takes its colours from the theme's own variables and
 * is therefore correct in all nineteen themes by construction rather than by
 * nineteen regenerations. The animation moves from the GIF's frame timings to a
 * CSS keyframe, which brings back something the GIF could not do at all:
 * `prefers-reduced-motion` applies to it directly, so a user who asked for less
 * motion gets the mark's resting state rather than a slower blink.
 *
 * **The GIF is still there, and still needed.** `ui/index.html` points the
 * favicon at it, a favicon cannot read the page's variables, and a browser tab
 * has no theme. `ui/tests/logo.test.ts` still walks those bytes and still holds
 * the brand palette; only the in-app badge moved.
 *
 * **`--codify-success` is the colour, deliberately.** It is the one tone every
 * theme publishes and the one the mark has always been: the caret is the app
 * answering. `ui/tests/contrast.test.ts` holds it to 4.5:1 on every surface it is
 * painted on, so the badge cannot become the unreadable thing the GIF was in two
 * themes.
 */
export interface LogoProps {
  className?: string;
  /** Edge in px. The mark is a 24-unit grid and scales cleanly. */
  size?: number;
  /**
   * Play the caret blink. Defaults to the user's motion preference: on unless
   * they asked for reduced motion. An explicit value overrides both — that is how
   * a test pins either state without owning a media-query stub.
   */
  animated?: boolean;
}

const prefersReducedMotion = (): boolean =>
  typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export const Logo: React.FC<LogoProps> = ({
  className = "",
  size = 24,
  animated,
}) => {
  const live = animated ?? !prefersReducedMotion();
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      role="presentation"
      aria-hidden="true"
      focusable="false"
      data-animated={live ? "true" : "false"}
      className={`codify-logo shrink-0 ${className}`}
    >
      {/* The tile is the app's own background, so the badge reads as a cut-out
          of the surface rather than a sticker on it. `fill` not `background`:
          a background would cover the rounded corners the clip gives. */}
      <rect
        x="0"
        y="0"
        width="24"
        height="24"
        rx="6"
        className="codify-logo-tile"
      />
      {/* Chevron: the ask. */}
      <path
        d="M7 9.5 L10.5 12 L7 14.5"
        fill="none"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="codify-logo-mark"
      />
      {/* Caret: the answer, and the only part that moves. */}
      <path
        d="M12.5 15 H17"
        fill="none"
        strokeWidth="2"
        strokeLinecap="round"
        className={`codify-logo-mark codify-logo-caret${live ? " codify-logo-caret-live" : ""}`}
      />
    </svg>
  );
};
