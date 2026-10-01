/**
 * Voice in the UI: what an answer sounds like, where dictated words land, and the one player.
 *
 * The engine does the speech (docs/04 §3.0.2): it records the microphone and asks the dictation
 * provider for text, and it asks the read-aloud provider for a WAV. What is left for this side is
 * small but easy to get wrong, so it lives here as plain functions rather than inside components:
 *
 * - **What is said.** An answer is Markdown, and a voice reading `**` and backticks aloud, or
 *   spelling out a forty-line code block, is worse than silence. `speakableText` keeps the words.
 * - **Where dictation lands.** At the caret, with a space where two words would otherwise touch,
 *   and never as a sent prompt: the person still presses Send.
 * - **One voice at a time.** Two answers talking over each other is never what anyone meant, so
 *   there is exactly one player and starting a second stops the first.
 */

/** The longest text one read-aloud request takes (the engine's `MAX_SPEAK_CHARS`). */
export const MAX_SPEAK_CHARS = 4096;

/** Markdown to the words worth saying: code is skipped, links read as their text, markup dropped. */
export function speakableText(markdown: string): string {
  let text = markdown.replace(/```[\s\S]*?```/g, "\n(code omitted)\n");
  text = text.replace(/`([^`\n]+)`/g, "$1");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  text = text.replace(/^\s{0,3}#{1,6}\s+/gm, "");
  text = text.replace(/^\s*>\s?/gm, "");
  text = text.replace(/^\s*(?:[-*+]|\d+\.)\s+/gm, "");
  text = text.replace(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/gm, "");
  text = text.replace(/(\*\*|__)(\S(?:.*?\S)?)\1/g, "$2");
  text = text.replace(/\*(\S(?:[^*]*?\S)?)\*/g, "$1");
  // `_word_` is emphasis, `file_name` is a name; only the first loses its underscores.
  text = text.replace(/(^|[^\w])_(\S(?:[^_]*?\S)?)_(?=[^\w]|$)/g, "$1$2");
  text = text.replace(/\|/g, " ");
  text = text.replace(/[ \t]+/g, " ").replace(/ *\n */g, "\n").replace(/\n{2,}/g, "\n");
  return text.trim();
}

/** Cut to what one request takes, at the last sentence end inside the limit when there is one. */
export function clipForSpeech(text: string, max: number = MAX_SPEAK_CHARS): string {
  if (text.length <= max) return text;
  const cut = text.slice(0, max);
  const end = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("! "), cut.lastIndexOf("? "), cut.lastIndexOf("\n"));
  return (end > max / 2 ? cut.slice(0, end + 1) : cut).trim();
}

/** Dictated words into a prompt at the caret, with a space wherever two words would touch. */
export function insertDictation(
  value: string,
  start: number,
  end: number,
  spoken: string,
): { value: string; caret: number } {
  const words = spoken.trim();
  if (!words) return { value, caret: end };
  const before = value.slice(0, start);
  const after = value.slice(end);
  const lead = before && !/\s$/.test(before) ? " " : "";
  const trail = after && !/^\s/.test(after) ? " " : "";
  return {
    value: before + lead + words + trail + after,
    caret: before.length + lead.length + words.length,
  };
}

/** `0:07` for a recording seven seconds in. */
export function formatElapsed(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

// ── the one player ──────────────────────────────────────────────────────────

type PlaybackListener = (playing: string | null) => void;

let current: { id: string; audio: HTMLAudioElement; url: string } | null = null;
/** The answer whose audio is being fetched, so its button shows "starting" rather than idle. */
let pending: string | null = null;
const listeners = new Set<PlaybackListener>();

function emit(): void {
  const id = nowPlaying();
  for (const listener of listeners) listener(id);
}

/** Which answer is playing (or about to), by the id it was started with. */
export function nowPlaying(): string | null {
  return current?.id ?? pending;
}

/** Hear about every start and stop. Returns the unsubscribe. */
export function onPlayback(listener: PlaybackListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Stop whatever is playing, or about to. */
export function stopPlayback(): void {
  pending = null;
  if (current) {
    current.audio.pause();
    URL.revokeObjectURL(current.url);
    current = null;
  }
  emit();
}

/**
 * Speak `text` as the answer `id`: fetch the audio, stop anything else, and play it.
 *
 * Resolves once playback has started. A stop, or another answer, that arrives while the audio is
 * still being fetched wins: the late audio is dropped rather than played over the newer choice.
 */
export async function playSpeech(
  id: string,
  text: string,
  fetchAudio: (text: string) => Promise<Blob>,
): Promise<void> {
  stopPlayback();
  pending = id;
  emit();
  let blob: Blob;
  try {
    blob = await fetchAudio(text);
  } catch (err) {
    if (pending === id) {
      pending = null;
      emit();
    }
    throw err;
  }
  if (pending !== id) return;
  pending = null;
  const url = URL.createObjectURL(blob);
  const audio = document.createElement("audio");
  audio.src = url;
  current = { id, audio, url };
  audio.addEventListener("ended", () => {
    if (current?.audio === audio) stopPlayback();
  });
  emit();
  try {
    await audio.play();
  } catch (err) {
    if (current?.audio === audio) stopPlayback();
    throw err;
  }
}
