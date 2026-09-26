import React from "react";

/**
 * A bordered surface with a heading row.
 *
 * The audit found drawers, cards and the command bar each re-deriving "a surface" as
 * `bg-codify-surface border border-codify-border rounded-2xl` and then disagreeing about the
 * radius. `DESIGN.md` §4 says nesting a `lg` inside a `lg` looks wrong, so a panel
 * inside a panel steps down; that rule only survives if the radius lives here.
 */
export interface PanelProps {
  /** Heading content. Omit for a panel with no title row. */
  title?: React.ReactNode;
  /** Rendered at the right of the heading row — usually a close or refresh control. */
  actions?: React.ReactNode;
  /** Body padding. `none` for a panel whose body scrolls edge to edge. */
  padding?: "none" | "sm" | "md";
  className?: string;
  children: React.ReactNode;
}

const PADDING = { none: "", sm: "p-2", md: "p-3" } as const;

export const Panel: React.FC<PanelProps> = ({
  title,
  actions,
  padding = "md",
  className = "",
  children,
}) => (
  <section
    className={
      "flex flex-col bg-codify-surface border border-codify-border rounded-lg " +
      "overflow-hidden " +
      (className ? className + " " : "")
    }
  >
    {(title || actions) && (
      <header className="flex items-center justify-between gap-2 px-4 py-3 border-b border-codify-border shrink-0">
        <div className="flex items-center gap-2 text-sm font-semibold text-codify-primary">
          {title}
        </div>
        {actions && <div className="flex items-center gap-1">{actions}</div>}
      </header>
    )}
    <div className={"flex-1 overflow-y-auto min-h-0 " + PADDING[padding]}>
      {children}
    </div>
  </section>
);
