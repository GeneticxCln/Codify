import React from "react";

/**
 * Codify's mark: the terminal prompt — chevron asks, caret answers — in the
 * success green on the app background, drawn by `scripts/make_logo.py` into
 * `ui/public/logo.gif` and its static companion `logo-static.gif`.
 *
 * The animated asset is the one sanctioned exception to DESIGN.md §7's
 * "nothing loops": it is a *pre-rendered asset*, not a CSS loop the UI drives,
 * and it is the brand speaking rather than a state pretending to be alive.
 * A GIF cannot read `prefers-reduced-motion`, so this component does it on the
 * GIF's behalf — users who opt out of motion get `logo-static.gif`, the loop's
 * resting frame, never a slower animation. §7's own rule, applied to the one
 * thing that cannot obey it directly.
 *
 * Reduced motion is read per render rather than tracked as state: the setting
 * changing mid-session is rarer than any re-render, and a subscription here
 * would be machinery for an event that changes nothing but this one `src`.
 */
export interface LogoProps {
  className?: string;
  /** Edge in px. The asset is 256px square and scales down cleanly. */
  size?: number;
  /**
   * Play the loop. Defaults to the user's motion preference: on unless they
   * asked for reduced motion. An explicit value overrides both — that is how
   * a test pins either asset without owning a media-query stub.
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
  const src = (animated ?? !prefersReducedMotion())
    ? "/logo.gif"
    : "/logo-static.gif";
  return (
    <img
      src={src}
      width={size}
      height={size}
      // Decorative by position: the wordmark "CODIFY" sits beside it in the
      // header, so announcing the picture too would say the name twice.
      alt=""
      draggable={false}
      className={`rounded-lg shadow flex-shrink-0 ${className}`}
    />
  );
};
