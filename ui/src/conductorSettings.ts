/**
 * What the conductor's model fields mean, as arithmetic rather than a component.
 *
 * The conductor is a loop and not a ninth role (docs/01 §5), so its model is
 * two `engine_settings` strings rather than an `AgentConfig` row — and for a
 * long time those two keys were read by the executor and writable by nobody at
 * all, which meant every install ran the conductor on the scribe's row whether
 * or not anyone wanted that. The settings are reachable now, and this module
 * is the half of that surface worth getting right away from a DOM: what the
 * pair you are typing will actually *do*, and when a save would store something
 * the engine ignores.
 *
 * That last one is the reason this file exists rather than three `if`s in the
 * card. `ExecutorService._conductor_config` treats a provider without a model as
 * "not configured" and borrows the scribe's row, so a card that saved a half
 * pair would be a setting the user can change and watch nothing happen — the
 * exact defect docs/09 §7.1 names. The engine stores a half pair on purpose
 * (clearing has to be one call), so the honesty has to live on this side of it.
 */

/** The conductor's provider and model, as typed in the card. */
export interface ConductorChoice {
  provider: string;
  model: string;
}

/** What the conductor will run on, given what is stored. */
export type ConductorStatus =
  /** Both keys empty: the conductor borrows the scribe's row. */
  | { kind: "borrowed"; text: string }
  /** Both keys set: the conductor runs on its own model. */
  | { kind: "configured"; text: string }
  /** Exactly one key set: stored, and deliberately ignored by the executor. */
  | { kind: "incomplete"; text: string };

/**
 * Why the conductor runs on the model it runs on, in one sentence.
 *
 * `borrowedModel` is the scribe's *stored* model, passed in rather than read
 * here because the role configs live in a store this module must not depend on.
 * An empty one is called out rather than papered over: a fresh install has no
 * scribe model either, which means there is no conductor at all — a card that
 * said "runs on the scribe's model" with nothing after it would be claiming a
 * configuration that does not exist.
 */
export function conductorStatus(
  choice: ConductorChoice,
  borrowedModel?: string
): ConductorStatus {
  const provider = (choice.provider || "").trim();
  const model = (choice.model || "").trim();
  if (provider && model) {
    return {
      kind: "configured",
      text: `The conductor runs on ${provider} · ${model}. Each turn is one loop on that model, and the sub-agents keep their own models below.`,
    };
  }
  if (provider || model) {
    return {
      kind: "incomplete",
      text:
        "A provider without a model is not a configuration, so this half-pair is ignored: " +
        "the conductor keeps running on the scribe's model until both are set. Clear both to keep it that way.",
    };
  }
  const borrowed = (borrowedModel || "").trim();
  if (!borrowed) {
    return {
      kind: "borrowed",
      text:
        "The conductor borrows the scribe's model, and the scribe has none chosen — " +
        "so there is no conductor on this install until a model is named here or on the scribe's card.",
    };
  }
  return {
    kind: "borrowed",
    text: `The conductor borrows the scribe's model (${borrowed}). Name a model here to put it on a stronger one than the scribe uses for summaries.`,
  };
}

/** Whether a typed pair is worth sending, and why not when it is not. */
export interface SaveVerdict {
  ok: boolean;
  /** Why the save is refused. Empty when it is allowed. */
  text: string;
}

/**
 * Whether the card may save the pair as typed.
 *
 * A half pair is refused rather than stored-and-ignored, so the one thing this
 * screen can do that the engine cannot do for itself is keep the user from
 * saving a setting that will not take effect. Clearing is a separate, explicit
 * action (`needsClear`) precisely so that "I typed a provider and nothing else"
 * and "I want it back to the scribe" do not share a button.
 */
export function canSaveChoice(choice: ConductorChoice): SaveVerdict {
  const provider = (choice.provider || "").trim();
  const model = (choice.model || "").trim();
  if (provider && model) return { ok: true, text: "" };
  return {
    ok: false,
    text: provider
      ? "Choose a model for this provider, or clear the provider to keep the scribe's."
      : "Choose a provider for this model, or clear the model to keep the scribe's.",
  };
}

/**
 * Whether the stored pair is something the card should offer to undo.
 *
 * True only for a *complete* stored pair with something missing from the draft:
 * that is a user who has begun to clear the setting. A stored half pair is not
 * offered, because undoing it would produce the same inert state it is already
 * in, and telling someone to press a button that changes nothing is its own
 * small lie.
 */
export function needsClear(stored: ConductorChoice, draft: ConductorChoice): boolean {
  const had = Boolean((stored.provider || "").trim() && (stored.model || "").trim());
  const now = Boolean((draft.provider || "").trim() || (draft.model || "").trim());
  return had && !now;
}

/** Where the conductor's second target actually comes from. */
export type FallbackSource =
  /** The `conductor_fallback_*` pair, which only applies to an own-pair conductor. */
  | "own"
  /** The scribe's own fallback row, because the conductor is still borrowing it. */
  | "borrowed"
  /** Nothing to fall back to: a dead model ends the turn. */
  | "none";

/**
 * What happens when the conductor's model cannot serve a call.
 *
 * The precedence is a rule rather than a merge, and the card has to state it
 * because the alternative is a field a user fills in and watches do nothing: a
 * conductor still on the scribe's row takes the *scribe's* fallback, which
 * already has a credential resolved, so the two sources are never combined into
 * one chain.
 */
export function conductorFallback(
  own: ConductorChoice,
  ownFallback: ConductorChoice,
  scribeFallbackModel?: string
): { source: FallbackSource; text: string } {
  const borrowed = !(own.provider || "").trim() && !(own.model || "").trim();
  if (borrowed) {
    const scribe = (scribeFallbackModel || "").trim();
    return scribe
      ? {
          source: "borrowed",
          text: `If ${scribe} goes down the turn moves to the scribe's fallback, ${scribe}. The pair below is not used while the conductor borrows the scribe's row — give the conductor its own model above to use it.`,
        }
      : {
          source: "none",
          text:
            "There is no fallback: a dead model ends the turn. The conductor borrows the scribe's row, and the scribe has no fallback either. The pair below takes effect once the conductor has a model of its own.",
        };
  }
  const provider = (ownFallback.provider || "").trim();
  const model = (ownFallback.model || "").trim();
  if (provider && model) {
    return {
      source: "own",
      text: `A provider that cannot serve a call moves the turn to ${provider} · ${model} — the call is retried, not the whole turn, so the moves already made are kept.`,
    };
  }
  return {
    source: "none",
    text:
      "There is no fallback: a dead model ends the turn, after the moves it already made. Set a provider and a model below to keep that from happening.",
  };
}
