/**
 * What an answer sounds like, where dictated words land, and the one player (`src/speech.ts`).
 *
 * The player is driven through a real DOM: an `<audio>` element whose `play` and `pause` are
 * recorded, because jsdom plays nothing and a test that only checked the calls were *made* would
 * not see which answer was stopped for which.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

// Dynamic, after the loader: `speech.ts` reaches `turnTranscript.ts`, whose extensionless imports
// only resolve once `registerTsx` has run, and a static import is hoisted above it.
const { clipForSpeech, formatElapsed, insertDictation, speakableText } = await import("../src/speech.ts");
const { withDom } = await import("./dom.ts");

test("an answer is read as its words, not its markup", () => {
  const spoken = speakableText(
    [
      "## Fixing the parser",
      "",
      "The **bug** was in `parse_line`, see [the issue](https://example.com/1).",
      "",
      "```python",
      "def parse_line(text):",
      "    return text.split()",
      "```",
      "",
      "- first, the _tokenizer_",
      "- then file_name handling",
      "",
      "> quoted note",
    ].join("\n"),
  );

  assert.equal(
    spoken,
    [
      "Fixing the parser",
      "The bug was in parse_line, see the issue.",
      "(code omitted)",
      "first, the tokenizer",
      "then file_name handling",
      "quoted note",
    ].join("\n"),
  );
});

test("a long answer is cut at a sentence end inside the limit", () => {
  const text = "One sentence here. " + "Another one. ".repeat(10);

  const clipped = clipForSpeech(text, 60);

  assert.ok(clipped.length <= 60, clipped);
  assert.ok(clipped.endsWith("."), `cut mid-sentence: ${JSON.stringify(clipped)}`);
  assert.equal(clipForSpeech("short", 60), "short");
});

test("dictated words land at the caret with a space where two words would touch", () => {
  assert.deepEqual(insertDictation("", 0, 0, " hello there "), { value: "hello there", caret: 11 });
  assert.deepEqual(insertDictation("fix the", 7, 7, "parser"), { value: "fix the parser", caret: 14 });
  assert.deepEqual(insertDictation("fix  bug", 4, 4, "the"), { value: "fix the bug", caret: 7 });
  assert.deepEqual(insertDictation("fix it", 4, 6, "the parser"), { value: "fix the parser", caret: 14 });
  assert.deepEqual(insertDictation("keep", 4, 4, "   "), { value: "keep", caret: 4 }, "silence inserts nothing");
});

test("a recording's time reads as minutes and seconds", () => {
  assert.equal(formatElapsed(0), "0:00");
  assert.equal(formatElapsed(7.9), "0:07");
  assert.equal(formatElapsed(125), "2:05");
});

interface Played {
  played: string[];
  paused: string[];
  /** Every element the player asked to play, so a test can end one the way the browser would. */
  elements: HTMLMediaElement[];
}

/** A DOM whose `<audio>` records what it was asked to do, and object URLs that name their blob. */
async function withPlayer(body: (dom: Dom, record: Played) => Promise<void>): Promise<void> {
  await withDom(async (dom) => {
    const record: Played = { played: [], paused: [], elements: [] };
    const proto = dom.window.HTMLMediaElement.prototype;
    proto.play = async function (this: HTMLMediaElement) {
      record.played.push(this.src);
      record.elements.push(this);
    };
    proto.pause = function (this: HTMLMediaElement) {
      record.paused.push(this.src);
    };
    const saved = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    let next = 0;
    URL.createObjectURL = (() => `blob:audio-${++next}`) as typeof URL.createObjectURL;
    URL.revokeObjectURL = (() => {}) as typeof URL.revokeObjectURL;
    try {
      const speech = await import("../src/speech.ts");
      speech.stopPlayback();
      await body(dom, record);
      speech.stopPlayback();
    } finally {
      URL.createObjectURL = saved.create;
      URL.revokeObjectURL = saved.revoke;
    }
  });
}

const audioOf = (label: string) => async (): Promise<Blob> => new Blob([label], { type: "audio/wav" });

test("one voice at a time: a second answer stops the first", async () => {
  await withPlayer(async (_dom, record) => {
    const { nowPlaying, onPlayback, playSpeech } = await import("../src/speech.ts");
    const heard: Array<string | null> = [];
    const off = onPlayback((id) => heard.push(id));

    await playSpeech("answer-1", "first", audioOf("one"));
    assert.equal(nowPlaying(), "answer-1");
    await playSpeech("answer-2", "second", audioOf("two"));

    assert.equal(nowPlaying(), "answer-2");
    assert.deepEqual(record.played, ["blob:audio-1", "blob:audio-2"]);
    assert.deepEqual(record.paused, ["blob:audio-1"], "the first answer was not stopped");
    off();
    assert.ok(heard.includes("answer-1") && heard.at(-1) === "answer-2", JSON.stringify(heard));
  });
});

test("a stop while the audio is still being fetched wins over the late audio", async () => {
  await withPlayer(async (_dom, record) => {
    const { nowPlaying, playSpeech, stopPlayback } = await import("../src/speech.ts");
    let deliver: (blob: Blob) => void = () => {};
    const slow = (): Promise<Blob> => new Promise((resolve) => (deliver = resolve));

    const started = playSpeech("answer-1", "text", slow);
    assert.equal(nowPlaying(), "answer-1", "a fetch in flight is shown as starting");
    stopPlayback();
    deliver(new Blob(["late"]));
    await started;

    assert.equal(nowPlaying(), null);
    assert.deepEqual(record.played, [], "audio that arrived after a stop was played anyway");
  });
});

test("a failed fetch leaves nothing playing and says why", async () => {
  await withPlayer(async () => {
    const { nowPlaying, playSpeech } = await import("../src/speech.ts");

    await assert.rejects(
      playSpeech("answer-1", "text", async () => {
        throw new Error("tts_not_configured: read-aloud has no voice yet");
      }),
      /tts_not_configured/,
    );
    assert.equal(nowPlaying(), null);
  });
});

test("an answer that finishes on its own is no longer playing", async () => {
  await withPlayer(async (dom, record) => {
    const { nowPlaying, playSpeech } = await import("../src/speech.ts");

    await playSpeech("answer-1", "text", audioOf("one"));
    assert.equal(nowPlaying(), "answer-1");
    assert.ok(dom.window.document.querySelector("audio") === null, "the player leaves no element in the page");
    record.elements[0].dispatchEvent(new dom.window.Event("ended"));

    assert.equal(nowPlaying(), null, "a finished answer still showed as playing");
  });
});
