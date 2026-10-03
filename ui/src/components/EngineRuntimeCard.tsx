/**
 * What the engine is running as, and what that interpreter can import.
 *
 * Every optional capability here is installed into *an* environment, and the
 * engine runs in whichever one something spawned. Those are not guaranteed to be
 * the same place, and nothing raises when they differ — the feature just quietly
 * does not work. The Laya gate is the sharpest case: the SDK in `.venv` makes
 * `make test` fast and leaves a shell-spawned engine unable to import it, so
 * every goal quietly pays the fallback model's latency and tokens. There is no
 * error to look for; the only evidence is a slowness nobody connected to a
 * missing package.
 *
 * So this card names the interpreter. It sits beside the gate card rather than
 * inside it because the two answer different questions: that one is *which
 * engine is gating goals*, this one is *what this engine is*. A gate can be off
 * because it was told to be, or because the package is not there, and the fix
 * for one is deleting a line of config and for the other is a `pip install`.
 *
 * The wording lives in `engineRuntime.ts` so it can be tested without a DOM; this
 * file is the layout around it.
 */
import React, { useEffect, useState } from "react";
import { AlertTriangle, Cpu, HardDriveDownload } from "lucide-react";

import { getEngineRuntime } from "../api";
import type { EngineRuntime } from "../types";
import { RuntimeTone, runtimeLines } from "../engineRuntime";

const TONE_CLASS: Record<RuntimeTone, string> = {
  normal: "text-codify-primary",
  muted: "text-codify-muted",
  success: "text-codify-success",
  warning: "text-codify-warning",
};

export const EngineRuntimeCard: React.FC = () => {
  const [report, setReport] = useState<EngineRuntime | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getEngineRuntime()
      .then((r) => {
        if (!cancelled) setReport(r);
      })
      .catch(() => {
        // A self-check that could not run is not a healthy engine, and saying so
        // is the difference between "unknown" and "fine". It is the same state
        // the gate card reports for its own route.
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (failed) {
    return (
      <div className="flex items-start gap-2 text-xs text-codify-muted bg-codify-surface border border-codify-border rounded-lg p-3">
        <AlertTriangle className="w-4 h-4 text-codify-warning shrink-0 mt-0.5" />
        <span>Engine runtime unavailable — the engine did not answer /settings/runtime.</span>
      </div>
    );
  }

  // Nothing to say yet, and nothing wrong: a card that renders an empty shell on
  // every settings open is a card that gets ignored.
  if (!report) return null;

  const lines = runtimeLines(report);
  const warnings = report.warnings ?? [];

  return (
    <div className="flex flex-col gap-1.5 bg-codify-surface border border-codify-border rounded-lg p-3">
      <div
        className={`flex items-center gap-2 text-xs font-semibold ${
          warnings.length ? "text-codify-warning" : "text-codify-primary"
        }`}
      >
        <HardDriveDownload className="w-4 h-4" />
        Engine runtime
        {warnings.length ? " — needs attention" : ""}
      </div>
      {lines.map((line) => (
        <div key={line.label} className="text-xs flex items-start gap-1.5">
          <Cpu className="w-3.5 h-3.5 shrink-0 mt-0.5 text-codify-muted" />
          <span className="min-w-0 break-all">
            <span className="text-codify-muted">{line.label}: </span>
            <span className={TONE_CLASS[line.tone]}>{line.value}</span>
          </span>
        </div>
      ))}
      {warnings.map((warning) => (
        <p
          key={warning}
          className="text-xs text-codify-warning-ink bg-codify-warning/20 border border-codify-warning/60 rounded-sm p-2"
        >
          {warning}
        </p>
      ))}
    </div>
  );
};
