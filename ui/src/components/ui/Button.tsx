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
 * Focus is a visible ring, never `focus:outline-hidden` on its own. The audit found
 * controls that could be seen but not tabbed to, and this is the one place a focus
 * ring is specified — so a component that forgets is a component that cannot be
 * reached by keyboard, which is the defect `DESIGN.md` §5 rules out.
 */
const TONES: Record<ButtonTone, string> = {
  // `brightness`, not a second colour. These two tones used to be
  // `hover:bg-codify-accent` and `hover:bg-codify-danger/20` — a themed fill that snapped back
  // to Tailwind the moment you touched it, which is the "click the button and it
  // stops matching the theme" report exactly. A filter derives from whatever the
  // theme published, so hover and rest can never be two different palettes, and
  // a tenth theme needs no edit here.
  //
  // `text-codify-bg` rather than white: the app's own background is the one token
  // guaranteed to contrast with a saturated fill in every theme. It is also the
  // *better* answer in the default theme — near-black on `#418cf8` is 5.7:1
  // where white is 3.4:1, and white on ASCII rain's near-white accent would have
  // been a white button with no writing on it.
  primary:
    "bg-codify-accent text-codify-bg hover:brightness-110 active:brightness-95 shadow-sm",
  subtle:
    "bg-codify-raised text-codify-secondary border border-codify-border " +
    "hover:bg-codify-border hover:text-codify-primary",
  ghost:
    "bg-transparent text-codify-muted border border-transparent " +
    "hover:bg-codify-raised hover:text-codify-primary",
  danger: "bg-codify-danger text-codify-bg hover:brightness-110 shadow-sm",
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
  /**
   * Where the label sits in the button.
   *
   * `center` is a button that sizes to its own label. `start` is a control that
   * fills a column — a menu item, a toolbar row — where centring the label would
   * put every item's text at a different x and leave the right half of the row
   * empty.
   *
   * A prop rather than a `justify-*` class the caller passes: two of those in one
   * class string both survive into the stylesheet and the later *stylesheet*
   * rule wins, not the later class — so a caller's `justify-start` silently lost
   * to `justify-center` here and every menu item's label sat in the middle of the
   * row. Emitting exactly one of the two is the fix.
   */
  align?: "center" | "start";
  /**
   * How heavy the label is.
   *
   * `semibold` is the app's button: a control whose label is the whole point of
   * the row. `normal` is a control in a list of equals — a menu item, where every
   * row is bold and so nothing is.
   *
   * For the same reason as `align`: `font-normal` passed as a class fought the
   * `font-semibold` in the base and lost to the stylesheet order, so a menu of
   * bold items was the result. One of the two is emitted, never both.
   */
  weight?: "normal" | "semibold";
}

export const Button: React.FC<ButtonProps> = ({
  tone = "subtle",
  size = "sm",
  align = "center",
  weight = "semibold",
  className = "",
  type = "button",
  children,
  ...rest
}) => (
  <button
    type={type}
    className={
      "inline-flex items-center " +
      (align === "start" ? "justify-start" : "justify-center") +
      " rounded-lg " +
      (weight === "normal" ? "font-normal" : "font-semibold") +
      " " +
      // A label wraps to a second line when its container is squeezed, which puts
      // the text under the icon and turns a header into two stacked words. Found by
      // looking at the running app, not by the tests — the header did it the moment
      // these three buttons adopted this primitive.
      "whitespace-nowrap " +
      "transition-colors cursor-pointer select-none " +
      "focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-codify-accent " +
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
