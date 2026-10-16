/**
 * Which answer about a goal is the newest one asked for.
 *
 * The window learns a goal's state from several places that run at once: the 1.5 s poll, a refresh after each
 * `goal_status` / `step_status` / `plan_updated` event, and the refresh when the stream ends. Each is a `GET
 * /goals/{id}` whose answer is written into the transcript, and answers can arrive in a different order than they were
 * asked: a poll sent before an event and answered after it replaced the fresher goal with an older one, until the next
 * tick. Nothing was lost for good, but the status badge, the plan and the buttons showed old state in between.
 *
 * `begin` is called just before a read is sent and `apply` when its answer arrives: only an answer to a read asked
 * later than the last one applied may be written. The ticket, not the goal's version, is what is compared, because a
 * step's status changes without the goal's version moving.
 */
export interface GoalReadOrder {
  /** A ticket for a read about to be sent. */
  begin(goalId: string): number;
  /** True, once, for an answer newer than every answer already applied for this goal; false for an older one. */
  apply(goalId: string, ticket: number): boolean;
}

export function createGoalReadOrder(): GoalReadOrder {
  const issued = new Map<string, number>();
  const applied = new Map<string, number>();
  return {
    begin(goalId) {
      const ticket = (issued.get(goalId) ?? 0) + 1;
      issued.set(goalId, ticket);
      return ticket;
    },
    apply(goalId, ticket) {
      if (ticket <= (applied.get(goalId) ?? 0)) return false;
      applied.set(goalId, ticket);
      return true;
    },
  };
}
