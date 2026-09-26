/**
 * A one-line summary of a role's raw reply, for a collapsed transcript.
 *
 * Why this exists: measured in the running app, one goal's transcript was 937px tall
 * in a 654px viewport, and two of those pixels belonged to unexpanded JSON — the
 * librarian's and the design role's replies were 228px each, so **half the panel was
 * model output nobody had asked to read**. The transcript rendered every payload at
 * the same weight as the user's own prompt.
 *
 * The pipeline has one orchestrator and eight ordered roles, so a reader wants to know
 * *where the run is* far more than they want the verbatim payload. This returns the
 * sentence that answers that, and leaves the full text one click away.
 *
 * Pure and exported so it can be tested without a DOM: the failure mode here is not a
 * crash, it is a panel that quietly stays 900px tall.
 */

/** Keys worth surfacing, in the order a reader would want them. */
const PREFERRED_KEYS = [
  "summary",
  "message",
  "reason",
  "answer",
  "decision",
  "verdict",
  "title",
  "explanation",
  "detail",
  "text",
];

const MAX = 140;

function clamp(s: string): string {
  const oneLine = s.replace(/\s+/g, " ").trim();
  if (oneLine.length <= MAX) return oneLine;
  return oneLine.slice(0, MAX - 1).trimEnd() + "…";
}

/** First string value among the preferred keys, at any depth. */
function preferredString(value: unknown, depth = 0): string | null {
  if (depth > 3 || value === null || typeof value !== "object") return null;
  const rec = value as Record<string, unknown>;
  for (const key of PREFERRED_KEYS) {
    const v = rec[key];
    if (typeof v === "string" && v.trim()) return v;
  }
  for (const v of Object.values(rec)) {
    const found = preferredString(v, depth + 1);
    if (found) return found;
  }
  return null;
}

/** The top-level keys, for when a payload has nothing readable in it. */
function keyList(value: unknown): string | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const keys = Object.keys(value as Record<string, unknown>);
  return keys.length ? keys.slice(0, 4).join(", ") : null;
}

/**
 * `raw` is whatever the role streamed, which may be a partial fragment, a complete
 * JSON document, or plain prose. Every branch returns a non-empty string, because a
 * disclosure whose summary is blank is worse than no disclosure.
 */
export function replyPreview(raw: string | null | undefined): string {
  const text = (raw ?? "").trim();
  if (!text) return "no output yet";

  const looksStructured =
    text.startsWith("{") || text.startsWith("[") || text.startsWith('"');

  if (looksStructured) {
    try {
      const parsed: unknown = JSON.parse(text);
      return clamp(preferredString(parsed) ?? keyList(parsed) ?? text);
    } catch {
      // A stream is truncated mid-document almost every time it is still arriving, so
      // this branch is the common case rather than an error path. Salvage the value
      // of a preferred key that has opened but not closed:
      //   {"summary": "The workspace contains a READ
      // becomes "The workspace contains a READ" instead of leaking the brace and the
      // key at the reader, which is precisely the noise the disclosure exists to
      // remove.
      const partial = new RegExp(
        `"(${PREFERRED_KEYS.join("|")})"\\s*:\\s*"([^"]*)`,
      ).exec(text);
      if (partial && partial[2].trim()) return clamp(partial[2]);
    }
  }

  const firstLine = text.split("\n").find((l) => l.trim().length > 0);
  return clamp(firstLine ?? text);
}
