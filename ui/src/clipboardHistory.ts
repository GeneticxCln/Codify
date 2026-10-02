/**
 * Clipboard history: what passed through this window, kept so it can be used again.
 *
 * ## What it is
 *
 * A bounded list of text a person copied, cut or pasted *inside Codify* (a selection in the transcript, the
 * composer or the terminal; a paste into any of them; a code block's Copy button). It is kept in this
 * window's storage (`CODIFY_CLIPBOARD`) and nowhere else: the engine never sees it, so it has no route, no
 * table and no place in the API. Not the system clipboard: nothing here reads what other applications
 * copy, which is what keeps a password manager's copy out of it, and why it needs no permission from the
 * shell (`docs/09` §11, `docs/03` §1.10).
 *
 * ## The rules
 *
 * - **Bounded, and never cut.** `MAX_CLIPS` unpinned items and `MAX_PINNED` pinned ones, at most
 *   `MAX_CLIP_CHARS` each. A clip over the limit is *refused*, not truncated: half a snippet pasted back is
 *   a silent corruption, and the person is told instead.
 * - **A repeat moves, it does not add.** Copying what is already in the list brings it to the top and keeps
 *   its pin, its id and where it first came from.
 * - **A pin outlasts the limit.** It does not count against `MAX_CLIPS`, and a clear leaves it.
 * - **Never a credential.** Anything credential-shaped is not kept (`looksSecret`), nor anything that came
 *   from a password field or a field that opts out (`isPrivateField`). A clipboard history that remembered
 *   a pasted API key would have made a copy of the one thing the rest of this app is careful never to echo
 *   (`docs/00` §6.4). It is a heuristic, not a guarantee, and it is checked again on the way *out* of
 *   storage, so something that got in by another route is not shown again.
 * - **Remembered, and never trusted.** A bad entry is dropped on its own and never repaired, a list that
 *   cannot be read is an empty list, and storage that throws is an empty list on load and a no-op on save.
 *
 * Pure and DOM-free, so every rule has a test that needs no renderer; `useClipboardHistory.ts` is the thin
 * part that listens and holds the list.
 */

export const CLIPBOARD_KEY = "CODIFY_CLIPBOARD";

/** How many unpinned items are kept; the oldest go first. */
export const MAX_CLIPS = 50;
/** How many items may be pinned. A pin that would pass this is refused. */
export const MAX_PINNED = 20;
/** The longest clip kept. Longer is refused whole, never cut. */
export const MAX_CLIP_CHARS = 10_000;

/** Where an item passed through: a selection copied or cut, a paste, or a code block's own Copy button. */
export type ClipSource = "selection" | "paste" | "code";
const SOURCES: readonly ClipSource[] = ["selection", "paste", "code"];

export interface Clip {
  id: string;
  /** Exactly as it was copied: indentation, blank lines and a trailing newline included. */
  text: string;
  /** When it was last copied or pasted (epoch ms). A repeat updates it. */
  at: number;
  source: ClipSource;
  pinned: boolean;
}

export type SkipReason = "secret" | "private" | "empty" | "too-long";

export type CaptureOutcome = { kind: "kept"; clip: Clip } | { kind: "skipped"; why: SkipReason };

/** The sentence for each way of not keeping something, so a missing item is explained and not a mystery. */
export const SKIP_MESSAGES: Readonly<Record<SkipReason, string>> = {
  secret: "Not kept: it looks like a key, token or other credential.",
  private: "Not kept: it came from a password or private field.",
  empty: "Not kept: it was only whitespace.",
  "too-long": `Not kept: it is longer than ${MAX_CLIP_CHARS.toLocaleString("en-US")} characters, and a clip is never cut.`,
};

export function sourceLabel(source: ClipSource): string {
  return source === "paste" ? "Pasted" : source === "code" ? "Code block" : "Copied";
}

// ── what is never kept ─────────────────────────────────────────────────────

// Shapes that are credentials however they got onto the clipboard. The first three mirror what the engine
// scrubs from anything it says (`engine/providers.py`: `_KEY_TOKEN`, `_BEARER`, `_NAMED_SECRET`); the rest
// are the formats people copy most.
const SECRET_SHAPES: readonly RegExp[] = [
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----/,
  /\bsk-[A-Za-z0-9_-]{8,}/,
  /\bAIza[0-9A-Za-z_-]{20,}/,
  /\bnvapi-[A-Za-z0-9_-]{16,}/,
  /\bgh[pousr]_[A-Za-z0-9]{30,}/,
  /\bgithub_pat_[A-Za-z0-9_]{30,}/,
  /\bxox[abprs]-[A-Za-z0-9-]{10,}/,
  /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/,
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{16,}/i,
  /\bAuthorization\s*:\s*(?:Basic|Token)\s+\S{8,}/i,
];

// A secret-named variable assigned a literal: quoted and long, or unquoted, long, with a digit and followed
// by the end of the token. The digit and the boundary are what keep `api_key = get_api_key_from_config()`
// and `password = some_long_identifier` (code, which is most of what is copied here) from being skipped.
const NAMED_SECRET =
  /\b(?:x-api-key|x-goog-api-key|api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd)\b\s*[:=]\s*(?:["'][A-Za-z0-9+/_.=~-]{12,}["']|(?=[A-Za-z0-9+/_=~-]*\d)[A-Za-z0-9+/_=~-]{20,}(?=[\s"',;)\]}]|$))/i;

// One unbroken run that looks random: long, no whitespace, upper and lower and a digit, and not hex (a git
// hash is copied all day and is not a secret). `/` and `.` are not in the set, so a path or a URL is never
// taken for one. This is what catches a token that has no recognisable prefix, such as the engine's own.
const TOKEN_RUN = /^[A-Za-z0-9_=+-]{32,512}$/;
const HEX_ONLY = /^[0-9a-fA-F-]+$/;

/** Whether text looks like a credential. A heuristic, and deliberately biased towards not keeping. */
export function looksSecret(text: string): boolean {
  if (SECRET_SHAPES.some((shape) => shape.test(text))) return true;
  if (NAMED_SECRET.test(text)) return true;
  const run = text.trim();
  if (TOKEN_RUN.test(run) && !HEX_ONLY.test(run) && /[a-z]/.test(run) && /[A-Z]/.test(run) && /\d/.test(run)) {
    return true;
  }
  return false;
}

/** The slice of an element `isPrivateField` reads, so a test needs no DOM. */
export interface PrivacyTarget {
  type?: string;
  closest?: (selector: string) => unknown;
}

/** A password field, or anything inside an element marked `data-clipboard="off"`: nothing from it is kept. */
export function isPrivateField(target: PrivacyTarget | null): boolean {
  if (!target) return false;
  if (target.type === "password") return true;
  return Boolean(target.closest?.('[data-clipboard="off"]'));
}

// ── reading what was copied ────────────────────────────────────────────────

/** The slice of an element `selectedTextOf` reads. */
export interface FieldLike {
  tagName?: string;
  value?: unknown;
  selectionStart?: number | null;
  selectionEnd?: number | null;
}

/**
 * The text a copy or cut event carries. In a text field it is the field's own selected slice: the page's
 * selection (`getSelection()`) does not reliably include a field's, and reading it there can come back
 * empty. Anywhere else it is the page's selection.
 */
export function selectedTextOf(target: FieldLike | null, windowSelection: string): string {
  if (
    target &&
    (target.tagName === "TEXTAREA" || target.tagName === "INPUT") &&
    typeof target.value === "string" &&
    typeof target.selectionStart === "number" &&
    typeof target.selectionEnd === "number"
  ) {
    return target.value.slice(target.selectionStart, target.selectionEnd);
  }
  return windowSelection;
}

// ── the list ───────────────────────────────────────────────────────────────

/** Pinned first, then the rest; newest first within each. A new array, never the input reordered. */
export function displayOrder(list: readonly Clip[]): Clip[] {
  return [...list].sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.at - a.at);
}

/** The unpinned items cut to `MAX_CLIPS` (the oldest go), the pinned ones untouched. */
function bounded(list: readonly Clip[]): Clip[] {
  const pinned = list.filter((c) => c.pinned);
  const rest = list.filter((c) => !c.pinned).sort((a, b) => b.at - a.at).slice(0, MAX_CLIPS);
  return displayOrder([...pinned, ...rest]);
}

export interface CaptureInput {
  id: string;
  text: string;
  at: number;
  source: ClipSource;
  /** It came from a password field or one that opts out (`isPrivateField`). */
  privateField?: boolean;
}

/** Offer text to the history: the new list, and whether it was kept or why not. */
export function captureClip(
  list: readonly Clip[],
  input: CaptureInput,
): { list: Clip[]; outcome: CaptureOutcome } {
  const skip = (why: SkipReason) => ({ list: [...list], outcome: { kind: "skipped", why } as const });
  if (input.privateField) return skip("private");
  if (input.text.trim().length === 0) return skip("empty");
  if (looksSecret(input.text)) return skip("secret");
  if (input.text.length > MAX_CLIP_CHARS) return skip("too-long");

  const existing = list.find((c) => c.text === input.text);
  if (existing) {
    const moved: Clip = { ...existing, at: input.at };
    // To the front, not left in place for the sort to move: the sort is stable, so a repeat that shares the
    // newest clip's millisecond would otherwise stay behind it.
    return {
      list: bounded([moved, ...list.filter((c) => c.id !== existing.id)]),
      outcome: { kind: "kept", clip: moved },
    };
  }
  const clip: Clip = { id: input.id, text: input.text, at: input.at, source: input.source, pinned: false };
  return { list: bounded([clip, ...list]), outcome: { kind: "kept", clip } };
}

/** Pin or unpin. Pinning past `MAX_PINNED` is refused: the list comes back as it was. */
export function togglePin(list: readonly Clip[], id: string): Clip[] {
  const target = list.find((c) => c.id === id);
  if (!target) return [...list];
  if (!target.pinned && list.filter((c) => c.pinned).length >= MAX_PINNED) return [...list];
  return bounded(list.map((c) => (c.id === id ? { ...c, pinned: !c.pinned } : c)));
}

export function removeClip(list: readonly Clip[], id: string): Clip[] {
  return list.filter((c) => c.id !== id);
}

/** Everything that is not pinned goes. */
export function clearUnpinned(list: readonly Clip[]): Clip[] {
  return list.filter((c) => c.pinned);
}

/** A case-insensitive substring match over the text, in display order. A blank query is everything. */
export function searchClips(list: readonly Clip[], query: string): Clip[] {
  const needle = query.trim().toLowerCase();
  const ordered = displayOrder(list);
  return needle ? ordered.filter((c) => c.text.toLowerCase().includes(needle)) : ordered;
}

// ── how a clip is shown ────────────────────────────────────────────────────

export const PREVIEW_LINES = 4;
export const PREVIEW_COLS = 120;

export interface Preview {
  lines: string[];
  /** Lines the preview left out. */
  hiddenLines: number;
  /** The clip's full length, for the row to say. */
  chars: number;
}

/** The first few lines, indentation kept, each bounded, with a count of what was left out. */
export function previewOf(text: string): Preview {
  const all = text.replace(/\s+$/, "").split("\n");
  const lines = all.slice(0, PREVIEW_LINES).map((line) => {
    const flat = line.replace(/\r$/, "");
    return flat.length <= PREVIEW_COLS ? flat : `${flat.slice(0, PREVIEW_COLS).trimEnd()}…`;
  });
  return { lines, hiddenLines: Math.max(0, all.length - PREVIEW_LINES), chars: text.length };
}

// ── remembered, and never trusted ──────────────────────────────────────────

/** The slice of `Storage` this module uses. */
export interface ClipboardStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function validClip(raw: unknown): Clip | null {
  if (typeof raw !== "object" || raw === null) return null;
  const { id, text, at, source, pinned } = raw as Record<string, unknown>;
  if (typeof id !== "string" || id.length === 0) return null;
  if (typeof text !== "string" || text.trim().length === 0 || text.length > MAX_CLIP_CHARS) return null;
  if (typeof at !== "number" || !Number.isFinite(at)) return null;
  if (typeof source !== "string" || !(SOURCES as readonly string[]).includes(source)) return null;
  if (typeof pinned !== "boolean") return null;
  // Checked again on the way out: a secret that got into storage some other way is not shown again.
  if (looksSecret(text)) return null;
  return { id, text, at, source: source as ClipSource, pinned };
}

/** What is stored, parsed: bad entries dropped, repeats collapsed (a pin survives the collapse), bounded. */
export function parseClips(raw: string | null): Clip[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  // Newest first, so the first of a repeated text is its newest copy; a pin on any of them survives.
  const byText = new Map<string, Clip>();
  for (const entry of parsed.map(validClip).filter((c): c is Clip => c !== null).sort((a, b) => b.at - a.at)) {
    const seen = byText.get(entry.text);
    if (seen) seen.pinned = seen.pinned || entry.pinned;
    else byText.set(entry.text, { ...entry });
  }
  // More pins than the ceiling allows (storage edited, or written by another build): the newest keep theirs
  // and the rest become ordinary items. Nothing is dropped for being pinned too eagerly.
  let pins = 0;
  const kept = [...byText.values()].map((c) => (c.pinned && ++pins > MAX_PINNED ? { ...c, pinned: false } : c));
  return bounded(kept);
}

export function loadClips(storage: ClipboardStorage | null): Clip[] {
  if (!storage) return [];
  try {
    return parseClips(storage.getItem(CLIPBOARD_KEY));
  } catch {
    return [];
  }
}

export function saveClips(storage: ClipboardStorage | null, list: readonly Clip[]): void {
  if (!storage) return;
  try {
    storage.setItem(CLIPBOARD_KEY, JSON.stringify(list));
  } catch {
    // A full or blocked store costs the history its memory, never the window its function.
  }
}
