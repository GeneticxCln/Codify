/**
 * The settings pickers, and the rule that keeps the list they open readable.
 *
 * A `<select>`'s open list is not painted by the page. The desktop draws it —
 * GTK under WebKitGTK, Cocoa on macOS — so it ignores every `--codify-*` token
 * and arrives in the machine's own palette. On this machine that was a white
 * panel with the theme's near-white option text on it: the agent-role and audio
 * pickers were lists you could not read. No class, no `color-scheme` and no
 * `option { background }` reaches that popup, so the only list that cannot be
 * white is one the app draws.
 *
 * The mounted half presses the pickers a person presses. The source half is the
 * guarantee for the ones not mounted here: a rule about what is written, because
 * a native popup is invisible to jsdom (it has no native widget at all), so no
 * mounted test could observe its colour.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { ProviderProtocol } from "../src/types.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "..", "src");

const PROVIDERS = ["openai", "ollama"];

/** The engine ships eight providers; with the custom row that is a grown list. */
const LONG_PROVIDERS = [
  "anthropic",
  "openai",
  "deepseek",
  "ollama",
  "google",
  "openrouter",
  "groq",
  "nvidia",
];

/** A controlled host, because the picker is controlled by whoever owns the value. */
async function mountPicker(dom: Dom, onChange?: (value: string) => void): Promise<void> {
  const { SelectMenu } = await import("../src/components/SelectMenu.tsx");
  const Host: React.FC = () => {
    const [value, setValue] = React.useState("openai");
    return h(SelectMenu, {
      label: "Provider",
      value,
      options: PROVIDERS.map((slug) => ({ value: slug, label: slug === "openai" ? "OpenAI" : "Ollama" })),
      onChange: (next: string) => {
        setValue(next);
        onChange?.(next);
      },
    });
  };
  await dom.render(h(Host));
}

const list = (dom: Dom): HTMLElement | null => dom.container.querySelector('[role="listbox"]');

/** A picker with a search field, as the bulk model choice is once ids exist. */
async function mountSearchable(dom: Dom, onChange?: (value: string) => void): Promise<void> {
  const { SelectMenu } = await import("../src/components/SelectMenu.tsx");
  const Host: React.FC = () => {
    const [value, setValue] = React.useState("");
    return h(SelectMenu, {
      label: "Provider",
      value,
      searchable: true,
      searchPlaceholder: "Search providers…",
      options: [
        { value: "openai", label: "OpenAI" },
        { value: "ollama", label: "Ollama" },
        { value: "google", label: "Google" },
      ],
      onChange: (next: string) => {
        setValue(next);
        onChange?.(next);
      },
    });
  };
  await dom.render(h(Host));
}

const optionLabels = (dom: Dom): (string | null)[] =>
  Array.from(dom.container.querySelectorAll("[role='option']")).map((option) => option.textContent);

test("the list is painted from the theme's tokens, and choosing a row closes it", async () => {
  await withDom(async (dom) => {
    const chosen: string[] = [];
    await mountPicker(dom, (value) => chosen.push(value));

    assert.ok(list(dom) === null, "a list was open before anything was pressed");
    await dom.click(dom.byLabel("Provider"));

    const popup = list(dom);
    assert.ok(popup, "pressing the field opened no list");
    // The two colours that were the bug: the panel and its rows. Both must come
    // from the theme, and neither may be a hard-coded light surface.
    assert.match(popup.className, /\bbg-codify-surface\b/, "the list has no themed background");
    const rows = Array.from(popup.querySelectorAll('[role="option"]'));
    assert.deepEqual(rows.map((row) => row.textContent), ["OpenAI", "Ollama"]);
    for (const row of rows) {
      assert.match(row.className, /\btext-codify-(secondary|accent-ink)\b/, "a row has no themed ink");
      assert.doesNotMatch(row.className, /\b(bg|text)-(white|black)\b/, "a row names a raw light/dark colour");
    }

    await dom.click(dom.byText("Ollama"));
    assert.deepEqual(chosen, ["ollama"], "choosing a row did not report the value");
    assert.ok(list(dom) === null, "the list stayed open after a choice");
    assert.equal(dom.byLabel("Provider").textContent, "Ollama", "the field kept the old value");
  });
});

test("a searchable list narrows as you type, and Enter takes the top match", async () => {
  await withDom(async (dom) => {
    const chosen: string[] = [];
    await mountSearchable(dom, (value) => chosen.push(value));

    await dom.click(dom.byLabel("Provider"));
    const search = dom.byLabel("Search Provider") as HTMLInputElement;
    assert.ok(search === dom.window.document.activeElement, "the search field did not take focus");
    assert.deepEqual(optionLabels(dom), ["OpenAI", "Ollama", "Google"]);

    await dom.fill(search, "oll");
    assert.deepEqual(optionLabels(dom), ["Ollama"], "typing did not narrow the list");
    await dom.press(search, "Enter");
    assert.deepEqual(chosen, ["ollama"], "Enter did not take the top match");
    assert.ok(list(dom) === null, "the list stayed open after a choice");
    assert.equal(dom.byLabel("Provider").textContent, "Ollama");

    // The query is spent with the opening: reopening shows the whole list.
    await dom.click(dom.byLabel("Provider"));
    assert.deepEqual(optionLabels(dom), ["OpenAI", "Ollama", "Google"]);
  });
});

test("a search with no match says so rather than showing an empty panel", async () => {
  await withDom(async (dom) => {
    await mountSearchable(dom);
    await dom.click(dom.byLabel("Provider"));
    const search = dom.byLabel("Search Provider") as HTMLInputElement;
    await dom.fill(search, "zzz");
    assert.deepEqual(optionLabels(dom), []);
    assert.match(dom.text(), /Nothing matches “zzz”\./);
  });
});

test("the arrow keys move through the list and Enter chooses", async () => {
  await withDom(async (dom) => {
    const chosen: string[] = [];
    await mountPicker(dom, (value) => chosen.push(value));

    await dom.press(dom.byLabel("Provider"), "ArrowDown");
    const rows = Array.from(dom.container.querySelectorAll<HTMLElement>('[role="option"]'));
    assert.ok(rows[0] === dom.window.document.activeElement, "opening did not focus the chosen row");

    await dom.press(rows[0], "ArrowDown");
    assert.ok(rows[1] === dom.window.document.activeElement, "ArrowDown did not move to the next row");
    await dom.press(rows[1], "Enter");
    assert.deepEqual(chosen, ["ollama"], "Enter did not choose the focused row");
  });
});

test("Escape closes the list and stops there, rather than closing Settings behind it", async () => {
  await withDom(async (dom) => {
    await mountPicker(dom);
    await dom.click(dom.byLabel("Provider"));
    assert.ok(list(dom));

    // Settings closes on a window-level Escape. A list that lets the key through
    // would discard the form the list was opened over.
    let heardOnWindow = 0;
    const onWindowKey = () => {
      heardOnWindow += 1;
    };
    dom.window.addEventListener("keydown", onWindowKey);
    const row = dom.container.querySelector<HTMLElement>('[role="option"]');
    assert.ok(row);
    await dom.press(row, "Escape");
    dom.window.removeEventListener("keydown", onWindowKey);

    assert.equal(heardOnWindow, 0, "Escape escaped the list and reached the window");
    assert.ok(list(dom) === null, "Escape left the list open");
  });
});

test("the agent-role pickers are this list, not a native select", async () => {
  await withDom(async (dom) => {
    const { ProviderSelect } = await import("../src/components/ProviderSelect.tsx");
    const { ProtocolSelect } = await import("../src/components/ProtocolSelect.tsx");

    const Host: React.FC = () => {
      const [provider, setProvider] = React.useState("openai");
      const [protocol, setProtocol] = React.useState<ProviderProtocol>("openai_compat");
      return h(
        "div",
        null,
        h(ProviderSelect, { value: provider, onChange: setProvider, builtins: LONG_PROVIDERS }),
        h(ProtocolSelect, { value: protocol, onChange: setProtocol }),
      );
    };
    await dom.render(h(Host));

    assert.ok(dom.container.querySelector("select") === null, "a native select is back on the role card");

    await dom.click(dom.byLabel("Provider"));
    assert.ok(dom.byLabel("Search Provider"), "a grown provider list has no search field");
    assert.deepEqual(
      Array.from(dom.container.querySelectorAll('[role="option"]')).map((o) => o.textContent),
      [
        "Anthropic",
        "OpenAI",
        "DeepSeek",
        "Ollama",
        "Google",
        "OpenRouter",
        "Groq",
        "NVIDIA",
        "Custom Provider...",
      ],
      "the provider list is not the engine's list",
    );
    const firstRow = dom.container.querySelector<HTMLElement>('[role="option"]');
    assert.ok(firstRow, "the provider list had no rows");
    await dom.press(firstRow, "Escape");

    await dom.click(dom.byLabel("Protocol"));
    assert.ok(list(dom), "the protocol picker opened no list");
  });
});

test("a provider list short enough to scan is not given a search field", async () => {
  await withDom(async (dom) => {
    const { ProviderSelect } = await import("../src/components/ProviderSelect.tsx");
    const Host: React.FC = () => {
      const [provider, setProvider] = React.useState("openai");
      return h(ProviderSelect, { value: provider, onChange: setProvider, builtins: PROVIDERS });
    };
    await dom.render(h(Host));

    await dom.click(dom.byLabel("Provider"));
    assert.ok(list(dom), "the provider picker opened no list");
    assert.ok(
      dom.container.querySelector('input[aria-label="Search Provider"]') === null,
      "three providers were given a search field they do not need",
    );
    assert.equal(dom.container.querySelectorAll('[role="option"]').length, 3);
  });
});

/** Every `.tsx` under `src/`, recursively. */
function tsxFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return tsxFiles(path);
    return entry.name.endsWith(".tsx") ? [path] : [];
  });
}

test("no component renders a native checkbox: its unchecked box cannot follow the theme", () => {
  // `accent-color` tints only the checked state. The box a person meets first is
  // drawn by the toolkit, and on a light desktop it is a white square on every
  // dark theme — the same defect as the select's popup, in a smaller control.
  const offenders = tsxFiles(SRC)
    .filter((file) => /type="checkbox"/.test(readFileSync(file, "utf8")))
    .map((file) => file.slice(SRC.length + 1));
  assert.deepEqual(
    offenders,
    [],
    "a native `<input type=\"checkbox\">` is back. Its unchecked box is painted by the " +
      "desktop's widget toolkit, not the page — use `Checkbox` from components/ui/Checkbox.tsx, " +
      "which draws both states from `--codify-*` tokens.",
  );
});

test("no component renders a native select: its list cannot follow the theme", () => {
  // The lookbehind skips the doc comment in `SelectMenu.tsx`, which names the
  // element it replaced in backticks. JSX writes `<select` followed by
  // whitespace or `>` and never by a backtick.
  const native = /(?<!`)<select[\s>]/;
  const offenders = tsxFiles(SRC)
    .filter((file) => native.test(readFileSync(file, "utf8")))
    .map((file) => file.slice(SRC.length + 1));
  assert.deepEqual(
    offenders,
    [],
    "a native `<select>` is back. Its open list is drawn by the desktop's widget toolkit, " +
      "not by the page, so it ignores the theme and renders white on a light desktop — use " +
      "`SelectMenu` from components/SelectMenu.tsx, which draws its list from `--codify-*` tokens.",
  );
});
