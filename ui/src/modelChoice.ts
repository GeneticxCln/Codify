import type { ConductorModel, ModelOption } from "./types";

/**
 * Which model a turn runs on, what the command bar shows, and what it sends.
 *
 * The bar's picker used to be a decoration: the engine ran the turn on whatever Settings named, and the model
 * on screen was a guess (the first local one) that nothing read. Now a pick *routes* the turn
 * (`ExecutorService._picked_pair`), so the rules have to be exact, because a wrong default here is a turn
 * answered by a model nobody chose:
 *
 * - **Nothing picked, a model configured:** the bar shows the configured one and sends nothing. The turn
 *   runs where it always ran. A new install of this UI changes no existing person's turns.
 * - **Something picked:** the bar shows it and sends it, until they pick another or choose the default again.
 * - **Nothing picked, nothing configured:** the bar shows the old guess (a local model, else the first one),
 *   and *sends it*, because with nothing configured nothing would otherwise answer.
 *
 * What is shown is always what runs. Pure: no fetch, no DOM.
 */

/** Marks a model the person typed in by hand (`provider/model`), which no discovery lists. */
export const CUSTOM_MODEL_DESCRIPTION = "User-defined custom model";

const sameModel = (a: Pick<ModelOption, "provider" | "id">, b: Pick<ModelOption, "provider" | "id">): boolean =>
  a.id === b.id && a.provider === b.provider;

/** A model entered by hand: not in any catalog, and not to be dropped for that. */
export function isCustomPick(m: ModelOption): boolean {
  return m.description === CUSTOM_MODEL_DESCRIPTION;
}

/**
 * The pick after a catalog refresh. A pick stays while the catalog still lists it, and a hand-typed one
 * stays whatever the catalog says: it was never going to be there, and dropping it silently would send the
 * next turn to a different model than the one on screen a moment ago. A listed model that has vanished
 * (its key removed, its server stopped) is dropped, as before.
 */
export function keepPick(picked: ModelOption | undefined, catalog: ModelOption[]): ModelOption | undefined {
  if (!picked) return undefined;
  if (isCustomPick(picked) || catalog.some((m) => sameModel(m, picked))) return picked;
  return undefined;
}

/** The model Settings gives the conversation, as a row the menu can show: from the catalog when it is there. */
export function configuredModel(
  catalog: ModelOption[],
  conductor: ConductorModel | null | undefined,
): ModelOption | undefined {
  if (!conductor || !conductor.provider || !conductor.model) return undefined;
  const found = catalog.find((m) => sameModel(m, { provider: conductor.provider, id: conductor.model }));
  if (found) return found;
  return {
    id: conductor.model,
    name: conductor.model,
    provider: conductor.provider,
    description: "Configured in Settings, and not in the discovered list",
  };
}

/** The old guess, for an install with nothing configured: a local model, else the first. */
export function preferLocal(catalog: ModelOption[]): ModelOption | undefined {
  return catalog.find((m) => m.protocol === "ollama") ?? catalog[0];
}

export interface ModelChoice {
  /** What the picker shows, and what will run. */
  shown: ModelOption | undefined;
  /** What to send with the turn, or undefined to send nothing and let the configured model run. */
  override: ModelOption | undefined;
  /** The configured model, which the menu marks and which choosing again clears a pick for. */
  configured: ModelOption | undefined;
}

export function resolveModelChoice(input: {
  catalog: ModelOption[];
  picked: ModelOption | undefined;
  conductor: ConductorModel | null | undefined;
}): ModelChoice {
  const configured = configuredModel(input.catalog, input.conductor);
  if (input.picked) return { shown: input.picked, override: input.picked, configured };
  if (configured) return { shown: configured, override: undefined, configured };
  const guess = preferLocal(input.catalog);
  return { shown: guess, override: guess, configured: undefined };
}

/**
 * What a click on a model row means. Choosing the configured model clears the pick (the turn runs where
 * Settings says, and follows Settings if it changes); choosing anything else is a pick.
 */
export function pickFromMenu(chosen: ModelOption, configured: ModelOption | undefined): ModelOption | undefined {
  return configured && sameModel(chosen, configured) ? undefined : chosen;
}
