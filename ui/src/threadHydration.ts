/**
 * Reopening a thread: what has to be true before a pane can show its history.
 *
 * The engine has always known what a thread said — `GET /conversations/{id}/turns`
 * derives the turns from the goals that answered them, and each turn's events are
 * the same ones a live stream delivers. Nothing in the UI asked. The store held
 * only what had streamed into it *this session*, so after a reload every thread was
 * an empty pane until a new message was sent into it: History's restore rehydrated a
 * goal, and clicking the thread itself did not. That is the gap this closes, and it
 * is the same rehydration the restore path performs, for a whole thread at a time.
 *
 * The decisions live here, pure, and `App.tsx` only fetches and applies them:
 *
 * * which turns actually need fetching — a thread opened twice in one session, or
 *   one whose last turn is still streaming, must not refetch what is already there;
 * * what the message pair for a hydrated turn looks like, so a restored turn and a
 *   live one are one format rather than two that drift;
 * * how the pair lands in the store, which is where the dangerous part is: the
 *   message ids of a *live* turn are minted from the clock (`user-${Date.now()}`)
 *   and a hydrated one from the goal id, so nothing but an explicit rule stops the
 *   same turn appearing twice, and nothing but an explicit rule stops a stale fetch
 *   overwriting the turn that is streaming in front of the user.
 */
import type { ChatMessage, ConversationTurn, Event, Goal } from "./types.ts";

/** The message ids a hydrated turn's pair carries — the same scheme `restoreGoal` mints. */
export function turnMessageIds(goalId: string): { user: string; assistant: string } {
  return { user: `user-${goalId}`, assistant: `assistant-${goalId}` };
}

/**
 * The message pair for one turn, as a restored transcript needs it.
 *
 * The user's bubble is `turn.prompt` — the words they typed, which the engine keeps
 * as the turn's prompt rather than as a second copy anywhere. The assistant side
 * carries the goal and its events and no streaming flag, so `ChatTimeline` draws it
 * from the same code that draws a live turn, conversational or not: whether a turn
 * is prose or a run card is decided from the goal, not from how it got here.
 */
export function turnMessages(
  turn: ConversationTurn,
  goal: Goal,
  events: Event[],
  conversationId: string,
): ChatMessage[] {
  const { user, assistant } = turnMessageIds(goal.id);
  return [
    {
      id: user,
      role: "user",
      content: turn.prompt,
      timestamp: turn.created_at * 1000,
      conversationId,
    },
    {
      id: assistant,
      role: "assistant",
      // The goal's own summary, as the restore path has always shown it. A
      // conversational turn's real answer is drawn from its events by
      // `turnTranscript.turnReply`, so this is only what a card with no reply yet
      // has to say.
      content: goal.description || goal.title,
      timestamp: turn.created_at * 1000,
      goal,
      events: [...events].sort((a, b) => a.sequence - b.sequence),
      isStreaming: false,
      conversationId,
    },
  ];
}

/** The goal ids the store is already showing, from either id scheme. */
export function presentGoalIds(messages: readonly ChatMessage[]): Set<string> {
  const ids = new Set<string>();
  for (const m of messages) {
    if (m.goal) ids.add(m.goal.id);
  }
  return ids;
}

/**
 * The turns still worth fetching.
 *
 * A thread reopened in the same session already holds most of its turns, and the
 * last one may be *live* — a fetch that raced the stream would otherwise add a
 * second copy of the turn the user is watching, under a different pair of ids.
 */
export function turnsNeedingHydration(
  turns: readonly ConversationTurn[],
  messages: readonly ChatMessage[],
): ConversationTurn[] {
  const present = presentGoalIds(messages);
  return turns.filter((t) => !present.has(t.goal_id));
}

/**
 * Put a thread's messages into the store, replacing what a previous read built.
 *
 * By message id, which is why the two id schemes matter: a hydrated turn's ids are
 * minted from its goal id, so a second read of a thread replaces its own pair
 * instead of appending a second one beside it. The store's order is kept, so a
 * thread reads oldest-first however many reads it took.
 *
 * What this deliberately does *not* do is protect a turn that is streaming: the
 * live turn's ids are the clock's, not the goal's, so an id rule here cannot
 * recognise it — an earlier draft tried, and merged the stale copy in beside the
 * live one. The rule that does work is by goal, and it belongs to
 * `turnsNeedingHydration`, which the caller runs against the store *as it is at
 * write time*. Two functions, one job each: this one owns the ids, that one owns
 * the turn.
 */
export function mergeThreadMessages(
  prev: readonly ChatMessage[],
  incoming: readonly ChatMessage[],
): ChatMessage[] {
  if (incoming.length === 0) return [...prev];
  const merged = new Map(prev.map((m) => [m.id, m]));
  for (const m of incoming) {
    merged.set(m.id, m);
  }
  return [...merged.values()];
}

/**
 * Whether opening this thread is a reason to go and read it.
 *
 * Once per thread per session: the store is not persisted, so the transcript it
 * holds is the whole truth about what has been shown, and a second pass over the
 * same thread can only repeat it. A failed hydration is *not* recorded here, so the
 * next time the thread is opened the read is tried again.
 */
export function shouldHydrateThread(
  conversationId: string | undefined,
  hydrated: ReadonlySet<string>,
  inFlight: ReadonlySet<string>,
): boolean {
  if (!conversationId) return false;
  return !hydrated.has(conversationId) && !inFlight.has(conversationId);
}
