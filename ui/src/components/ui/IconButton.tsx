import React from "react";
import { Button, type ButtonTone } from "./Button";

/**
 * A square, icon-only control.
 *
 * It requires `label` — not `aria-label`, and not optional. An icon is the only thing
 * carrying the meaning here, so a control without a label is invisible to a screen
 * reader and to anyone who cannot see the glyph, and `DESIGN.md` §5 says every
 * icon-only control carries one. Making the prop *required* means forgetting is a
 * type error rather than a silent omission, which is the only reliable way to hold a
 * rule like that across a codebase this size.
 */
export interface IconButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "aria-label"> {
  /** Required, and the reason is above: the icon does not describe itself. */
  label: string;
  tone?: ButtonTone;
  size?: "sm" | "md";
}

export const IconButton: React.FC<IconButtonProps> = ({
  label,
  tone = "ghost",
  size = "sm",
  className = "",
  children,
  ...rest
}) => (
  <Button
    tone={tone}
    aria-label={label}
    title={rest.title ?? label}
    className={
      // `!p-0`, with the important modifier, and it has to be: `Button` already carries `px-2.5`
      // for its size, and Tailwind emits `px-*` after `p-*` in the stylesheet, so a plain `p-0`
      // lost. A 28px button was left a 8px content box, the icon inside it flex-shrank to match,
      // and every small icon button in the app drew a sliver (found by measuring the running
      // app: a `w-4` icon was 8px wide). The svg is also kept from shrinking, so a squeezed
      // button crops its icon rather than squashing it.
      "!p-0 shrink-0 [&>svg]:shrink-0 " +
      (size === "sm" ? "w-7 h-7 justify-center" : "w-9 h-9 justify-center") +
      (className ? " " + className : "")
    }
    {...rest}
  >
    {children}
  </Button>
);
