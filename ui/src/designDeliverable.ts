import type { Goal, PlanStep } from "./types";

/** The convention the engine itself discovers when nothing is pinned. */
export const DEFAULT_DESIGN_MD = "DESIGN.md";

/**
 * The knowledge file's own convention, which the engine resolves from the same
 * table (`DELIVERABLE_FILES` in `engine/executor.py`).
 */
export const DEFAULT_KNOWLEDGE_MD = "CODIFY.md";

/**
 * Which file each mode's deliverable is, lowercased for matching.
 *
 * This is a mirror of the engine's `DELIVERABLE_FILES`, and the mirror is the
 * point: the engine decides which file a step is *allowed* to be handed a draft
 * for, so a UI that resolved `DESIGN.md` for a knowledge goal would name a
 * document the run will never write. A mode absent from the table has no
 * deliverable at all, which is why a normal goal falls back to the design
 * convention rather than to `CODIFY.md` — nothing reads that answer, since
 * `deliverableReviewed` is false for a normal goal either way.
 */
const DELIVERABLE_NAMES: Record<string, string> = {
  design: "design.md",
  knowledge: "codify.md",
};

/** The file this goal's mode delivers, as the fallback for an unresolvable plan. */
function deliverableName(goal?: Goal, mode?: string): string {
  return DELIVERABLE_NAMES[mode ?? goal?.mode ?? ""] ?? DELIVERABLE_NAMES.design;
}

/** Whether one step named the deliverable this goal's mode is allowed to write. */
function stepNamesDeliverable(step: PlanStep, name: string): boolean {
  return (step.suggested_paths ?? []).some((p) => p.toLowerCase().includes(name));
}

/**
 * The file a deliverable was written to, read out of the plan that wrote it.
 *
 * The step names its target, and the engine keys the same signal — the fixer and
 * the verifier both match on the mode's file — so what the card names is the
 * file that was actually written rather than a convention. The fallback is not a
 * guess: it is the name the write step is planned for and the name the engine
 * discovers on its own.
 *
 * The first match wins. A plan that named two deliverable files is a plan the
 * card cannot resolve from the transcript, and the engine's own validation is
 * what says so — for a pin it refuses with the reason rather than quietly
 * binding the wrong file.
 *
 * `mode` overrides the goal's own, and a card rendered from a
 * `design_contract` event should pass the event's: a transcript message can
 * carry no goal at all, and without the event's mode a knowledge card would
 * fall back to `DESIGN.md` — naming, on a card headed "Knowledge deliverable",
 * the one file this mode can never write.
 */
export function deliverablePath(goal?: Goal, mode?: string): string {
  const name = deliverableName(goal, mode);
  for (const step of goal?.steps ?? []) {
    const hit = (step.suggested_paths ?? []).find((p) =>
      p.toLowerCase().includes(name)
    );
    if (hit) return hit;
  }
  return name === DELIVERABLE_NAMES.knowledge
    ? DEFAULT_KNOWLEDGE_MD
    : DEFAULT_DESIGN_MD;
}

/**
 * Whether the deliverable has been written *and* reviewed — the point a pin may
 * be offered, and the point a knowledge file becomes one.
 *
 * The draft is published on `design_contract` during planning, long before any
 * step runs, so "there is a body" and "there is a file the critic has approved"
 * are different moments. A step reaches `COMPLETED` only after the critic
 * approved it: a `request-changes` leaves it `IN_PROGRESS` and pauses the goal.
 * Gating on that is how "the critic reviews it first" stops being a claim about
 * the pipeline and becomes one about the card.
 */
export function deliverableReviewed(goal?: Goal, mode?: string): boolean {
  const name = deliverableName(goal, mode);
  return (goal?.steps ?? []).some(
    (step) => step.status === "COMPLETED" && stepNamesDeliverable(step, name)
  );
}

/**
 * How a deliverable's body is described beside its file, in three states.
 *
 * One vocabulary for both cards, because the states are the same facts and a
 * transcript that called the same bytes "as written" on one card and "the
 * deliverable" on the other would make the reviewer do the comparison.
 */
const BODY_LABELS = {
  draft: "the draft, not yet reviewed",
  written: "the deliverable, as written",
  proposed: "the proposed draft — nothing written to disk yet",
} as const;

/**
 * Which of the three states this goal is in.
 *
 * The dry-run state is decided here rather than by the caller because it is not
 * a separate question: a dry run that was reviewed proposed a file and wrote
 * nothing, so "reviewed" and "on disk" are different moments and a card that
 * conflated them would offer a bind for a document that does not exist.
 */
function bodyLabel(goal: Goal | undefined): string {
  if (!deliverableReviewed(goal)) return BODY_LABELS.draft;
  return goal?.dry_run ? BODY_LABELS.proposed : BODY_LABELS.written;
}

/**
 * What the card is called. A function of the mode, not of taste: a knowledge
 * goal's body is the workspace's knowledge file, and calling it a "design
 * direction" describes a contract nothing is bound by.
 */
export function deliverableHeading(mode?: string): string {
  if (mode === "knowledge") return "Knowledge deliverable";
  if (mode === "design") return "Design deliverable";
  return "Design direction";
}

/** What the card may offer, why not, and how to describe the body. */
/**
 * What a knowledge run was rewriting, as the engine published it.
 *
 * The text is the very copy the drafter's prompt quoted, published so a user
 * reviewing the new document is not left reconstructing the old one from a
 * diff — for this file that comparison *is* the review, because a prior that
 * only looks plausible is what the run exists to replace.
 *
 * `stale_paths` is the pack's verdict on that file, not the fresh read's: the
 * drafter's copy was read with no tree listing, so its own list is empty by
 * construction while the pack's is the check the drafter was also given. The
 * engine publishes the pack's, and this is the shape of it.
 */
export type Revises = {
  path: string;
  text: string;
  chars: number;
  truncated: boolean;
  stale_paths: string[];
};

/**
 * Whether a knowledge deliverable is replacing a file, and what that file said.
 *
 * `undefined` for a design or normal goal, and for a knowledge goal in a
 * workspace that had no `CODIFY.md` to begin with — in which case there is
 * nothing on the other side of the comparison, and rendering an empty block
 * would be a claim about a file that does not exist.
 */
export function revises(
  payload: Record<string, any> | undefined
): Revises | undefined {
  const block = payload?.revises;
  if (!block || typeof block.text !== "string" || block.text.trim() === "") {
    return undefined;
  }
  return {
    path: typeof block.path === "string" ? block.path : DEFAULT_KNOWLEDGE_MD,
    text: block.text,
    chars: typeof block.chars === "number" ? block.chars : block.text.length,
    truncated: block.truncated === true,
    stale_paths: Array.isArray(block.stale_paths) ? block.stale_paths : [],
  };
}

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
  const label = bodyLabel(goal);
  if (!deliverableReviewed(goal)) {
    return {
      ready: false,
      reason: "waiting on the review — a step must write it and the critic must approve it first",
      bodyLabel: label,
      pinned: false,
    };
  }
  if (goal?.dry_run) {
    return {
      ready: false,
      reason: `this run proposed ${path} without writing it — apply the goal, then pin it`,
      bodyLabel: label,
      pinned: false,
    };
  }
  if (sameFile(pinnedPath, path)) {
    return {
      ready: false,
      reason: "",
      bodyLabel: label,
      pinned: true,
    };
  }
  return {
    ready: true,
    reason: "",
    bodyLabel: label,
    pinned: false,
  };
}

/**
 * What a knowledge card has to say instead of offering an action.
 *
 * There is no pin for this mode and there is nothing else to click, so the card
 * carries one sentence that answers the question a design card answers with a
 * button: *what makes this file real?* The answer is that it takes effect by
 * being written — every later run's librarian reads it as a prior — and that
 * makes the dry-run state the one worth stating, because a reviewed proposal
 * that never reached the disk leaves the next run holding whatever an earlier
 * run left, or nothing at all.
 */
export type KnowledgeReadiness = {
  /** Whether a step wrote the file and the critic approved it. */
  reviewed: boolean;
  /** How the body is labelled beside the file. */
  bodyLabel: string;
  /** The one sentence that has to be on the card, naming the file. */
  note: string;
};

export function knowledgeReadiness(
  goal: Goal | undefined,
  path: string
): KnowledgeReadiness {
  const reviewed = deliverableReviewed(goal);
  if (!reviewed) {
    return {
      reviewed,
      bodyLabel: BODY_LABELS.draft,
      note: `no pin for this one — ${path} takes effect when a step writes it, and every later run reads it as a prior`,
    };
  }
  if (goal?.dry_run) {
    return {
      reviewed,
      bodyLabel: BODY_LABELS.proposed,
      note: `apply this goal to write ${path} — until then the next run still reads what an earlier run left, or nothing`,
    };
  }
  return {
    reviewed,
    bodyLabel: BODY_LABELS.written,
    note: `every later run's librarian now reads ${path} as a prior`,
  };
}
