/**
 * What a provider row does, as arithmetic rather than as a component.
 *
 * A provider row is the first place in this app where a model is chosen next to
 * a credential rather than on a role card. That raises two questions this
 * module answers, both of which are worth getting right away from a DOM:
 *
 * 1. **What does the search match?** Model ids are long and rarely what a person
 *    remembers — `meta-llama/llama-3.3-70b-instruct` arrives as "llama", as
 *    "70b", as "instruct", or as nothing at all. A search that only matches the
 *    id from the left is a search most people give up on.
 *
 * 2. **What would "apply" change?** Only `PUT /settings/agents/{role}` may mutate
 *    agent config (docs/00 §6.2), so this screen cannot set a provider-wide
 *    default and call it done. It writes through that one endpoint, once per
 *    role. Which means the button is a *bulk edit*, and a bulk edit that says
 *    nothing about its blast radius is the kind that repoints eight roles on a
 *    stray click — so the plan is computed, shown, and only then applied.
 *
 * The wording helpers live here too, beside the arithmetic they describe. They
 * were first written inside the two components, which meant the suite for them
 * had to import `.tsx` through a loader hook — and a test that can only run
 * after a global side effect is a test that quietly stops running.
 */

import type { AgentRole, ModelOption } from "./types";
import { providerLabel } from "./providerLabels.ts";

/** A row's worth of role config: who, and which provider they are on. */
export interface RoleProvider {
  role: AgentRole;
  provider: string;
  model_name?: string;
}

/** One role, and what its model is right now. */
export interface ApplyStep {
  role: AgentRole;
  from: string;
  to: string;
  /** False when the role already runs this exact model — nothing to write. */
  changes: boolean;
}

/**
 * The roles a provider row's model would be written to.
 *
 * Only roles *already on this provider*. Widening it to "every role" would be a
 * different button with a different name — "point everything at one provider" is
 * a decision, and this row is not where you make it. The settings panel's bulk
 * assign already does that, across providers, on purpose.
 */
export function rolesOnProvider(
  configs: readonly RoleProvider[],
  provider: string,
): AgentRole[] {
  return configs.filter((c) => c.provider === provider).map((c) => c.role);
}

/**
 * The whole write, as a list — so a caller can show it, and a test can assert
 * it, without a role having been touched.
 *
 * A role already on the chosen model is included with `changes: false` rather
 * than dropped: the count in the button's label is "how many roles this
 * touches", and a role that does not need writing should be visible as not
 * needing writing rather than silently absent from a total.
 */
export function planApply(
  configs: readonly RoleProvider[],
  provider: string,
  model: string,
): ApplyStep[] {
  const trimmed = model.trim();
  if (!trimmed) return [];
  return rolesOnProvider(configs, provider).map((role) => {
    const from = configs.find((c) => c.role === role)?.model_name ?? "";
    return { role, from, to: trimmed, changes: from !== trimmed };
  });
}

/** The roles `planApply` would actually write to. */
export function changedRoles(steps: readonly ApplyStep[]): ApplyStep[] {
  return steps.filter((s) => s.changes);
}

/**
 * One term, matched case-insensitively anywhere in the model.
 *
 * `id`, `name` and `description` are all searched, because a provider's list
 * carries a display name and a blurb that the id does not — "70B" is in one and
 * not the other often enough that searching one field loses models.
 */
function matchesTerm(model: ModelOption, term: string): boolean {
  const needle = term.toLowerCase();
  return (
    model.id.toLowerCase().includes(needle) ||
    (model.name ?? "").toLowerCase().includes(needle) ||
    (model.description ?? "").toLowerCase().includes(needle)
  );
}

/**
 * The search, over whitespace-separated terms that must *all* match.
 *
 * All-of rather than any-of on purpose: ids are dotted and slashed
 * (`meta-llama/llama-3.3-70b-instruct`), so a person who remembers two fragments
 * of one id types two fragments and expects one list. Any-of would give them the
 * union and make the result look like the search ignored half of what they typed.
 *
 * An empty query matches everything, which is what opening the menu on purpose
 * should show — the whole list, not nothing.
 */
export function searchModels(
  options: readonly ModelOption[],
  query: string,
): ModelOption[] {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return [...options];
  return options.filter((m) => terms.every((term) => matchesTerm(m, term)));
}

/**
 * The list header's count, worded so a cut-off list is not a lie.
 *
 * A provider serving two hundred models can only show a scroll box, and a scroll
 * box with no number reads as "these twelve are all of them". So the header says
 * both numbers, and says "of" only when they differ — "12 of 12" is noise.
 */
export function countLabel(shown: number, total: number): string {
  if (total === 0) return "No models discovered";
  if (shown === total) return `${total} discovered`;
  return `${shown} of ${total}`;
}

/**
 * The panel footer's summary of what discovery found.
 *
 * Discovery is per provider, so the two numbers can disagree, and the honest
 * reading of "0 models across 1 provider" is that the one provider answered and
 * served nothing — not that nothing was asked. It also pluralizes, because
 * "1 providers" is the kind of detail that makes a person stop trusting a
 * number they cannot otherwise check.
 */
export function discoveredFooter(models: number, providers: number): string {
  const where =
    providers === 1
      ? "1 provider answered"
      : `${providers} providers answered`;
  if (models === 0) return `No models · ${where}`;
  return `${models} ${models === 1 ? "model" : "models"} · ${where}`;
}

/**
 * How long ago the list was actually asked for, in words.
 *
 * The panel re-discovers on its own now (docs/06 §6), which means the list can
 * change while somebody is looking at it. A list that changes on its own and
 * says nothing reads as a bug — or worse, as a model quietly vanishing — so the
 * footer always says when the last answer arrived. `fetchedAt` is the engine's
 * own timestamp, which means a cached answer correctly reports the *older* time
 * rather than claiming to have just been asked.
 *
 * `checking` wins over the age: while a request is in flight the honest thing to
 * say is that a request is in flight, not how old the previous answer is.
 */
export function checkedLabel(
  fetchedAt: number | null,
  now: number,
  checking: boolean,
): string {
  if (checking) return "checking providers…";
  if (fetchedAt === null) return "";
  // A clock that went backwards, or a timestamp in the future, is not a negative
  // age to render. "just now" is the least alarming true-ish reading, and the
  // next tick fixes it either way.
  const ageS = Math.max(0, Math.round(now - fetchedAt));
  if (ageS < 10) return "checked just now";
  if (ageS < 60) return `checked ${ageS}s ago`;
  if (ageS < 3600) return `checked ${Math.round(ageS / 60)} min ago`;
  const at = new Date(fetchedAt * 1000);
  const hh = String(at.getHours()).padStart(2, "0");
  const mm = String(at.getMinutes()).padStart(2, "0");
  return `checked at ${hh}:${mm}`;
}

/**
 * Does this screen refresh the catalogue itself?
 *
 * The rule is one line and it is the whole point of the engine pushing: while the
 * engine is telling us the catalogue, no screen owns a timer. One sweep serves
 * every open window, and a timer per screen would ask the same eight providers
 * once per screen to learn what they already told us.
 *
 * The other two conditions are not decoration. A closed panel has nothing on
 * screen to be stale, and a panel that is open while the engine is *not* pushing
 * is the one case where a stale list is worse than the discovery that fixes it.
 */
export function needsOwnRefresh(opts: { live: boolean; isOpen: boolean }): boolean {
  return opts.isOpen && !opts.live;
}

/**
 * What the button is about to do, in words.
 *
 * "Apply to 3 roles" says how many. It does not say *which*, and a bulk edit
 * that names no roles is one a person cannot check against what they meant. The
 * first role is named with its move; the rest are counted.
 */
export function planSummary(plan: readonly ApplyStep[]): string {
  const changes = changedRoles(plan);
  if (!changes.length) return "";
  const first = changes[0];
  const rest = changes.length - 1;
  const arrow = first.from ? `${first.from} → ${first.to}` : `set to ${first.to}`;
  return rest > 0 ? `${first.role} ${arrow}, and ${rest} more` : `${first.role} ${arrow}`;
}

/**
 * The apply button's own label.
 *
 * Pure and separate because two of its four states cannot be reached by
 * rendering the component: staging a model needs a keystroke, so a static
 * snapshot is only ever the "nothing chosen yet" state. Asserting the rest from
 * markup would mean asserting nothing.
 *
 * The order matters. "Choose a model" is checked *before* the no-roles case on
 * purpose: a fresh provider with no model picked is missing a model first, and
 * telling it that no role is on this provider answers a question it did not
 * ask. And nothing is ever labelled "already on it" unless a model really is
 * staged — otherwise the button opens claiming a state nobody is in, on a
 * control that looks like it has already run.
 */
export function applyLabel(opts: {
  staged: string;
  /** How many roles the staged model would actually write to. */
  changes: number;
  /** How many roles are on this provider at all. */
  onProvider: number;
  /** The staged model is the one just written. */
  applied: boolean;
}): string {
  if (opts.applied && opts.staged.trim()) {
    const n = opts.changes || opts.onProvider;
    return `Applied to ${n} role${n === 1 ? "" : "s"}`;
  }
  if (opts.changes > 0) {
    return `Apply to ${opts.changes} role${opts.changes === 1 ? "" : "s"}`;
  }
  if (!opts.staged.trim()) return "Choose a model";
  if (opts.onProvider === 0) return "No roles on this provider";
  return `${opts.onProvider} role${opts.onProvider === 1 ? "" : "s"} already on it`;
}

/** Why the apply button is disabled, for its tooltip. */
export function applyHint(opts: {
  staged: string;
  changes: number;
  onProvider: number;
}): string {
  if (!opts.staged.trim()) return "Choose a model first";
  if (opts.onProvider === 0) return "No role is on this provider yet";
  if (opts.changes === 0) return "Every role on this provider already runs this model";
  return "Write this model to every role on this provider, one at a time";
}

/**
 * What the empty state says.
 *
 * The three cases are genuinely different and collapsing them sends people to
 * the wrong screen: a provider that has no key cannot be asked anything, a
 * provider that answered with nothing has said something, and a search that
 * matched nothing is the reader's own two words being wrong. "No models — add
 * its API key" is worse than useless for a local provider, which needs none.
 */
export function noModelsMessage(
  provider: string,
  opts: { hasKey?: boolean; needsKey?: boolean; discoveryError?: string | null },
): string {
  // The name a person reads, not the slug (`providerLabels.ts`): "Could not reach NVIDIA", not "nvidia".
  const name = providerLabel(provider) || provider;
  if (opts.discoveryError) {
    return `Could not reach ${name} — ${opts.discoveryError}`;
  }
  if (opts.needsKey && !opts.hasKey) {
    return `Add a ${name} API key, then refresh — its models cannot be listed without one.`;
  }
  if (opts.needsKey) {
    return `${name} is configured but reported no models.`;
  }
  return `${name} reported no models. Check that the server is running.`;
}
