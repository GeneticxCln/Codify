import type { AgentConfig, ModelOption, ProviderModelStatus } from "./types.ts";

/** A role whose saved model the provider no longer reports. */
export interface StaleModel {
  model: string;
  provider: string;
  /** How many models the provider reported in the run this verdict came from. */
  reportedCount: number;
}

/**
 * Whether a role's *saved* model is missing from its provider's live catalog.
 *
 * The one place this rule lives, so the per-card warning and the panel-level
 * summary can never disagree. Two honesty rules keep it from crying wolf:
 *
 *  1. Only a **successful** discovery can say a model is gone. A provider that
 *     failed to answer (no key, unreachable endpoint) has told us nothing, and
 *     warning there would dress "add a key" up as "your model was retired" —
 *     a false statement with the exact shape of the true one.
 *  2. Callers pass the **saved** config, not a draft, so typing a new id does
 *     not flash the warning on every keystroke that is not yet a real model.
 */
export function staleTarget(
  provider: string,
  model: string,
  models: ModelOption[],
  providerStatus: ProviderModelStatus[]
): StaleModel | null {
  provider = (provider || "").trim();
  model = (model || "").trim();
  if (!provider || !model) return null;

  const discovery = providerStatus.find((s) => s.provider === provider);
  // Not asked yet (no entry) counts as "unknown", same as a failed ask: warning
  // on an unqueried provider would dress "the catalog has not loaded yet" up as
  // "your model was retired".
  if (!discovery?.ok) return null;

  // A discovery that answered but never listed this provider's models (engine
  // shape drift) is also unknown — only a real, successful listing may condemn.
  const catalog = models.filter((m) => m && m.provider === provider && typeof m.id === "string");
  if (discovery.count > 0 && catalog.length === 0) return null;
  if (catalog.some((m) => m.id === model)) return null;

  return { model, provider, reportedCount: catalog.length };
}

export function findStaleModel(
  config: Pick<AgentConfig, "provider" | "model_name"> | undefined,
  models: ModelOption[],
  providerStatus: ProviderModelStatus[]
): StaleModel | null {
  return staleTarget(
    config?.provider ?? "",
    config?.model_name?.trim() ?? "",
    models,
    providerStatus
  );
}

/**
 * The same verdict for a role's fallback target.
 *
 * A fallback rots exactly like a primary does — the provider retires the model and
 * nobody notices, because a fallback is only *used* when something else has already
 * gone wrong. Flagging it while the user is looking at the setting is the whole
 * difference between a rescue and a second failure.
 */
export function findStaleFallback(
  config:
    | Pick<AgentConfig, "fallback_provider" | "fallback_model_name">
    | undefined,
  models: ModelOption[],
  providerStatus: ProviderModelStatus[]
): StaleModel | null {
  return staleTarget(
    config?.fallback_provider ?? "",
    config?.fallback_model_name?.trim() ?? "",
    models,
    providerStatus
  );
}
