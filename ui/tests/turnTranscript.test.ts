/**
 * A turn is a conversation, not a gate report.
 *
 * Two layers, because a rule can be right and still not reach the screen:
 *
 * 1. **the rules** — `turnTranscript.ts`: what a turn's reply is, when the live
 *    streamed text stands in for it, and what counts as something to act on;
 * 2. **the screen** — `ChatTimeline`, rendered through `react-dom/server`: a
 *    benign turn shows the answer as prose and *nothing about Laya*, no status
 *    badge and no role spine, while a blocked turn shows the verdict, because a
 *    refusal with no reason is a bug report.
 *
 * The events in the fixtures are the ones the engine now publishes for each case
 * (`ExecutorService.run_chat`): a benign turn logs its reply and a `goal_status`,
 * and never a `laya_decision` — the stage record is where the gate's having run
 * is measured, and that is a Python-side pin (`tests/test_turns.py`).
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

// Dynamic, and after the hook: a static import is hoisted and resolved before
// `registerTsx()` has run, which fails with ERR_UNKNOWN_FILE_EXTENSION.
const { isConversationalTurn, turnAlerts, turnLiveText, turnReply } = await import(
  "../src/turnTranscript.ts"
);
const { ChatTimeline } = await import("../src/components/ChatTimeline.tsx");

import type { Event, Goal, PlanStep } from "../src/types.ts";

const event = (
  sequence: number,
  type: Event["type"],
  payload: Record<string, any>,
): Event => ({
  id: `e${sequence}`,
  goal_id: "g1",
  step_id: null,
  type,
  payload,
  timestamp: sequence,
  sequence,
});

const goal = (overrides: Partial<Goal> = {}): Goal => ({
  id: "g1",
  workspace_id: "w1",
  conversation_id: "c1",
  title: "hi",
  description: "hi",
  status: "COMPLETED",
  dry_run: false,
  plan_only: false,
  parallel: false,
  mode: "chat",
  trace: false,
  version: 3,
  created_at: 0,
  updated_at: 0,
  steps: [],
  ...overrides,
});

// ── the rules ─────────────────────────────────────────────────────────────

test("the reply is the log line the engine flagged as this turn's", () => {
  const events = [
    event(1, "log", { level: "info", message: "conductor called read_file({})" }),
    event(2, "log", { level: "info", message: "Hello! What would you like to work on?", turn: true }),
    event(3, "goal_status", { status: "COMPLETED" }),
  ];
  assert.equal(turnReply(events), "Hello! What would you like to work on?");
});

test("an unfiled log line is never mistaken for the answer", () => {
  // The flag, not "the last log", is the contract: a run's telemetry is a log
  // line too, and reading the newest one as the reply would quote a warning as
  // if the assistant had said it.
  const events = [
    event(1, "log", { level: "info", message: "Hello!", turn: true }),
    event(2, "log", { level: "warn", message: "risk score 2.00/2 — destructive" }),
  ];
  assert.equal(turnReply(events), "Hello!");
});

test("a turn with no reply yet has none, rather than an empty string", () => {
  assert.equal(turnReply([event(1, "goal_status", { status: "PLANNING" })]), null);
  assert.equal(turnReply([]), null);
  assert.equal(turnReply(undefined), null);
});

test("the live text is the newest snapshot, not every snapshot concatenated", () => {
  // `Conductor.on_text` hands over the whole reply each time the model finishes
  // talking, so the events are snapshots of one answer: joining them would print
  // the sentence three times.
  const events = [
    event(1, "model_delta", { role: "conductor", text: "Look" }),
    event(2, "model_delta", { role: "conductor", text: "Looking at the file." }),
  ];
  assert.equal(turnLiveText(events), "Looking at the file.");
  assert.equal(turnLiveText([event(1, "log", { message: "x", turn: true })]), null);
});

test("only what a person can act on is an alert", () => {
  const events = [
    event(1, "agent_assigned", { role: "laya", provider: "laya", model: "m" }),
    event(2, "log", { level: "info", message: "conductor called read_file({})" }),
    event(3, "log", { level: "warn", message: "risk score 2.00/2 — destructive" }),
    event(4, "log", { level: "info", message: "the answer", turn: true }),
    event(5, "error", { code: "laya_blocked", message: "blocked by Laya" }),
    event(6, "laya_decision", { engine: "sdk", blocked: true }),
  ];
  assert.deepEqual(
    turnAlerts(events).map((a) => [a.kind, a.text]),
    [
      ["warn", "risk score 2.00/2 — destructive"],
      ["error", "[laya_blocked] blocked by Laya"],
      ["gate", ""],
    ],
  );
});

test("an answer that came from another model says so", () => {
  // The model the user picked was not the model that replied. That is a fact
  // about the answer, and a conversation silent about it reads as though the
  // chosen model answered.
  const events = [
    event(1, "provider_fallback", {
      role: "scribe",
      from: { provider: "openai", model: "gpt-4o" },
      to: { provider: "ollama", model: "qwen2.5-coder:7b" },
      code: "provider_unreachable",
      detail: "connection refused",
    }),
    event(2, "agent_call_failed", {
      role: "scribe",
      target: "primary",
      code: "http_500",
      message: "the provider answered 500",
    }),
  ];
  assert.deepEqual(
    turnAlerts(events).map((a) => [a.kind, a.text]),
    [
      ["warn", "openai/gpt-4o → ollama/qwen2.5-coder:7b (provider_unreachable)"],
      ["error", "[http_500] the provider answered 500"],
    ],
  );
});

test("a turn that planned is a run, so the conversation rules stand aside", () => {
  assert.equal(isConversationalTurn(goal()), true);
  assert.equal(
    isConversationalTurn(goal({ steps: [{ id: "s1" } as PlanStep] })),
    false,
    "a plan to approve is the whole point of the run's card",
  );
  assert.equal(isConversationalTurn(goal({ mode: "normal" })), false);
  assert.equal(isConversationalTurn(undefined), false);
});

// ── the screen ────────────────────────────────────────────────────────────

const noop = (): void => {};

const timeline = (events: Event[], goalOverrides: Partial<Goal> = {}): string =>
  renderToStaticMarkup(
    React.createElement(ChatTimeline, {
      messages: [
        { id: "u1", role: "user", content: "hi", timestamp: 0 },
        {
          id: "a1",
          role: "assistant",
          content: "",
          timestamp: 1,
          goal: goal(goalOverrides),
          events,
        },
      ],
      pinnedContracts: {},
      onStartGoal: noop,
      onEnableExecution: noop,
      onApplyGoal: noop,
      onEditStep: () => true,
      onPauseGoal: noop,
      onCancelGoal: noop,
      onSetGoalTrace: noop,
      onDeleteGoal: noop,
      onRetryStep: noop,
      onOpenSettings: noop,
      onImportAudit: noop,
      onPinDesignContract: async () => {},
    }) as never,
  );

const text = (markup: string): string =>
  markup
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/\s+/g, " ")
    .trim();

test("a benign turn renders as the reply, and says nothing about the gate", () => {
  const markup = timeline([
    event(1, "stage_result", { stage: "laya", role: "laya", outcome: "allow" }),
    event(2, "agent_assigned", { role: "scribe", provider: "ollama", model: "m" }),
    event(3, "log", { level: "info", message: "conductor called read_file({})" }),
    event(4, "log", {
      level: "info",
      message: "Hello! What would you like to work on?",
      turn: true,
    }),
    event(5, "goal_status", { status: "COMPLETED" }),
  ]);
  const words = text(markup);
  assert.match(words, /Hello! What would you like to work on\?/);
  assert.doesNotMatch(words, /Laya/i, "the gate is not a thing the conversation reports");
  assert.doesNotMatch(
    words,
    /scribe|conductor called/,
    "the answer is not narrated by the machinery under it",
  );
  assert.doesNotMatch(
    words,
    /COMPLETED/,
    "a status badge belongs to a run; a conversation ends by not being busy",
  );
});

test("a conversation renders the fallback that answered it", () => {
  const markup = timeline([
    event(1, "provider_fallback", {
      role: "scribe",
      from: { provider: "openai", model: "gpt-4o" },
      to: { provider: "ollama", model: "qwen2.5-coder:7b" },
      code: "provider_unreachable",
      detail: "connection refused",
    }),
    event(2, "log", { level: "info", message: "Hello! What would you like to work on?", turn: true }),
    event(3, "goal_status", { status: "COMPLETED" }),
  ]);
  assert.match(
    text(markup),
    /openai\/gpt-4o → ollama\/qwen2\.5-coder:7b \(provider_unreachable\)/,
    "an answer from a model the user did not pick must not pass as one from the one they did",
  );
});

test("a blocked turn still shows the verdict, because a refusal needs a reason", () => {
  const markup = timeline(
    [
      event(1, "laya_decision", {
        engine: "sdk",
        blocked: true,
        block_reason: "prompt-injection probability 0.99 >= 0.85",
        answers: { prompt_injection: { noul: 0.99 } },
      }),
      event(2, "error", { code: "laya_blocked", message: "blocked by Laya", role: "laya" }),
      event(3, "goal_status", { status: "FAILED" }),
    ],
    { status: "FAILED" },
  );
  const words = text(markup);
  assert.match(words, /Laya gate blocked/);
  assert.match(words, /blocked by Laya/);
});

test("a turn that planned keeps the run's card", () => {
  const markup = timeline(
    [
      event(1, "log", { level: "info", message: "PLANNING_DONE", turn: false }),
      event(2, "agent_assigned", { role: "planner", provider: "ollama", model: "m" }),
      event(3, "goal_status", { status: "PENDING" }),
    ],
    { status: "PENDING", steps: [{ id: "s1", title: "S1" } as PlanStep] },
  );
  const words = text(markup);
  assert.match(words, /PENDING/, "a plan awaiting approval shows the status it awaits it in");
  assert.match(words, /planner/, "and the roles that produced it");
});

test("a turn's reply and its live snapshot are both drawn as Markdown", () => {
  const finished = timeline([
    event(1, "log", { level: "info", message: "## Plan\n\nRun **make test** first.", turn: true }),
  ]);
  assert.match(finished, /<h4[^>]*>Plan<\/h4>/, "the reply's heading is raw text");
  assert.match(finished, /<strong[^>]*>make test<\/strong>/);
  assert.ok(!text(finished).includes("**make test**") && !text(finished).includes("## Plan"));

  // The streamed snapshot is the same answer in progress, and goes through the same component, so a
  // half-written fence already looks like code instead of snapping into shape at the end.
  const streaming = timeline(
    [event(1, "model_delta", { role: "conductor", text: "Try:\n\n```sh\nmake te" })],
    { status: "RUNNING" },
  );
  assert.match(streaming, /<pre[^>]*><code>make te<\/code><\/pre>/);
  assert.ok(!text(streaming).includes("```"), "an open fence showed its backticks while it streamed");
});

test("a plan step's description keeps its marks, and the person's own message wraps", () => {
  const markup = timeline([event(1, "goal_status", { status: "PENDING" })], {
    status: "PENDING",
    steps: [
      { id: "s1", title: "S1", description: "Fix `parse()` in **utils**", status: "PENDING" } as unknown as PlanStep,
    ],
  });
  assert.match(markup, /<code[^>]*>parse\(\)<\/code>/, "a step description's `code` is raw text");
  assert.match(markup, /<strong[^>]*>utils<\/strong>/);
  assert.ok(!markup.includes("`parse()`"), "the backticks of a step description are on screen");
  // The person's own words: line breaks kept, and a long word wraps instead of widening the column.
  assert.match(markup, /<div class="[^"]*\bwhitespace-pre-wrap\b[^"]*\bbreak-words\b[^"]*">hi<\/div>/);
});
