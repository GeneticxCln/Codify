import type { Goal } from "./types";

/** The convention the engine itself discovers when nothing is pinned. */
export const DEFAULT_DESIGN_MD = "DESIGN.md";

/**
 * The file a design deliverable was written to, read out of the plan that wrote
 * it.
 *
 * The step names its target, and the engine keys the same signal — the fixer and
 * the verifier both match on a `design.md` path — so the pin points at the file
 * that was actually written rather than at a convention. `DESIGN.md` is the
 * fallback, not a guess: it is the name the write step is planned for and the
 * name the engine discovers on its own.
 *
 * The first match wins. A plan that named two design files is a plan the pin
 * cannot resolve from the transcript, and the engine's own validation is what
 * says so — the pin is refused with the reason rather than quietly binding the
 * wrong file.
 */
export function deliverablePath(goal?: Goal): string {
  for (const step of goal?.steps ?? []) {
    const hit = (step.suggested_paths ?? []).find((p) =>
      p.toLowerCase().includes("design.md")
    );
    if (hit) return hit;
  }
  return DEFAULT_DESIGN_MD;
}

/**
 * Whether the deliverable has been written *and* reviewed — the point the pin
 * is allowed to be offered.
 *
 * The draft is published on `design_contract` during planning, long before any
 * step runs, so "there is a body to pin" and "there is a file the critic has
 * approved" are different moments. A step reaches `COMPLETED` only after the
 * critic approved it: a `request-changes` leaves it `IN_PROGRESS` and pauses the
 * goal. Gating on that is how "the critic reviews it before it is pinned" stops
 * being a claim about the pipeline and becomes one about the button.
 */
export function deliverableReviewed(goal?: Goal): boolean {
  return (goal?.steps ?? []).some(
    (step) =>
      step.status === "COMPLETED" &&
      (step.suggested_paths ?? []).some((p) => p.toLowerCase().includes("design.md"))
  );
}

/** What the card may offer, why not, and how to describe the body. */
export type PinReadiness = {
  /** Whether the pin action may be offered at all. */
  ready: boolean;
  /** Why it is not offered, in the card's own voice. Empty when ready. */
  reason: string;
  /** How the body is labelled beside it — the same facts, in words. */
  bodyLabel: string;
  /**
   * The workspace already obeys this file, so the card states that instead of
   * offering the pin again. False is the ordinary case and says nothing.
   */
  pinned: boolean;
};

/**
 * The same file, named two ways.
 *
 * A pin stores the path it was given and the plan is free to capitalise it, so
 * `design.md` and `DESIGN.md` are one file. A card that called that "not
 * pinned" would offer to pin a contract that is already in force.
 */
function sameFile(pinned: string | undefined, path: string): boolean {
  const one = (pinned ?? "").trim().toLowerCase();
  return one !== "" && one === path.trim().toLowerCase();
}

/**
 * Whether a design deliverable can be pinned, and what to say when it cannot.
 *
 * Three independent facts decide it, and they have to be told apart because the
 * user's next move differs:
 *
 * - **Has the critic approved it?** A step reaches `COMPLETED` only on an
 *   approval — `request-changes` leaves it `IN_PROGRESS` and pauses the goal —
 *   so this is the review gate rather than a guess about progress.
 * - **Did the run write anything?** A dry run reaches the disk not at all, so
 *   the draft was reviewed as a proposal and there is no file to bind. The
 *   engine refuses that pin (`design_contract_missing`), and the reason names
 *   applying the goal as the way forward rather than leaving a dead button.
 * - **Is it already pinned?** `pinnedPath` is the workspace's own answer, read
 *   from `GET /workspaces` rather than remembered from a click. A pin is
 *   workspace state that outlives the tab that set it — it may have been set
 *   from the workspace picker, or by an earlier goal, or before a reload — and
 *   a card that forgot it offered the pin again on a contract already in force.
 *
 * The pinned case is decided *last*, and that order is the claim: "this is the
 * contract" is a statement about the reviewed draft, so an unreviewed goal in a
 * workspace that happens to obey the same path still gets the review reason.
 *
 * The copy lives here, not in the component, for the same reason the readiness
 * does: a `disabled` button does not reliably surface a `title`, so the
 * explanation has to be rendered text, and rendered text is worth testing.
 *
 * None of this is the authority — the engine is. It decides what the UI offers;
 * a pin that slips through is still refused with the engine's own message.
 */
export function pinReadiness(
  goal: Goal | undefined,
  path: string,
  pinnedPath?: string
): PinReadiness {
  if (!deliverableReviewed(goal)) {
    return {
      ready: false,
      reason: "waiting on the review — a step must write it and the critic must approve it first",
      bodyLabel: "the draft, not yet reviewed",
      pinned: false,
    };
  }
  if (goal?.dry_run) {
    return {
      ready: false,
      reason: `this run proposed ${path} without writing it — apply the goal, then pin it`,
      bodyLabel: "the proposed draft — nothing written to disk yet",
      pinned: false,
    };
  }
  if (sameFile(pinnedPath, path)) {
    return {
      ready: false,
      reason: "",
      bodyLabel: "the deliverable, as written",
      pinned: true,
    };
  }
  return {
    ready: true,
    reason: "",
    bodyLabel: "the deliverable, as written",
    pinned: false,
  };
}
