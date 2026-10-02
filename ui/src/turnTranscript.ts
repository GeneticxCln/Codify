/**
 * What a turn puts in the transcript.
 *
 * A turn is a goal (`events.goal_id` is NOT NULL, so the event log is the only
 * place a streamed token can live) but it is not a *run*. Rendering it as one is
 * what made typing "hi" look like a pipeline: a status badge, a spine of roles
 * dispatched, and a Laya verdict card over the top of the answer — "your request
 * was classified before it was answered", which is exactly what docs/09 §10
 * exists to stop. The engine no longer announces a benign verdict at all
 * (`ExecutorService.run_chat`; docs/09 §10.15), and this module is the other
 * half: the transcript's rules for what a turn's message *is*.
 *
 *  - the answer is the reply, rendered as prose;
 *  - while it is still arriving, the conductor's streamed text stands in for it,
 *    because the engine publishes that snapshot before it publishes the answer;
 *  - only what a person has to act on is shown beside it: the gate's card when
 *    the engine published one, which now means a block or a warning, plus
 *    warnings and errors;
 *  - the engine's own narration ("conductor called `read_file`…") is *not* shown.
 *    It is in the event log and one click away in the audit export; a
 *    conversation quoting its own tool calls is the run's shape again, one line
 *    lower down;
 *  - a turn that planned is a run and steps aside: what the user asked for turned
 *    out to be a change, and the plan, the approve button and the step list are
 *    the whole point of the card they render in.
 *
 * Pure functions over the events, in a module of their own, so what a turn shows
 * can be asserted without rendering it — the way `stageMetrics` and
 * `replyPreview` are. A rule that only exists inside a component's JSX is a rule
 * nothing can check.
 */
import { isChatMode } from "./types";
import type { Event, Goal } from "./types";

/** The last log the engine flagged as this turn's reply: what `turnReply` and `turnQuestion` both read, so a question can never come from a different event than the words above it. */
function lastTurnLog(events: Event[] | undefined): Event | null {
  const ordered = [...(events ?? [])].reverse();
  for (const ev of ordered) {
    if (ev.type === "log" && ev.payload?.turn === true) return ev;
  }
  return null;
}

/** The turn's answer: the last reply the engine flagged as this turn's. */
export function turnReply(events: Event[] | undefined): string | null {
  const ev = lastTurnLog(events);
  if (!ev) return null;
  const text = ev.payload?.message;
  return typeof text === "string" && text.trim() ? text : null;
}

/** The engine's limits on a question's options (`engine/ask.py`), applied again here rather than trusted. */
export const MAX_QUESTION_OPTIONS = 4;
export const MAX_QUESTION_OPTION_CHARS = 80;

export interface TurnQuestion {
  text: string;
  /** The choices to offer as buttons. Empty for an open question, and for anything that is not a choice. */
  options: string[];
}

/**
 * The question the conductor put to the person, when the turn ended by asking one (`ask_user`; `docs/04` §1.4).
 *
 * It rides on the same event as the turn's reply, whose words are the question as prose, so history and speech
 * carry it; this is the same question as data so the options can be buttons. The newest reply decides: a later
 * plain answer clears an earlier question. Whatever the event holds is bounded again here (one line per
 * option, at most four, each cut at 80 characters, a repeat said once), and a list that does not come to at
 * least two options is not a choice and offers none. A `question` this build cannot read is no question: the
 * reply's words are still shown.
 */
export function turnQuestion(events: Event[] | undefined): TurnQuestion | null {
  const raw = lastTurnLog(events)?.payload?.question;
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const { text, options: offered } = raw as { text?: unknown; options?: unknown };
  if (typeof text !== "string" || text.trim().length === 0) return null;

  let options: string[] = [];
  if (Array.isArray(offered)) {
    const seen = new Set<string>();
    for (const entry of offered) {
      if (typeof entry !== "string") continue;
      let option = entry.replace(/\s+/g, " ").trim();
      if (option.length === 0) continue;
      if (option.length > MAX_QUESTION_OPTION_CHARS) option = `${option.slice(0, MAX_QUESTION_OPTION_CHARS - 1).trimEnd()}…`;
      const key = option.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      options.push(option);
      if (options.length === MAX_QUESTION_OPTIONS) break;
    }
  }
  if (options.length < 2) options = [];
  return { text: text.trim(), options };
}

/**
 * The conductor's prose as it arrives: the newest `model_delta` snapshot, or null
 * before it has said anything.
 *
 * Newest-wins rather than concatenated, because that is what the engine
 * publishes: `Conductor.on_text` hands over the whole reply each time the model
 * finishes talking, so the events are snapshots of one answer, not chunks of one.
 * Appending them would print the same sentence three times.
 */
export function turnLiveText(events: Event[] | undefined): string | null {
  let latest: string | null = null;
  for (const ev of events ?? []) {
    if (ev.type !== "model_delta") continue;
    const text = ev.payload?.text;
    if (typeof text === "string" && text.trim()) latest = text;
  }
  return latest;
}

export type TurnAlertKind = "gate" | "warn" | "error";

export interface TurnAlert {
  kind: TurnAlertKind;
  /** The words to show. Empty for a `gate`, which renders its own card. */
  text: string;
  /** The `laya_decision` payload, for the card. Absent for log lines. */
  payload?: Record<string, any>;
}

/**
 * Everything a turn has to say for itself other than the answer, in the order the
 * engine said it.
 *
 * The reply is a `log` line too, and it is skipped here: it is the answer, and it
 * is rendered above as prose. Showing it twice — once as prose, once as a warn
 * line — is how a transcript starts arguing with itself.
 */
export function turnAlerts(events: Event[] | undefined): TurnAlert[] {
  const alerts: TurnAlert[] = [];
  for (const ev of events ?? []) {
    if (ev.type === "laya_decision") {
      alerts.push({ kind: "gate", text: "", payload: ev.payload });
      continue;
    }
    if (ev.type === "error") {
      alerts.push({ kind: "error", text: bracket(ev.payload?.code, ev.payload?.message) });
      continue;
    }
    // A call that failed and a call that was answered somewhere else are both
    // facts about the *answer* — the model that was picked was not the model
    // that replied — so a conversation says them rather than answering as if
    // nothing had happened. The run card shows them as badges; here they are a
    // line each, naming the two targets and the engine's own code.
    if (ev.type === "agent_call_failed") {
      alerts.push({
        kind: "error",
        text: bracket(ev.payload?.code, ev.payload?.message),
      });
      continue;
    }
    if (ev.type === "provider_fallback") {
      const from = target(ev.payload?.from);
      const to = target(ev.payload?.to);
      alerts.push({
        kind: "warn",
        text: `${from} → ${to} (${String(ev.payload?.code ?? "fallback")})`,
      });
      continue;
    }
    if (ev.type !== "log" || ev.payload?.turn === true) continue;
    const level = ev.payload?.level;
    if (level !== "warn" && level !== "error") continue;
    const message = ev.payload?.message;
    if (typeof message === "string" && message.trim()) {
      alerts.push({ kind: level, text: message });
    }
  }
  return alerts;
}

/** `[code] message`, with neither half required to be there. */
function bracket(code: unknown, message: unknown): string {
  const tag = typeof code === "string" && code.trim() ? code : "error";
  return `[${tag}] ${typeof message === "string" ? message : ""}`.trim();
}

/** `provider/model`, or `?` — a target the engine did not name. */
function target(value: unknown): string {
  const t = (value ?? {}) as { provider?: unknown; model?: unknown };
  const provider = typeof t.provider === "string" ? t.provider : "?";
  const model = typeof t.model === "string" ? t.model : "?";
  return `${provider}/${model}`;
}

/**
 * Whether this message is a conversation rather than a run.
 *
 * A chat goal *with* steps is a run: the turn asked for a change, the pipeline
 * planned it, and `run_planning` left the goal PENDING for approval. Steps are
 * therefore the test, not the mode — which is also why the mode alone cannot be
 * it, since a turn and the run it turned into share the same goal row.
 */
export function isConversationalTurn(goal: Goal | undefined): boolean {
  return isChatMode(goal?.mode) && (goal?.steps?.length ?? 0) === 0;
}
