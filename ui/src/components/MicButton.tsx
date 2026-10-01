import React, { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Mic, Square } from "lucide-react";

import { cancelDictation, startDictation, stopDictation } from "../api";
import { readRejection } from "../rejection.ts";
import { formatElapsed } from "../speech.ts";
import { IconButton } from "./ui/IconButton";

type Phase = "idle" | "starting" | "recording" | "transcribing";

interface MicButtonProps {
  /** The prompt cannot take text right now. Holds back a *new* recording only: one in progress can always be stopped. */
  disabled?: boolean;
  /** What was said, for the composer to put at its caret. Nothing is ever sent from here. */
  onTranscript: (text: string) => void;
  /** Dictation has no provider yet, so the click takes the person to where one is chosen. */
  onNeedsSetup: () => void;
}

/**
 * Dictation into the prompt, beside Send.
 *
 * The engine records (docs/04 §3.0.2): this button asks it to start, shows the time, and asks it to
 * stop, at which point the engine sends the recording to the dictation provider and answers with the
 * words. The words land in the prompt and stay there; sending is still the person's own press.
 *
 * Esc while recording cancels and deletes the recording. It is listened for on the document, in the
 * capture phase, rather than on the prompt: a click on this button leaves focus on the button (or, in
 * WebKit, nowhere), and the same Esc must not also reach the prompt's own handler and stop a goal.
 */
export const MicButton: React.FC<MicButtonProps> = ({ disabled = false, onTranscript, onNeedsSetup }) => {
  const [phase, setPhase] = useState<Phase>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const limit = useRef(0);
  const startedAt = useRef(0);
  const mounted = useRef(true);
  const live = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      // A composer that goes away mid-recording must not leave the microphone open behind it.
      if (live.current) void cancelDictation().catch(() => {});
    };
  }, []);

  const finish = useCallback(async () => {
    if (!live.current) return;
    live.current = false;
    setPhase("transcribing");
    try {
      const { text } = await stopDictation();
      if (!mounted.current) return;
      if (text.trim()) onTranscript(text);
      else setError("No speech was recognised.");
    } catch (err) {
      if (mounted.current) setError(readRejection(err, "Could not transcribe the dictation."));
    } finally {
      if (mounted.current) setPhase("idle");
    }
  }, [onTranscript]);

  const cancel = useCallback(() => {
    if (!live.current) return;
    live.current = false;
    setPhase("idle");
    void cancelDictation().catch(() => {});
  }, []);

  const begin = async () => {
    setError(null);
    setPhase("starting");
    try {
      const started = await startDictation();
      if (!mounted.current) {
        void cancelDictation().catch(() => {});
        return;
      }
      live.current = true;
      limit.current = started.max_seconds;
      startedAt.current = Date.now();
      setElapsed(0);
      setPhase("recording");
    } catch (err) {
      if (!mounted.current) return;
      setPhase("idle");
      if ((err as { code?: string }).code === "stt_not_configured") onNeedsSetup();
      else setError(readRejection(err, "Could not start dictation."));
    }
  };

  // The clock, and the engine's cap: at the limit the engine has already ended the recording and
  // kept it, so stopping here transcribes what was said rather than losing it.
  useEffect(() => {
    if (phase !== "recording") return;
    const tick = window.setInterval(() => {
      const seconds = (Date.now() - startedAt.current) / 1000;
      setElapsed(seconds);
      if (limit.current > 0 && seconds >= limit.current) void finish();
    }, 250);
    return () => window.clearInterval(tick);
  }, [phase, finish]);

  useEffect(() => {
    if (phase !== "recording") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      cancel();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [phase, cancel]);

  const recording = phase === "recording";
  const busy = phase === "starting" || phase === "transcribing";

  return (
    <div className="flex items-center gap-2 min-w-0">
      {error && (
        <span role="alert" title={error} className="text-2xs text-codify-danger max-w-[16rem] line-clamp-2">
          {error}
        </span>
      )}
      {recording && (
        <span className="flex items-center gap-1.5 text-2xs text-codify-danger tabular-nums" role="timer">
          <span className="w-1.5 h-1.5 rounded-full bg-codify-danger motion-safe:animate-pulse" aria-hidden />
          {formatElapsed(elapsed)}
          <span className="text-codify-muted hidden sm:inline">esc to discard</span>
        </span>
      )}
      <IconButton
        label={
          recording ? "Stop dictation and transcribe" : phase === "transcribing" ? "Transcribing…" : "Dictate"
        }
        title={
          recording
            ? "Stop and put what you said in the prompt"
            : "Dictate into the prompt (Settings → Audio chooses the speech provider)"
        }
        tone={recording ? "danger" : "ghost"}
        size="md"
        onClick={recording ? () => void finish() : () => void begin()}
        disabled={busy || (phase === "idle" && disabled)}
        aria-pressed={recording}
        className="w-8 h-8"
      >
        {busy ? (
          <Loader2 className="w-4 h-4 animate-spin" />
        ) : recording ? (
          <Square className="w-3 h-3 fill-current" />
        ) : (
          <Mic className="w-4 h-4" />
        )}
      </IconButton>
    </div>
  );
};
