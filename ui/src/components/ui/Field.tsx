import React from "react";

/**
 * A labelled control, with room for a hint and an error.
 *
 * The settings panels are where this earns its keep: the same label/hint/error
 * scaffolding was re-derived at every input, which is how a hint ends up rendered at
 * one size under one field and a different size under the next, and how an error
 * appears beside one control and silently not beside another.
 *
 * `error` wins over `hint` rather than sitting under it. Two messages where one is a
 * failure and one is advice is a case the reader has to resolve, and the wrong
 * resolution is to read the advice and miss the failure.
 */
export interface FieldProps {
  label: React.ReactNode;
  /** Optional, shown under the control when there is no error. */
  hint?: React.ReactNode;
  /** An error replaces the hint, and is announced rather than only coloured. */
  error?: string | null;
  /** The control itself. */
  children: React.ReactNode;
  /** Rendered at the right of the label row — a status, a count, a link. */
  aside?: React.ReactNode;
  className?: string;
}

export const Field: React.FC<FieldProps> = ({
  label,
  hint,
  error,
  children,
  aside,
  className = "",
}) => (
  <label
    className={"block " + (className ? className : "")}
  >
    <span className="flex items-center justify-between gap-2 mb-1">
      <span className="text-xs font-medium text-codify-secondary">{label}</span>
      {aside}
    </span>
    {children}
    {/*
      `role="alert"` so the error is announced when it appears, rather than being a
      red line the reader has to notice. A form that fails silently is the failure
      mode `DESIGN.md` §6 rules out.
    */}
    {error ? (
      <span role="alert" className="block mt-1 text-2xs text-codify-danger">
        {error}
      </span>
    ) : hint ? (
      <span className="block mt-1 text-2xs text-codify-muted leading-relaxed">
        {hint}
      </span>
    ) : null}
  </label>
);
