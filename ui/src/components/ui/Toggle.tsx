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
 * The armed shape is `tone/-20` over `tone/-50` with the tone's own text, for
 * every tone. Same shape each time: the hue says which feature, the shape says
 * "you turned this on", so the two are readable independently.
 *
 * One token per row, all four slots. The `accent` row briefly mixed `info` for
 * its resting fill with `accent` for its hover, which is the kind of drift this
 * table exists to prevent: the two hues are the same blue in the default theme
 * and different colours in five others, so it looked finished and was not.
 */
const ARMED: Record<ToggleTone, string> = {
  accent: "bg-codify-accent/20 border-codify-accent/50 text-codify-accent-ink hover:bg-codify-accent/30",
  // "This records every model call" is genuinely a caution, so amber is right here.
  warning: "bg-codify-warning/20 border-codify-warning/50 text-codify-warning-ink hover:bg-codify-warning/30",
  design: "bg-codify-design/20 border-codify-design/50 text-codify-design-ink hover:bg-codify-design/30",
  knowledge:
    "bg-codify-knowledge/20 border-codify-knowledge/50 text-codify-knowledge-ink hover:bg-codify-knowledge/30",
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
      "focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-codify-accent " +
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
