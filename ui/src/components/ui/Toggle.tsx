import React from "react";

/**
 * A toolbar toggle: armed, or at rest.
 *
 * This is the control the audit found broken. Seven of them sit in one toolbar, they
 * were each hand-typed, and **Record and Knowledge Deliverable carried byte-identical
 * classes** — two unrelated features announcing themselves the same way. It survived
 * because there was no way to say "an armed toggle" without copying a class string.
 *
 * So the rule lives here. `DESIGN.md` §2's distinction is the whole point:
 *
 * - **armed** — the toggle's own tone, tinted. A state, not an identity.
 * - **at rest** — `raised` fill, `border` border, `muted` text. The default.
 *
 * A caller picks a tone because the feature *means* that colour, and cannot invent a
 * new one: adding a sixth armed hue means editing this file, which shows up in a diff.
 * That is the mechanism that stops the collision recurring, rather than a convention
 * asking nicely.
 */
export type ToggleTone = "accent" | "warning" | "design" | "knowledge";

/*
 * The armed shape is `tone/-20` over `tone/-50` with `-300` text, for every tone.
 * Same shape each time: the hue says which feature, the shape says "you turned this
 * on", so the two are readable independently.
 */
const ARMED: Record<ToggleTone, string> = {
  accent: "bg-blue-600/20 border-blue-500/50 text-blue-300 hover:bg-blue-600/30",
  // "This records every model call" is genuinely a caution, so amber is right here.
  warning: "bg-amber-600/20 border-amber-500/50 text-amber-300 hover:bg-amber-600/30",
  design: "bg-pink-600/20 border-pink-500/50 text-pink-300 hover:bg-pink-600/30",
  knowledge:
    "bg-cyan-600/20 border-cyan-500/50 text-cyan-300 hover:bg-cyan-600/30",
};

const REST =
  "bg-codify-raised border-codify-border text-codify-muted hover:bg-codify-border";

export interface ToggleProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  armed: boolean;
  /** The feature's own hue. Only consulted while armed. */
  tone?: ToggleTone;
  children: React.ReactNode;
}

export const Toggle: React.FC<ToggleProps> = ({
  armed,
  tone = "accent",
  className = "",
  children,
  ...rest
}) => (
  <button
    type="button"
    aria-pressed={armed}
    className={
      "inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs " +
      "font-medium whitespace-nowrap transition-colors cursor-pointer select-none " +
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-codify-accent " +
      "focus-visible:ring-offset-1 focus-visible:ring-offset-codify-bg " +
      "disabled:opacity-40 disabled:cursor-not-allowed " +
      (armed ? ARMED[tone] : REST) +
      (className ? " " + className : "")
    }
    {...rest}
  >
    {children}
  </button>
);
