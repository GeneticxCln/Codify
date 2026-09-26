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
 * Two very different documents behind one endpoint: digests only (the
 * default) cannot be read back, and kept text can. The panel has to say which
 * it is holding rather than let a reader assume a transcript exists.
 */
export function promptStorageNote(promptsKept: boolean): string {
  return promptsKept
    ? "Prompt text kept (CODIFY_TRACE_PROMPTS=1)."
    : "Digests only — the prompt text was not stored.";
}

/**
 * Whether this goal can still be armed for recording: only a goal that has
 * not started. The engine enforces the same rule (`trace_locked`); this is so
 * the control is not offered for a refusal the user cannot act on.
 */
export function canArmTrace(status: string): boolean {
  return status === "PLANNING";
}
