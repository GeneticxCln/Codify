/**
 * How the engine's self-check is worded, kept apart from the component that shows it.
 *
 * Same reason `statusTone.ts` holds `STALE_AUTH_FIX` rather than the banner: the
 * suite runs through `node --test` with no DOM, so anything worth asserting has to
 * be importable without React and without `api.ts` — which reads `localStorage` at
 * module load and would throw on import. The decisions in here are string
 * decisions, which is exactly what a test can pin.
 *
 * The one that matters is which interpreter paths get shown. `sys.executable`
 * inside a virtualenv is `…/.venv/bin/python`, while the interpreter the shell
 * chooses for the same environment is `…/.venv/bin/python3`: different strings,
 * the same interpreter. Comparing the strings would report a mismatch on every
 * healthy install, and a card that cries wolf is a card nobody reads.
 */

import type { EngineRuntime } from "./types.ts";

/** How a line reads, so a warning is never styled as a fact. */
export type RuntimeTone = "normal" | "muted" | "success" | "warning";

export interface RuntimeLine {
  label: string;
  value: string;
  tone: RuntimeTone;
}

/** The venv's own root, from the interpreter path the shell would choose. */
export function venvRootOf(checkoutInterpreter: string): string {
  return checkoutInterpreter.replace(/\/bin\/python3$/, "");
}

/**
 * The report as labelled lines.
 *
 * The checkout's interpreter appears only when it is a genuinely different
 * environment. On a correct install there is nothing to reconcile, and a line
 * reading "Checkout's interpreter: …" next to "Interpreter: …" is noise that
 * trains people to skim past the one case that matters.
 */
export function runtimeLines(report: EngineRuntime): RuntimeLine[] {
  const { interpreter, checkout_interpreter: checkout, laya_sdk: sdk } = report;
  const lines: RuntimeLine[] = [
    {
      label: "Interpreter",
      value: `${interpreter.executable} — Python ${interpreter.version} (${interpreter.implementation})`,
      tone: "normal",
    },
    {
      label: "Environment",
      value: interpreter.in_virtualenv
        ? interpreter.prefix
        : `system Python (${interpreter.base_prefix})`,
      tone: "muted",
    },
  ];

  if (venvRootOf(checkout) !== interpreter.prefix) {
    lines.push({ label: "Checkout's interpreter", value: checkout, tone: "warning" });
  }

  lines.push({
    label: "Laya SDK",
    value: sdk.importable
      ? sdk.version
        ? `importable (${sdk.version})`
        : "importable"
      : `not importable by this interpreter${sdk.import_error ? ` — ${sdk.import_error}` : ""}`,
    tone: sdk.importable ? "success" : "warning",
  });

  return lines;
}
