/**
 * The window's side of the surface bridge: a registry of surfaces, and the loop that polls the engine, hands each
 * question to the right one, and posts the answer.
 *
 * The engine puts a question (`engine/surfaces.py`), and the window has to answer it from wherever the thing lives: the
 * editor's text is here and nowhere else. A *surface* is whatever the window owns that the assistant may look at or act on;
 * each registers one handler under its name, and the engine's operation table decides which operations exist. Adding a
 * surface is a registration here, a vocabulary and op table in the engine, and the tools that use it. This file does
 * not change.
 *
 * The loop is boring on purpose, in three ways that are easy to get wrong:
 *
 *  - **It never dies.** An engine that is down, a handler that throws, an answer that cannot be posted: each is
 *    survived, because a dead loop is an assistant that is silently blind and nothing says so.
 *  - **It never spins.** An error is waited out with a growing pause (1 s, doubling, to 10 s, starting over after a good
 *    poll), and an engine that answers instantly with nothing is not asked again at once.
 *  - **It never answers for the wrong thing.** A question for a surface that is not registered is refused, by name, so the
 *    engine is not left to time out; one question is handled at a time, in the order asked; and a handler's return is only
 *    trusted if it is the shape of a reply.
 */
import type { SurfaceRequest } from "./types";

export type SurfaceReply = { ok: true; result: unknown } | { ok: false; error: string };
export type SurfaceHandler = (request: SurfaceRequest) => Promise<SurfaceReply>;

export interface SurfaceRegistry {
  /** Register a surface's handler. Returns the function that removes it, which only removes *this* registration. */
  register(surface: string, handler: SurfaceHandler): () => void;
  /** Hand a question to its surface. Never rejects: whatever goes wrong is a refusal with a reason. */
  dispatch(request: SurfaceRequest): Promise<SurfaceReply>;
}

export function createSurfaceRegistry(): SurfaceRegistry {
  const handlers = new Map<string, SurfaceHandler>();
  return {
    register(surface, handler) {
      handlers.set(surface, handler);
      return () => {
        // A later registration under the same name is not this one's to remove.
        if (handlers.get(surface) === handler) handlers.delete(surface);
      };
    },
    async dispatch(request) {
      const handler = handlers.get(request.surface);
      if (!handler) return { ok: false, error: `This window has no ${request.surface} surface.` };
      try {
        const reply = await handler(request);
        if (reply && typeof reply === "object" && typeof (reply as { ok?: unknown }).ok === "boolean") return reply;
        return { ok: false, error: `The ${request.surface} surface answered in a shape the window does not accept.` };
      } catch (error) {
        return { ok: false, error: error instanceof Error ? error.message : String(error) };
      }
    },
  };
}

/** A pause that ends early, by rejecting, when the loop is told to stop. */
export function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", stop);
      resolve();
    }, ms);
    const stop = (): void => {
      clearTimeout(timer);
      reject(new DOMException("aborted", "AbortError"));
    };
    signal.addEventListener("abort", stop, { once: true });
  });
}

export interface SurfaceLoopIo {
  /** The next question, or null after `wait` seconds with none. Throws when the engine cannot be reached. */
  next(wait: number, signal: AbortSignal): Promise<SurfaceRequest | null>;
  answer(body: { id: string; ok: boolean; result?: unknown; error?: string }): Promise<unknown>;
  sleep(ms: number, signal: AbortSignal): Promise<void>;
  now(): number;
}

/** How long one poll is held open: the engine's own `POLL_WAIT_S`. */
const POLL_WAIT_S = 20;
const FIRST_PAUSE_MS = 1_000;
const LONGEST_PAUSE_MS = 10_000;
/** A poll that came back faster than this with nothing was not a poll the engine held, so it is not repeated at once. */
const INSTANT_MS = 100;
const INSTANT_PAUSE_MS = 500;

const aborted = (signal: AbortSignal, error: unknown): boolean =>
  signal.aborted || (error instanceof DOMException && error.name === "AbortError");

export async function runSurfaceLoop(io: SurfaceLoopIo, registry: SurfaceRegistry, signal: AbortSignal): Promise<void> {
  let pause = FIRST_PAUSE_MS;
  while (!signal.aborted) {
    const started = io.now();
    let request: SurfaceRequest | null;
    try {
      request = await io.next(POLL_WAIT_S, signal);
    } catch (error) {
      if (aborted(signal, error)) return;
      try {
        await io.sleep(pause, signal);
      } catch {
        return;
      }
      pause = Math.min(pause * 2, LONGEST_PAUSE_MS);
      continue;
    }
    pause = FIRST_PAUSE_MS;
    if (request === null) {
      if (io.now() - started < INSTANT_MS) {
        try {
          await io.sleep(INSTANT_PAUSE_MS, signal);
        } catch {
          return;
        }
      }
      continue;
    }
    const reply = await registry.dispatch(request);
    try {
      await io.answer(reply.ok ? { id: request.id, ok: true, result: reply.result } : { id: request.id, ok: false, error: reply.error });
    } catch {
      // The engine will give up on a question nobody answers; the loop's job is to be there for the next one.
    }
  }
}
