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
 * The suite runs through `node --test` with no DOM, so the wording is imported
 * from `engineRuntime.ts` — deliberately not from the component, which pulls in
 * `api.ts` and its module-load `localStorage` read. The component and its wiring
 * are checked as source, the way `shell.test.ts` does.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { runtimeLines, venvRootOf } from "../src/engineRuntime.ts";
import type { EngineRuntime } from "../src/types.ts";

const CARD_SRC = readFileSync(
  new URL("../src/components/EngineRuntimeCard.tsx", import.meta.url),
  "utf8",
);
const PANEL_SRC = readFileSync(
  new URL("../src/components/SettingsPanel.tsx", import.meta.url),
  "utf8",
);
const API_SRC = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");

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

test("the card renders every warning the engine sent", () => {
  // Warnings are the whole point of the report; dropping one because a map was
  // written over a boolean would leave a real problem invisible.
  assert.match(CARD_SRC, /warnings\.map\(/, "each warning is rendered, not just the first");
  assert.match(CARD_SRC, /report\.warnings \?\? \[\]/, "an absent list renders as none, not a crash");
});

test("a self-check that could not run says so instead of looking healthy", () => {
  assert.match(CARD_SRC, /did not answer \/settings\/runtime/);
  assert.match(CARD_SRC, /setFailed\(true\)/);
});

test("the panel mounts the card, and the fetch is authenticated", () => {
  assert.match(PANEL_SRC, /<EngineRuntimeCard \/>/, "the card is reachable from Settings");
  assert.match(PANEL_SRC, /import \{ EngineRuntimeCard \}/);
  // It names filesystem paths, so the boot token is the whole of the guard.
  assert.match(
    API_SRC,
    /getEngineRuntime[\s\S]{0,400}?\/settings\/runtime[\s\S]{0,200}?Authorization: `Bearer \$\{currentEngine\.token\}`/,
    "the route is called with the boot token",
  );
});
