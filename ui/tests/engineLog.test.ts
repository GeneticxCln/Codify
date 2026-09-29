/**
 * The engine's stderr, and which of it a person should read.
 *
 * The case that matters is the first test: the bounded-shutdown backstop's one
 * line is the entire explanation of an engine that disappeared while holding a
 * hung websocket, and it arrives buried in a session's worth of ordinary
 * output. A filter that just kept the tail would show that line only when it
 * happened to be one of the last three, which is exactly when it is not.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
import { STDERR_NOTABLE, notableStderrLines } from "../src/engineLog.ts";

/** What the backstop prints when a graceful shutdown does not finish in time. */
const BACKSTOP = "[engine] shutdown unfinished after 6s — exiting anyway";

test("the bounded-shutdown warning survives a session's worth of chatter", () => {
  const noise = Array.from(
    { length: 40 },
    (_unused, i) => `INFO: connection closed by peer ${i}`,
  );
  const lines = [
    "[engine] parent watchdog armed on pid 1234 (poll 1s)",
    ...noise,
    "Uvicorn running on http://127.0.0.1:7430",
    ...noise,
    BACKSTOP,
  ];
  const shown = notableStderrLines(lines);
  assert.ok(
    shown.includes(BACKSTOP),
    `the one line that explains the exit has to be on screen: ${JSON.stringify(shown)}`,
  );
  // And it is a quote, not a verdict: the lines that came after it are shown
  // too, so the reader sees the engine's last moments rather than one sentence
  // with no context.
  assert.deepEqual(shown, [
    "INFO: connection closed by peer 38",
    "INFO: connection closed by peer 39",
    BACKSTOP,
  ]);
  // The boot banner forty lines up is neither notable nor recent, so it is not
  // what fills the panel.
  assert.ok(
    !shown.some((line) => line.includes("Uvicorn running")),
    "the tail is the tail: a line from the middle of a session is not context",
  );
});

test("a provider error is notable even without the word traceback", () => {
  const shown = notableStderrLines([
    "state dir /home/me/.codify: database /home/me/.codify/codify.db",
    "httpx: connection refused while calling the provider",
    "retrying in 2s",
  ]);
  assert.deepEqual(shown, [
    "state dir /home/me/.codify: database /home/me/.codify/codify.db",
    "httpx: connection refused while calling the provider",
    "retrying in 2s",
  ]);
});

test("the last lines are shown even when nothing matches", () => {
  // A panel that renders nothing because no keyword matched is the same blank
  // panel this exists to remove.
  const shown = notableStderrLines(["a", "b", "c", "d"]);
  assert.deepEqual(shown, ["b", "c", "d"]);
});

test("lines come back in the order the engine said them", () => {
  const shown = notableStderrLines([
    "first: an error happened",
    "second: nothing to report",
    "third: another error",
    "fourth: and another",
  ]);
  assert.deepEqual(shown, [
    "first: an error happened",
    "third: another error",
    "fourth: and another",
  ]);
});

test("blank lines are dropped rather than shown as space", () => {
  assert.deepEqual(notableStderrLines(["", "   ", "\t"]), []);
  assert.deepEqual(notableStderrLines(["", "  shutdown complete  ", ""]), [
    "shutdown complete",
  ]);
});

test("repeated identical lines stay repeated", () => {
  // A Set of line *strings* would collapse these into one and the panel would
  // under-report how often the engine said it.
  const shown = notableStderrLines(["connection reset", "connection reset"]);
  assert.deepEqual(shown, ["connection reset", "connection reset"]);
});

test("only the newest notable lines are kept when there are too many", () => {
  const lines = Array.from({ length: 12 }, (_unused, i) => `error number ${i}`);
  const shown = notableStderrLines(lines, { matches: 3, context: 1 });
  assert.deepEqual(shown, ["error number 9", "error number 10", "error number 11"]);
});

test("an empty tail is an empty panel, not a crash", () => {
  assert.deepEqual(notableStderrLines([]), []);
  // Outside Tauri the shell has no pipe to tail, and says so with [].
  assert.deepEqual(notableStderrLines([], { matches: 0, context: 0 }), []);
});

test("the parent's death is notable, being how an orphan announces itself", () => {
  assert.ok(
    STDERR_NOTABLE.test("[engine] parent process 2366209 is gone — exiting"),
    "the watchdog's line is one of the two the panel exists to surface",
  );
});

registerTsx();

test("a window whose engine is gone shows the engine's last words, and only the notable ones", async () => {
  // The filter being right is worth nothing if nothing calls it, and the browser
  // build cannot see this: outside the shell `fetchEngineStderr` returns [] and
  // the panel never appears. So this mounts the app under the shell with an engine
  // that is not answering and a session's worth of stderr, and reads the screen.
  const { withApp } = await import("./appHarness.ts");
  const chatter = Array.from({ length: 80 }, (_unused, i) => `INFO: connection closed by peer ${i}`);
  const tail = ["[engine] parent watchdog armed on pid 1234 (poll 1s)", ...chatter, BACKSTOP, "INFO: bye"];
  await withApp(
    { health: "down", shellAnswers: { codify_engine_log: tail } },
    async ({ dom, shell, settle }) => {
      await settle();
      assert.ok(shell.calls.includes("codify_engine_log"), "the window never asked the shell for the engine's stderr");
      const text = dom.container.textContent ?? "";
      assert.match(text, /What the engine said before it stopped/, "the tail was fetched but never rendered");
      assert.ok(text.includes(BACKSTOP), "the shutdown backstop's line is not on screen");
      assert.ok(!text.includes("connection closed by peer 3"), "ordinary chatter crowded the panel");
    },
  );
});

test("a window whose engine is up shows no engine log at all", async () => {
  const { withApp } = await import("./appHarness.ts");
  await withApp({ shellAnswers: { codify_engine_log: [BACKSTOP] } }, async ({ dom, shell, settle }) => {
    await settle();
    assert.ok(!shell.calls.includes("codify_engine_log"), "the log was fetched for a healthy engine");
    assert.doesNotMatch(dom.container.textContent ?? "", /What the engine said before it stopped/);
  });
});
