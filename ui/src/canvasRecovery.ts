/**
 * Surviving a canvas that stops being a canvas.
 *
 * **What this is for.** Every theme paints its backdrop onto a 2D context, and
 * there are two ways that stops happening without anybody noticing. The first is
 * context loss: a GPU driver reset, a laptop lid closing, a monitor renegotiating
 * its mode, or a machine left open for a week on a driver that decides to reset
 * itself. The browser fires `contextlost`, the context becomes unusable, and
 * every `fillRect` after that is a no-op. Nothing throws, nothing is logged, and
 * the backdrop is simply gone — the app looks broken in a way that has no error
 * to chase and no way back short of a reload.
 *
 * The second is a painter that throws. The render loop re-arms its own
 * `requestAnimationFrame` on the first line, so a throwing painter does not stop
 * the loop — it throws again on the next frame, and the one after, thirty times
 * a second, for as long as the window is open. That is a console flood rather
 * than a crash, which is worse in the way that matters: it looks like noise, and
 * the backdrop is half-drawn behind it.
 *
 * **Why the module is separate from the two loops that need it.** There are two
 * render loops — the shared hook and the matrix rain — and a third arrives with
 * the next effect. A rule that lives in the first one to need it is a rule the
 * second one silently does not have, and neither failure is visible from the
 * outside: an unhandled `contextlost` and a missing listener look identical, which
 * is a blank canvas.
 *
 * So the recovery is a small unit with a fake-canvas test rather than a few lines
 * copied into each loop, and each loop's own test asserts it is wired in.
 */

/**
 * Consecutive frames a painter may throw before the loop stops.
 *
 * Three is not a tuning number, it is the smallest count that tells the two
 * cases apart. One failure is a transient bad value and the next frame proves it;
 * a painter failing on *every* frame is a bug, and continuing to call it turns
 * that bug into thirty console messages a second until somebody closes the window.
 * One number, shared by both render loops, because a loop that gave up sooner
 * than its twin would be a difference nobody could account for later.
 */
export const FRAMES_BEFORE_GIVING_UP = 3;

/** The slice of `HTMLCanvasElement` this module needs. */
export interface RecoverableCanvas {
  addEventListener(type: string, listener: (event: Event) => void): void;
  removeEventListener(type: string, listener: (event: Event) => void): void;
}

/** What a loop has to be told when its context goes and comes back. */
export interface RecoveryHooks {
  /**
   * The context is gone. Stop drawing: every call into it is a silent no-op, so a
   * loop that keeps going burns its whole frame budget painting into a void.
   */
  onLost?: () => void;
  /**
   * It is back. Re-measure and repaint before the next frame — the canvas comes
   * back blank, and its backing store may be a different size than the one the
   * last frame was drawn into.
   */
  onRestored?: () => void;
}

/** Whether the context is currently lost, and how to stop listening. */
export interface RecoveryBinding {
  isLost(): boolean;
  detach(): void;
}

/**
 * Listen for context loss and restoration.
 *
 * **`preventDefault()` is load-bearing, not decoration.** A canvas that does not
 * cancel `contextlost` will never be given a `contextrestored` — the browser's
 * default action is to let the context die permanently. That one call is the
 * difference between a backdrop that comes back after a driver reset and one that
 * needs the app restarted, and it is the single easiest line to leave out.
 */
export function bindContextRecovery(
  canvas: RecoverableCanvas,
  hooks: RecoveryHooks = {},
): RecoveryBinding {
  let lost = false;

  const onLost = (event: Event): void => {
    // Cancel the default action, or the context is gone for good.
    event.preventDefault?.();
    lost = true;
    hooks.onLost?.();
  };
  const onRestored = (): void => {
    lost = false;
    hooks.onRestored?.();
  };

  canvas.addEventListener("contextlost", onLost);
  canvas.addEventListener("contextrestored", onRestored);

  return {
    isLost: () => lost,
    detach: () => {
      canvas.removeEventListener("contextlost", onLost);
      canvas.removeEventListener("contextrestored", onRestored);
    },
  };
}

/** What a loop does with a frame that threw. */
export type FrameOutcome = "ok" | "failed" | "gave-up";

export interface FrameGuardOptions {
  /**
   * Consecutive failures before the loop stops. Consecutive, because the thing
   * worth catching is a painter that throws every frame; a painter that throws
   * on the rare frame it has bad input for should keep going.
   */
  budget?: number;
  /** Called once, with the error that ended it. Not called for a survivable frame. */
  onGiveUp?: (error: unknown) => void;
}

/**
 * Contain a frame that throws, and stop the loop if it is going to keep throwing.
 *
 * The decision this makes is *when to give up*, and it is deliberately not "never":
 * a loop that retries a permanently-throwing painter forever is the flood above,
 * and one that gives up after a single failure kills a backdrop over a transient
 * bad value. Consecutive failures, with the count reset by any success, sits
 * between the two.
 */
/**
 * How many times slower than asked a loop may run before it is not a backdrop
 * any more but the whole frame budget.
 *
 * A 30 fps effect asks for a frame every 33 ms. `rAF` is aligned with the
 * compositor, so the interval the browser actually delivers is the honest
 * measure of whether the machine can afford the animation — and on a renderer
 * that rasterises in software (WebKitGTK without dma-buf, an Nvidia/Wayland
 * session, a VM, a machine under load) the delivered interval runs to hundreds
 * of milliseconds while the page's own paint looks cheap. Four is chosen to sit
 * far outside ordinary jitter: at 30 fps it fires below ~7.5 fps, which is where
 * an effect has stopped being decoration and started starving the main loop that
 * has to dispatch the user's input.
 */
export const SLOW_FRAME_FACTOR = 4;

/**
 * Delivered frames a loop is judged over.
 *
 * A verdict from one or two frames would stop a backdrop over a single stall —
 * a garbage collection, a big layout, the engine answering — and 24 frames at
 * 30 fps is 0.8 s of evidence, short enough to act before a window is left
 * unusable and long enough that a hiccup cannot masquerade as a verdict.
 */
export const FRAMES_BEFORE_JUDGING = 24;

/**
 * An interval longer than this is a suspension, not a frame rate.
 *
 * A hidden or occluded window, a sleeping machine and a throttled background tab
 * all deliver `rAF` at roughly 1 Hz by design. Counting those intervals as
 * frames would read as "this machine cannot animate" and leave every user who
 * ever switched away with a still backdrop for the rest of the session.
 */
export const SUSPENDED_FRAME_MS = 1000;

/** What a loop is told about its own frame delivery. */
export type FrameVerdict = "warming-up" | "ok" | "too-slow";

/**
 * Milliseconds of frame delivery a loop watches before it will judge anything.
 *
 * Startup is not steady state. The first second of a window holds layout, style
 * resolution, the engine handshake, the first data from `/workspaces` and a
 * mount storm across every panel — frames that are genuinely slow *because the
 * app is starting*, not because the machine cannot animate. A budget that judged
 * those would leave some healthy machines with a still backdrop forever, which
 * is the failure mode this constant exists to rule out.
 */
export const FRAME_BUDGET_WARMUP_MS = 2000;

export interface FrameBudgetOptions {
  /** The interval the loop is asking for, in ms: `1000 / fps`. */
  targetMs: number;
  /** How much slower than `targetMs` the mean may be. Default `SLOW_FRAME_FACTOR`. */
  factor?: number;
  /** Delivered frames per verdict. Default `FRAMES_BEFORE_JUDGING`. */
  frames?: number;
  /** Startup grace, in ms, before any frame is counted. Default `FRAME_BUDGET_WARMUP_MS`. */
  warmupMs?: number;
  /** Called once, with the measured mean, when the verdict first comes back `too-slow`. */
  onTooSlow?: (measuredMs: number) => void;
}

export interface FrameBudget {
  /** Record a delivered frame at `now` (the `rAF` clock) and return the verdict. */
  observe(now: number): FrameVerdict;
  /** The mean interval of the last completed window, or 0 before one. */
  measuredMs(): number;
  /** Whether this loop has already been told it cannot afford to animate. */
  gaveUp(): boolean;
  /**
   * Start the measurement over — for a window that has just become visible
   * again, whose frames were suspended rather than slow.
   */
  reset(): void;
}

/**
 * Judge whether a loop can afford to keep asking for frames.
 *
 * This is the second half of the budget in DESIGN.md §7. The first half keeps a
 * loop from drawing more often than it asked to; this half is what happens when
 * the *compositor* cannot keep up with what it asked for. Those are different
 * failures and only one of them is visible from inside the page's own paint: an
 * effect that costs nothing to draw still costs a full-surface software
 * composite per frame, and the app's window stops answering input long before
 * the drawing is the expensive part.
 *
 * Measured, not inferred. There is no reliable way to ask a browser "is your
 * compositor accelerating?" — `WEBKIT_DISABLE_DMABUF_RENDERER` is not set on the
 * machines this matters on, and a feature test for dma-buf would be a guess about
 * the driver. The delivered interval is not a guess: it is the thing itself.
 */
export function makeFrameBudget(options: FrameBudgetOptions): FrameBudget {
  const targetMs = Math.max(1, options.targetMs);
  const factor = Math.max(1, options.factor ?? SLOW_FRAME_FACTOR);
  const window = Math.max(1, options.frames ?? FRAMES_BEFORE_JUDGING);
  const warmupMs = Math.max(0, options.warmupMs ?? FRAME_BUDGET_WARMUP_MS);

  // A bool, not `previous === 0`: `rAF` timestamps are a document-relative clock
  // and the first one really can be 0, so the obvious sentinel silently discards
  // the first interval of every loop — which is the interval that would have
  // completed the window the effect is judged on.
  let started = false;
  let warmed = 0;
  let previous = 0;
  let samples: number[] = [];
  let summed = 0;
  let lastMean = 0;
  let gave = false;

  const clear = (): void => {
    samples = [];
    summed = 0;
  };

  return {
    observe(now: number): FrameVerdict {
      if (gave) return "too-slow";
      if (!started) {
        started = true;
        previous = now;
        warmed = now + warmupMs;
        return "warming-up";
      }
      const interval = now - previous;
      previous = now;
      if (now < warmed) {
        // Still the app coming up: counted as frame delivery, never as evidence.
        clear();
        return "warming-up";
      }
      if (interval > SUSPENDED_FRAME_MS) {
        // Suspended, not slow: start over rather than measuring a nap.
        clear();
        return "warming-up";
      }
      samples.push(interval);
      summed += interval;
      if (samples.length < window) return "warming-up";
      lastMean = summed / samples.length;
      clear();
      if (lastMean <= targetMs * factor) return "ok";
      gave = true;
      options.onTooSlow?.(lastMean);
      return "too-slow";
    },
    measuredMs: () => lastMean,
    gaveUp: () => gave,
    reset: () => {
      started = false;
      warmed = 0;
      clear();
    },
  };
}

export function makeFrameGuard(options: FrameGuardOptions = {}): {
  run: (frame: () => void) => FrameOutcome;
  failures: () => number;
  gaveUp: () => boolean;
} {
  const budget = Math.max(1, options.budget ?? FRAMES_BEFORE_GIVING_UP);
  let consecutive = 0;
  let gave = false;

  return {
    run(frame: () => void): FrameOutcome {
      if (gave) return "gave-up";
      try {
        frame();
        consecutive = 0;
        return "ok";
      } catch (error) {
        consecutive += 1;
        if (consecutive < budget) return "failed";
        gave = true;
        options.onGiveUp?.(error);
        return "gave-up";
      }
    },
    failures: () => consecutive,
    gaveUp: () => gave,
  };
}
