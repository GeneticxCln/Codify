import React from "react";
import { Check } from "lucide-react";

/**
 * A checkbox this app paints.
 *
 * The native one was the last control in Settings that came out of the desktop's
 * palette instead of the theme's: checked, `accent-color` tints the fill, but the
 * *unchecked* box — the state a person actually meets first — is a light square
 * with a system border, and on every one of the nineteen dark themes it reads as
 * a hole punched through the card. WebKitGTK draws it the same way, from the
 * GTK theme's light controls, whatever `accent-color` says.
 *
 * A `role="checkbox"` button with the theme's own tokens has no state the
 * desktop can paint. It keeps what the input gave the form: a toggle on click,
 * an accessible checked/unchecked state, and the whole sentence as its name
 * (a native input beside a label was one hit target; this is the line itself).
 */
export interface CheckboxProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  children: React.ReactNode;
  disabled?: boolean;
  id?: string;
  className?: string;
}

export const Checkbox: React.FC<CheckboxProps> = ({
  checked,
  onChange,
  children,
  disabled = false,
  id,
  className = "",
}) => (
  <button
    type="button"
    role="checkbox"
    id={id}
    aria-checked={checked}
    disabled={disabled}
    onClick={() => onChange(!checked)}
    className={
      "group inline-flex items-start gap-2 text-left text-xs font-medium text-codify-secondary " +
      "cursor-pointer select-none disabled:opacity-40 disabled:cursor-not-allowed " +
      "focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-codify-accent " +
      "focus-visible:ring-offset-1 focus-visible:ring-offset-codify-bg" +
      (className ? " " + className : "")
    }
  >
    <span
      aria-hidden="true"
      className={
        "mt-px flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-[3px] border " +
        "transition-colors " +
        (checked
          ? "border-codify-accent bg-codify-accent text-codify-bg"
          : "border-codify-border-strong bg-codify-bg text-transparent group-hover:border-codify-accent")
      }
    >
      <Check className="h-3 w-3" strokeWidth={3} />
    </span>
    <span>{children}</span>
  </button>
);
