/**
 * The rules a recording panel reads its own text from.
 *
 * Pure and separate from `TracePanel` for the same reason `stageMetrics` is:
 * what the panel *says* about a recording is a claim about the user's data
 * ("digests only", "7 calls"), and a claim that only exists inside a
 * component's render cannot be asserted on its own.
 */
import type { TraceSummary } from "./types.ts";

/** "7 calls recorded", or the honest zero — never "0 calls" dressed as data. */
export function traceHeadline(summary: TraceSummary): string {
  if (summary.calls === 0) return "No calls recorded";
  return `${summary.calls} call${summary.calls === 1 ? "" : "s"} recorded`;
}

/**
 * Roles by how often they were called, most first, ties broken by name so the
 * list is stable between renders rather than following object key order.
 */
export function traceRoles(summary: TraceSummary): Array<[string, number]> {
  return Object.entries(summary.by_role).sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
  );
}

/**
 * What the recording holds of the prompt itself.
 *
 * Full prompts and replies are kept by default when a run is recorded; setting
 * `CODIFY_TRACE_PROMPTS=0` omits prompt text and keeps digests + replies only.
 */
export function promptStorageNote(promptsKept: boolean): string {
  return promptsKept
    ? "Full prompts and replies kept — click any call below to inspect its context."
    : "Digests only — prompt text was omitted (CODIFY_TRACE_PROMPTS=0).";
}

/**
 * Render a stored prompt or reply body for human inspection: conductor message
 * arrays and structured tool-call replies are JSON-encoded on write, so pretty-
 * printing valid JSON objects/arrays makes multi-turn context readable while
 * leaving plain prose and system prompts untouched.
 */
export function formatTracePayload(raw: string | null | undefined): string {
  if (raw === null || raw === undefined || raw === "") return "";
  const trimmed = raw.trim();
  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  ) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (typeof parsed === "object" && parsed !== null) {
        return JSON.stringify(parsed, null, 2);
      }
    } catch {
      /* ordinary text that starts with a bracket — return verbatim */
    }
  }
  return raw;
}

/**
 * Whether this goal can still be armed for recording: only a goal that has
 * not started. The engine enforces the same rule (`trace_locked`); this is so
 * the control is not offered for a refusal the user cannot act on.
 */
export function canArmTrace(status: string): boolean {
  return status === "PLANNING";
}
