/**
 * The pipeline, folded.
 *
 * There is one orchestrator and a fixed sequence of roles, so the transcript's job is
 * to show *where the run is*, not to narrate every dispatch. It was doing the second.
 * One goal emitted four consecutive `Sub-agent assigned: planner` lines — the planner
 * consults the librarian, which is a real and interesting fact — but four identical
 * lines read as four separate agents, which is precisely the wrong mental model.
 *
 * So dispatch events are folded per role: one row, with the call count that made the
 * repetition worth having.
 *
 * **The role list is not hardcoded here.** `engine/models.py` owns `ROLES` and its own
 * comment says a second copy in the UI is how a screen and its enforcement drift apart.
 * These helpers take the roles they are given, in the order they were observed, so the
 * spine reflects whatever the engine actually dispatched — including a role added
 * before this file is touched.
 */

export interface AssignmentEvent {
  type: string;
  step_id?: string | null;
  payload?: Record<string, unknown> | null;
}

export interface Stage {
  role: string;
  /** How many times the orchestrator dispatched this role in this goal. */
  calls: number;
  provider?: string;
  model?: string;
  /** Distinct step titles this role was dispatched for, in first-seen order. */
  steps: string[];
}

function str(v: unknown): string | undefined {
  return typeof v === "string" && v.trim() ? v : undefined;
}

/**
 * Fold every `agent_assigned` event into one `Stage` per role.
 *
 * Roles come back in first-dispatch order, which is the pipeline's own order — the
 * orchestrator walks them in sequence, so the log's arrival order *is* the dependency
 * order and there is nothing to sort.
 *
 * Non-assignment events are ignored rather than passed through, so a caller can feed
 * the whole event list without pre-filtering it.
 */
export function foldStages(
  events: AssignmentEvent[],
  stepTitle?: (stepId: string) => string | undefined,
): Stage[] {
  const byRole = new Map<string, Stage>();

  for (const ev of events) {
    if (ev.type !== "agent_assigned") continue;
    const role = str(ev.payload?.role);
    if (!role) continue;

    let stage = byRole.get(role);
    if (!stage) {
      stage = { role, calls: 0, steps: [] };
      byRole.set(role, stage);
    }
    stage.calls += 1;

    // Later dispatches can carry a fallback target, and the one that actually ran is
    // the one worth naming — so the last non-empty provider/model wins rather than
    // the first. See executor.py: a fallback publishes its own `agent_assigned`.
    stage.provider = str(ev.payload?.provider) ?? stage.provider;
    stage.model = str(ev.payload?.model) ?? stage.model;

    const title = ev.step_id ? stepTitle?.(ev.step_id) : undefined;
    if (title && !stage.steps.includes(title)) stage.steps.push(title);
  }

  return [...byRole.values()];
}

export type StageState = "done" | "active" | "pending";

/**
 * Mark the spine. `activeRole` is the role the orchestrator dispatched most recently
 * while the goal is still live; pass null once the goal is terminal, and every
 * observed stage reads as done.
 *
 * A stage is never reported as `pending` unless the goal is still running, because a
 * finished goal has no pending work and claiming otherwise would be a status pill
 * inventing a diagnosis.
 */
export function stageStates(
  stages: Stage[],
  activeRole: string | null,
): { role: string; state: StageState }[] {
  return stages.map((s) => ({
    role: s.role,
    state:
      activeRole === null ? "done" : activeRole === s.role ? "active" : "done",
  }));
}

/** "1 call" / "4 calls". Singular reads as a typo at a glance. */
export function callLabel(calls: number): string {
  return `${calls} call${calls === 1 ? "" : "s"}`;
}
