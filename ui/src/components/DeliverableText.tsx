import React, { useState } from "react";
import { Markdown } from "./Markdown";

/**
 * A deliverable's body: the Markdown file a run wrote, as a document by default and as the exact
 * bytes one click away.
 *
 * `CODIFY.md` and `DESIGN.md` are Markdown, and read as a preformatted block they are a wall of `##`
 * and `**`. Rendered is therefore the default. But these are also files the *next run will treat as
 * fact* (and, for a design file, one the user may pin as a contract), so the user has to be able to
 * see precisely what will be written, with nothing interpreted. That is Source: the same string in a
 * `<pre>`, untouched. Both views are of the same `body`, so neither can show a different document.
 *
 * `onOpenLink` is the transcript's: a link in a deliverable opens the app's own browser tab like a
 * link in an answer (`markdownLinks.ts`).
 */
export const DeliverableText: React.FC<{
  body: string;
  /** Height cap and spacing for the box, as Tailwind classes: the two cards cap differently. */
  boxClassName: string;
  onOpenLink?: (url: string) => void;
}> = ({ body, boxClassName, onOpenLink }) => {
  const [view, setView] = useState<"rendered" | "source">("rendered");
  const tab = (id: "rendered" | "source", label: string): React.ReactElement => (
    <button
      type="button"
      aria-pressed={view === id}
      onClick={() => setView(id)}
      className={
        "rounded border px-1.5 py-0.5 text-2xs transition-colors " +
        (view === id
          ? "border-codify-border-strong bg-codify-raised text-codify-primary"
          : "border-transparent text-codify-muted hover:text-codify-secondary")
      }
    >
      {label}
    </button>
  );
  return (
    <div className="mt-1.5 flex flex-col gap-1.5">
      <div role="group" aria-label="View the file as" className="flex items-center gap-1">
        {tab("rendered", "Rendered")}
        {tab("source", "Source")}
      </div>
      {view === "rendered" ? (
        <div
          className={
            "rounded border border-codify-raised bg-codify-surface p-2 text-xs text-codify-secondary overflow-auto " +
            boxClassName
          }
        >
          <Markdown text={body} onOpenLink={onOpenLink} />
        </div>
      ) : (
        <pre
          className={
            "whitespace-pre-wrap rounded border border-codify-raised bg-codify-surface p-2 text-2xs text-codify-secondary overflow-auto " +
            boxClassName
          }
        >
          {body}
        </pre>
      )}
    </div>
  );
};
