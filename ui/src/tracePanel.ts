/**
 * Which goal, if any, has its trace panel open.
 *
 * This exists because of a crash that shipped in `03c1c6f`. The timeline rendered
 *
 *     {traceFor === msg.goal!.id && (<TracePanel … />)}
 *
 * one line *outside* the `{msg.goal && …}` block that guards the rest of the goal
 * card. A user message has no `goal` — it is the thing the user typed — so the `===`
 * evaluated `msg.goal.id` on `undefined` and took the whole transcript down with it:
 * `TypeError: Cannot read properties of undefined (reading 'id')`, no error boundary,
 * a blank window. Every other `msg.goal!` in that file was inside the guard; this
 * was the one that was not, and a `!` is an assertion to the compiler, not a check
 * at runtime.
 *
 * Returning the id rather than a boolean is deliberate. The caller then needs no
 * non-null assertion at all — `openTraceId` is either a usable string or null — so
 * the same mistake cannot be re-encoded one call away. Fixing only the crash and
 * leaving `msg.goal!.id` in the JSX would have left the trap in place for the next
 * line written underneath it.
 *
 * And it is a plain module, so it is testable. The UI suite runs pure logic through
 * `node --test` and has no renderer, which is exactly why a JSX-only condition
 * could ship a crash that blanks the app on the first prompt anyone ever typed.
 */

/** The minimum a message must expose for the trace panel to belong to it. */
export interface TraceOwner {
  id: string;
}

/**
 * The goal id whose trace panel should be open, or null when none should be.
 *
 * Null in three cases that all used to be one crash or a lie: no goal (a user
 * message, or an assistant message whose goal has not loaded yet), nothing selected,
 * or a selection that names a goal this transcript does not hold.
 */
export function traceGoalId(
  goal: TraceOwner | null | undefined,
  traceFor: string | null,
): string | null {
  if (!goal || traceFor === null) return null;
  return traceFor === goal.id ? goal.id : null;
}
