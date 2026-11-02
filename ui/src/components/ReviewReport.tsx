/**
 * What a deep review found, and which of its readers actually ran.
 *
 * The payload is the engine's `review_report` event (docs/04, `engine/review.py`), drawn as it arrives: the status line
 * first, because "2 of 3 lenses ran" changes how much an empty list of findings means, then each reader, then the
 * findings merged across them. A reader that did not run is shown as not run, never as a clean pass; the same rule the
 * engine keeps when it counts them.
 *
 * Findings here are evidence handed to the critic, which still decides, so nothing on this card approves or blocks a
 * step. The step's own blocking reasons stay in `review_notes`.
 */

export interface ReviewLensRun {
  lens: string;
  status: string;
  findings: number;
  calls: number;
  duration_ms: number;
  detail?: string;
}

export interface ReviewFinding {
  severity: string;
  category: string;
  scope: string;
  evidence?: string;
  recommendation: string;
  lenses: string[];
}

export interface ReviewReportPayload {
  status: string;
  detail?: string;
  lenses: ReviewLensRun[];
  findings: ReviewFinding[];
}

/** Structural check, so an event from an older or newer engine that lacks the shape draws nothing rather than crashing. */
export function looksLikeReviewReport(value: unknown): value is ReviewReportPayload {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Partial<ReviewReportPayload>;
  if (typeof v.status !== "string" || !Array.isArray(v.lenses) || !Array.isArray(v.findings)) return false;
  // The entries are read field by field below, so one that is not an object, or lacks the fields, would throw while
  // rendering and take the whole timeline with it. An event from another engine version draws nothing instead.
  const lensOk = v.lenses.every(
    (l) => typeof l === "object" && l !== null && typeof l.lens === "string" && typeof l.status === "string",
  );
  const findingOk = v.findings.every(
    (f) =>
      typeof f === "object" && f !== null && typeof f.severity === "string" && typeof f.scope === "string" &&
      typeof f.recommendation === "string" && Array.isArray(f.lenses),
  );
  return lensOk && findingOk;
}

const SEVERITY_STYLES: Record<string, string> = {
  blocker: "text-codify-danger-ink border-codify-danger/30 bg-codify-danger/10",
  major: "text-codify-warning-ink border-codify-warning/30 bg-codify-warning/10",
  nit: "text-codify-muted border-codify-border bg-codify-neutral/10",
};

const NEUTRAL = "text-codify-muted border-codify-border bg-codify-neutral/10";

export function ReviewReport({ report }: { report: ReviewReportPayload }) {
  const notRun = report.lenses.filter((l) => l.status !== "ran");
  return (
    <div className="rounded-xl border border-codify-border bg-codify-bg p-3 space-y-2 text-left" aria-label="Deep review">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs font-semibold text-codify-secondary">Deep review</span>
        <span
          className={`px-2 py-0.5 rounded-full border text-2xs font-semibold ${
            notRun.length > 0 ? SEVERITY_STYLES.major : NEUTRAL
          }`}
        >
          {report.status}
        </span>
      </div>

      {report.detail && <div className="text-xs text-codify-warning-ink">{report.detail}</div>}

      <ul className="flex flex-wrap gap-1.5" aria-label="Reviewers">
        {report.lenses.map((lens) => (
          <li
            key={lens.lens}
            className={`px-2 py-0.5 rounded-full border text-2xs ${
              lens.status === "ran" ? NEUTRAL : SEVERITY_STYLES.major
            }`}
            title={lens.detail || undefined}
          >
            {lens.lens}: {lens.status === "ran" ? `${lens.findings} found` : "not run"}
          </li>
        ))}
      </ul>

      {report.findings.length === 0 ? (
        <div className="text-xs text-codify-muted">No findings from the lenses that ran.</div>
      ) : (
        <ul className="space-y-1.5" aria-label="Findings">
          {report.findings.map((f, i) => (
            <li key={i} className="rounded-lg border border-codify-border p-2.5 space-y-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span
                  className={`px-1.5 py-0.5 rounded-full border text-2xs font-semibold uppercase tracking-wide ${
                    SEVERITY_STYLES[f.severity] ?? NEUTRAL
                  }`}
                >
                  {f.severity}
                </span>
                <span className="font-mono text-xs text-codify-secondary break-all">{f.scope}</span>
                <span className="text-2xs text-codify-muted">
                  {f.category} · {f.lenses.join(", ")}
                </span>
              </div>
              {f.evidence && <div className="text-xs text-codify-muted">{f.evidence}</div>}
              <div className="text-xs text-codify-secondary">{f.recommendation}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
