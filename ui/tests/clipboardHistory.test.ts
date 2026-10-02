/**
 * The clipboard history: the rules, with no renderer.
 *
 * What a copy-and-paste history is allowed to remember is a privacy decision, so it is written down as
 * rules a test can hold. The history is made of what passed through *this window* (a selection copied, a
 * paste, a code block's Copy button) and is kept in this window's storage and nowhere else. It is
 * bounded; an item is never truncated (half a snippet pasted back is a silent corruption, so a clip that
 * is too long is refused, not cut); a repeat moves the item up instead of adding another; a pin keeps an
 * item past the limit. And it never keeps something that looks like a credential, or anything that came
 * from a password field, because a clipboard history that remembers a pasted API key has made a copy of
 * the one thing the rest of this app is careful never to echo (`docs/00` §6.4).
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  CLIPBOARD_KEY,
  MAX_CLIPS,
  MAX_CLIP_CHARS,
  MAX_PINNED,
  SKIP_MESSAGES,
  captureClip,
  clearUnpinned,
  displayOrder,
  isPrivateField,
  loadClips,
  looksSecret,
  parseClips,
  previewOf,
  removeClip,
  saveClips,
  searchClips,
  selectedTextOf,
  sourceLabel,
  togglePin,
  type Clip,
  type ClipboardStorage,
} from "../src/clipboardHistory.ts";

const clip = (id: string, text: string, over: Partial<Clip> = {}): Clip => ({
  id,
  text,
  at: Number(id.replace(/\D/g, "")) || 1,
  source: "selection",
  pinned: false,
  ...over,
});

const keep = (list: Clip[], text: string, at: number, over: Record<string, unknown> = {}) =>
  captureClip(list, { id: `c${at}`, text, at, source: "selection", ...over });

class MemoryStorage implements ClipboardStorage {
  data = new Map<string, string>();
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
}

// ── what is kept ───────────────────────────────────────────────────────────

test("a copy is kept, newest first", () => {
  let list: Clip[] = [];
  list = keep(list, "first", 1).list;
  list = keep(list, "second", 2).list;
  assert.deepEqual(
    displayOrder(list).map((c) => c.text),
    ["second", "first"],
  );
});

test("a kept clip says what it is and where it passed through", () => {
  const out = keep([], "git status", 5, { source: "paste" });
  assert.equal(out.outcome.kind, "kept");
  assert.deepEqual(out.list, [{ id: "c5", text: "git status", at: 5, source: "paste", pinned: false }]);
});

test("copying what is already there moves it up and keeps its pin, its id and where it first came from", () => {
  let list: Clip[] = [clip("c1", "keep me", { pinned: true, source: "code" }), clip("c2", "other")];
  const out = keep(list, "keep me", 9, { source: "selection", id: "c9" });
  list = out.list;
  assert.equal(list.length, 2, "a repeat was added as a second item");
  const again = list.find((c) => c.text === "keep me");
  assert.deepEqual(again, { id: "c1", text: "keep me", at: 9, source: "code", pinned: true });
  assert.deepEqual(
    displayOrder(list).map((c) => c.text),
    ["keep me", "other"],
  );
});

test("whitespace alone is not a clip", () => {
  for (const text of ["", "   ", "\n\t\n"]) {
    const out = keep([], text, 1);
    assert.deepEqual(out.outcome, { kind: "skipped", why: "empty" });
    assert.deepEqual(out.list, []);
  }
});

test("a clip over the limit is refused whole, never cut", () => {
  const long = "x".repeat(MAX_CLIP_CHARS + 1);
  const out = keep([], long, 1);
  assert.deepEqual(out.outcome, { kind: "skipped", why: "too-long" });
  assert.deepEqual(out.list, []);
  assert.equal(keep([], "x".repeat(MAX_CLIP_CHARS), 1).outcome.kind, "kept", "exactly the limit is fine");
});

test("the text is kept exactly as copied: indentation, blank lines and a trailing newline", () => {
  const text = "def f():\n    return 1\n\n";
  assert.equal(keep([], text, 1).list[0]!.text, text);
});

// ── the bounds ─────────────────────────────────────────────────────────────

test("the unpinned list is capped, and what goes is the oldest", () => {
  let list: Clip[] = [];
  for (let n = 1; n <= MAX_CLIPS + 5; n++) list = keep(list, `item ${n}`, n).list;
  assert.equal(list.length, MAX_CLIPS);
  assert.equal(displayOrder(list)[0]!.text, `item ${MAX_CLIPS + 5}`);
  assert.equal(list.some((c) => c.text === "item 1"), false);
  assert.equal(list.some((c) => c.text === "item 6"), true);
});

test("a pin is kept past the cap, and does not count against it", () => {
  let list: Clip[] = [clip("c1", "pinned one", { pinned: true })];
  for (let n = 2; n <= MAX_CLIPS + 10; n++) list = keep(list, `item ${n}`, n).list;
  assert.equal(list.filter((c) => c.pinned).length, 1);
  assert.equal(list.filter((c) => !c.pinned).length, MAX_CLIPS);
  assert.ok(list.some((c) => c.text === "pinned one"));
});

test("pinned items sort first, newest first within each group", () => {
  const list = [
    clip("c1", "old pinned", { pinned: true }),
    clip("c5", "new unpinned"),
    clip("c3", "new pinned", { pinned: true }),
    clip("c2", "old unpinned"),
  ];
  assert.deepEqual(
    displayOrder(list).map((c) => c.text),
    ["new pinned", "old pinned", "new unpinned", "old unpinned"],
  );
});

test("pinning toggles, and there is a ceiling on how many", () => {
  let list = [clip("c1", "a")];
  list = togglePin(list, "c1");
  assert.equal(list[0]!.pinned, true);
  list = togglePin(list, "c1");
  assert.equal(list[0]!.pinned, false);

  let full: Clip[] = Array.from({ length: MAX_PINNED }, (_, n) => clip(`c${n + 1}`, `p${n}`, { pinned: true }));
  full = [...full, clip("c999", "one more")];
  const refused = togglePin(full, "c999");
  assert.equal(refused.find((c) => c.id === "c999")!.pinned, false, "pinned past the ceiling");
  assert.equal(refused.filter((c) => c.pinned).length, MAX_PINNED);
});

test("pinning something that is not there changes nothing", () => {
  const list = [clip("c1", "a"), clip("c2", "b", { pinned: true })];
  assert.deepEqual(togglePin(list, "nope"), list);
});

test("removing and clearing: a clear leaves the pins", () => {
  const list = [clip("c1", "keep", { pinned: true }), clip("c2", "gone"), clip("c3", "also gone")];
  assert.deepEqual(removeClip(list, "c2").map((c) => c.id), ["c1", "c3"]);
  assert.deepEqual(clearUnpinned(list).map((c) => c.id), ["c1"]);
  assert.deepEqual(removeClip(list, "missing"), list);
});

test("search is a case-insensitive substring over the text, in display order", () => {
  const list = [clip("c1", "Run the Tests"), clip("c2", "cargo test", { pinned: true }), clip("c3", "unrelated")];
  assert.deepEqual(
    searchClips(list, "TEST").map((c) => c.id),
    ["c2", "c1"],
  );
  assert.deepEqual(searchClips(list, "   ").map((c) => c.id), ["c2", "c3", "c1"], "a blank query is everything");
  assert.deepEqual(searchClips(list, "zzz"), []);
});

// ── what is never kept ─────────────────────────────────────────────────────

test("a credential-shaped clip is skipped, whatever it is", () => {
  const secrets: Array<[string, string]> = [
    ["an OpenAI or Anthropic key", "sk-proj-abc123DEF456ghi789JKL"],
    ["a Google key", "AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"],
    ["an NVIDIA key", "nvapi-abcdefghijklmnop1234567890"],
    ["a GitHub token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"],
    ["a fine-grained GitHub token", "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz_0123456789"],
    ["a Slack token", "xoxb-1234567890-abcdefghijkl"],
    ["an AWS access key id", "AKIAIOSFODNN7EXAMPLE"],
    ["a JWT", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"],
    ["a private key block", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n-----END OPENSSH PRIVATE KEY-----"],
    ["an Authorization header value", "Authorization: Bearer abcdef0123456789abcdef"],
    ["a bearer token", "Bearer 4f9c1d8e2b7a6c5d3e1f0a9b8c7d6e5f"],
    ["a named secret in a header", "x-api-key: 0123456789abcdefghij"],
    ["a quoted assignment", 'API_KEY = "abcd1234efgh5678ijkl"'],
    ["a secret in an env line", "ACCESS_TOKEN=abcdefghijklmnop12345678"],
    ["the engine's boot token shape", "Zq3v9XfK2mP7wLs0TnB4rYhD1cGaJe8uVoNiQ5xAtMk"],
  ];
  for (const [what, text] of secrets) {
    assert.equal(looksSecret(text), true, what);
    const out = keep([], text, 1);
    assert.deepEqual(out.outcome, { kind: "skipped", why: "secret" }, what);
    assert.deepEqual(out.list, [], what);
  }
});

test("every kind of secret is caught inside a longer clip too, not only on its own", () => {
  // A token alone is also caught by the random-run rule, which hides a missing shape; inside a sentence it
  // is the shape or nothing.
  const embedded = [
    "-----BEGIN RSA PRIVATE KEY-----",
    "sk-proj-abc123DEF456ghi789JKL",
    "AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q",
    "nvapi-abcdefghijklmnop1234567890",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz_0123456789",
    "xoxb-1234567890-abcdefghijkl",
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "Bearer 4f9c1d8e2b7a6c5d3e1f0a9b8c7d6e5f",
    "Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ1Ng==",
    "Authorization: Token abcd1234efgh5678",
  ];
  for (const secret of embedded) {
    assert.equal(looksSecret(`here is what I ran, please look: ${secret} and then it failed`), true, secret);
  }
});

test("the length floors on a named secret are where they are said to be", () => {
  assert.equal(looksSecret('secret = "abcdefgh1234"'), true, "twelve quoted characters");
  assert.equal(looksSecret('secret = "abcdefgh123"'), false, "eleven is an ordinary string");
  assert.equal(looksSecret("access_token=abcdefghij0123456789"), true, "twenty unquoted characters with a digit");
  assert.equal(looksSecret("access_token=abcdefghij012345678"), false, "nineteen is not");
});

test("a secret in the middle of a longer clip still skips the whole clip", () => {
  const text = "export OPENAI=sk-proj-abc123DEF456ghi789JKL\nnpm run dev";
  assert.equal(keep([], text, 1).outcome.kind, "skipped");
});

test("the engine's own redaction shapes are all caught (engine/providers.py)", () => {
  // `redact_secrets` removes `sk-…`, `AIza…`, `Bearer …` and four named headers from anything the engine
  // says. A clipboard history that kept what the engine would scrub would be the gap the rule is about.
  for (const text of [
    "sk-abcdefgh12",
    "AIzaSyAbcdefghijklmnopqrstu",
    "Bearer abcdefghijklmnopqrstuv",
    "x-goog-api-key: abcdefghijklmnop1234",
    "api_key=abcdefghijklmnop1234",
    "api-key: abcdefghijklmnop1234",
    "access_token: abcdefghijklmnop1234",
    'secret = "abcdefghijklmnop1234"',
  ]) {
    assert.equal(looksSecret(text), true, text);
  }
});

test("what is copied all day is not mistaken for a secret", () => {
  const ordinary: Array<[string, string]> = [
    ["a git commit hash", "8dbb2b2e7f1c4a9d0b3e5f6a7c8d9e0f1a2b3c4d"],
    ["a sha-256", "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"],
    ["a uuid", "123e4567-e89b-12d3-a456-426614174000"],
    ["a url", "https://github.com/GeneticxCln/Codify/pull/27"],
    ["a file path", "/home/user/Codify/ui/src/components/ChatTimeline.tsx"],
    ["a long identifier", "ThisIsAVeryLongCamelCaseIdentifierNameForAClass"],
    ["code reading a secret from the environment", 'password = os.environ["DB_PASSWORD"]'],
    ["code calling a getter", "api_key = get_api_key_from_config()"],
    ["a template placeholder", "secret: ${{ secrets.DEPLOY_TOKEN }}"],
    ["prose about bearer auth", "Bearer authentication is described in RFC 6750."],
    ["a short assignment", "token = 42"],
    ["a long identifier assigned to a secret-named variable", "password = some_function_name_that_is_long_enough"],
    ["a long path with no dots", "/home/user/Codify/ui/src/components/FooBar2"],
    ["code calling a getter whose name has a digit", "api_key = get_key_v2_from_config_service()"],
    ["a mixed-case identifier a little under the token length", "useClipboardHistoryV2Hook"],
    ["a hash typed in mixed-case hex", "a94a8fe5ccb19ba61c4c0873D391E987982FBBD3a94a8fe5"],
    ["a screaming-case constant with a digit", "MAX_RETRY_COUNT_FOR_HTTP2_REQUESTS_PER_HOST"],
    ["a snake-case identifier with a digit", "some_snake_case_identifier_with_a_digit_v2_inside"],
    ["a sentence", "Please review the secret handling in the settings route before merging."],
    ["a command", "cargo test --manifest-path src-tauri/Cargo.toml"],
    ["json", '{"name": "codify", "version": "0.2.0"}'],
  ];
  for (const [what, text] of ordinary) {
    assert.equal(looksSecret(text), false, what);
    assert.equal(keep([], text, 1).outcome.kind, "kept", what);
  }
});

test("anything from a password field is skipped, and so is a field that opts out", () => {
  const pw = { type: "password", getAttribute: () => null, closest: () => null };
  const optedOut = { type: "text", getAttribute: () => null, closest: (sel: string) => (sel.includes("clipboard") ? {} : null) };
  const plain = { type: "text", getAttribute: () => null, closest: () => null };
  assert.equal(isPrivateField(pw), true);
  assert.equal(isPrivateField(optedOut), true);
  assert.equal(isPrivateField(plain), false);
  assert.equal(isPrivateField(null), false);

  const out = keep([], "perfectly ordinary words", 1, { privateField: true });
  assert.deepEqual(out.outcome, { kind: "skipped", why: "private" });
  assert.deepEqual(out.list, []);
});

test("every way of skipping has a sentence a person can read", () => {
  for (const why of ["secret", "private", "empty", "too-long"] as const) {
    assert.ok(SKIP_MESSAGES[why].length > 10, why);
  }
  assert.match(SKIP_MESSAGES.secret, /key|token|credential/i);
});

// ── reading what was copied ────────────────────────────────────────────────

test("a copy in a text field is the field's selected slice, not the page selection", () => {
  const field = { tagName: "TEXTAREA", value: "hello world", selectionStart: 6, selectionEnd: 11 };
  assert.equal(selectedTextOf(field, "ignored page selection"), "world");
  assert.equal(selectedTextOf({ tagName: "INPUT", value: "abc", selectionStart: 1, selectionEnd: 1 }, "page"), "");
});

test("a copy anywhere else is the page's selection", () => {
  assert.equal(selectedTextOf({ tagName: "DIV" }, "some selected words"), "some selected words");
  assert.equal(selectedTextOf(null, "from the document"), "from the document");
});

test("a field whose selection cannot be read falls back to the page's", () => {
  assert.equal(selectedTextOf({ tagName: "INPUT", value: "x", selectionStart: null, selectionEnd: null }, "page"), "page");
});

// ── how a clip is shown ────────────────────────────────────────────────────

test("a preview is a few lines, each bounded, and says how much it left out", () => {
  const text = ["one", "two", "three", "four", "five", "six"].join("\n");
  const p = previewOf(text);
  assert.deepEqual(p.lines, ["one", "two", "three", "four"]);
  assert.equal(p.hiddenLines, 2);
  assert.equal(p.chars, text.length);
  const wide = previewOf("y".repeat(500));
  assert.ok(wide.lines[0]!.length <= 121, String(wide.lines[0]!.length));
  assert.ok(wide.lines[0]!.endsWith("…"));
});

test("a preview keeps the indentation code depends on", () => {
  assert.deepEqual(previewOf("def f():\n    return 1").lines, ["def f():", "    return 1"]);
});

test("each source has a label", () => {
  assert.equal(sourceLabel("selection"), "Copied");
  assert.equal(sourceLabel("paste"), "Pasted");
  assert.equal(sourceLabel("code"), "Code block");
});

// ── remembered, and never trusted ──────────────────────────────────────────

test("the list is saved and read back", () => {
  const storage = new MemoryStorage();
  const list = [clip("c2", "two", { pinned: true, source: "paste" }), clip("c1", "one")];
  saveClips(storage, list);
  assert.ok(storage.data.has(CLIPBOARD_KEY));
  assert.deepEqual(loadClips(storage), list);
});

test("a bad entry is dropped on its own and the rest are kept", () => {
  const storage = new MemoryStorage();
  storage.setItem(
    CLIPBOARD_KEY,
    JSON.stringify([
      { id: "c1", text: "good", at: 1, source: "selection", pinned: false },
      { id: 2, text: "id is a number", at: 2, source: "selection", pinned: false },
      { id: "c3", text: "", at: 3, source: "selection", pinned: false },
      { id: "c4", text: "bad source", at: 4, source: "elsewhere", pinned: false },
      { id: "c5", text: "no time", source: "selection", pinned: false },
      { id: "c6", text: "pinned wrong type", at: 6, source: "selection", pinned: "yes" },
      "nonsense",
      null,
    ]),
  );
  assert.deepEqual(loadClips(storage).map((c) => c.id), ["c1"]);
});

test("a secret that got into storage some other way is not shown again", () => {
  const storage = new MemoryStorage();
  storage.setItem(
    CLIPBOARD_KEY,
    JSON.stringify([
      { id: "c1", text: "sk-proj-abc123DEF456ghi789JKL", at: 1, source: "paste", pinned: true },
      { id: "c2", text: "fine", at: 2, source: "paste", pinned: false },
    ]),
  );
  assert.deepEqual(loadClips(storage).map((c) => c.id), ["c2"]);
});

test("what is stored is bounded again on the way in", () => {
  const storage = new MemoryStorage();
  const many = Array.from({ length: MAX_CLIPS + 30 }, (_, n) => ({
    id: `c${n + 1}`, text: `item ${n + 1}`, at: n + 1, source: "selection", pinned: false,
  }));
  const huge = { id: "c9999", text: "z".repeat(MAX_CLIP_CHARS + 1), at: 9999, source: "selection", pinned: false };
  storage.setItem(CLIPBOARD_KEY, JSON.stringify([...many, huge]));
  const loaded = loadClips(storage);
  assert.equal(loaded.length, MAX_CLIPS);
  assert.equal(loaded.some((c) => c.id === "c9999"), false, "an oversize clip came back");
});

test("duplicate texts in storage collapse to one, with the newest time and a pin from either", () => {
  const storage = new MemoryStorage();
  storage.setItem(
    CLIPBOARD_KEY,
    JSON.stringify([
      { id: "c1", text: "same", at: 1, source: "selection", pinned: true },
      { id: "c2", text: "same", at: 5, source: "selection", pinned: false },
    ]),
  );
  const loaded = loadClips(storage);
  assert.equal(loaded.length, 1);
  assert.equal(loaded[0]!.pinned, true, "the older copy's pin was lost to the collapse");
  assert.equal(loaded[0]!.at, 5, "the collapse kept the older time");
});

test("more pins in storage than the ceiling are demoted, not dropped", () => {
  const storage = new MemoryStorage();
  const entries = Array.from({ length: MAX_PINNED + 5 }, (_, n) => ({
    id: `c${n + 1}`, text: `pinned ${n + 1}`, at: n + 1, source: "selection", pinned: true,
  }));
  storage.setItem(CLIPBOARD_KEY, JSON.stringify(entries));
  const loaded = loadClips(storage);
  assert.equal(loaded.length, MAX_PINNED + 5, "an item was lost for being pinned");
  assert.equal(loaded.filter((c) => c.pinned).length, MAX_PINNED);
  assert.equal(loaded.find((c) => c.text === `pinned ${MAX_PINNED + 5}`)!.pinned, true, "the newest keep their pins");
  assert.equal(loaded.find((c) => c.text === "pinned 1")!.pinned, false);
});

test("parsing text that is not a list, or not JSON, is an empty list and does not throw", () => {
  assert.deepEqual(parseClips("{not json"), []);
  assert.deepEqual(parseClips('{"items": []}'), []);
  assert.deepEqual(parseClips("7"), []);
  assert.deepEqual(parseClips(null), []);
  assert.deepEqual(parseClips(""), []);
});

test("nothing readable is an empty list, and storage that throws is too", () => {
  assert.deepEqual(loadClips(null), []);
  assert.deepEqual(loadClips(new MemoryStorage()), []);
  const broken = new MemoryStorage();
  broken.setItem(CLIPBOARD_KEY, "{not json");
  assert.deepEqual(loadClips(broken), []);
  const notAList = new MemoryStorage();
  notAList.setItem(CLIPBOARD_KEY, JSON.stringify({ items: [] }));
  assert.deepEqual(loadClips(notAList), []);
  const throwing: ClipboardStorage = {
    getItem() {
      throw new Error("blocked");
    },
    setItem() {
      throw new Error("blocked");
    },
  };
  assert.deepEqual(loadClips(throwing), []);
  assert.doesNotThrow(() => saveClips(throwing, [clip("c1", "x")]));
  assert.doesNotThrow(() => saveClips(null, [clip("c1", "x")]));
});
