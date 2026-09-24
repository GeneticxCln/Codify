import type { AgentConfig, AgentRole, ModelOption, RecentRunModel } from "./types.ts";

/**
 * What the app already knows about each discovered model.
 *
 * The menu used to be provider order and nothing else, so the two ids that matter
 * most — the one a role runs on and the one you ran last — sat wherever they
 * happened to fall in a provider's list. These are the signals that fix that, and
 * every one of them is something the app already holds rather than a new guess:
 *
 *  - **in use** — the roles configured with that exact model (`/settings/agents`),
 *  - **ran recently** — the models the engine's `agent_assigned` events record as
 *    having actually answered (`GET /models/recent`). Deliberately *not* the goals'
 *    requested model: since per-role configs became authoritative, a goal can name
 *    one model and be executed by four others, so ordering a menu by intent would
 *    label a model the engine never called. The engine keeps the event log, so
 *    local storage here would be a second, thinner copy of a stored fact,
 *  - **chat support** — the provider's own `supports_chat`, never inferred from a
 *    name, because a name-based "looks like an embeddings model" list is a
 *    hardcoded list in disguise.
 *
 * Pure by design: no fetch, no DOM. The menu component renders what this returns.
 */

/** One model is identified by provider *and* id: two providers can serve the same id. */
export function modelKey(provider: string, id: string): string {
  return `${provider}:${id}`;
}

export interface ModelUsage {
  /** Roles whose config names this exact model, in pipeline order. */
  roles: AgentRole[];
  /** Epoch seconds this model last answered, or null if it never did. */
  ranAt: number | null;
  /** The role that ran it, as the engine reported it. */
  ranRole: AgentRole | null;
}

/** Did this model answer anything, however long ago? */
function hasRun(usage?: ModelUsage): boolean {
  return Boolean(usage && usage.ranAt != null);
}

export interface ModelSignals {
  /** Keyed by `modelKey`; a model nothing points at is absent. */
  usage: Record<string, ModelUsage>;
}

export const EMPTY_SIGNALS: ModelSignals = { usage: {} };

/**
 * Build the signals from role configs and the engine's record of what ran.
 *
 * `recentRuns` is already newest-first and already only contains models that
 * answered, so nothing here has to guess from a status: a goal that was cancelled
 * during planning never emitted `agent_assigned`, and therefore cannot relabel the
 * menu with a model that produced nothing.
 */
export function buildModelSignals(
  configs: AgentConfig[] = [],
  recentRuns: RecentRunModel[] = [],
): ModelSignals {
  const usage: Record<string, ModelUsage> = {};
  const entry = (provider: string, id: string): ModelUsage => {
    const key = modelKey(provider, id);
    if (!usage[key]) usage[key] = { roles: [], ranAt: null, ranRole: null };
    return usage[key];
  };

  for (const cfg of configs) {
    const primary = (cfg.model_name || "").trim();
    if (primary) entry(cfg.provider, primary).roles.push(cfg.role);
    // A role's fallback is a model this install genuinely runs, so it belongs in
    // the same signal — it is not "unused" just because it is second choice.
    const fallback = (cfg.fallback_model_name || "").trim();
    if (fallback && cfg.fallback_provider) {
      const u = entry(cfg.fallback_provider, fallback);
      if (!u.roles.includes(cfg.role)) u.roles.push(cfg.role);
    }
  }

  // Newest wins: the array is ordered by the engine, and a model that ran twice
  // keeps its most recent run rather than its first.
  for (const run of recentRuns) {
    const provider = (run.provider || "").trim();
    const model = (run.model || "").trim();
    if (!provider || !model) continue;
    const u = entry(provider, model);
    if (u.ranAt == null) {
      u.ranAt = run.ran_at || 0;
      u.ranRole = run.role ?? null;
    }
  }

  return { usage };
}

export interface ModelSection {
  /** `roles` | `last-run` | `provider:<slug>` */
  id: string;
  label: string;
  /** True for the two sections that lead the menu. */
  pinned: boolean;
  models: ModelOption[];
}

interface OrderOptions {
  /** The model currently chosen; it leads its own group. */
  selected?: Pick<ModelOption, "provider" | "id">;
  /** Already-typed filter. Filtering happens here so no signal can bypass it. */
  filter?: string;
  signals?: ModelSignals;
}

function matches(m: ModelOption, query: string): boolean {
  if (!query) return true;
  return (
    m.name.toLowerCase().includes(query) ||
    m.id.toLowerCase().includes(query) ||
    m.provider.toLowerCase().includes(query)
  );
}

function isCurrent(m: ModelOption, selected?: OrderOptions["selected"]): boolean {
  return !!selected && m.id === selected.id && m.provider === selected.provider;
}

/** The row you are looking for is the one already in use, so it leads its group. */
function hoistCurrent(models: ModelOption[], selected?: OrderOptions["selected"]): ModelOption[] {
  const index = models.findIndex((m) => isCurrent(m, selected));
  if (index <= 0) return models;
  const rest = [...models];
  const [lead] = rest.splice(index, 1);
  rest.unshift(lead);
  return rest;
}

/**
 * Chat-capable first, then newest, then the provider's own order.
 *
 * Non-chat models are kept in the list and pushed to the end rather than hidden:
 * a provider can legitimately serve an id its own list omits, and "we removed some
 * of your models" is worse than "this one cannot answer". The *current* model still
 * leads, because the row you are looking for is the one already in use.
 */
function orderWithinGroup(models: ModelOption[], selected?: OrderOptions["selected"]): ModelOption[] {
  const rank = (m: ModelOption) => (m.supports_chat === false ? 1 : 0);
  const created = (m: ModelOption) => (typeof m.created === "number" ? m.created : -1);
  const sorted = [...models].sort((a, b) => {
    if (rank(a) !== rank(b)) return rank(a) - rank(b);
    if (created(a) !== created(b)) return created(b) - created(a);
    return 0;
  });
  return hoistCurrent(sorted, selected);
}

/**
 * The menu: what your roles use, then what you ran last, then every provider.
 *
 * Nothing is dropped — a model that is neither in use nor recent is still one
 * group further down, under the provider that serves it. The two leading sections
 * are conveniences over the same list, not filters on it.
 */
export function orderModelMenu(models: ModelOption[], options: OrderOptions = {}): ModelSection[] {
  const { selected, signals = EMPTY_SIGNALS } = options;
  const query = (options.filter || "").trim().toLowerCase();
  const matching = models.filter((m) => matches(m, query));

  const inUse: ModelOption[] = [];
  const ranRecently: ModelOption[] = [];
  const rest: ModelOption[] = [];
  for (const m of matching) {
    const u = signals.usage[modelKey(m.provider, m.id)];
    if (u?.roles?.length) inUse.push(m);
    else if (hasRun(u)) ranRecently.push(m);
    else rest.push(m);
  }

  // Inside "in use", evidence beats intent: a model that actually answered leads
  // one that is merely configured, newest answer first. Only then do the tiebreakers
  // apply — a chat-capable model ahead of one the provider marks as non-chat, and
  // among those the model carrying the most roles (a shared model is the likeliest
  // intent behind opening the menu at all).
  const recency = (m: ModelOption) =>
    signals.usage[modelKey(m.provider, m.id)]?.ranAt ?? -1;
  const roleCount = (m: ModelOption) =>
    signals.usage[modelKey(m.provider, m.id)]?.roles.length ?? 0;
  inUse.sort((a, b) => {
    const ra = recency(a);
    const rb = recency(b);
    if (ra !== rb) return rb - ra;
    const rank = (m: ModelOption) => (m.supports_chat === false ? 1 : 0);
    if (rank(a) !== rank(b)) return rank(a) - rank(b);
    return roleCount(b) - roleCount(a);
  });

  const sections: ModelSection[] = [];
  if (inUse.length) {
    sections.push({
      id: "roles",
      label: "In use by roles",
      pinned: true,
      // Already ranked by the comparator above; only the current pick is hoisted.
      models: hoistCurrent(inUse, selected),
    });
  }
  if (ranRecently.length) {
    // What is left after the in-use group, so these are models that answered but
    // that no role currently points at — an assignment you changed, or a direct
    // run. Worth surfacing rather than burying, because it is the short list of
    // ids this install has proven it can call.
    //
    // Newest first: this section *is* the recency signal, so the model you ran last
    // leads it, and recency outranks the chat/date ordering used in a provider group.
    const byRecency = [...ranRecently].sort(
      (a, b) =>
        (signals.usage[modelKey(b.provider, b.id)]?.ranAt ?? 0) -
        (signals.usage[modelKey(a.provider, a.id)]?.ranAt ?? 0),
    );
    sections.push({
      id: "recent",
      label: "Ran recently",
      pinned: true,
      models: hoistCurrent(byRecency, selected),
    });
  }

  const groups = new Map<string, ModelOption[]>();
  for (const m of rest) {
    const bucket = groups.get(m.provider);
    if (bucket) bucket.push(m);
    else groups.set(m.provider, [m]);
  }
  for (const [provider, group] of groups) {
    sections.push({
      id: `provider:${provider}`,
      label: provider,
      pinned: false,
      models: orderWithinGroup(group, selected),
    });
  }
  return sections;
}

/**
 * A single provider's list, for the Settings model field.
 *
 * Same ordering rules as the menu's provider groups, so an id sits in the same
 * place in both pickers. No sections here: the field already belongs to one role,
 * and a role's own list is short enough that headings would be noise.
 */
export function orderProviderModels(
  models: ModelOption[],
  options: { selected?: OrderOptions["selected"]; filter?: string } = {},
): ModelOption[] {
  const query = (options.filter || "").trim().toLowerCase();
  return orderWithinGroup(
    models.filter((m) => matches(m, query)),
    options.selected,
  );
}

/** The badge text for one row: roles first, then recency, then chat support. */
export function modelBadges(
  model: ModelOption,
  signals: ModelSignals = EMPTY_SIGNALS,
  maxRoles = 3,
): {
  roles: string;
  rolesTitle: string;
  lastRun: boolean;
  /** Tooltip for the recency chip, naming the role that actually ran it. */
  lastRunTitle: string;
  notChat: boolean;
} {
  const u = signals.usage[modelKey(model.provider, model.id)];
  const roles = u?.roles ?? [];
  const shown = roles.slice(0, maxRoles);
  const extra = roles.length - shown.length;
  const ran = hasRun(u);
  return {
    roles: extra > 0 ? `${shown.join(", ")} +${extra}` : shown.join(", "),
    rolesTitle: roles.length ? `Configured on: ${roles.join(", ")}` : "",
    lastRun: ran,
    lastRunTitle: !ran
      ? ""
      : u?.ranRole
        ? `Last run by the ${u.ranRole}`
        : "The last model a goal ran",
    notChat: model.supports_chat === false,
  };
}
