import React, { useCallback, useEffect, useState } from "react";
import { deleteGoalTrace, fetchGoalTrace } from "../api";
import type { Goal, TraceSummary } from "../types";
import { canArmTrace, promptStorageNote, traceHeadline, traceRoles } from "../traceSummary";
import { CircleDot, Loader2, Radio, Trash2, X } from "lucide-react";
import { readRejection } from "../rejection.ts";

/**
 * One goal's recording: what it holds, and the only control that removes it.
 *
 * Opened on demand rather than rendered into every card, because a recording
 * is a copy of the model's output about the user's code and the panel that
 * lists it should be something they asked to see. Deleting is offered here
 * and nowhere else — it is the user's call, always allowed, and it names what
 * it will remove before it does.
 *
 * Arming and disarming live here too, because this is the one surface that
 * talks about recordings: the engine can turn recording on for a goal that has
 * not started, and a control that exists only on the command bar at creation
 * time cannot reach a goal the user decided to record a moment later.
 */
export const TracePanel: React.FC<{
  goalId: string;
  /** The goal itself: the panel says what recording is possible, and that is a
   * function of the goal's status and its armed flag, not of the recording. */
  goal: Goal;
  onSetTrace: (enabled: boolean) => void | Promise<void>;
  onClose: () => void;
}> = ({ goalId, goal, onSetTrace, onClose }) => {
  const [summary, setSummary] = useState<TraceSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState<boolean>(Boolean(goal.trace));

  // The parent owns the goal, so an arming done here has to show up even if the
  // panel is reopened from a stale copy of it.
  useEffect(() => {
    setArmed(Boolean(goal.trace));
  }, [goal.trace]);

  const armable = canArmTrace(goal.status);

  const load = useCallback(async () => {
    try {
      setError(null);
      setSummary(await fetchGoalTrace(goalId));
    } catch (err: any) {
      setError(readRejection(err, "Could not read this run's recording."));
    }
  }, [goalId]);

  useEffect(() => {
    let live = true;
    fetchGoalTrace(goalId)
      .then((s) => {
        if (live) setSummary(s);
      })
      .catch((err: any) => {
        if (live) setError(readRejection(err, "Could not read this run's recording."));
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
      setError(readRejection(err, "Could not delete the recording."));
    } finally {
      setBusy(false);
    }
  };

  const handleToggleRecording = async (enabled: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await onSetTrace(enabled);
      setArmed(enabled);
    } catch (err: any) {
      setError(readRejection(err, "Could not change this run's recording."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-codify-bg border border-codify-border rounded-xl p-3 mt-2">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-xs font-semibold text-codify-muted uppercase tracking-wider">
          <Radio className="w-3.5 h-3.5 text-codify-warning" />
          {summary ? traceHeadline(summary) : "Recording"}
        </div>
        <div className="flex items-center gap-1">
          {armed ? (
            <button
              type="button"
              onClick={() => handleToggleRecording(false)}
              disabled={busy}
              className="p-1 text-codify-muted hover:text-codify-warning rounded-lg transition-colors disabled:opacity-40"
              title="Stop recording this run's model calls"
              aria-label="Stop recording"
            >
              <CircleDot className="w-4 h-4" />
            </button>
          ) : armable ? (
            <button
              type="button"
              onClick={() => handleToggleRecording(true)}
              disabled={busy}
              className="p-1 text-codify-muted hover:text-codify-warning rounded-lg transition-colors disabled:opacity-40"
              title="Record this run's model calls — it has not started, so every call it makes will be kept"
              aria-label="Start recording"
            >
              <Radio className="w-4 h-4" />
            </button>
          ) : null}
          {summary && summary.calls > 0 && (
            <button
              type="button"
              onClick={handleDelete}
              disabled={busy}
              className="p-1 text-codify-muted hover:text-codify-danger rounded-lg transition-colors disabled:opacity-40"
              title="Delete this recording (the goal and your files are kept)"
              aria-label="Delete recording"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-codify-muted hover:text-codify-secondary rounded-lg transition-colors"
            title="Close"
            aria-label="Close recording panel"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {error && <div className="mt-2 text-xs text-codify-danger">{error}</div>}

      {!error && !summary && (
        <div className="mt-2 flex items-center gap-2 text-xs text-codify-muted">
          <Loader2 className="w-3.5 h-3.5 animate-spin" /> Reading…
        </div>
      )}

      {summary && summary.calls === 0 && !error && (
        <p className="mt-2 text-xs text-codify-muted">
          {summary.recording_error ? (
            <span className="text-codify-warning">
              Recording failed: {summary.recording_error}
            </span>
          ) : armed ? (
            <>This run is recording, but no model call has been kept yet.</>
          ) : (
            <>
              This run recorded no model calls — it was not armed
              {!armable && ", and it has already started, so it cannot be armed now"}.
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
                className="text-2xs font-mono px-1.5 py-0.5 rounded-sm bg-codify-surface border border-codify-border text-codify-secondary"
                title={`${n} call${n === 1 ? "" : "s"} from the ${role}`}
              >
                {role} × {n}
              </span>
            ))}
          </div>

          <p className="mt-2 text-xs text-codify-muted">
            {promptStorageNote(summary.prompts_kept)}
          </p>

          <div className="mt-2 max-h-48 overflow-y-auto">
            <table className="w-full text-xs font-mono">
              <thead className="text-codify-muted text-left">
                <tr>
                  <th className="pr-2 py-0.5 font-normal">#</th>
                  <th className="pr-2 py-0.5 font-normal">role</th>
                  <th className="pr-2 py-0.5 font-normal">model</th>
                  <th className="pr-2 py-0.5 font-normal">prompt</th>
                  <th className="pr-2 py-0.5 font-normal text-right">tok</th>
                  <th className="py-0.5 font-normal text-right">ms</th>
                </tr>
              </thead>
              <tbody className="text-codify-secondary">
                {summary.recorded.map((call) => (
                  <tr key={call.seq} className="border-t border-codify-surface">
                    <td className="pr-2 py-0.5 text-codify-muted">{call.seq}</td>
                    <td className="pr-2 py-0.5">{call.role}</td>
                    <td className="pr-2 py-0.5 text-codify-muted">{call.model}</td>
                    <td
                      className="pr-2 py-0.5 text-codify-muted"
                      title={`Digest of the prompt this call was sent — the identity a replay matches on.${
                        summary.prompts_kept ? " The text itself is stored." : ""
                      }`}
                    >
                      {call.prompt_hash.slice(0, 8)}…
                    </td>
                    <td className="pr-2 py-0.5 text-right text-codify-muted">
                      {(call.input_tokens ?? 0) + (call.output_tokens ?? 0)}
                    </td>
                    <td className="py-0.5 text-right text-codify-muted">
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
