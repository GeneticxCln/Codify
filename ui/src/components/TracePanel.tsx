import React, { useCallback, useEffect, useState } from "react";
import { deleteGoalTrace, fetchGoalTrace } from "../api";
import type { TraceSummary } from "../types";
import { promptStorageNote, traceHeadline, traceRoles } from "../traceSummary";
import { Loader2, Radio, Trash2, X } from "lucide-react";

/**
 * One goal's recording: what it holds, and the only control that removes it.
 *
 * Opened on demand rather than rendered into every card, because a recording
 * is a copy of the model's output about the user's code and the panel that
 * lists it should be something they asked to see. Deleting is offered here
 * and nowhere else — it is the user's call, always allowed, and it names what
 * it will remove before it does.
 */
export const TracePanel: React.FC<{
  goalId: string;
  onClose: () => void;
}> = ({ goalId, onClose }) => {
  const [summary, setSummary] = useState<TraceSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setError(null);
      setSummary(await fetchGoalTrace(goalId));
    } catch (err: any) {
      setError(err?.message || "Could not read this run's recording.");
    }
  }, [goalId]);

  useEffect(() => {
    let live = true;
    fetchGoalTrace(goalId)
      .then((s) => {
        if (live) setSummary(s);
      })
      .catch((err: any) => {
        if (live) setError(err?.message || "Could not read this run's recording.");
      });
    return () => {
      live = false;
    };
  }, [goalId, load]);

  const handleDelete = async () => {
    if (!summary) return;
    const sure = window.confirm(
      `Delete this run's recording?\n\n${summary.calls} recorded model call${
        summary.calls === 1 ? "" : "s"
      } will be removed. The goal, its events and your files are not touched.`,
    );
    if (!sure) return;
    setBusy(true);
    try {
      await deleteGoalTrace(goalId);
      await load();
    } catch (err: any) {
      setError(err?.message || "Could not delete the recording.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-codify-bg border border-codify-border rounded-xl p-3 mt-2">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-xs font-semibold text-gray-400 uppercase tracking-wider">
          <Radio className="w-3.5 h-3.5 text-amber-400" />
          {summary ? traceHeadline(summary) : "Recording"}
        </div>
        <div className="flex items-center gap-1">
          {summary && summary.calls > 0 && (
            <button
              type="button"
              onClick={handleDelete}
              disabled={busy}
              className="p-1 text-gray-500 hover:text-red-400 rounded-lg transition-colors disabled:opacity-40"
              title="Delete this recording (the goal and your files are kept)"
              aria-label="Delete recording"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-gray-500 hover:text-gray-300 rounded-lg transition-colors"
            title="Close"
            aria-label="Close recording panel"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {error && <div className="mt-2 text-xs text-red-400">{error}</div>}

      {!error && !summary && (
        <div className="mt-2 flex items-center gap-2 text-xs text-gray-500">
          <Loader2 className="w-3.5 h-3.5 animate-spin" /> Reading…
        </div>
      )}

      {summary && summary.calls === 0 && !error && (
        <p className="mt-2 text-xs text-gray-500">
          {summary.recording_error ? (
            <span className="text-amber-300">
              Recording failed: {summary.recording_error}
            </span>
          ) : (
            <>
              This run recorded no model calls — either it was not armed, or the
              provider never answered.
            </>
          )}
        </p>
      )}

      {summary && summary.calls > 0 && (
        <>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {traceRoles(summary).map(([role, n]) => (
              <span
                key={role}
                className="text-2xs font-mono px-1.5 py-0.5 rounded bg-codify-surface border border-codify-border text-gray-300"
                title={`${n} call${n === 1 ? "" : "s"} from the ${role}`}
              >
                {role} × {n}
              </span>
            ))}
          </div>

          <p className="mt-2 text-xs text-gray-500">
            {promptStorageNote(summary.prompts_kept)}
          </p>

          <div className="mt-2 max-h-48 overflow-y-auto">
            <table className="w-full text-xs font-mono">
              <thead className="text-gray-500 text-left">
                <tr>
                  <th className="pr-2 py-0.5 font-normal">#</th>
                  <th className="pr-2 py-0.5 font-normal">role</th>
                  <th className="pr-2 py-0.5 font-normal">model</th>
                  <th className="pr-2 py-0.5 font-normal">prompt</th>
                  <th className="pr-2 py-0.5 font-normal text-right">tok</th>
                  <th className="py-0.5 font-normal text-right">ms</th>
                </tr>
              </thead>
              <tbody className="text-gray-300">
                {summary.recorded.map((call) => (
                  <tr key={call.seq} className="border-t border-codify-surface">
                    <td className="pr-2 py-0.5 text-gray-500">{call.seq}</td>
                    <td className="pr-2 py-0.5">{call.role}</td>
                    <td className="pr-2 py-0.5 text-gray-500">{call.model}</td>
                    <td
                      className="pr-2 py-0.5 text-gray-500"
                      title={`Digest of the prompt this call was sent — the identity a replay matches on.${
                        summary.prompts_kept ? " The text itself is stored." : ""
                      }`}
                    >
                      {call.prompt_hash.slice(0, 8)}…
                    </td>
                    <td className="pr-2 py-0.5 text-right text-gray-400">
                      {(call.input_tokens ?? 0) + (call.output_tokens ?? 0)}
                    </td>
                    <td className="py-0.5 text-right text-gray-400">
                      {call.duration_ms ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
};
