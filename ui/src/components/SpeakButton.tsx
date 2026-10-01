import React, { useCallback, useEffect, useRef, useState } from "react";
import { Square, Volume2 } from "lucide-react";

import { speak } from "../api";
import { readRejection } from "../rejection.ts";
import { clipForSpeech, nowPlaying, onPlayback, playSpeech, speakableText, stopPlayback } from "../speech.ts";

interface SpeakButtonProps {
  /** The answer's message id: what the one player knows it by. */
  id: string;
  /** The answer as written. What is said is its words, not its markup (`speakableText`). */
  text: string;
  /** Read it as soon as this is true, once: auto-read chose this answer (`answersToRead`). */
  autoPlay?: boolean;
}

/**
 * Read one answer aloud, or stop it.
 *
 * The audio comes from the engine (`POST /audio/speak`, docs/04 §3.0.2) and plays through the one
 * shared player, so starting this answer stops any other. A failure is shown beside the button in
 * the engine's words, which is also where an auto-read that could not run says why.
 */
export const SpeakButton: React.FC<SpeakButtonProps> = ({ id, text, autoPlay = false }) => {
  const [playing, setPlaying] = useState(() => nowPlaying() === id);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPlaying(nowPlaying() === id);
    return onPlayback((now) => setPlaying(now === id));
  }, [id]);

  const play = useCallback(() => {
    setError(null);
    playSpeech(id, clipForSpeech(speakableText(text)), speak).catch((err: unknown) => {
      setError(readRejection(err, "Could not read this answer aloud."));
    });
  }, [id, text]);

  // A ref, not state: auto-read asks once, and a re-render (or StrictMode's second effect run) must
  // not read the answer twice.
  const autoPlayed = useRef(false);
  useEffect(() => {
    if (!autoPlay || autoPlayed.current) return;
    autoPlayed.current = true;
    play();
  }, [autoPlay, play]);

  return (
    <div className="flex items-center gap-2 pl-2">
      <button
        type="button"
        onClick={playing ? stopPlayback : play}
        aria-pressed={playing}
        className="flex items-center gap-1.5 text-2xs text-codify-muted hover:text-codify-info transition-colors"
        title={playing ? "Stop reading this answer" : "Read this answer aloud"}
      >
        {playing ? <Square className="w-3 h-3 fill-current" /> : <Volume2 className="w-3 h-3" />}
        {playing ? "Stop reading" : "Read aloud"}
      </button>
      {error && (
        <span role="alert" className="text-2xs text-codify-danger">
          {error}
        </span>
      )}
    </div>
  );
};
