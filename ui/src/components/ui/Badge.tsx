import React from "react";

/**
 * A status pill: one of the five tones, at `2xs`.
 *
 * This is where the six-hues-for-one-status problem gets a single answer. Before this
 * existed, a status was drawn as a hand-typed `bg-codify-success/20 text-codify-success
 * border-codify-success/60` at every call site, and "success" was green in one place, emerald
 * in another and teal in a third. The tone names here are the only way to say it, so
 * a sixth shade cannot be introduced without editing this file — which is visible in
 * a diff.
 *
 * **The four saturated tones now name theme variables, not Tailwind hues.** They
 * were `blue/green/amber/red` literals, which meant a success pill in the OLED
 * CMatrix app was Tailwind green: the one saturated colour on screen, and it
 * belonged to no palette the user had chosen. Each tone now names the token
 * `THEME_TONES` fills per theme, and `ui/src/index.css` carries the class names,
 * so these strings stay literal — a computed `text-codify-${tone}` would not be
 * in the source for a scanner to find, and the reason this file can be read is
 * that the answer is on one line.
 */
export type BadgeTone = "neutral" | "info" | "success" | "warning" | "danger";

const TONES: Record<BadgeTone, string> = {
  // The `/40` resting pill is the same shape for every tone on purpose: the hue
  // says what, the shape says "this is a state". `neutral` is the resting surface
  // rather than a hue, because idle is "nothing to report", not a colour.
  neutral: "bg-codify-raised text-codify-muted border-codify-border",
  info: "bg-codify-info/40 text-codify-info border-codify-info",
  success: "bg-codify-success/40 text-codify-success border-codify-success",
  warning: "bg-codify-warning/40 text-codify-warning border-codify-warning",
  danger: "bg-codify-danger/40 text-codify-danger border-codify-danger",
};

/*
 * The text/icon colour for a tone, for the places a tone is shown without a pill — a
 * step's status icon, a link. Same token as the pill, so an icon and a badge
 * about the same state read as the same news. Exported rather than retyped because
 * the step icons used to hard-code `text-codify-success` and friends, which is how a
 * seventh shade of "success" appears. `tests/appearance.test.ts` asserts every
 * entry here names the same token as its pill above, because the two maps
 * agreeing is a thing that can quietly stop being true.
 */
const TONE_TEXT: Record<BadgeTone, string> = {
  neutral: "text-codify-muted",
  info: "text-codify-info",
  success: "text-codify-success",
  warning: "text-codify-warning",
  danger: "text-codify-danger",
};

export function toneText(tone: BadgeTone): string {
  return TONE_TEXT[tone];
}

export interface BadgeProps {
  tone?: BadgeTone;
  className?: string;
  /**
   * Optional, and not decoration. A status pill sits next to a truncated model id or
   * a commit subject often enough that the full explanation needs somewhere to live —
   * `DESIGN.md` §6 asks a component to carry a title for long content, and a caller
   * that had to hand-roll a `<span>` to get one was a caller without a primitive.
   */
  title?: string;
  children: React.ReactNode;
}

export const Badge: React.FC<BadgeProps> = ({
  tone = "neutral",
  className = "",
  title,
  children,
}) => (
  <span
    title={title}
    className={
      "inline-flex items-center rounded-full border font-mono text-2xs px-1.5 " +
      "py-0.5 font-medium leading-none shrink-0 " +
      TONES[tone] +
      (className ? " " + className : "")
    }
  >
    {children}
  </span>
);
