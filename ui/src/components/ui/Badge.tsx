import React from "react";

/**
 * A status pill: one of the five tones, at `2xs`.
 *
 * This is where the six-hues-for-one-status problem gets a single answer. Before this
 * existed, a status was drawn as a hand-typed `bg-green-950/40 text-green-400
 * border-green-800` at every call site, and "success" was green in one place, emerald
 * in another and teal in a third. The tone names here are the only way to say it, so
 * a sixth shade cannot be introduced without editing this file — which is visible in
 * a diff.
 */
export type BadgeTone = "neutral" | "info" | "success" | "warning" | "danger";

const TONES: Record<BadgeTone, string> = {
  // The `/-950 / -400 / -800` shape is the resting pill, and it is the same shape for
  // every tone on purpose: the hue says what, the shape says "this is a state".
  neutral: "bg-codify-raised text-codify-muted border-codify-border",
  info: "bg-blue-950/40 text-blue-400 border-blue-800",
  success: "bg-green-950/40 text-green-400 border-green-800",
  warning: "bg-amber-950/40 text-amber-400 border-amber-800",
  danger: "bg-red-950/40 text-red-400 border-red-800",
};

/*
 * The text/icon colour for a tone, for the places a tone is shown without a pill — a
 * step's status icon, a link. Same hue family as the pill, so an icon and a badge
 * about the same state read as the same news. Exported rather than retyped because
 * the step icons used to hard-code `text-green-400` and friends, which is how a
 * seventh shade of "success" appears.
 */
const TONE_TEXT: Record<BadgeTone, string> = {
  neutral: "text-codify-muted",
  info: "text-blue-400",
  success: "text-green-400",
  warning: "text-amber-400",
  danger: "text-red-400",
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
