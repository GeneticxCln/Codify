import React from "react";

/**
 * The button. A tone, a size, and nothing else.
 *
 * These primitives exist because the audit found 267 hardcoded hex class strings and
 * six hues doing one job, over a palette defined in `tailwind.config.js` and used zero
 * times. A component that writes its own className is free to invent a seventh blue,
 * and "one rule per state" only holds if there is exactly one place a state is drawn.
 *
 * Tones are the five status tokens from `DESIGN.md` §2 plus `subtle`, because most
 * controls in this app are not reporting a state — they are at rest, and at rest is
 * grey. There is deliberately no `accent`/`info` distinction here: "press this" and
 * "this is happening" are the same hue by design, and a caller that has to choose
 * between two names for one colour will eventually pick differently from its
 * neighbour.
 */
export type ButtonTone = "primary" | "subtle" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

/*
 * Focus is a visible ring, never `focus:outline-none` on its own. The audit found
 * controls that could be seen but not tabbed to, and this is the one place a focus
 * ring is specified — so a component that forgets is a component that cannot be
 * reached by keyboard, which is the defect `DESIGN.md` §5 rules out.
 */
const TONES: Record<ButtonTone, string> = {
  primary:
    "bg-codify-accent text-white hover:bg-blue-500 active:bg-blue-600 shadow",
  subtle:
    "bg-codify-raised text-codify-secondary border border-codify-border " +
    "hover:bg-codify-border hover:text-codify-primary",
  ghost:
    "bg-transparent text-codify-muted border border-transparent " +
    "hover:bg-codify-raised hover:text-codify-primary",
  danger: "bg-codify-danger text-white hover:bg-red-500 shadow",
};

const SIZES: Record<ButtonSize, string> = {
  // `sm` is the toolbar size, and it is the size this app's toolbars are already
  // built at — 10px text, 1px vertical padding. `md` is for a dialog's own action.
  sm: "px-2.5 py-1 text-xs gap-1.5",
  md: "px-3.5 py-2 text-sm gap-2",
};

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  tone?: ButtonTone;
  size?: ButtonSize;
}

export const Button: React.FC<ButtonProps> = ({
  tone = "subtle",
  size = "sm",
  className = "",
  type = "button",
  children,
  ...rest
}) => (
  <button
    type={type}
    className={
      "inline-flex items-center justify-center rounded-lg font-semibold " +
      // A label wraps to a second line when its container is squeezed, which puts
      // the text under the icon and turns a header into two stacked words. Found by
      // looking at the running app, not by the tests — the header did it the moment
      // these three buttons adopted this primitive.
      "whitespace-nowrap " +
      "transition-colors cursor-pointer select-none " +
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-codify-accent " +
      "focus-visible:ring-offset-1 focus-visible:ring-offset-codify-bg " +
      "disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-inherit " +
      SIZES[size] +
      " " +
      TONES[tone] +
      (className ? " " + className : "")
    }
    {...rest}
  >
    {children}
  </button>
);
