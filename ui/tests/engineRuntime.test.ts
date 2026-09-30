/**
 * The engine's self-check, and the one line in it that can cry wolf.
 *
 * The bug this guards is invisible by construction. `pip install laya` into
 * `.venv` makes the gate fast for `make test` and leaves the desktop shell,
 * which resolves `python3` from the login shell's PATH, running an engine that
 * cannot import it — so every goal pays the fallback model's ~20 s and ~1.4k
 * tokens against ~31 ms and none. Nothing raises. The only symptom is a gate
 * that is slow, and nothing connects that to a package in a different directory.
 *
 * The trap in the wording is the one a naive implementation walks into. Inside a
 * virtualenv `sys.executable` is `…/.venv/bin/python`, while the interpreter the
 * shell picks for that same environment is `…/.venv/bin/python3`. Compare those
 * two strings and every healthy install looks like a mismatch, so the card warns
 * always — and a card that warns always is a card that has trained everyone to
 * skim it. That case is pinned here explicitly, because the bug it prevents is
 * invisible precisely when it works.
 *
 * The wording is imported from `engineRuntime.ts` and tested as plain functions.
 * The card around it is mounted in a DOM, with the engine's answer supplied by
 * the test, so "it renders every warning" and "it says so when the check could
 * not run" are claims about what is on screen rather than about the file.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { runtimeLines, venvRootOf } from "../src/engineRuntime.ts";
import type { EngineRuntime } from "../src/types.ts";
import type { Dom } from "./dom.ts";
import type { EngineCall } from "./appHarness.ts";

const { withDom } = await import("./dom.ts");
const { withApp } = await import("./appHarness.ts");
const React = (await import("react")).default;
const h = React.createElement;

function report(overrides: Partial<EngineRuntime> = {}): EngineRuntime {
  return {
    interpreter: {
      executable: "/home/me/Codify/.venv/bin/python",
      version: "3.12.4",
      version_info: [3, 12],
      implementation: "CPython",
      in_virtualenv: true,
      prefix: "/home/me/Codify/.venv",
      base_prefix: "/usr",
    },
    project_root: "/home/me/Codify",
    checkout_interpreter: "/home/me/Codify/.venv/bin/python3",
    laya_sdk: { importable: true, import_error: null, version: "0.3.21", disabled_by_env: false },
    warnings: [],
    ...overrides,
  };
}

function lineFor(lines: ReturnType<typeof runtimeLines>, label: string) {
  const found = lines.find((line) => line.label === label);
  assert.ok(found, `expected a "${label}" line, got ${lines.map((l) => l.label).join(", ")}`);
  return found;
}

test("a healthy install names the interpreter and the SDK it can import", () => {
  const lines = runtimeLines(report());
  const interpreter = lineFor(lines, "Interpreter");
  assert.match(interpreter.value, /\.venv\/bin\/python/, "the running interpreter is named");
  assert.match(interpreter.value, /3\.12\.4/, "with the version it is running");
  assert.equal(lineFor(lines, "Laya SDK").value, "importable (0.3.21)");
});

test("`bin/python` and `bin/python3` are one environment, not a mismatch", () => {
  // The regression this whole module exists to avoid. Same venv, two spellings of
  // the same interpreter, and a card that reports a difference which is not there
  // is worse than no card.
  assert.equal(venvRootOf("/home/me/Codify/.venv/bin/python3"), "/home/me/Codify/.venv");
  const lines = runtimeLines(report());
  assert.equal(
    lines.filter((l) => l.label === "Checkout's interpreter").length,
    0,
    "a checkout running its own venv has nothing to reconcile",
  );
});

test("an engine outside the checkout's venv says so, and says which is which", () => {
  // The case the report was written for: the SDK is in `.venv`, the shell
  // spawned `/usr/bin/python3`, and nothing else in the product would say so.
  const lines = runtimeLines(
    report({
      interpreter: {
        executable: "/usr/bin/python3",
        version: "3.12.4",
        version_info: [3, 12],
        implementation: "CPython",
        in_virtualenv: false,
        prefix: "/usr",
        base_prefix: "/usr",
      },
      laya_sdk: {
        importable: false,
        import_error: "ModuleNotFoundError: No module named 'laya'",
        version: null,
        disabled_by_env: false,
      },
    }),
  );
  const checkout = lineFor(lines, "Checkout's interpreter");
  assert.equal(checkout.value, "/home/me/Codify/.venv/bin/python3");
  assert.equal(checkout.tone, "warning", "a difference is styled as a warning, not a fact");
  assert.match(lineFor(lines, "Environment").value, /system Python/);
  const sdk = lineFor(lines, "Laya SDK");
  assert.equal(sdk.tone, "warning");
  assert.match(sdk.value, /not importable by this interpreter/);
  // The reason has to survive: "not importable" alone sends the reader to
  // `pip install`, which is not the fix for a package in the wrong environment.
  assert.match(sdk.value, /No module named 'laya'/);
});

test("a disabled SDK is not reported as a missing one", () => {
  // The two need different fixes — delete a line of config versus install a
  // package — so the card must not collapse them into one red state.
  const disabled = runtimeLines(
    report({ laya_sdk: { importable: true, import_error: null, version: "0.3.21", disabled_by_env: true } }),
  );
  assert.equal(
    lineFor(disabled, "Laya SDK").tone,
    "success",
    "importable is a fact about the interpreter; being switched off is the gate card's business",
  );
});

test("a broken install is reported differently from an absent one", () => {
  const broken = runtimeLines(
    report({
      laya_sdk: { importable: false, import_error: "ImportError: bad stub", version: null, disabled_by_env: false },
    }),
  );
  assert.match(lineFor(broken, "Laya SDK").value, /ImportError: bad stub/);
});

test("the SDK line survives a report with no version and no error", () => {
  // Every field the engine sends is optional in practice; a card that renders
  // "undefined" is the failure a user sees and no probe catches.
  const bare = runtimeLines(
    report({ laya_sdk: { importable: true, disabled_by_env: false } }),
  );
  assert.equal(lineFor(bare, "Laya SDK").value, "importable");
  const missing = runtimeLines(
    report({ laya_sdk: { importable: false, disabled_by_env: false } }),
  );
  assert.equal(lineFor(missing, "Laya SDK").value, "not importable by this interpreter");
});

/**
 * Mount the card while the engine answers `/settings/runtime` with `answer`.
 *
 * `sent` records what the card asked for, so a test can say what was sent and
 * to where. The card is imported inside the DOM, for the reason `dom.ts` gives.
 */
async function withCard(
  answer: (() => Response | Promise<Response>) | Error,
  body: (dom: Dom, sent: Array<{ url: string; authorization: string | null }>) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const sent: Array<{ url: string; authorization: string | null }> = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      sent.push({
        url: String(input),
        authorization: new Headers(init?.headers).get("authorization"),
      });
      if (answer instanceof Error) throw answer;
      return answer();
    }) as typeof fetch;
    const { EngineRuntimeCard } = await import("../src/components/EngineRuntimeCard.tsx");
    await dom.render(h(EngineRuntimeCard));
    await dom.settle();
    await body(dom, sent);
  });
}

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

test("the card renders every warning the engine sent", async () => {
  // Warnings are the whole point of the report; dropping one because a map was
  // written over a boolean would leave a real problem invisible.
  const warnings = [
    "the engine is not running in the checkout's virtualenv",
    "laya is installed in a different environment than the one running",
    "a third thing, so that showing only the first two is a failure",
  ];
  await withCard(
    () => json(report({ warnings })),
    async (dom) => {
      const text = dom.text();
      for (const warning of warnings) {
        assert.ok(text.includes(warning), `the card dropped a warning: ${warning}`);
      }
      assert.match(text, /needs attention/, "warnings did not change the card's heading");
    },
  );
});

test("a report with no warnings reads as healthy, and an absent list is not a crash", async () => {
  await withCard(
    () => json(report({ warnings: [] })),
    async (dom) => {
      assert.match(dom.text(), /Engine runtime/);
      assert.doesNotMatch(dom.text(), /needs attention/, "a clean report is styled as a problem");
    },
  );
  // Every field the engine sends is optional in practice.
  const withoutList = { ...report() } as Partial<EngineRuntime>;
  delete withoutList.warnings;
  await withCard(
    () => json(withoutList),
    async (dom) => {
      assert.match(dom.text(), /Engine runtime/, "an absent warnings list took the card down");
      assert.doesNotMatch(dom.text(), /needs attention/);
    },
  );
});

test("a self-check that could not run says so instead of looking healthy", async () => {
  const unavailable = /did not answer \/settings\/runtime/;
  await withCard(
    () => json({ message: "boom" }, 500),
    async (dom) => assert.match(dom.text(), unavailable, "a refused check rendered as healthy"),
  );
  await withCard(
    new TypeError("Failed to fetch"),
    async (dom) => assert.match(dom.text(), unavailable, "an unreachable engine rendered as healthy"),
  );
});

test("the fetch is authenticated with the boot token", async () => {
  // It names filesystem paths, so the boot token is the whole of the guard.
  const { setEngineInfo } = await import("../src/api.ts");
  setEngineInfo({ port: 43117, token: "boot-token-under-test" });
  await withCard(
    () => json(report()),
    async (_dom, sent) => {
      assert.equal(sent.length, 1, "the card asked for the report zero or several times");
      assert.equal(sent[0].url, "http://127.0.0.1:43117/settings/runtime");
      assert.equal(sent[0].authorization, "Bearer boot-token-under-test");
    },
  );
});

test("the Agent Roles tab of Settings shows the card, so the report is reachable from the app", async () => {
  await withApp({}, async ({ dom, engine, settle }) => {
    const asked = (): EngineCall[] =>
      engine.filter((c) => c.method === "GET" && c.path === "/settings/runtime");
    await dom.click(dom.byButton("Settings"));
    await settle();
    assert.equal(asked().length, 0, "the self-check ran before anyone asked for the roles tab");
    await dom.click(dom.byButton("Agent Roles"));
    await settle();
    assert.equal(asked().length, 1, "the Agent Roles tab did not mount the engine runtime card");
    // The card answers even when the check cannot run, and says so.
    assert.match(dom.text(), /Engine runtime|did not answer \/settings\/runtime/);
  });
});
