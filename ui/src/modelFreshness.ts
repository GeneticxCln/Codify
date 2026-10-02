/**
 * Which models are new since the reader last looked.
 *
 * Discovery is live (docs/06), which is exactly the problem: a provider that
 * released a model this morning puts it in the catalogue, and on a list of two
 * hundred sorted by a `created` field most providers do not even send, it lands
 * wherever the response happened to put it. A picker with a search box is not
 * a notification — nobody opens a dropdown to look for the one entry they did
 * not know about. So the panel remembers what it last showed per provider and
 * marks the difference.
 *
 * **The baseline is the last visit, not the last fetch.** Both are defensible;
 * only one of them can say "since you last looked". The index is therefore read
 * once per launch and never updated in place, so a marker survives for the whole
 * session instead of blinking out the moment the panel re-fetches. The stored
 * copy is what the *next* launch compares against. That is also what makes the
 * panel's own re-discovery (a model released while it sits open) worth having:
 * the release is not in the record, so it is badged, and it stays badged until
 * the next launch rather than until the next tick.
 *
 * **A first sighting is a baseline, not a release.** Someone opening the panel
 * for the first time has nothing to be new *to*, and marking all sixteen local
 * models "new" is how a marker gets ignored. `newModelIds` returns nothing for a
 * provider the index has never heard of, and the first successful discovery is
 * what puts it in.
 *
 * **A failed discovery must not become a mass of new models.** A rejected key
 * answers with an empty list, and an index that accepted that would come back
 * one refresh later claiming every model on the provider is new. `mergeIndex`
 * refuses to fold in a provider that did not answer.
 *
 * The store is `localStorage` because this is the only fact here that is
 * per-person and per-machine — the engine has no idea which of a provider's two
 * hundred models you have already been shown, and a model list on the server
 * would be a list of what *someone* saw.
 */

/** Where the per-provider baseline lives. Namespaced with the other UI keys. */
import { providerLabel } from "./providerLabels.ts";

export const SEEN_KEY = "CODIFY_SEEN_MODELS";

/**
 * A cap, so a provider that serves five hundred models cannot grow this without
 * bound over months of use. The engine already refuses to report more than 500
 * per provider, so matching it keeps the whole index inside one localStorage
 * value. Ids are held in first-seen order and the *oldest* are dropped: a model
 * seen long ago is the one whose "new" badge matters least.
 */
export const SEEN_LIMIT = 500;

/** provider slug → model ids, in the order they were first seen. */
export type SeenIndex = Record<string, string[]>;

/** The slice of `Storage` this uses, so a test can hand in a map. */
export interface SeenStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export const emptyIndex = (): SeenIndex => ({});

/**
 * Read an index, treating anything unreadable as "no history".
 *
 * A corrupt value is not worth an error the reader has to dismiss: the cost of
 * getting it wrong is one missing "new" badge on the next visit, and the cost of
 * refusing to open the panel over a bad JSON blob in localStorage is higher.
 * Every shape check is on the way in, because `localStorage` is not a type.
 */
export function parseIndex(raw: string | null): SeenIndex {
  if (!raw) return emptyIndex();
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return emptyIndex();
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return emptyIndex();
  }
  const out: SeenIndex = {};
  for (const [provider, ids] of Object.entries(parsed as Record<string, unknown>)) {
    if (!Array.isArray(ids)) continue;
    out[provider] = ids.filter((id): id is string => typeof id === "string");
  }
  return out;
}

export function readIndex(storage: SeenStorage): SeenIndex {
  try {
    return parseIndex(storage.getItem(SEEN_KEY));
  } catch {
    // A browser that refuses localStorage (private mode, disabled storage) is a
    // browser that will not be storing the index either. No history, no badges.
    return emptyIndex();
  }
}

export function writeIndex(storage: SeenStorage, index: SeenIndex): void {
  try {
    storage.setItem(SEEN_KEY, JSON.stringify(index));
  } catch {
    // Same reasoning as the read: a full or unavailable store costs the badge,
    // not the panel. Quota is the realistic case — 500 ids across eight
    // providers is a few tens of kilobytes.
  }
}

/**
 * The ids this provider serves that the last visit did not.
 *
 * In the order the provider returned them, which the engine has already put
 * newest-first where it can. Empty for a provider with no history yet.
 */
export function newModelIds(
  index: SeenIndex,
  provider: string,
  ids: readonly string[],
): string[] {
  const seen = index[provider];
  // No history means no "new": the first time this provider has ever been listed
  // there is nothing to have been new *to*.
  if (!seen) return [];
  const known = new Set(seen);
  return ids.filter((id) => !known.has(id));
}

/**
 * Fold one provider's fresh discovery into the index.
 *
 * A provider that did not answer is left exactly as it was — see the module
 * docstring for why that is not pedantry.
 */
export function mergeIndex(
  index: SeenIndex,
  provider: string,
  ids: readonly string[],
  ok = true,
): SeenIndex {
  if (!ok) return index;
  const known = new Set(index[provider] ?? []);
  const merged = [...(index[provider] ?? [])];
  for (const id of ids) {
    if (known.has(id)) continue;
    known.add(id);
    merged.push(id);
  }
  return {
    ...index,
    [provider]: merged.length > SEEN_LIMIT ? merged.slice(-SEEN_LIMIT) : merged,
  };
}

/**
 * Every provider folded in at once, skipping the ones that did not answer.
 *
 * The `ok` travels with the ids because the model list alone cannot tell "this
 * provider serves nothing" from "this provider refused to answer": the first is
 * a fact worth remembering, the second is a failure to forget.
 */
export function mergeAll(
  index: SeenIndex,
  providers: readonly { provider: string; ok: boolean; ids: readonly string[] }[],
): SeenIndex {
  let next = index;
  for (const p of providers) {
    next = mergeIndex(next, p.provider, p.ids, p.ok);
  }
  return next;
}

/**
 * The new ones first, the rest in the order they arrived.
 *
 * Floating them is the point of the marker. The alternative — a green dot in
 * row 170 of a scroll box — is a badge nobody scrolls to. Within each group the
 * engine's own order is untouched, so a model that is not new still reads
 * newest-first where the provider dates it.
 */
export function newFirst<T extends { id: string }>(
  options: readonly T[],
  newIds: readonly string[],
): T[] {
  if (newIds.length === 0) return [...options];
  const isNew = new Set(newIds);
  const fresh = options.filter((m) => isNew.has(m.id));
  if (fresh.length === 0) return [...options];
  return [...fresh, ...options.filter((m) => !isNew.has(m.id))];
}

/** The header's count of new models, worded for a badge. */
export function newCountLabel(count: number): string {
  if (count <= 0) return "";
  return count === 1 ? "1 new" : `${count} new`;
}

/** The badge's tooltip, which says what "new" is measured against. */
export function newCountTitle(provider: string, count: number): string {
  if (count <= 0) return "";
  // "When you last looked", not "the last time you were here": the panel
  // re-discovers on its own (docs/06 §6), so a release can land while the reader
  // is looking straight at it, and "the last time you were here" would then be
  // false in the only sense the reader can check.
  const name = providerLabel(provider) || provider;
  return count === 1
    ? `1 model ${name} did not list when you last looked`
    : `${count} models ${name} did not list when you last looked`;
}

// ── the browser's own store ────────────────────────────────────────────────

/**
 * The panel's baseline on this machine, or nothing where there is no window.
 *
 * The `typeof window` guard is not defensive decoration: this panel is rendered
 * to a string in the test suite, and touching `localStorage` at render time is a
 * crash there and a hydration mismatch anywhere else.
 */
export function loadSeen(): SeenIndex {
  if (typeof window === "undefined" || !window.localStorage) return emptyIndex();
  return readIndex(window.localStorage);
}

export function saveSeen(index: SeenIndex): void {
  if (typeof window === "undefined" || !window.localStorage) return;
  writeIndex(window.localStorage, index);
}
