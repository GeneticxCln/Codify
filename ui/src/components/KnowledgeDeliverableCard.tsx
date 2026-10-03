import React from "react";
import { BookOpen, ShieldCheck } from "lucide-react";
import {
  deliverableHeading,
  deliverablePath,
  knowledgeReadiness,
  revises,
} from "../designDeliverable";
import type { Goal } from "../types";
import { DeliverableText } from "./DeliverableText";

/**
 * A knowledge deliverable: the workspace's own `CODIFY.md`, as the body the run
 * authored and the file it is replacing.
 *
 * The design card's counterpart, and deliberately not the same card. There is no
 * pin here — `CODIFY.md` binds by being written, and every later run's librarian
 * reads it as a prior — so there is no action to offer, and one sentence has to
 * do the work a button does: what makes this file real, and when.
 *
 * The body is the point of the card. It is a document the next run will treat as
 * fact about the repository, and until this existed it reached the transcript
 * only as a `+`-prefixed diff, which is a poor thing to read a file from. It is
 * open by default for that reason, and height-capped so a long file cannot flood
 * the timeline. The file it replaces is collapsed: the new document is what the
 * user asked for, and the old one is the context for judging it.
 *
 * Its own file rather than a section of `ChatTimeline`, for the same reason the
 * logic lives in `designDeliverable.ts` and not here: a card that can be
 * rendered on its own is a card that can be *tested* on its own —
 * `ui/tests/knowledgeCard.test.ts` renders this markup through
 * `react-dom/server` rather than asserting on a function's return value.
 */
export const KnowledgeDeliverableCard: React.FC<{
  goal?: Goal;
  payload: Record<string, any>;
  /** A link in the body was clicked: open it in a browser tab. */
  onOpenLink?: (url: string) => void;
}> = ({ goal, payload, onOpenLink }) => {
  const body: string =
    typeof payload.design_md === "string" ? payload.design_md : "";
  // The event's own `mode`, not the goal's: a transcript message can carry no
  // goal, and the goal is only consulted for the plan and the review state. The
  // engine's statement of what this run is lives on the event, and it is the
  // one that has to win — otherwise a knowledge card with no goal attached
  // names `DESIGN.md`, the one file this mode can never write.
  const path = deliverablePath(goal, payload.mode);
  const readiness = knowledgeReadiness(goal, path);
  const previous = revises(payload);
  return (
    <div className="p-2.5 rounded-lg bg-codify-bg border border-codify-border flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        {/* Cyan, because the composer's Knowledge toggle is cyan and the two
            are the same choice seen from two places. */}
        <BookOpen className="w-3.5 h-3.5 text-codify-knowledge" />
        <span className="font-semibold text-xs uppercase tracking-wider text-codify-muted">
          {deliverableHeading(payload.mode)}
        </span>
        {payload.artifact && (
          <span className="text-2xs font-mono text-codify-muted">
            {payload.artifact}
          </span>
        )}
      </div>
      {payload.direction && (
        <p className="text-xs text-codify-secondary leading-relaxed">
          {payload.direction}
        </p>
      )}
      {body ? (
        <>
          <div className="flex items-start gap-1.5 text-2xs text-codify-muted">
            <ShieldCheck className="w-3 h-3 shrink-0 mt-0.5 text-codify-knowledge" />
            <span>{readiness.note}</span>
          </div>
          <details className="text-xs text-codify-muted" open>
            <summary className="cursor-pointer text-codify-muted">
              {path} ({body.length} chars) — {readiness.bodyLabel}
            </summary>
            <DeliverableText body={body} boxClassName="max-h-72" onOpenLink={onOpenLink} />
          </details>
          {/* What this run is rewriting, in the drafter's own words. The stale
              list is the engine telling the user which of the old claims were
              already dead when the run started — the same warning the drafter
              was given, and the reason a plausible-looking prior is not a safe
              one. */}
          {previous && (
            <details className="text-xs text-codify-muted">
              <summary className="cursor-pointer text-codify-muted">
                replacing {previous.path} ({previous.chars} chars)
                {previous.stale_paths.length > 0 &&
                  ` — ${previous.stale_paths.length} path(s) it named are already gone`}
              </summary>
              <div className="flex flex-col gap-1.5 mt-1.5">
                {previous.stale_paths.length > 0 && (
                  <div className="text-2xs text-codify-warning">
                    the engine told the drafter to disregard these:{" "}
                    <span className="font-mono">{previous.stale_paths.join(", ")}</span>
                  </div>
                )}
                {previous.truncated && (
                  <div className="text-2xs text-codify-muted">
                    shown from the top — the file is longer than the {previous.chars}{" "}
                    chars the drafter read
                  </div>
                )}
                <pre className="p-2 rounded-sm bg-codify-surface border border-codify-raised text-2xs text-codify-muted whitespace-pre-wrap max-h-48 overflow-auto">
                  {previous.text}
                </pre>
              </div>
            </details>
          )}
        </>
      ) : (
        /* The engine refuses to publish a knowledge contract with no body, so
           an empty one means the drafter failed or is still answering — there
           is nothing to show, and blaming the write step for it would be
           naming the wrong stage. */
        <span className="text-2xs text-codify-muted">
          no body authored — the drafter has not produced one to write
        </span>
      )}
    </div>
  );
};
