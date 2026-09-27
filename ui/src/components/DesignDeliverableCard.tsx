import React from "react";
import { Check, Palette, Pin } from "lucide-react";
import {
  deliverableHeading,
  deliverablePath,
  pinReadiness,
  type PinReadiness,
} from "../designDeliverable";
import type { Goal } from "../types";

/** The outcome of the last pin attempt, per goal card. */
export interface PinOutcome {
  goalId: string;
  ok: boolean;
  message: string;
}

/**
 * The one action that gives a design deliverable authority.
 *
 * The two reasons it can be unavailable are rendered as text rather than only a
 * `title`: a disabled button does not reliably surface a tooltip, and "why can't
 * I pin this" is the question the card exists to answer.
 *
 * A workspace that already obeys this file gets a state instead of an action.
 * The pin is workspace state — it can be set from the workspace picker, or by an
 * earlier goal, or before this tab was reloaded — so the card reads it from the
 * workspace rather than from whether this transcript happened to watch it
 * happen. Offering the pin again there would be asking the user to do something
 * they have already done.
 *
 * Only this part needs a goal, because only the click needs an id to send.
 */
const DesignDeliverablePin: React.FC<{
  goal: Goal;
  path: string;
  readiness: PinReadiness;
  outcome: PinOutcome | null;
  onPin: (goalId: string, workspaceId: string, path: string) => Promise<void>;
}> = ({ goal, path, readiness, outcome, onPin }) => (
  <div className="flex items-center gap-2 flex-wrap">
    {readiness.pinned ? (
      <span className="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-pink-500/10 border border-pink-500/30 text-pink-200/90 text-xs">
        <Check className="w-3 h-3" />
        {path} is this workspace’s brand contract
      </span>
    ) : (
      <button
        type="button"
        disabled={!readiness.ready}
        onClick={() => void onPin(goal.id, goal.workspace_id, path)}
        className="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-pink-600/20 border border-pink-500/50 text-pink-300 hover:bg-pink-600/30 text-xs cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-pink-600/20"
      >
        <Pin className="w-3 h-3" />
        Pin as brand contract
      </button>
    )}
    {!readiness.ready && readiness.reason && (
      <span className="text-2xs text-gray-500">{readiness.reason}</span>
    )}
    {readiness.ready && outcome !== null && outcome.goalId === goal.id && (
      <span
        className={`text-2xs ${outcome.ok ? "text-green-400" : "text-red-400"}`}
      >
        {outcome.message}
      </span>
    )}
  </div>
);

/**
 * The deliverable's own body: the file it went to, what state that file is in,
 * and the text to read.
 *
 * Deliberately independent of the pin, and therefore of the goal. The draft is
 * published on `design_contract` during planning, and a transcript message can
 * carry no goal at all — which used to cost this card its entire body, because
 * the body lived inside the component that owns the button. The button needs an
 * id to send; the text does not, and a document the user cannot read is not a
 * deliverable.
 *
 * Collapsed, unlike the knowledge card: a full brand contract with a token list
 * sits directly under a pin that is trying to get the user's attention, and
 * expanded by default it would swallow the transcript it is part of.
 */
const DeliverableBody: React.FC<{
  path: string;
  body: string;
  label: string;
}> = ({ path, body, label }) => (
  <details className="text-xs text-gray-400">
    <summary className="cursor-pointer text-gray-500">
      {path} ({body.length} chars) — {label}
    </summary>
    <pre className="mt-1.5 p-2 rounded bg-codify-surface border border-codify-raised text-2xs text-gray-300 whitespace-pre-wrap max-h-64 overflow-auto">
      {body}
    </pre>
  </details>
);

/**
 * A design deliverable: the locked direction, and the one action that gives it
 * authority.
 *
 * The engine drafts it and a step writes it; only the user can make it binding,
 * and this is where the file they were just shown becomes the contract. A
 * normal goal's contract is an *input* rather than a deliverable, which is why
 * the same event renders this card with the pin absent.
 *
 * Its own file rather than a section of `ChatTimeline`, for the reason the
 * knowledge card has one: a card that can be rendered on its own is a card that
 * can be tested on its own. `ui/tests/designCard.test.ts` renders this through
 * `react-dom/server`, because "the reason the pin is unavailable" is a fact
 * about the JSX and not about the function that computes it.
 */
export const DesignDeliverableCard: React.FC<{
  payload: Record<string, any>;
  goal?: Goal;
  /** What the last pin click on this goal reported, if it reported anything. */
  pinOutcome: PinOutcome | null;
  /** The file this workspace already obeys, keyed by the goal's workspace. */
  pinnedPath?: string;
  onPin: (goalId: string, workspaceId: string, path: string) => Promise<void>;
}> = ({ payload, goal, pinOutcome, pinnedPath, onPin }) => {
  // A design-mode goal's contract is the artifact itself, not the direction a
  // step is written against: the difference between input and deliverable, so it
  // is named.
  const isDeliverable = payload.mode === "design" && !!payload.design_md;
  // The event's mode, not the goal's: a card rendered from a `design_contract`
  // with no goal attached would otherwise name the other mode's file.
  const path = deliverablePath(goal, payload.mode);
  const readiness = pinReadiness(goal, path, pinnedPath);
  return (
    <div className="p-2.5 rounded-lg bg-codify-bg border border-codify-border flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        <Palette className="w-3.5 h-3.5 text-pink-400" />
        <span className="font-semibold text-xs uppercase tracking-wider text-gray-400">
          {deliverableHeading(payload.mode)}
        </span>
        <span className="text-2xs font-mono text-gray-500">
          {payload.artifact}
        </span>
      </div>
      {payload.direction && (
        <p className="text-xs text-gray-300 leading-relaxed">
          {payload.direction}
        </p>
      )}
      {payload.design_system?.name && (
        <div className="text-xs text-gray-400">
          design system:{" "}
          <span className="font-mono text-gray-300">
            {payload.design_system.name}
          </span>
          {/* Three states, said differently on purpose: a pin is the user's
          instruction, a discovery is a convention the engine noticed, and a
          proposal exists because there was nothing to obey. */}
          {payload.design_system.origin === "pinned" ? (
            <span className="text-pink-300/90">
              {" "}
              — pinned at {payload.design_system.source}
            </span>
          ) : payload.design_system.origin === "discovered" ? (
            <span className="text-gray-500">
              {" "}
              — found at {payload.design_system.source}
            </span>
          ) : payload.design_system.source ? (
            <span className="text-gray-500">
              {" "}
              — from {payload.design_system.source}
            </span>
          ) : (
            <span className="text-gray-500">
              {" "}
              — proposed, no existing contract
            </span>
          )}
        </div>
      )}
      {(payload.tokens?.colors?.length ?? 0) > 0 && (
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
          {payload.tokens.colors.map(
            (c: { name: string; value: string }, i: number) => (
              <span key={i} className="text-xs flex items-center gap-1">
                <span
                  className="w-2.5 h-2.5 rounded-sm border border-codify-border"
                  style={{ background: c.value }}
                />
                <span className="font-mono text-gray-400">{c.name}</span>
                <span className="font-mono text-gray-500">{c.value}</span>
              </span>
            )
          )}
        </div>
      )}
      {(payload.tokens?.typography?.length ?? 0) > 0 && (
        <div className="text-xs text-gray-400">
          type:{" "}
          {payload.tokens.typography
            .map(
              (t: { name: string; value: string }) => `${t.name} ${t.value}`
            )
            .join(" · ")}
        </div>
      )}
      {(payload.components?.length ?? 0) > 0 && (
        <div className="flex flex-col gap-0.5">
          {payload.components.map(
            (c: { name: string; purpose?: string }, i: number) => (
              <div key={i} className="text-xs flex items-start gap-1.5">
                <span className="font-mono text-pink-300/90">{c.name}</span>
                {c.purpose && <span className="text-gray-400">— {c.purpose}</span>}
              </div>
            )
          )}
        </div>
      )}
      {(payload.acceptance?.length ?? 0) > 0 && (
        <div className="text-xs text-gray-400">
          acceptance: {payload.acceptance.join("; ")}
        </div>
      )}
      {(payload.constraints?.length ?? 0) > 0 && (
        <div className="text-xs text-amber-400/90">
          constraints: {payload.constraints.join("; ")}
        </div>
      )}
      {/* The deliverable's own body, and the one action that gives it authority. */}
      {isDeliverable && (
        <div className="flex flex-col gap-1.5 mt-0.5 pt-1.5 border-t border-codify-raised">
          {goal && (
            <DesignDeliverablePin
              goal={goal}
              path={path}
              readiness={readiness}
              outcome={pinOutcome}
              onPin={onPin}
            />
          )}
          <DeliverableBody
            path={path}
            body={payload.design_md}
            label={readiness.bodyLabel}
          />
        </div>
      )}
    </div>
  );
};
