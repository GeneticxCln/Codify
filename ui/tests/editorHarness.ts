/**
 * The harness the editor's App tests share.
 *
 * Split across files for the reason the split-pane tests are: every test mounts the whole App, and here with a real
 * CodeMirror view as well, and a file of forty of them outgrows a process's memory.
 *
 * `editorBuffers.test.ts` is the text's rules, `editorSurface.test.ts` the assistant's operations and `editorPane.test.ts` the
 * pane. What none of them can show is that `App.tsx` joins them: that the palette opens a file into a tab, that a tab's
 * unsaved text survives showing another, that the assistant's open lands where `docs/09` §13 says and never takes the
 * keyboard, and that nothing about an editor reaches the tab strip's storage. This is the App, over a fake engine that has
 * a disk (`AppOptions.files`) and a window the engine can put questions to (`AppContext.surface`).
 *
 * The buffer store is module state that outlives an app, as it does in production, so every test closes what it opened.
 */
import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext, AppOptions } from "./appHarness.ts";
export type { AppContext, AppOptions };

import {
  SEEDED,
  WIDE,
  beat,
  chord,
  click,
  composer,
  conversation,
  focusedSide,
  id,
  openShell,
  openThread,
  pane,
  panes,
  selectedTab,
  shells,
  tabNamed,
  withApp as splitApp,
} from "./splitHarness.ts";

export { SEEDED, WIDE, beat, chord, click, composer, conversation, focusedSide, id, openShell, openThread, pane, panes, selectedTab, shells, tabNamed };

const { editorBuffers } = await import("../src/editorStore.ts");
const { EditorView } = await import("@codemirror/view");
export { editorBuffers };

export const MAIN = "def main():\n    return 1\n";
export const FILES: Record<string, string> = {
  "ws-a:src/main.py": MAIN,
  "ws-a:src/util.py": "def util():\n    pass\n",
  "ws-a:README.md": "# Readme\n",
};

/** The App over a fake engine with files on its disk, wide enough for a split, with every buffer closed afterwards. */
export const withEditorApp: typeof splitApp = (options, body) =>
  splitApp({ viewport: WIDE, files: FILES, ...options }, async (ctx) => {
    try {
      await body(ctx);
    } finally {
      // Inside `act`: closing a buffer tells the mounted App and pane, and a test that ends with an update React was not
      // expecting is a test that prints a warning for it.
      await ctx.act(async () => {
        for (const tabId of [...editorBuffers.getSnapshot().byTab.keys()]) editorBuffers.close(tabId);
      });
    }
  });

export async function openPalette(ctx: AppContext): Promise<void> {
  await chord(ctx, "k", "KeyK");
}

export async function queryPalette(ctx: AppContext, text: string): Promise<void> {
  await ctx.dom.fill(ctx.dom.byLabel("Command palette") as HTMLInputElement, text);
  // The palette asks the engine for the workspace's files when it opens, and the answer lands a tick later.
  await beat(30);
  await ctx.settle();
}

export const paletteOption = (ctx: AppContext, title: string): HTMLElement | undefined =>
  [...ctx.dom.container.querySelectorAll<HTMLElement>('[role="option"]')].find((o) => o.textContent?.includes(title));

/** Open a file the way a person does: Ctrl+K, type, press the row. */
export async function openFile(ctx: AppContext, query: string, title: string): Promise<void> {
  await openPalette(ctx);
  await queryPalette(ctx, query);
  const row = paletteOption(ctx, title);
  if (!row) throw new Error(`no palette row "${title}": ${[...ctx.dom.container.querySelectorAll('[role="option"]')].map((o) => o.textContent).join(" | ")}`);
  await ctx.dom.click(row);
  await beat(60);
  await ctx.settle();
}

export const cmText = (ctx: AppContext): string =>
  [...ctx.dom.container.querySelectorAll(".cm-content")].map((c) => c.textContent ?? "").join("\n---\n");

/** The live view in the editor pane at this position (0 for the first on screen). */
export function viewOf(ctx: AppContext, at = 0): InstanceType<typeof EditorView> {
  const dom = ctx.dom.container.querySelectorAll<HTMLElement>(".cm-editor")[at];
  const view = dom ? EditorView.findFromDOM(dom) : null;
  if (!view) throw new Error(`no editor view at ${at}`);
  return view;
}

/** What a person does: text typed at the top of the file in front. */
export async function typeAtTop(ctx: AppContext, text: string, at = 0): Promise<void> {
  await ctx.act(async () => {
    viewOf(ctx, at).dispatch({ changes: { from: 0, insert: text }, userEvent: "input.type" });
  });
  await ctx.settle();
}

export const button = (ctx: AppContext, name: string): HTMLButtonElement | undefined =>
  [...ctx.dom.container.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === name);

export const editorTabs = (ctx: AppContext): HTMLElement[] =>
  ctx.tabs().filter((t) => (t.getAttribute("aria-label") ?? "").startsWith("Editor:"));

export const dotIn = (tab: Element, which: "unsaved" | "assistant"): boolean =>
  tab.querySelector(`[data-testid="editor-${which}-dot"]`) !== null;

export const CHAT_AND_FILE: Partial<AppOptions> = { ...SEEDED };
