/**
 * Every argument this app sends across the IPC boundary is camelCase.
 *
 * Tauri v2 renames JS arguments to the Rust parameter names on the way in, and
 * the default is camelCase → snake_case. A Rust `workspace_id: String` is
 * therefore reached with `{ workspaceId }`; sending `{ workspace_id }` is not a
 * silently-ignored extra key, it is a *missing required key*, and the refusal
 * reads:
 *
 * > invalid args `workspaceId` for command `codify_terminal_open`: command
 * > codify_terminal_open missing required key workspaceId
 *
 * which names the key it wanted and never the one it got — the single most
 * expensive half-hour I have spent on this codebase.
 *
 * It is worth a file of its own because the failure is invisible from the
 * browser build. Outside Tauri every one of these calls takes the HTTP fallback
 * in `api.ts`, which ignores its arguments entirely and throws a refusal about
 * the desktop shell — so `paneRefusals.test.ts` and every other test here pass
 * while the real app rejects all seven pane commands. A test that only runs the
 * standalone path cannot see this class of bug at all; this one reads the
 * committed source instead.
 *
 * The check is deliberately general rather than seven assertions. Any future
 * `tauriInvoke` is covered by the same rule, and a command nobody thought to
 * list is still caught.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const api = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");

/**
 * Every `tauriInvoke(...)` payload in the file, as
 * `{ command, keys }`.
 *
 * The argument object is flat in every call site here, so `\{([^{}]*)\}` is
 * enough to capture it. A payload that ever grows a nested object would stop
 * matching and the `assert.ok` in the first test below would notice.
 */
function invokePayloads(): Array<{ command: string; keys: string[] }> {
  const out: Array<{ command: string; keys: string[] }> = [];
  const call = /tauriInvoke<[^>]*>\(\s*"([a-z_]+)"\s*,\s*\{([^{}]*)\}/g;
  for (let m = call.exec(api); m !== null; m = call.exec(api)) {
    // Both spellings count: `key: value` and the shorthand `key`. Reading only
    // the colon form misses every shorthand, which is most of the payload —
    // and a scan that silently under-reports is worse than no scan.
    const keys = m[2]
      .split(",")
      .map((part) => part.split(":")[0].trim())
      .filter((part) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(part));
    out.push({ command: m[1], keys });
  }
  return out;
}

const payloads = invokePayloads();

test("the payload scan actually found the pane commands", () => {
  // Without this every assertion below would pass on an empty list, which is
  // the failure mode a source-freeze is most prone to.
  const commands = payloads.map((p) => p.command);
  for (const expected of [
    "codify_browser_open",
    "codify_browser_navigate",
    "codify_browser_close",
    "codify_terminal_open",
    "codify_terminal_write",
    "codify_terminal_resize",
    "codify_terminal_close",
  ]) {
    assert.ok(
      commands.includes(expected),
      `api.ts no longer calls tauriInvoke for ${expected}, or its payload is no ` +
        "longer a flat object literal this scan can read",
    );
  }
});

test("no command sends a snake_case argument", () => {
  const offenders = payloads
    .map((p) => ({
      command: p.command,
      bad: p.keys.filter((k) => /^[a-z][a-zA-Z]*_[a-z_]+$/.test(k)),
    }))
    .filter((p) => p.bad.length > 0);
  assert.deepEqual(
    offenders,
    [],
    "Tauri v2 converts camelCase to the Rust parameter names, so a snake_case " +
      "key arrives as a missing required key: " +
      JSON.stringify(offenders),
  );
});

test("the pane commands send the keys their Rust signatures name", () => {
  // The converse of the rule above, so that renaming a key away — or sending
  // the right key for the wrong command — fails too.
  const expected: Record<string, string[]> = {
    codify_browser_open: ["tabId", "url"],
    codify_browser_navigate: ["tabId", "url"],
    codify_browser_close: ["tabId"],
    codify_terminal_open: ["workspaceId", "cols", "rows"],
    codify_terminal_write: ["terminalId", "data"],
    codify_terminal_resize: ["terminalId", "cols", "rows"],
    codify_terminal_close: ["terminalId"],
  };
  for (const [command, keys] of Object.entries(expected)) {
    const found = payloads.find((p) => p.command === command);
    assert.ok(found, `${command} has no payload to check`);
    for (const key of keys) {
      assert.ok(
        found.keys.includes(key),
        `${command} no longer sends \`${key}\`; it sends ` +
          JSON.stringify(found.keys),
      );
    }
  }
});

test("the Rust signatures are the snake_case half of the same names", () => {
  // Read from the Rust source so the two halves are pinned against each other
  // rather than against a list in this file. If a Rust parameter is renamed,
  // this fails and says which side moved.
  const rust = readFileSync(
    new URL("../../src-tauri/src/lib.rs", import.meta.url),
    "utf8",
  );
  const pairs: Array<[string, string]> = [
    ["codify_browser_open", "tab_id"],
    ["codify_browser_navigate", "tab_id"],
    ["codify_browser_close", "tab_id"],
    ["codify_terminal_open", "workspace_id"],
    ["codify_terminal_write", "terminal_id"],
    ["codify_terminal_resize", "terminal_id"],
    ["codify_terminal_close", "terminal_id"],
  ];
  for (const [command, rustParam] of pairs) {
    const body = rust
      .split(`fn ${command}(`)[1]
      ?.split(") ->")[0];
    assert.ok(body, `lib.rs no longer defines ${command} in the expected shape`);
    assert.match(
      body,
      new RegExp(`\\b${rustParam}:\\s*String`),
      `${command} no longer takes \`${rustParam}\`; the TypeScript side sends ` +
        `\`${rustParam.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase())}\`` +
        " and the two must keep matching",
    );
  }
});
