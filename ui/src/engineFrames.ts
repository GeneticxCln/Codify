import type { EngineEvent } from "./types.ts";

/**
 * What one engine-level frame means, decided without a socket.
 *
 * Separate from `engineStream.ts` for the same reason `modelMenu.ts` exists
 * beside `ModelSelect.tsx`: the socket module imports the API client, which
 * reads `localStorage` at import time, and a decision worth testing should not
 * need a browser to reach. This module imports nothing but a type.
 *
 * A malformed frame is ignored rather than thrown. The connection is shared by
 * every screen in the app, so a single bad frame from a future engine build must
 * not take down the settings panel, the command bar and the chat at once.
 *
 * Both frames mean the same thing to a caller — "the engine has a current
 * answer, re-read it" — and they are told apart only so a test can say which one
 * arrived. The distinction is the engine's, not the screen's.
 */
export type FrameOutcome = "catalog" | "checked" | "ignored" | "unreadable";

export function readEngineFrame(
  raw: unknown,
  onFrame: (ev: EngineEvent) => void,
): FrameOutcome {
  let parsed: unknown;
  try {
    parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
  } catch {
    return "unreadable";
  }
  if (!parsed || typeof parsed !== "object") return "unreadable";
  const frame = parsed as { type?: unknown; payload?: unknown };
  // The type is the check, not the payload's shape. A frame that arrives without
  // one has not been labelled by the engine, and guessing that it was a catalogue
  // change would re-read the catalogue on every frame of any kind.
  if (frame.type !== "model_catalog_changed" && frame.type !== "model_catalog_checked") {
    return "ignored";
  }
  onFrame({
    type: frame.type,
    payload: frame.payload,
  } as EngineEvent);
  return frame.type === "model_catalog_changed" ? "catalog" : "checked";
}
