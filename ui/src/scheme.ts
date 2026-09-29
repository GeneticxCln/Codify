import { THEMES, type AppearanceTheme } from "./appearance";
import { readRejection } from "./rejection";
import { isTintable, type TintMap, type TintStore } from "./tint";

/**
 * Colour schemes: a theme's custom colours, in a file you can hand to someone.
 *
 * ## Why there is a format at all, rather than sharing the store
 *
 * The store in `tint.ts` is keyed by **theme id** — `toxic-lab` — because that
 * is the only key the app can resolve without asking a human. That is exactly
 * what makes it a bad wire format: a person opening the file sees
 * `{"toxic-lab": {"--reagent": "#8b1e2d"}}` and learns nothing. So a scheme
 * carries a **label** beside every id, and a **version**, and a name derived
 * from what is inside it. The id is still there because it is the exact match;
 * the label is there because the file is read by people, sometimes without this
 * app.
 *
 * ## Why import reports rather than refuses
 *
 * A scheme is one machine's answer to "what did I do to my themes", and the
 * receiving machine has a different set of themes. Matching on id, then on
 * label, then giving up on that entry, is the difference between a feature and
 * a file that only works between two identical installs. The entries that did
 * not match are *named* in the result: a scheme that half-applied and said
 * nothing looks exactly like one that fully applied, and the user is the only
 * one who can tell the difference.
 *
 * ## Why nothing here clamps
 *
 * Storing a raw proposal and clamping at apply time is what `tint.ts` already
 * does, and a scheme has to obey the same rule. A colour that is perfectly
 * usable on a near-black theme may be unusable on a lighter one, and the right
 * answer is the one the local guard gives — the receiver's own contrast rule,
 * not the sender's. Clamping on import would bake the sender's surfaces into
 * the receiver's palette.
 *
 * ## Why the file is not a security boundary
 *
 * A scheme is data a user chose to open. It is parsed defensively — every value
 * that is not a hex is dropped, every theme that ends up with nothing is
 * dropped, and a version from the future is refused with a reason rather than
 * guessed at — but it is not sandboxed, because it arrives from the user's own
 * disk and there is nothing to sandbox it from.
 */

/** The discriminator, so a random JSON file is not mistaken for a scheme. */
export const SCHEME_KIND = "codify-scheme";

/** Bumped only for a shape this file cannot read. See `decodeScheme`. */
export const SCHEME_VERSION = 1;

/** One theme's overrides, with the name a person would recognise. */
export interface SchemeThemeEntry {
  /** The id the app resolves with. */
  id: string;
  /** The theme's own label, so the file is readable without this app. */
  label: string;
  /** `var -> #rrggbb`. A variable this build does not offer is dropped later. */
  tints: TintMap;
}

export interface Scheme {
  kind: typeof SCHEME_KIND;
  version: typeof SCHEME_VERSION;
  /** Derived from the contents, so the file names what it is. */
  name: string;
  themes: ReadonlyArray<SchemeThemeEntry>;
}

/**
 * What a scheme is called, from what is in it.
 *
 * One theme borrows that theme's own name, because "Toxic Lab" is a better name
 * for a file than "1 custom themes" and costs nothing. Several get a count,
 * which is the only fact about a bundle that is worth stating in its filename.
 */
function schemeName(themes: ReadonlyArray<SchemeThemeEntry>): string {
  if (themes.length === 0) return "No custom colours";
  if (themes.length === 1) return themes[0]!.label;
  return `${themes.length} custom themes`;
}

/**
 * The store as a scheme: every theme that has at least one tint.
 *
 * In `THEMES` order rather than in the order the store's keys happen to be in,
 * because two exports of the same settings should be the same bytes. A file
 * that reorders itself on every export cannot be diffed, and a scheme is
 * plausibly kept in a repository next to the thing it themes.
 *
 * An empty store produces a scheme with no themes, which `decodeScheme` then
 * refuses — so the export button and the import check cannot disagree about
 * whether "nothing customised" is a thing worth writing to a file.
 */
export function encodeScheme(store: TintStore): Scheme {
  const themes: SchemeThemeEntry[] = [];
  for (const theme of THEMES) {
    const tints = store[theme.id];
    if (!tints) continue;
    const kept = keepTints(tints);
    if (Object.keys(kept).length === 0) continue;
    themes.push({ id: theme.id, label: theme.label, tints: kept });
  }
  return { kind: SCHEME_KIND, version: SCHEME_VERSION, name: schemeName(themes), themes };
}

/** The hexes out of a map, dropping everything that is not one. */
function keepTints(map: Readonly<Record<string, unknown>>): TintMap {
  const out: Record<string, string> = {};
  for (const [name, value] of Object.entries(map)) {
    if (typeof value === "string" && isTintable(value)) out[name] = value.toLowerCase();
  }
  return out;
}

/** Why a file was refused, so the pane can say something a person can act on. */
export type DecodeFailure = "not-a-scheme" | "newer-version" | "empty";

export type Decoded =
  | { ok: true; scheme: Scheme }
  | { ok: false; reason: DecodeFailure };

/**
 * Read a scheme out of untrusted JSON, or say why not.
 *
 * The three refusals are three different sentences for a person, which is why
 * they are three values and not one `false`: a file that is not a scheme at all
 * is a wrong file, a file from a **newer** version is one this build cannot
 * honestly read, and a well-formed scheme with nothing in it is a scheme that
 * would import as a silent no-op. The last one is refused rather than applied
 * because the most confusing possible outcome of an import is the app looking
 * exactly as it did before.
 */
export function decodeScheme(value: unknown): Decoded {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return { ok: false, reason: "not-a-scheme" };
  }
  const record = value as Record<string, unknown>;
  if (record.kind !== SCHEME_KIND) return { ok: false, reason: "not-a-scheme" };
  if (typeof record.version !== "number") return { ok: false, reason: "not-a-scheme" };
  if (record.version > SCHEME_VERSION) return { ok: false, reason: "newer-version" };
  if (!Array.isArray(record.themes)) return { ok: false, reason: "not-a-scheme" };

  const themes: SchemeThemeEntry[] = [];
  for (const raw of record.themes) {
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) continue;
    const entry = raw as Record<string, unknown>;
    const id = typeof entry.id === "string" ? entry.id : "";
    const label = typeof entry.label === "string" ? entry.label : "";
    // The id is what resolves a theme; the label is only there to be read. An
    // entry with neither is not a theme, and an entry whose tints are all
    // corruption is a theme with nothing to say — both are dropped rather than
    // carried as empty, so `empty` means "this file has no colours in it".
    if (!id && !label) continue;
    const tints =
      typeof entry.tints === "object" && entry.tints !== null && !Array.isArray(entry.tints)
        ? keepTints(entry.tints as Record<string, unknown>)
        : {};
    if (Object.keys(tints).length === 0) continue;
    const theme = matchTheme(id, label);
    themes.push({ id: theme?.id ?? id, label: theme?.label ?? label, tints });
  }
  if (themes.length === 0) return { ok: false, reason: "empty" };
  return {
    ok: true,
    scheme: {
      kind: SCHEME_KIND,
      version: SCHEME_VERSION,
      name: typeof record.name === "string" && record.name.trim().length > 0
        ? record.name
        : schemeName(themes),
      themes,
    },
  };
}

/**
 * Find the local theme an entry is about: **id first, then label.**
 *
 * Both, and in that order, because they fail in opposite directions. The id is
 * exact and survives a rename, but a scheme from a build where the theme was
 * called something else will not match on it. The label is what a person sees
 * and what they would retype, but it is a string and strings change. Trying the
 * id first means a rename on either side still matches while the other side's
 * id is unchanged, and the label is the fallback for the case where it is not.
 */
function matchTheme(id: string, label: string): AppearanceTheme | undefined {
  if (id) {
    const byId = THEMES.find((t) => t.id === id);
    if (byId) return byId;
  }
  if (label) {
    const wanted = label.trim().toLowerCase();
    return THEMES.find((t) => t.label.trim().toLowerCase() === wanted);
  }
  return undefined;
}

/** What an import did, and what it could not do. Both halves are for the user. */
export interface ImportReport {
  /** Themes this build has, that were replaced by the scheme's values. */
  applied: ReadonlyArray<{ label: string; count: number }>;
  /** Entries the scheme carried that this build cannot use, and why. */
  skipped: ReadonlyArray<{ label: string; reason: "no-such-theme" }>;
}

/**
 * Fold a scheme into a store, and report what happened.
 *
 * **Replace within a theme, merge across themes.** An incoming theme's tints
 * replace that theme's own entirely, because "apply this scheme" means the
 * scheme's idea of the theme, and a merge would leave a colour behind that the
 * person sharing it never chose. Themes the scheme does not mention are left
 * exactly as they were, so importing a two-theme file cannot quietly clear the
 * other seventeen.
 *
 * Raw, not resolved: the incoming values go in as the sender wrote them and the
 * local contrast guard runs when they are applied, which is the same path a
 * locally-picked colour takes. Storing a resolved value would freeze this
 * machine's surfaces into a file that travels to another one.
 */
export function mergeScheme(
  store: TintStore,
  scheme: Scheme,
): { store: TintStore; report: ImportReport } {
  const next: Record<string, TintMap> = { ...store };
  const applied: Array<{ label: string; count: number }> = [];
  const skipped: Array<{ label: string; reason: "no-such-theme" }> = [];

  for (const entry of scheme.themes) {
    const theme = matchTheme(entry.id, entry.label);
    if (!theme) {
      skipped.push({ label: entry.label || entry.id || "(unnamed)", reason: "no-such-theme" });
      continue;
    }
    // Stored under *this build's* id, which is the one `select` and
    // `applyTintedTheme` will read it back with. A scheme that matched by label
    // arrived carrying an id this build does not use, and keeping that one
    // would produce a store whose keys nothing resolves.
    const tints = keepTints(entry.tints);
    if (Object.keys(tints).length === 0) continue;
    next[theme.id] = tints;
    applied.push({ label: theme.label, count: Object.keys(tints).length });
  }
  return { store: next, report: { applied, skipped } };
}

/**
 * The filename, following the audit export's `kind-slug-stamp` shape.
 *
 * `now` is a parameter rather than a hidden `new Date()` so the test can assert
 * a name instead of a pattern. The date is in `YYYY-MM-DD` and not the audit's
 * full timestamp because a colour scheme is not an event log: two exports of the
 * same settings on the same day are the same file, and that is a property worth
 * having.
 */
export function schemeFilename(now: Date = new Date()): string {
  const stamp = now.toISOString().slice(0, 10);
  return `codify-colours-${stamp}.json`;
}

/** The slice of the DOM `downloadJson` needs, so a test can pass a fake. */
export interface DownloadTarget {
  document: Pick<Document, "createElement">;
  createObjectURL: (blob: Blob) => string;
  revokeObjectURL: (url: string) => void;
}

/**
 * Save a scheme as a file.
 *
 * The `<a download>` + object-URL shape is already used twice in this app — the
 * audit export in `ChatTimeline` and the stats export in `StatsPanel` — and this
 * is the third. It is written here rather than imported because those two live
 * in files other work is happening in, and consolidating them is a separate
 * change to two components this one does not own.
 */
export function downloadScheme(
  scheme: Scheme,
  target: DownloadTarget,
  now: Date = new Date(),
): string {
  const blob = new Blob([JSON.stringify(scheme, null, 2)], { type: "application/json" });
  const url = target.createObjectURL(blob);
  const anchor = target.document.createElement("a") as HTMLAnchorElement;
  anchor.href = url;
  anchor.download = schemeFilename(now);
  anchor.click();
  target.revokeObjectURL(url);
  return anchor.download;
}

/** The slice of the clipboard this module touches. */
export interface CopyTarget {
  writeText?: (text: string) => Promise<void>;
  /** `execCommand` is on `Document` but not on the `Pick`, so it is named. */
  document?: Pick<Document, "createElement" | "body"> & { execCommand?: (c: string) => boolean };
}

export type CopyOutcome = "copied" | "copied-by-fallback" | "failed";

/**
 * Put text on the clipboard, and say whether it worked.
 *
 * Two mechanisms because one is not enough. `navigator.clipboard` is the right
 * API and it is unavailable in more places than it should be — a webview
 * without the permission, a page served over something the browser does not
 * consider a secure context — and when it is unavailable it is unavailable
 * *silently*, so a feature that only calls it can appear to work and copy
 * nothing. The `execCommand` fallback is deprecated and always will be, and it
 * still works everywhere the first one does not.
 *
 * The outcome is a value rather than a boolean because the pane says something
 * different for each: a fallback copy did happen, so it is not a failure, but
 * the next click may not repeat it, and a user who is told "copied" twice and
 * pastes nothing the second time has been lied to by a boolean that collapsed
 * three cases into two.
 */
export async function copySchemeText(
  text: string,
  target: CopyTarget,
): Promise<CopyOutcome> {
  if (target.writeText) {
    try {
      await target.writeText(text);
      return "copied";
    } catch {
      // Fall through to the fallback rather than reporting a failure: the
      // clipboard being unwritable by API is exactly the case it exists for.
    }
  }
  const doc = target.document;
  if (!doc) return "failed";
  const field = doc.createElement("textarea") as HTMLTextAreaElement;
  field.value = text;
  // Off-screen rather than `display: none`: a hidden element cannot be
  // selected, and the copy would then be of nothing.
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.opacity = "0";
  doc.body.appendChild(field);
  try {
    field.select();
    const ok = doc.execCommand?.("copy") ?? false;
    return ok ? "copied-by-fallback" : "failed";
  } catch {
    return "failed";
  } finally {
    doc.body.removeChild(field);
  }
}

/** The sentence for each refusal, next to the parsing that decided it. */
export const DECODE_MESSAGES: Readonly<Record<DecodeFailure, string>> = {
  "not-a-scheme": "That is not a Codify colour scheme.",
  "newer-version": "That scheme came from a newer version of Codify, so this build cannot read it.",
  empty: "That scheme has no custom colours in it.",
};

/**
 * Turn a thrown value into a sentence, for the import path's catch.
 *
 * `readRejection` is the app's one way of turning a rejected promise into
 * something a person can read, and the import is a rejected promise like any
 * other: a file the user cannot read, a paste that was truncated, a
 * `FileReader` that errored. Swallowing it into "import failed" is how a bug in
 * the file path becomes a support question with no answer in it.
 */
export function describeImportFailure(err: unknown): string {
  return readRejection(err, "Could not read that colour scheme.");
}
