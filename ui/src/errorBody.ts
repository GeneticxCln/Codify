/**
 * How the client reads what the engine sends.
 *
 * `engine/models.py`'s `ErrorBody` is the declared owner of the refusal shape:
 * `{code, message}`, plus whatever the refusal attaches. Pure and separate from
 * `api.ts` for the reason `stageMetrics` and `traceSummary` are: what the client
 * *claims* about a failure is a claim about the engine's contract, and a claim
 * that only exists inside a function wrapped around a `fetch` cannot be
 * asserted on its own.
 *
 * That is not hypothetical. This reading lived in `engineError` and was untestable
 * — importing `api.ts` needs a `localStorage`, so no test could reach it — and it
 * drifted: it understood `detail` only as a *string*, while a rejected request
 * body arrives as `{"detail": [...]}`. The result was that a validation failure
 * matched nothing and reached the user as a bare "HTTP 422", the one failure
 * shape with no explanation at all. The engine now answers a rejected body in
 * the same shape as every other refusal, and this is where that is read.
 */

/** What the engine sent, read from a refusal body. */
export interface EngineErrorBody {
  /** The stable code a caller switches on — `version_conflict`, `trace_locked`. */
  code: string;
  /** A sentence for a person. Empty when the body carried none. */
  message: string;
  /**
   * Whatever else the refusal attached, kept rather than stringified away.
   *
   * `ErrorBody` allows extras on purpose, and the routes use them: a
   * `workspace_not_empty` refusal carries the goal count so a confirm dialog can
   * name the cascade, and a rejected body carries its field errors under
   * `detail`. A message flattened to a string would lose the one number the user
   * needs before agreeing to delete anything, and the one place that says which
   * field they got wrong.
   */
  extra: Record<string, unknown>;
}

/**
 * Read a refusal body. Never throws: a body that is not the declared shape
 * yields empty strings, and the caller falls back to the HTTP status, so a
 * failure is still reported rather than shown as nothing.
 */
export function readErrorBody(body: unknown): EngineErrorBody {
  if (!body || typeof body !== "object") {
    return { code: "", message: "", extra: {} };
  }
  const record = body as Record<string, unknown>;
  const code = typeof record.code === "string" ? record.code : "";
  let message = typeof record.message === "string" ? record.message : "";
  // A `detail` that is a *string* is an older engine answering a rejected body,
  // read for backwards compatibility: a machine mid-upgrade should not regress
  // to a bare "HTTP 422". The array form is not read — the `message` beside it
  // already names the fields that were rejected, and `detail` survives in `extra`
  // for a caller that wants the whole report.
  if (!message && typeof record.detail === "string") message = record.detail;
  // Only `code` and `message` are lifted out. `detail` in particular stays: a
  // rejected body carries the field-level errors there, and the engine keeps them
  // on purpose — dropping them here would undo, on the client side, the one
  // thing handling the rejection in our shape was for.
  const { code: _c, message: _m, ...rest } = record;
  return { code, message, extra: rest };
}
