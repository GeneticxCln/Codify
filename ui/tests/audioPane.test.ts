/**
 * Settings → Audio, mounted against an engine that answers from a table.
 *
 * Every assertion is about what the pane *did*: which keys it saved, what it asked the engine for,
 * and which of the engine's reasons it put in front of the person. The engine's own behaviour is
 * tested on its side (`tests/test_speech.py`); this is the screen that drives it.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Route = (body: unknown) => Response;

const str = (value: string) => ({ value, max: 128 });

/** What a configured engine answers, before a test changes any of it. */
function engine(): Record<string, Route> {
  return {
    "GET /settings/engine": () =>
      json({
        parallel_width: { value: 4, min: 1, max: 16 },
        stt_provider: str("openai"),
        stt_model: str("whisper-1"),
        stt_language: str(""),
        tts_provider: str("openai"),
        tts_model: str("tts-1"),
        tts_voice: str("alloy"),
        audio_input: str(""),
        tts_auto_read: { value: 0, min: 0, max: 1 },
      }),
    "GET /settings/providers": () =>
      json({ builtins: [{ slug: "openai" }, { slug: "groq" }, { slug: "ollama" }], custom: [] }),
    "GET /audio/inputs": () =>
      json({
        available: true,
        reason: null,
        inputs: [
          { name: "usb.mic", description: "USB Microphone", default: true },
          { name: "builtin.mic", description: "Built-in", default: false },
        ],
      }),
    "GET /audio/status": () =>
      json({
        dictation: { provider: "openai", model: "whisper-1", configured: true, reason: null },
        read_aloud: { provider: "openai", model: "tts-1", configured: true, reason: null },
        recorder: { available: true, reason: null, recording: false, max_seconds: 120 },
        auto_read: false,
      }),
    "PUT /settings/engine": (body) => json({ saved: body }),
    "POST /audio/dictation/start": () => json({ recording: true, max_seconds: 120 }),
    "POST /audio/dictation/stop": () => json({ text: "hello world", seconds: 1.2 }),
    "POST /audio/speak": () => new Response(new Blob(["RIFF"], { type: "audio/wav" }), { status: 200 }),
  };
}

async function withPane(
  overrides: Record<string, Route>,
  body: (dom: Dom, calls: Call[]) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const routes = { ...engine(), ...overrides };
    const calls: Call[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const method = (init?.method ?? "GET").toUpperCase();
      const parsed = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      calls.push({ method, path: url.pathname, body: parsed });
      const route = routes[`${method} ${url.pathname}`];
      if (!route) throw new Error(`no answer for ${method} ${url.pathname}`);
      return route(parsed);
    }) as typeof fetch;
    const { AudioPane } = await import("../src/components/AudioPane.tsx");
    await dom.render(h(AudioPane, { models: [], providerStatus: [] }));
    await dom.settle();
    await body(dom, calls);
  });
}

const called = (calls: Call[], method: string, path: string): Call[] =>
  calls.filter((c) => c.method === method && c.path === path);

test("the pane shows what the engine has, and the engine's reason for what cannot run", async () => {
  await withPane(
    {
      "GET /audio/status": () =>
        json({
          dictation: {
            provider: "", model: "", configured: false,
            reason: "dictation has no provider and model yet: choose them in Settings → Audio",
          },
          read_aloud: { provider: "openai", model: "tts-1", configured: true, reason: null },
          recorder: { available: true, reason: null, recording: false, max_seconds: 120 },
          auto_read: false,
        }),
    },
    async (dom) => {
      assert.match(dom.text(), /dictation has no provider and model yet/);
      assert.match(dom.text(), /Read-aloud is ready: openai · tts-1/);
      assert.equal((dom.byField("Voice") as HTMLInputElement).value, "alloy");
      const mics = Array.from((dom.byField("Input") as HTMLSelectElement).options).map((o) => o.textContent);
      assert.deepEqual(mics, ["Default microphone", "USB Microphone (default)", "Built-in"]);
      assert.ok((dom.byButton("Try dictation") as HTMLButtonElement).disabled, "dictation that cannot run was offered");
    },
  );
});

test("saving sends the voice keys as edited, then reads the status again", async () => {
  await withPane({}, async (dom, calls) => {
    const save = dom.byButton("Save audio settings") as HTMLButtonElement;
    assert.ok(save.disabled, "nothing changed, and Save was enabled");

    await dom.fill(dom.byField("Voice") as HTMLInputElement, "nova");
    await dom.fill(dom.byField("Language (optional)") as HTMLInputElement, "en");
    const mic = dom.byField("Input") as HTMLSelectElement;
    await React.act(async () => {
      mic.value = "usb.mic";
      mic.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    });
    await dom.click(dom.byButton("Read each answer aloud as it arrives"));
    await dom.click(dom.byButton("Save audio settings"));
    await dom.settle();

    const [put] = called(calls, "PUT", "/settings/engine");
    assert.deepEqual(put.body, {
      stt_provider: "openai",
      stt_model: "whisper-1",
      stt_language: "en",
      tts_provider: "openai",
      tts_model: "tts-1",
      tts_voice: "nova",
      audio_input: "usb.mic",
      tts_auto_read: true,
    });
    assert.equal(called(calls, "GET", "/audio/status").length, 2, "the status was not read again after saving");
    assert.match(dom.text(), /Saved\./);
  });
});

test("without PipeWire the microphone section says what is missing instead of offering a list", async () => {
  await withPane(
    {
      "GET /audio/inputs": () =>
        json({
          available: false,
          reason: "pw-dump was not found: voice input needs PipeWire's tools (the pipewire package)",
          inputs: [],
        }),
    },
    async (dom) => {
      assert.match(dom.text(), /voice input needs PipeWire's tools/);
      assert.throws(() => dom.byField("Input"), /no field is labelled "Input"/, "a microphone list was offered without PipeWire");
    },
  );
});

test("try dictation records, stops, and shows what was heard", async () => {
  await withPane({}, async (dom, calls) => {
    await dom.click(dom.byButton("Try dictation"));
    await dom.settle();
    await dom.click(dom.byButton("Stop and transcribe"));
    await dom.settle();

    assert.equal(called(calls, "POST", "/audio/dictation/start").length, 1);
    assert.equal(called(calls, "POST", "/audio/dictation/stop").length, 1);
    assert.match(dom.text(), /“hello world”/);
  });
});

test("a refusal from the engine is shown, in the engine's words", async () => {
  await withPane(
    {
      "POST /audio/dictation/start": () =>
        json(
          { code: "recorder_unavailable", message: "pw-record was not found: voice input needs PipeWire's tools" },
          503,
        ),
    },
    async (dom) => {
      await dom.click(dom.byButton("Try dictation"));
      await dom.settle();

      assert.match(dom.text(), /pw-record was not found/);
      const alert = dom.container.querySelector("[role=alert]")?.textContent ?? "";
      assert.match(alert, /pw-record was not found/, "the refusal was not announced as an alert");
    },
  );
});

test("an unsaved change holds the trials back until it is saved", async () => {
  await withPane({}, async (dom) => {
    await dom.fill(dom.byField("Voice") as HTMLInputElement, "nova");

    const trial = dom.byButton("Try dictation") as HTMLButtonElement;
    assert.ok(trial.disabled);
    assert.match(trial.title, /Save first/);
    assert.ok((dom.byButton("Play sample") as HTMLButtonElement).disabled);
  });
});

test("play sample asks the engine for the sample and plays it", async () => {
  await withPane({}, async (dom, calls) => {
    const played: string[] = [];
    dom.window.HTMLMediaElement.prototype.play = async function (this: HTMLMediaElement) {
      played.push(this.src);
    };
    const saved = URL.createObjectURL;
    URL.createObjectURL = (() => "blob:sample") as typeof URL.createObjectURL;
    try {
      await dom.click(dom.byButton("Play sample"));
      await dom.settle();
    } finally {
      URL.createObjectURL = saved;
    }

    const [asked] = called(calls, "POST", "/audio/speak");
    assert.deepEqual(asked.body, { text: "This is how Codify will read its answers aloud." });
    assert.deepEqual(played, ["blob:sample"]);
  });
});

test("an engine without voice settings is told so, and nothing is offered to save", async () => {
  await withPane(
    { "GET /settings/engine": () => json({ parallel_width: { value: 4, min: 1, max: 16 } }) },
    async (dom) => {
      assert.match(dom.text(), /This engine has no voice settings/);
      const offered = Array.from(dom.container.querySelectorAll("button")).map((el) => el.textContent);
      assert.deepEqual(offered, [], "an engine without voice settings was offered controls");
    },
  );
});
