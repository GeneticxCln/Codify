/**
 * Reading a rejection, whatever shape it arrived in.
 *
 * This exists because `err?.message` silently threw away the two most useful
 * messages in the app. `terminal::open` and `browser::open` return
 * `Result<String, String>`, and Tauri 2.11.6 rejects with that string; an ACL
 * denial in `RuntimeAuthority::resolve_access` is a formatted `String` too. Both
 * produced `undefined` for `.message` and a generic fallback, which is how a
 * shell that would not start showed "Could not start a shell" and never the
 * engine's own sentence.
 *
 * The string cases are the point of the file. An `Error`-only test would pass
 * against the old `err?.message` code and prove nothing.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { readRejection } = await import("../src/rejection.ts");

test("a Rust command's refusal arrives as a bare string, and is read", () => {
  // Exactly what `terminal::open` rejects with when `pin_cwd` cannot resolve a
  // workspace. This is the sentence that tells a user what is actually wrong.
  const refusal =
    "workspace w1 has no root_path recorded, so there is nowhere to start a shell";
  assert.equal(readRejection(refusal, "Could not start a shell"), refusal);
});

test("an ACL denial arrives as a bare string, and is read", () => {
  const denial =
    'codify_terminal_open not allowed on window "main", webview "main", URL: http://tauri.localhost';
  assert.equal(readRejection(denial, "Could not start a shell"), denial);
});

test("an Error is read the way it always was", () => {
  const err = new Error("Failed to fetch");
  assert.equal(readRejection(err, "fallback"), "Failed to fetch");
});

test("a plain object carrying a message is read", () => {
  // Tauri also rejects with object-shaped payloads in places, and a rejected
  // promise is not required to hold an Error.
  assert.equal(readRejection({ message: "engine refused" }, "fallback"), "engine refused");
});

test("an object with no usable message falls back rather than saying [object Object]", () => {
  // `String({})` is "[object Object]", which tells a user nothing and looks like
  // a bug on screen. The caller's own sentence is strictly better.
  assert.equal(readRejection({ code: 42 }, "fallback"), "fallback");
});

test("an object that does name itself usefully is read", () => {
  // The refusal for "[object Object]" specifically, not for every object: a
  // thrown class instance or a custom error shape often stringifies to the one
  // sentence worth showing.
  class EngineRefusal extends Error {}
  const named = new EngineRefusal("the engine said no");
  assert.equal(readRejection(named, "fallback"), "the engine said no");
});

test("nothing readable falls back rather than rendering an empty banner", () => {
  // The four shapes a rejected promise arrives in that carry no message at all.
  for (const empty of [null, undefined, 0, ""]) {
    assert.equal(
      readRejection(empty, "Could not start a shell"),
      "Could not start a shell",
      `a rejection of ${JSON.stringify(empty)} produced no message`,
    );
  }
});

test("a whitespace-only string is treated as no message", () => {
  // An engine that refused with `" "` has said nothing, and the caller's own
  // sentence is more use than a blank banner.
  assert.equal(readRejection("   \n ", "fallback"), "fallback");
});

test("the fallback is optional but never empty", () => {
  // A caller with nothing better to say should still not render nothing.
  assert.equal(readRejection(null), "Something went wrong");
});

test("a boxed String is read, because `instanceof String` is not a string", () => {
  // eslint-disable-next-line no-new-wrappers
  assert.equal(readRejection(new String("boxed refusal"), "fallback"), "boxed refusal");
});

test("an object whose toString throws does not escape into a render", () => {
  const hostile = {
    toString() {
      throw new Error("no string for you");
    },
  };
  assert.equal(readRejection(hostile, "fallback"), "fallback");
});
