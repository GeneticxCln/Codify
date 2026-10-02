/**
 * A question the conductor put to the person, as the window reads and answers it.
 *
 * `ask_user` ends a turn (`engine/ask.py`, `tests/test_ask_user.py`). The reply is the question as prose, so
 * history and speech carry it, and the same event carries it as data (`question: {text, options}`) so the
 * window can offer the options as choices. These are the rules for reading it, with no renderer, and then the
 * App's side of it: an option is a button, pressing one sends its text as an ordinary turn (there is no
 * "answer" route and no state held between the question and the reply), and a question that has been
 * answered, or has been followed by anything, offers nothing.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Event } from "../src/types.ts";
import type { AppContext } from "./appHarness.ts";

const { MAX_QUESTION_OPTIONS, MAX_QUESTION_OPTION_CHARS, turnQuestion } = await import("../src/turnTranscript.ts");
const { withApp, conversation } = await import("./appHarness.ts");

const reply = (sequence: number, payload: Record<string, unknown>): Event =>
  ({ id: `e${sequence}`, goal_id: "g1", step_id: null, type: "log", payload, timestamp: sequence, sequence }) as Event;

const asked = (sequence: number, question: unknown, message = "Which one?\n\n1. A\n2. B"): Event =>
  reply(sequence, { level: "info", message, turn: true, question });

test("the question and its options come from the turn's reply", () => {
  const found = turnQuestion([asked(3, { text: "Which database?", options: ["SQLite", "Postgres"] })]);
  assert.ok(found);
  assert.equal(found.text, "Which database?");
  assert.deepEqual(found.options, ["SQLite", "Postgres"]);
});

test("a reply with no question, and no replies at all, are not questions", () => {
  assert.equal(turnQuestion([reply(1, { level: "info", message: "Just an answer.", turn: true })]), null);
  assert.equal(turnQuestion([]), null);
  assert.equal(turnQuestion(undefined), null);
  // A `question` on a log that is not the turn's reply is not the turn asking.
  assert.equal(turnQuestion([reply(1, { level: "info", message: "x", question: { text: "Q", options: ["a", "b"] } })]), null);
});

test("the newest reply decides, so a later plain answer clears an earlier question", () => {
  const events = [asked(2, { text: "Which?", options: ["A", "B"] }), reply(5, { level: "info", message: "Fine, done.", turn: true })];
  assert.equal(turnQuestion(events), null);
});

test("a question this build cannot read is not drawn, and does not take the answer's words with it", () => {
  for (const bad of [null, "Which?", 7, [], {}, { text: "" }, { text: "   " }, { text: 3 }, { options: ["a", "b"] }]) {
    assert.equal(turnQuestion([asked(1, bad)]), null, JSON.stringify(bad));
  }
});

test("an open question has no options", () => {
  assert.deepEqual(turnQuestion([asked(1, { text: "What should it be called?" })])?.options, []);
  assert.deepEqual(turnQuestion([asked(1, { text: "What should it be called?", options: [] })])?.options, []);
  assert.deepEqual(turnQuestion([asked(1, { text: "What?", options: "A, B" })])?.options, []);
});

test("options are plain, short, few and distinct: whatever the event said", () => {
  const long = "x".repeat(500);
  const options = [" A ", "a", "B\n\nC", long, "D", "E", 7, null, ""];
  const found = turnQuestion([asked(1, { text: "Which?", options })]);
  assert.ok(found);
  assert.ok(found.options.length <= MAX_QUESTION_OPTIONS, String(found.options.length));
  assert.deepEqual(found.options.slice(0, 2), ["A", "B C"], "trimmed, one line, and a repeat said once");
  for (const option of found.options) assert.ok(option.length <= MAX_QUESTION_OPTION_CHARS, option.length.toString());
  assert.ok(found.options[2]!.endsWith("…"), "a cut option says it was cut");
});

test("one option is not a choice, so there are no buttons for it", () => {
  assert.deepEqual(turnQuestion([asked(1, { text: "Which?", options: ["only this"] })])?.options, []);
  assert.deepEqual(turnQuestion([asked(1, { text: "Which?", options: ["same", "SAME"] })])?.options, []);
});

test("the limits are the engine's", () => {
  assert.equal(MAX_QUESTION_OPTIONS, 4);
  assert.equal(MAX_QUESTION_OPTION_CHARS, 80);
});

// ── through the App ───────────────────────────────────────────────────────

const THREAD = conversation({ id: "c1", title: "Thread" });
const PROSE = "Which database should this use?\n\n1. SQLite\n2. Postgres";

const turn = (id: string, createdAt: number, status = "COMPLETED") =>
  ({ id, conversation_id: "c1", title: `prompt ${id}`, status, mode: "chat", created_at: createdAt, updated_at: createdAt }) as const;

const eventsFor = (goalId: string, question: unknown, message = PROSE) => [
  {
    id: `${goalId}-1`,
    goal_id: goalId,
    step_id: null,
    type: "log",
    payload: { level: "info", message, turn: true, ...(question === undefined ? {} : { question }) },
    timestamp: 2,
    sequence: 1,
  },
];

const QUESTION = { text: "Which database should this use?", options: ["SQLite", "Postgres"] };

async function withThread(
  goals: ReturnType<typeof turn>[],
  goalEvents: Record<string, ReturnType<typeof eventsFor>>,
  body: (ctx: AppContext) => Promise<void>,
): Promise<void> {
  await withApp(
    { conversations: [THREAD], goals: goals as never, goalEvents: goalEvents as never },
    async (ctx) => {
      await ctx.dom.click(ctx.dom.container.querySelector('[data-thread-row="true"]') as Element);
      await ctx.settle();
      await body(ctx);
    },
  );
}

const choices = (ctx: AppContext): HTMLElement[] => [...ctx.dom.container.querySelectorAll<HTMLElement>("[data-ask-option]")];
const turnsSent = (ctx: AppContext) => ctx.engine.filter((c) => c.method === "POST" && /\/conversations\/[^/]+\/turns$/.test(c.path));

test("the options are buttons under the question, and the question is still in words above them", async () => {
  await withThread([turn("g1", 10)], { g1: eventsFor("g1", QUESTION) }, async (ctx) => {
    assert.deepEqual(
      choices(ctx).map((b) => b.textContent),
      ["SQLite", "Postgres"],
    );
    for (const b of choices(ctx)) assert.equal(b.tagName, "BUTTON");
    assert.match(ctx.dom.text(), /Which database should this use\?/);
    assert.equal(choices(ctx)[0]!.closest('[role="group"]')?.getAttribute("aria-label"), "Answer choices");
  });
});

test("pressing an option sends its words as an ordinary turn in the same thread, and nothing else", async () => {
  await withThread([turn("g1", 10)], { g1: eventsFor("g1", QUESTION) }, async (ctx) => {
    await ctx.dom.click(choices(ctx)[1]!);
    await ctx.settle();

    const sent = turnsSent(ctx);
    assert.equal(sent.length, 1, "pressing an option did not send exactly one turn");
    assert.equal(sent[0]!.path, "/conversations/c1/turns");
    assert.equal(sent[0]!.body?.prompt, "Postgres");
    assert.equal("answer" in (sent[0]!.body ?? {}), false, "an answer is a turn, not a new kind of request");
    assert.equal(
      ctx.engine.some((c) => /answer|question/i.test(c.path)),
      false,
      "the window called a route that does not exist",
    );
  });
});

test("once anything follows the question it offers nothing: the choices were for that moment", async () => {
  await withThread(
    [turn("g1", 10), turn("g2", 20)],
    { g1: eventsFor("g1", QUESTION), g2: eventsFor("g2", undefined, "Postgres it is.") },
    async (ctx) => {
      assert.equal(choices(ctx).length, 0, "an answered question is still offering its buttons");
      assert.match(ctx.dom.text(), /Which database should this use\?/, "the question itself stays in the history");
    },
  );
});

test("pressing an option removes the buttons, because the question now has an answer after it", async () => {
  await withThread([turn("g1", 10)], { g1: eventsFor("g1", QUESTION) }, async (ctx) => {
    await ctx.dom.click(choices(ctx)[0]!);
    await ctx.settle();

    assert.equal(choices(ctx).length, 0);
  });
});

test("an open question draws no buttons, and a question with no readable options draws none", async () => {
  await withThread([turn("g1", 10)], { g1: eventsFor("g1", { text: "What should it be called?" }, "What should it be called?") }, async (ctx) => {
    assert.equal(choices(ctx).length, 0);
    assert.equal(ctx.dom.container.querySelector('[aria-label="Answer choices"]') === null, true, "an empty group was drawn");
  });
});

test("an option the model filled with markup is text on a button, never an element", async () => {
  const hostile = { text: "Which?", options: ['<img src=x onerror="window.pwned=1">', "<b>bold</b>"] };
  await withThread([turn("g1", 10)], { g1: eventsFor("g1", hostile, "Which?") }, async (ctx) => {
    assert.equal(choices(ctx).length, 2);
    for (const b of choices(ctx)) assert.equal(b.querySelector("img, b") === null, true, b.innerHTML);
    assert.equal(choices(ctx)[1]!.textContent, "<b>bold</b>");
  });
});
