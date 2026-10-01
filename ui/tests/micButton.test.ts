/**
 * The mic beside Send: dictation into the prompt, mounted.
 *
 * Most of this runs through the whole App, because what matters is where the words land in the
 * real composer and that a press of the mic never sends anything. Two claims are about the button
 * alone (Esc not reaching the prompt behind it, and a composer going away mid-recording), and those
 * mount the button by itself, where the thing behind it can be a recorder rather than a whole app.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext, EngineCall } from "./appHarness.ts";

const { withApp } = await import("./appHarness.ts");
const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;

const RECORDING = { body: { recording: true, max_seconds: 120 } };

const posted = (engine: EngineCall[], path: string): number =>
  engine.filter((c) => c.method === "POST" && c.path === path).length;

const prompt = (ctx: AppContext): HTMLTextAreaElement => ctx.dom.byField("Chat prompt") as HTMLTextAreaElement;

test("dictated words land at the caret, and nothing is sent", async () => {
  await withApp(
    {
      answers: {
        "POST /audio/dictation/start": RECORDING,
        "POST /audio/dictation/stop": { body: { text: " parser ", seconds: 1.4 } },
      },
    },
    async (ctx) => {
      const { dom } = ctx;
      const box = prompt(ctx);
      await dom.fill(box, "fix the bug");
      box.setSelectionRange(8, 8);

      await dom.click(dom.byLabel("Dictate"));
      await ctx.settle();
      assert.equal(posted(ctx.engine, "/audio/dictation/start"), 1);
      assert.match(dom.container.querySelector('[role="timer"]')?.textContent ?? "", /0:0\d/);

      await dom.click(dom.byLabel("Stop dictation and transcribe"));
      await ctx.settle();

      assert.equal(box.value, "fix the parser bug");
      assert.equal(box.selectionStart, 14, "the caret was not left after the dictated words");
      assert.equal(posted(ctx.engine, "/audio/dictation/stop"), 1);
      const sent = ctx.engine.filter((c) => c.method === "POST" && /\/turns$|^\/goals$/.test(c.path));
      assert.deepEqual(sent.map((c) => c.path), [], "dictation sent the prompt");
      assert.ok(dom.allByLabel("Dictate").length === 1, "the mic did not return to idle");
    },
  );
});

test("words typed while speaking are kept: the transcript is placed when it arrives", async () => {
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  await withApp(
    {
      answers: {
        "POST /audio/dictation/start": RECORDING,
        "POST /audio/dictation/stop": { body: { text: "please", seconds: 1 } },
      },
      beforeRespond: (call) => (call.path === "/audio/dictation/stop" ? held : undefined),
    },
    async (ctx) => {
      const { dom } = ctx;
      await dom.click(dom.byLabel("Dictate"));
      await ctx.settle();
      await dom.click(dom.byLabel("Stop dictation and transcribe"));
      await ctx.settle();
      assert.ok(dom.allByLabel("Transcribing…").length === 1, "no sign that the words are on their way");

      const box = prompt(ctx);
      await dom.fill(box, "run the tests");
      box.setSelectionRange(13, 13);
      release();
      await ctx.settle();

      assert.equal(box.value, "run the tests please");
    },
  );
});

test("with no dictation provider, the mic opens Settings → Audio instead of recording", async () => {
  await withApp(
    {
      answers: {
        "POST /audio/dictation/start": {
          status: 409,
          body: { code: "stt_not_configured", message: "dictation has no provider and model yet" },
        },
      },
    },
    async (ctx) => {
      await ctx.dom.click(ctx.dom.byLabel("Dictate"));
      await ctx.settle();

      assert.match(ctx.dom.text(), /The microphone, dictation into the prompt, and answers read aloud/);
      assert.ok(ctx.dom.allByLabel("Stop dictation and transcribe").length === 0, "it recorded anyway");
    },
  );
});

test("a refusal is shown beside the mic, in the engine's words", async () => {
  await withApp(
    {
      answers: {
        "POST /audio/dictation/start": {
          status: 503,
          body: { code: "recorder_unavailable", message: "pw-record was not found: voice input needs PipeWire's tools" },
        },
      },
    },
    async (ctx) => {
      await ctx.dom.click(ctx.dom.byLabel("Dictate"));
      await ctx.settle();

      const alert = ctx.dom.container.querySelector('[role="alert"]')?.textContent ?? "";
      assert.match(alert, /pw-record was not found/);
      assert.ok(ctx.dom.allByLabel("Dictate").length === 1, "the mic did not return to idle");
    },
  );
});

test("at the engine's limit the recording is transcribed, not lost", async () => {
  await withApp(
    {
      answers: {
        "POST /audio/dictation/start": { body: { recording: true, max_seconds: 0.3 } },
        "POST /audio/dictation/stop": { body: { text: "long thought", seconds: 0.3 } },
      },
    },
    async (ctx) => {
      await ctx.dom.click(ctx.dom.byLabel("Dictate"));
      await ctx.act(() => new Promise((resolve) => setTimeout(resolve, 700)));
      await ctx.settle();

      assert.equal(posted(ctx.engine, "/audio/dictation/stop"), 1);
      assert.equal(prompt(ctx).value, "long thought");
    },
  );
});

/** The button alone, against an engine that records what it was asked. */
async function withMic(
  body: (dom: import("./dom.ts").Dom, asked: string[], heardBehind: string[]) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const asked: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      asked.push(path);
      const answer = path.endsWith("/start") ? { recording: true, max_seconds: 120 } : { cancelled: true };
      return new Response(JSON.stringify(answer), { status: 200 });
    }) as typeof fetch;
    const { MicButton } = await import("../src/components/MicButton.tsx");
    // What sits behind the mic in the composer: a prompt whose own Esc would stop a goal.
    const heardBehind: string[] = [];
    await dom.render(
      h(
        "div",
        { onKeyDown: (e: React.KeyboardEvent) => heardBehind.push(e.key) },
        h("textarea", { "aria-label": "Chat prompt" }),
        h(MicButton, { onTranscript: () => {}, onNeedsSetup: () => {} }),
      ),
    );
    await body(dom, asked, heardBehind);
  });
}

test("Esc while recording discards it, and does not also reach the prompt behind", async () => {
  await withMic(async (dom, asked, heardBehind) => {
    await dom.click(dom.byLabel("Dictate"));
    await dom.settle();

    await dom.press(dom.byField("Chat prompt"), "Escape");
    await dom.settle();

    assert.deepEqual(asked, ["/audio/dictation/start", "/audio/dictation/cancel"]);
    assert.deepEqual(heardBehind.filter((k) => k === "Escape"), [], "the same Esc would also have stopped the goal");
    assert.ok(dom.allByLabel("Dictate").length === 1, "the mic did not return to idle");

    await dom.press(dom.byField("Chat prompt"), "Escape");
    assert.deepEqual(heardBehind.filter((k) => k === "Escape"), ["Escape"], "Esc stayed captured after the recording ended");
  });
});

test("a composer that goes away mid-recording closes the microphone", async () => {
  await withMic(async (dom, asked) => {
    await dom.click(dom.byLabel("Dictate"));
    await dom.settle();

    await dom.render(h(React.Fragment));
    await dom.settle();

    assert.deepEqual(asked, ["/audio/dictation/start", "/audio/dictation/cancel"]);
  });
});
