import React, { useCallback, useEffect, useRef, useState } from "react";
import { deleteGoalTrace, fetchGoalTrace, fetchGoalTraceCall, replayGoalTrace } from "../api";
import { confirmDialog } from "../appDialog";
import type { Goal, TraceCallDetail, TraceReplayReport, TraceSummary } from "../types";
import {
  canArmTrace,
  formatTracePayload,
  promptStorageNote,
  traceHeadline,
  traceRoles,
} from "../traceSummary";
import { ChevronDown, ChevronRight, CircleDot, Loader2, Play, Radio, Trash2, X } from "lucide-react";
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
  const [selectedSeq, setSelectedSeq] = useState<number | null>(null);
  const [detail, setDetail] = useState<TraceCallDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [replaying, setReplaying] = useState(false);
  const [replay, setReplay] = useState<TraceReplayReport | null>(null);
  const detailReqRef = useRef(0);
  const replayReqRef = useRef(0);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

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
    // A different goal: invalidate any in-flight call-detail or replay responses for the previous goal.
    detailReqRef.current += 1;
    replayReqRef.current += 1;
    setSummary(null);
    setError(null);
    setSelectedSeq(null);
    setDetail(null);
    setDetailError(null);
    setDetailLoading(false);
    setReplaying(false);
    setReplay(null);
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

  const handleSelectCall = async (seq: number) => {
    const req = ++detailReqRef.current;
    if (selectedSeq === seq) {
      setSelectedSeq(null);
      setDetail(null);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }
    setSelectedSeq(seq);
    setDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      const loaded = await fetchGoalTraceCall(goalId, seq);
      if (!mountedRef.current || detailReqRef.current !== req) return;
      setDetail(loaded);
    } catch (err: any) {
      if (!mountedRef.current || detailReqRef.current !== req) return;
      setDetailError(readRejection(err, `Could not read call #${seq}.`));
    } finally {
      if (mountedRef.current && detailReqRef.current === req) {
        setDetailLoading(false);
      }
    }
  };

  const handleDelete = async () => {
    if (!summary) return;
    const sure = await confirmDialog({
      title: "Delete this run's recording?",
      message: `${summary.calls} recorded model call${
        summary.calls === 1 ? "" : "s"
      } will be removed. The goal, its events and your files are not touched.`,
      confirmLabel: "Delete recording",
      tone: "danger",
    });
    if (!sure) return;
    setBusy(true);
    try {
      await deleteGoalTrace(goalId);
      detailReqRef.current += 1;
      replayReqRef.current += 1;
      setSelectedSeq(null);
      setDetail(null);
      setDetailError(null);
      setDetailLoading(false);
      setReplaying(false);
      setReplay(null);
      await load();
    } catch (err: any) {
      setError(readRejection(err, "Could not delete the recording."));
    } finally {
      setBusy(false);
    }
  };

  const handleReplay = async () => {
    const req = ++replayReqRef.current;
    setReplaying(true);
    setError(null);
    setReplay(null);
    try {
      const result = await replayGoalTrace(goalId);
      if (!mountedRef.current || replayReqRef.current !== req) return;
      setReplay(result);
    } catch (err: any) {
      if (!mountedRef.current || replayReqRef.current !== req) return;
      setError(readRejection(err, "Could not replay this run's recording."));
    } finally {
      if (mountedRef.current && replayReqRef.current === req) {
        setReplaying(false);
      }
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
            <>
              <button
                type="button"
                onClick={() => void handleReplay()}
                disabled={busy || replaying}
                className="flex items-center gap-1 px-2 py-0.5 text-2xs text-codify-secondary hover:text-codify-primary bg-codify-surface border border-codify-border rounded-md transition-colors disabled:opacity-40"
                title="Replay this recording in a scratch copy without calling a provider"
                aria-label="Replay recording"
              >
                {replaying ? <Loader2 className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />}
                <span>{replaying ? "Replaying…" : "Replay"}</span>
              </button>
              <button
                type="button"
                onClick={handleDelete}
                disabled={busy || replaying}
                className="p-1 text-codify-muted hover:text-codify-danger rounded-lg transition-colors disabled:opacity-40"
                title="Delete this recording (the goal and your files are kept)"
                aria-label="Delete recording"
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </>
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

      {replay && (
        <div
          data-trace-replay={replay.matched ? "matched" : "diverged"}
          className="mt-2 rounded-lg border border-codify-border bg-codify-surface px-2.5 py-2 text-xs"
        >
          <span className={replay.matched ? "font-semibold text-codify-success" : "font-semibold text-codify-warning"}>
            {replay.matched ? "Replay matched" : "Replay diverged"}
          </span>{" "}
          <span className="text-codify-muted">
            — {replay.served_calls}/{replay.recorded_calls} call{replay.recorded_calls === 1 ? "" : "s"} served
            {replay.gate_calls_replayed > 0 ? `, ${replay.gate_calls_replayed} gate` : ""}
            {replay.conductor_calls_skipped > 0 ? `, ${replay.conductor_calls_skipped} conductor skipped` : ""}.
          </span>
          {replay.diverged && <div className="mt-1 font-mono text-2xs text-codify-secondary">{replay.diverged}</div>}
        </div>
      )}

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
                {summary.recorded.map((call) => {
                  const expanded = selectedSeq === call.seq;
                  return (
                    <tr
                      key={call.seq}
                      onClick={() => void handleSelectCall(call.seq)}
                      className={
                        "border-t border-codify-surface cursor-pointer transition-colors " +
                        (expanded ? "bg-codify-raised" : "hover:bg-codify-raised")
                      }
                    >
                      <td className="pr-2 py-0.5 text-codify-muted">
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            void handleSelectCall(call.seq);
                          }}
                          aria-expanded={expanded}
                          aria-label={`Inspect call ${call.seq}`}
                          className="inline-flex items-center gap-0.5 text-left text-codify-muted hover:text-codify-primary"
                        >
                          {expanded ? (
                            <ChevronDown className="w-3 h-3 shrink-0" />
                          ) : (
                            <ChevronRight className="w-3 h-3 shrink-0" />
                          )}
                          <span>{call.seq}</span>
                        </button>
                      </td>
                      <td className="pr-2 py-0.5">{call.role}</td>
                      <td className="pr-2 py-0.5 text-codify-muted">{call.model}</td>
                      <td
                        className="pr-2 py-0.5 text-codify-muted"
                        title={`Digest of the prompt this call was sent — the identity a replay matches on.${
                          summary.prompts_kept ? " Click to inspect full prompt and reply." : ""
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
                  );
                })}
              </tbody>
            </table>
          </div>

          {selectedSeq !== null && (
            <div
              data-trace-inspector="true"
              className="mt-3 rounded-lg border border-codify-border bg-codify-surface p-2.5 space-y-2.5"
            >
              <div className="flex items-center justify-between gap-2 text-2xs font-mono text-codify-muted">
                <span>
                  Call #{selectedSeq}
                  {detail
                    ? ` · ${detail.role} · ${detail.provider ? `${detail.provider}/` : ""}${detail.model} · hash ${detail.prompt_hash.slice(0, 12)}…`
                    : ""}
                </span>
                <button
                  type="button"
                  onClick={() => {
                    detailReqRef.current += 1;
                    setSelectedSeq(null);
                    setDetail(null);
                    setDetailError(null);
                    setDetailLoading(false);
                  }}
                  aria-label="Close call inspector"
                  className="text-codify-muted hover:text-codify-secondary"
                >
                  Close
                </button>
              </div>

              {detailLoading && (
                <div className="flex items-center gap-2 text-xs text-codify-muted">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading call #{selectedSeq}…
                </div>
              )}

              {detailError && <div className="text-xs text-codify-danger">{detailError}</div>}

              {detail && !detailLoading && (
                <div className="space-y-2">
                  <div>
                    <div className="text-2xs font-semibold uppercase tracking-wider text-codify-muted">
                      System prompt
                    </div>
                    {detail.system_prompt !== null && detail.system_prompt !== "" ? (
                      <pre
                        data-trace-section="system"
                        className="mt-1 max-h-40 overflow-auto rounded-md border border-codify-border bg-codify-bg p-2 font-mono text-2xs leading-relaxed whitespace-pre-wrap break-words text-codify-secondary"
                      >
                        {detail.system_prompt}
                      </pre>
                    ) : (
                      <p className="mt-1 text-2xs text-codify-muted italic">
                        System prompt text was not stored for this call.
                      </p>
                    )}
                  </div>

                  <div>
                    <div className="text-2xs font-semibold uppercase tracking-wider text-codify-muted">
                      Prompt / newly shown context
                    </div>
                    {detail.user_prompt !== null && detail.user_prompt !== "" ? (
                      <pre
                        data-trace-section="user"
                        className="mt-1 max-h-48 overflow-auto rounded-md border border-codify-border bg-codify-bg p-2 font-mono text-2xs leading-relaxed whitespace-pre-wrap break-words text-codify-primary"
                      >
                        {formatTracePayload(detail.user_prompt)}
                      </pre>
                    ) : (
                      <p className="mt-1 text-2xs text-codify-muted italic">
                        Prompt text was not stored for this call (digest {detail.prompt_hash}).
                      </p>
                    )}
                  </div>

                  <div>
                    <div className="text-2xs font-semibold uppercase tracking-wider text-codify-muted">
                      Model reply
                    </div>
                    {detail.response !== null && detail.response !== "" ? (
                      <pre
                        data-trace-section="response"
                        className="mt-1 max-h-48 overflow-auto rounded-md border border-codify-border bg-codify-bg p-2 font-mono text-2xs leading-relaxed whitespace-pre-wrap break-words text-codify-primary"
                      >
                        {formatTracePayload(detail.response)}
                      </pre>
                    ) : (
                      <p className="mt-1 text-2xs text-codify-muted italic">(empty reply)</p>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
};
