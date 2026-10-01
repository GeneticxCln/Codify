/**
 * Answers read aloud: the speaker on each answer, and auto-read.
 *
 * The transcript is mounted and then *re-rendered* the way the app feeds it: a turn in flight, then
 * the same turn finished with its reply. That is the only way to test what auto-read is for, which
 * is the difference between an answer the person watched arrive and one that was already there.
 * jsdom plays nothing, so `<audio>` records what it was asked to play and pause.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { ChatMessage, Event, Goal } from "../src/types.ts";

const { withDom } = await import("./dom.ts");
const { answersToRead } = await import("../src/speech.ts");
const React = (await import("react")).default;
const h = React.createElement;

// ── fixtures ────────────────────────────────────────────────────────────

const replyEvent = (goalId: string, message: string): Event => ({
  id: `${goalId}-reply`,
  goal_id: goalId,
  step_id: null,
  type: "log",
  payload: { turn: true, message },
  timestamp: 2,
  sequence: 2,
});

const turnGoal = (id: string, status: string, over: Partial<Goal> = {}): Goal => ({
  id,
  workspace_id: "w1",
  conversation_id: "c1",
  title: "a question",
  description: "a question",
  status,
  dry_run: false,
  plan_only: false,
  parallel: false,
  mode: "chat",
  trace: false,
  version: 1,
  created_at: 0,
  updated_at: 0,
  steps: [],
  ...over,
} as Goal);

/** A turn's answer message: in flight with no reply, or finished with one. */
const turn = (id: string, status: string, reply: string | null, over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: `assistant-${id}`,
  role: "assistant",
  content: "",
  timestamp: 1,
  goal: turnGoal(id, status),
  events: reply === null ? [] : [replyEvent(id, reply)],
  ...over,
});

// ── the rule ────────────────────────────────────────────────────────────

const memory = () => ({ watched: new Set<string>(), read: new Set<string>() });

test("auto-read takes only answers this window watched arrive, and each once", () => {
  const seen = memory();

  assert.deepEqual(answersToRead([turn("old", "COMPLETED", "from before")], seen), [], "history read itself");
  assert.deepEqual(answersToRead([turn("new", "PLANNING", null)], seen), []);
  // Over, but the reply is not in yet: wait for it rather than give up on it.
  assert.deepEqual(answersToRead([turn("new", "COMPLETED", null)], seen), []);

  const finished = [turn("old", "COMPLETED", "from before"), turn("new", "COMPLETED", "the answer")];
  assert.deepEqual(answersToRead(finished, seen), [{ id: "assistant-new", text: "the answer" }]);
  assert.deepEqual(answersToRead(finished, seen), [], "the same answer was read twice");
});

test("a turn still being dispatched counts as watched, and a turn that planned is never read", () => {
  const seen = memory();
  const dispatching = turn("t", "COMPLETED", null, { isStreaming: true });
  answersToRead([dispatching], seen);
  assert.deepEqual(answersToRead([turn("t", "COMPLETED", "done")], seen), [{ id: "assistant-t", text: "done" }]);

  const run = (status: string) =>
    turn("run", status, "planned", { goal: turnGoal("run", status, { steps: [{ id: "s1" } as never] }) });
  answersToRead([run("RUNNING")], seen);
  assert.deepEqual(answersToRead([run("COMPLETED")], seen), [], "a run's card was read as an answer");
});

// ── mounted ─────────────────────────────────────────────────────────────

interface Speaker {
  /** Every text the engine was asked to speak, in order. */
  spoken: string[];
  /** Object URLs `<audio>` was asked to play, and pause. */
  played: string[];
  paused: string[];
  /** Re-render the transcript over these messages, as the app does when they change. */
  show(messages: ChatMessage[]): Promise<void>;
}

interface EngineVoice {
  autoRead?: boolean;
  speakRefusal?: { code: string; message: string };
}

async function withTranscript(voice: EngineVoice, body: (dom: Dom, io: Speaker) => Promise<void>): Promise<void> {
  await withDom(async (dom) => {
    const io: Speaker = { spoken: [], played: [], paused: [], show: async () => {} };
    let urls = 0;
    const saved = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = (() => `blob:answer-${++urls}`) as typeof URL.createObjectURL;
    URL.revokeObjectURL = (() => {}) as typeof URL.revokeObjectURL;
    const proto = dom.window.HTMLMediaElement.prototype;
    proto.play = async function (this: HTMLMediaElement) {
      io.played.push(this.src);
    };
    proto.pause = function (this: HTMLMediaElement) {
      io.paused.push(this.src);
    };
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname;
      if (path === "/audio/status") {
        return new Response(
          JSON.stringify({
            dictation: { provider: "", model: "", configured: false, reason: "not set" },
            read_aloud: { provider: "openai", model: "tts-1", configured: true, reason: null },
            recorder: { available: true, reason: null, recording: false, max_seconds: 120 },
            auto_read: voice.autoRead ?? false,
          }),
          { status: 200 },
        );
      }
      if (path === "/audio/speak") {
        io.spoken.push(String(JSON.parse(String(init?.body)).text));
        if (voice.speakRefusal) return new Response(JSON.stringify(voice.speakRefusal), { status: 409 });
        return new Response(new Blob(["RIFF"], { type: "audio/wav" }), { status: 200 });
      }
      throw new Error(`no answer for ${path}`);
    }) as typeof fetch;

    const speech = await import("../src/speech.ts");
    speech.stopPlayback();
    const { ChatTimeline } = await import("../src/components/ChatTimeline.tsx");
    io.show = async (messages) => {
      await dom.render(
        h(ChatTimeline, {
          messages,
          pinnedContracts: {},
          onStartGoal: () => {},
          onEnableExecution: () => {},
          onApplyGoal: () => {},
          onEditStep: () => true,
          onPauseGoal: () => {},
          onCancelGoal: () => {},
          onSetGoalTrace: () => {},
          onDeleteGoal: () => {},
          onRetryStep: () => {},
          onOpenSettings: () => {},
          onImportAudit: () => {},
          onPinDesignContract: async () => {},
        }),
      );
      // Auto-read is a chain of answers (the engine's switch, then the audio), each a turn of the
      // event loop, so one settle is not enough to see where it ends.
      for (let i = 0; i < 4; i++) await dom.settle();
    };
    try {
      await body(dom, io);
    } finally {
      // Inside `act`: stopping tells every speaker still on screen, and that is a render.
      await React.act(async () => speech.stopPlayback());
      URL.createObjectURL = saved.create;
      URL.revokeObjectURL = saved.revoke;
    }
  });
}

/** The speaker on one answer, found inside that answer's own card. */
const speakerOf = (dom: Dom, answer: string): HTMLButtonElement => {
  const buttons = [...dom.container.querySelectorAll("button[aria-pressed]")] as HTMLButtonElement[];
  const found = buttons.filter((b) => b.closest(".space-y-2")?.textContent?.includes(answer));
  if (found.length !== 1) throw new Error(`expected one speaker on "${answer}", found ${found.length}`);
  return found[0];
};

test("the speaker reads the answer's words, not its markup, and stops it", async () => {
  await withTranscript({}, async (dom, io) => {
    await io.show([turn("t1", "COMPLETED", "The **fix** is in `parse_line`.\n\n```py\nx = 1\n```")]);
    assert.deepEqual(io.spoken, [], "a finished answer read itself with auto-read off");

    await dom.click(speakerOf(dom, "The **fix**"));
    await dom.settle();

    assert.deepEqual(io.spoken, ["The fix is in parse_line.\n(code omitted)"]);
    assert.deepEqual(io.played, ["blob:answer-1"]);
    assert.equal(speakerOf(dom, "The **fix**").textContent, "Stop reading");

    await dom.click(speakerOf(dom, "The **fix**"));
    assert.deepEqual(io.paused, ["blob:answer-1"]);
    assert.equal(speakerOf(dom, "The **fix**").textContent, "Read aloud");
  });
});

test("one voice at a time: reading a second answer stops the first", async () => {
  await withTranscript({}, async (dom, io) => {
    await io.show([turn("a", "COMPLETED", "First answer."), turn("b", "COMPLETED", "Second answer.")]);

    await dom.click(speakerOf(dom, "First answer."));
    await dom.settle();
    await dom.click(speakerOf(dom, "Second answer."));
    await dom.settle();

    assert.deepEqual(io.paused, ["blob:answer-1"]);
    assert.equal(speakerOf(dom, "First answer.").textContent, "Read aloud");
    assert.equal(speakerOf(dom, "Second answer.").textContent, "Stop reading");
  });
});

test("an answer still arriving has no speaker", async () => {
  await withTranscript({}, async (dom, io) => {
    await io.show([turn("t", "PLANNING", null)]);
    assert.ok(dom.container.querySelectorAll("button[aria-pressed]").length === 0, "a draft can be read aloud");
    assert.deepEqual(io.spoken, []);
  });
});

test("auto-read reads the answer that arrives, once, and never the history above it", async () => {
  await withTranscript({ autoRead: true }, async (_dom, io) => {
    const history = turn("old", "COMPLETED", "An old answer.");
    await io.show([history]);
    await io.show([history, turn("new", "PLANNING", null)]);
    await io.show([history, turn("new", "COMPLETED", "A new answer.")]);

    assert.deepEqual(io.spoken, ["A new answer."]);
    assert.deepEqual(io.played, ["blob:answer-1"]);

    await io.show([history, turn("new", "COMPLETED", "A new answer.")]);
    assert.deepEqual(io.spoken, ["A new answer."], "the same answer was read again on a re-render");
  });
});

test("with auto-read off, an answer that arrives waits for its button", async () => {
  await withTranscript({ autoRead: false }, async (_dom, io) => {
    await io.show([turn("new", "PLANNING", null)]);
    await io.show([turn("new", "COMPLETED", "A new answer.")]);

    assert.deepEqual(io.spoken, []);
  });
});

test("an auto-read that cannot run says why on the answer", async () => {
  await withTranscript(
    { autoRead: true, speakRefusal: { code: "tts_not_configured", message: "read-aloud has no provider and model yet" } },
    async (dom, io) => {
      await io.show([turn("new", "PLANNING", null)]);
      await io.show([turn("new", "COMPLETED", "A new answer.")]);
      await dom.settle();

      assert.deepEqual(io.spoken, ["A new answer."]);
      const alert = dom.container.querySelector('[role="alert"]')?.textContent ?? "";
      assert.match(alert, /read-aloud has no provider and model yet/);
    },
  );
});
