/**
 * The clipboard history, through the whole App: the icon, the drawer, what the window feeds it and what each
 * button does with a clip.
 *
 * `clipboardHistory.test.ts` holds the rules, `clipboardCapture.test.ts` the listeners and `clipboardDrawer.test.ts`
 * the view. What none of them can show is that `App.tsx` joins them: that the button sits where it should, that a
 * code block's Copy in the real transcript reaches the history, that Insert lands in the real composer and Paste
 * in a real xterm, and that a press which cannot work says so instead of doing nothing.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext, AppOptions } from "./appHarness.ts";

const { withApp, conversation } = await import("./appHarness.ts");
const { CLIPBOARD_KEY } = await import("../src/clipboardHistory.ts");

const WIDE = { width: 2400, height: 900 };

const header = (ctx: AppContext): HTMLElement => ctx.dom.container.querySelector("header") as HTMLElement;
const toggleNames = (ctx: AppContext): string[] =>
  [...header(ctx).querySelectorAll("button[aria-pressed]")].map((b) => b.getAttribute("aria-label") ?? (b.textContent ?? "").trim());
const toggle = (ctx: AppContext): HTMLElement => ctx.dom.byLabel("Clipboard history");
const drawer = (ctx: AppContext): HTMLElement | null =>
  ctx.dom.container.querySelector('aside[aria-label="Clipboard"]') as HTMLElement | null;
const rows = (ctx: AppContext): HTMLElement[] => [...(drawer(ctx)?.querySelectorAll("li") ?? [])] as HTMLElement[];
const rowTexts = (ctx: AppContext): string[] => rows(ctx).map((r) => r.querySelector("pre")?.textContent ?? "");
const inRow = (row: HTMLElement, label: string): HTMLButtonElement => {
  const found = row.querySelector(`[aria-label="${label}"]`);
  if (!found) throw new Error(`no "${label}" in the row: ${row.textContent}`);
  return found as HTMLButtonElement;
};
const rowOf = (ctx: AppContext, text: string): HTMLElement => {
  const found = rows(ctx).find((r) => r.querySelector("pre")?.textContent === text);
  if (!found) throw new Error(`no clip "${text}" in the drawer: ${rowTexts(ctx).join(" | ")}`);
  return found;
};
const stored = (ctx: AppContext): string => ctx.dom.window.localStorage.getItem(CLIPBOARD_KEY) ?? "";
const open = (ctx: AppContext): Promise<void> => ctx.dom.click(toggle(ctx));

/** A clipboard event as the browser builds it. jsdom has no ClipboardEvent, so the one property read is added by hand. */
function clipboardEvent(ctx: AppContext, type: "copy" | "cut" | "paste", text?: string): Event {
  const event = new ctx.dom.window.Event(type, { bubbles: true, cancelable: true });
  const store = new Map<string, string>(text === undefined ? [] : [["text/plain", text]]);
  Object.defineProperty(event, "clipboardData", {
    value: { getData: (k: string) => store.get(k) ?? "", setData: (k: string, v: string) => void store.set(k, v) },
  });
  return event;
}
const fire = (ctx: AppContext, target: EventTarget, event: Event): Promise<void> =>
  ctx.act(async () => void target.dispatchEvent(event));
/** The window pastes something somewhere: one entry in the history. */
const pasteSomething = (ctx: AppContext, text: string): Promise<void> =>
  fire(ctx, ctx.dom.window.document.body, clipboardEvent(ctx, "paste", text));

function stubClipboard(ctx: AppContext, writeText: (t: string) => Promise<void>): void {
  Object.defineProperty(ctx.dom.window.navigator, "clipboard", { value: { writeText }, configurable: true });
}

const REPLY = ["Run this:", "", "```sh", "make test", "```", "", "and read the output."].join("\n");

/** A thread with one finished answer that has a code block in it, opened. */
async function withAnswer(body: (ctx: AppContext) => Promise<void>, extra: Partial<AppOptions> = {}): Promise<void> {
  await withApp(
    {
      viewport: WIDE,
      conversations: [conversation({ id: "c1", title: "Thread" })],
      goals: [{ id: "g1", conversation_id: "c1", title: "How do I test it?", status: "COMPLETED", mode: "chat" }],
      goalEvents: {
        g1: [{ id: "e1", goal_id: "g1", step_id: null, type: "log", payload: { turn: true, message: REPLY }, timestamp: 2, sequence: 1 }],
      },
      ...extra,
    },
    async (ctx) => {
      await ctx.dom.click(ctx.dom.container.querySelector('[data-thread-row="true"]') as Element);
      await ctx.settle();
      await body(ctx);
    },
  );
}

// ── the button and the drawer ───────────────────────────────────────────────

test("the clipboard icon sits right after Notifications, with no word beside it", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    const names = toggleNames(ctx);
    const at = (re: RegExp): number => names.findIndex((n) => re.test(n));
    assert.deepEqual(
      [at(/^Stats/), at(/^Notifications/), at(/^Clipboard history$/), at(/^History/)].map((n, i, all) => (i === 0 ? n >= 0 : n === all[i - 1] + 1)),
      [true, true, true, true],
      `toggles, in order: ${names.join(" | ")}`,
    );
    const button = toggle(ctx);
    assert.equal((button.textContent ?? "").trim(), "", "an icon-only button has a word on it");
    assert.ok(button.querySelector("svg"), "no icon");
    assert.equal(button.getAttribute("aria-pressed"), "false");
    const title = button.getAttribute("title") ?? "";
    assert.match(title, /^Clipboard — /, "the title does not say what it is");
    // How the panel's own buttons are found (docs/09 §7): this title must not be mistaken for them.
    assert.ok(!/^(Browser|Terminal|Keys|Settings)/.test(title));
  });
});

test("the four drawers are one at a time, and each button is armed only for its own", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    const armed = (name: RegExp): string | null =>
      [...header(ctx).querySelectorAll("button[aria-pressed]")].find((b) => name.test(b.getAttribute("aria-label") ?? b.textContent ?? ""))?.getAttribute("aria-pressed") ?? null;
    const which = (): string[] =>
      [
        ctx.dom.container.querySelector('[aria-label="Close statistics"]') ? "stats" : "",
        ctx.dom.container.querySelector('aside[aria-label="Notifications"]') ? "notifications" : "",
        drawer(ctx) ? "clipboard" : "",
        ctx.dom.container.querySelector('[aria-label="Close goal history"]') ? "history" : "",
      ].filter(Boolean);

    await ctx.dom.click(ctx.dom.byButton("Stats"));
    await open(ctx);
    assert.deepEqual(which(), ["clipboard"], "opening Clipboard left Stats open");
    assert.equal(armed(/^Clipboard history/), "true");
    assert.equal(armed(/^Stats/), "false");
    await ctx.dom.click(ctx.dom.byButton("History"));
    assert.deepEqual(which(), ["history"]);
    assert.equal(armed(/^Clipboard history/), "false");
    await open(ctx);
    await ctx.dom.click(ctx.dom.byLabel("Notifications"));
    assert.deepEqual(which(), ["notifications"]);
    await open(ctx);
    await open(ctx);
    assert.deepEqual(which(), [], "pressing the open drawer's button did not close it");
    await open(ctx);
    await ctx.dom.click(ctx.dom.byLabel("Close clipboard"));
    assert.deepEqual(which(), [], "the drawer's own close button did not close it");
  });
});

test("an empty drawer says what it is for, and what it never keeps", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    await open(ctx);
    assert.match(ctx.dom.text(), /Nothing yet/);
    assert.match(ctx.dom.text(), /never kept/i);
  });
});

// ── what the window feeds it ────────────────────────────────────────────────

test("what is copied in the transcript is kept even while the drawer is closed", async () => {
  await withAnswer(async (ctx) => {
    const para = [...ctx.dom.container.querySelectorAll("p")].find((p) => /read the output/.test(p.textContent ?? ""))!;
    ctx.dom.window.getSelection()?.selectAllChildren(para);
    await fire(ctx, para, clipboardEvent(ctx, "copy"));
    assert.equal(drawer(ctx), null, "the drawer was open");
    await open(ctx);
    assert.deepEqual(rowTexts(ctx), ["and read the output."]);
    assert.match(rowOf(ctx, "and read the output.").textContent ?? "", /Copied/);
  });
});

test("a code block's Copy button reaches the history through the real transcript", async () => {
  await withAnswer(async (ctx) => {
    const written: string[] = [];
    stubClipboard(ctx, async (t) => void written.push(t));
    await ctx.dom.click(ctx.dom.byLabel("Copy code"));
    await ctx.settle();
    assert.deepEqual(written, ["make test"]);
    await open(ctx);
    assert.deepEqual(rowTexts(ctx), ["make test"]);
    assert.match(rowOf(ctx, "make test").textContent ?? "", /Code block/);
  });
});

test("a paste into the message box is kept, and so is one anywhere else in the window", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    await fire(ctx, ctx.dom.byField("Chat prompt"), clipboardEvent(ctx, "paste", "src/parser/lex.ts"));
    await open(ctx);
    assert.deepEqual(rowTexts(ctx), ["src/parser/lex.ts"]);
    assert.match(rowOf(ctx, "src/parser/lex.ts").textContent ?? "", /Pasted/);
  });
});

test("a key is not kept, the drawer says so, and nothing of it is stored", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    await pasteSomething(ctx, "sk-proj-AbCdEf0123456789AbCdEf0123456789");
    await open(ctx);
    assert.deepEqual(rowTexts(ctx), []);
    assert.match(ctx.dom.text(), /looks like a key, token or other credential/);
    assert.ok(!stored(ctx).includes("sk-proj"), "a key reached the window's storage");
    await pasteSomething(ctx, "an ordinary sentence");
    assert.ok(!/looks like a key/.test(ctx.dom.text()), "the note outlived what it was about");
  });
});

test("a search term pasted into the drawer's own field is not a clip", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    await pasteSomething(ctx, "a real clip");
    await open(ctx);
    await fire(ctx, ctx.dom.byField("Search clipboard history"), clipboardEvent(ctx, "paste", "searching for this"));
    assert.deepEqual(rowTexts(ctx), ["a real clip"]);
  });
});

test("the history is remembered across a restart, with its pins", async () => {
  let saved = "";
  await withApp({ viewport: WIDE }, async (ctx) => {
    await pasteSomething(ctx, "first thing");
    await pasteSomething(ctx, "second thing");
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "first thing"), "Pin clip"));
    saved = stored(ctx);
    assert.match(saved, /first thing/);
  });
  await withApp({ viewport: WIDE, localStorage: { [CLIPBOARD_KEY]: saved } }, async (ctx) => {
    await open(ctx);
    assert.deepEqual(rowTexts(ctx), ["first thing", "second thing"], "the pin, or the list, did not come back");
    assert.equal(inRow(rowOf(ctx, "first thing"), "Unpin clip").getAttribute("aria-pressed"), "true");
  });
});

// ── what each button does ───────────────────────────────────────────────────

test("Copy again writes the clip to the clipboard and brings it to the top", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    const written: string[] = [];
    stubClipboard(ctx, async (t) => void written.push(t));
    await pasteSomething(ctx, "older");
    await pasteSomething(ctx, "newer");
    await open(ctx);
    assert.deepEqual(rowTexts(ctx), ["newer", "older"]);
    await ctx.dom.click(inRow(rowOf(ctx, "older"), "Copy again"));
    await ctx.settle();
    assert.deepEqual(written, ["older"]);
    assert.deepEqual(rowTexts(ctx), ["older", "newer"], "the clip just copied did not move up");
    assert.equal(rows(ctx).length, 2, "copying again added a second entry");
  });
});

test("a Copy again the system refuses says so and changes nothing", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    stubClipboard(ctx, async () => {
      throw new Error("denied");
    });
    (ctx.dom.window.document as unknown as { execCommand?: unknown }).execCommand = undefined;
    await pasteSomething(ctx, "older");
    await pasteSomething(ctx, "newer");
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "older"), "Copy again"));
    await ctx.settle();
    assert.match(ctx.dom.text(), /Could not copy/);
    assert.deepEqual(rowTexts(ctx), ["newer", "older"]);
  });
});

test("the drawer's message closes with the drawer and does not come back", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    stubClipboard(ctx, async () => {
      throw new Error("denied");
    });
    (ctx.dom.window.document as unknown as { execCommand?: unknown }).execCommand = undefined;
    await pasteSomething(ctx, "one");
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "one"), "Copy again"));
    await ctx.settle();
    assert.match(ctx.dom.text(), /Could not copy/, "there was no message to close");
    await ctx.dom.click(ctx.dom.byLabel("Close clipboard"));
    await open(ctx);
    assert.doesNotMatch(ctx.dom.text(), /Could not copy/, "a stale message was waiting");
  });
});

test("a message can be dismissed without closing the drawer", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    stubClipboard(ctx, async () => {
      throw new Error("denied");
    });
    (ctx.dom.window.document as unknown as { execCommand?: unknown }).execCommand = undefined;
    await pasteSomething(ctx, "one");
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "one"), "Copy again"));
    await ctx.settle();
    await ctx.dom.click(ctx.dom.byLabel("Dismiss message"));
    assert.doesNotMatch(ctx.dom.text(), /Could not copy/);
    assert.ok(drawer(ctx), "dismissing the message closed the drawer");
  });
});

test("Insert puts the clip in the message box at the caret, exactly, and sends nothing", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    await pasteSomething(ctx, "src/a b.ts");
    const box = ctx.dom.byField("Chat prompt") as HTMLTextAreaElement;
    await ctx.dom.fill(box, "fix the bug");
    box.setSelectionRange(3, 3);
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "src/a b.ts"), "Insert into the message box"));
    await ctx.settle();
    assert.equal(box.value, "fixsrc/a b.ts the bug", "nothing was added or trimmed, or it went in the wrong place");
    assert.equal(box.selectionStart, 13, "the caret was not left after the clip");
    assert.ok(ctx.dom.window.document.activeElement === box, "the message box did not take the focus back");
    assert.deepEqual(
      ctx.engine.filter((c) => c.method === "POST" && /\/turns$|^\/goals$/.test(c.path)).map((c) => c.path),
      [],
      "inserting a clip sent the message",
    );
  });
});

test("Insert twice puts it in twice, each at the caret the last one left", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    await pasteSomething(ctx, "ab");
    const box = ctx.dom.byField("Chat prompt") as HTMLTextAreaElement;
    await open(ctx);
    const insert = inRow(rowOf(ctx, "ab"), "Insert into the message box");
    await ctx.dom.click(insert);
    await ctx.dom.click(insert);
    await ctx.settle();
    assert.equal(box.value, "abab");
  });
});

test("a new message box does not replay an Insert that was already done", async () => {
  await withAnswer(
    async (ctx) => {
      await pasteSomething(ctx, "once");
      await open(ctx);
      await ctx.dom.click(inRow(rowOf(ctx, "once"), "Insert into the message box"));
      await ctx.settle();
      assert.equal((ctx.dom.byField("Chat prompt") as HTMLTextAreaElement).value, "once");
      // Away to a terminal, which replaces the message box, and back to the thread: a new one, starting empty.
      await ctx.dom.click(ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
      await ctx.settle();
      assert.ok(ctx.dom.container.querySelector('textarea[aria-label="Chat prompt"]') === null, "the message box did not go away");
      await ctx.dom.click(ctx.tabs()[0]);
      await ctx.settle();
      assert.equal((ctx.dom.byField("Chat prompt") as HTMLTextAreaElement).value, "", "an old Insert was replayed into a new message box");
    },
    { shellAnswers: { codify_terminal_open: () => "clip-term-r" } },
  );
});

test("Insert and Paste say why they cannot work where they cannot", async () => {
  await withApp({ viewport: WIDE, shellAnswers: { codify_terminal_open: () => "clip-term-a" } }, async (ctx) => {
    await pasteSomething(ctx, "ls");
    await open(ctx);
    // In a chat view there is a message box and no terminal.
    let row = rowOf(ctx, "ls");
    assert.equal(inRow(row, "Insert into the message box").disabled, false);
    assert.equal(inRow(row, "Paste into the terminal").disabled, true);
    assert.match(inRow(row, "Paste into the terminal").title, /terminal tab/);

    // In a terminal tab it is the other way round.
    await ctx.dom.click(ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
    await ctx.settle();
    row = rowOf(ctx, "ls");
    assert.equal(inRow(row, "Insert into the message box").disabled, true);
    assert.match(inRow(row, "Insert into the message box").title, /chat thread/);
    assert.equal(inRow(row, "Paste into the terminal").disabled, false);
  });
});

const writes = (ctx: AppContext): Array<Record<string, unknown>> =>
  ctx.shell.calls.flatMap((c, i) => (c === "codify_terminal_write" ? [ctx.shell.args[i]!] : []));

test("Paste puts a one-line clip into the shell the terminal tab shows", async () => {
  await withApp({ viewport: { width: 900, height: 600 }, shellAnswers: { codify_terminal_open: () => "clip-term-b" } }, async (ctx) => {
    await pasteSomething(ctx, "npm test");
    await ctx.dom.click(ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
    await ctx.settle();
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "npm test"), "Paste into the terminal"));
    await ctx.settle();
    assert.deepEqual(
      writes(ctx).map((w) => [w.terminalId, w.data]),
      [["clip-term-b", "npm test"]],
      "the clip did not reach the shell it was meant for, or was changed on the way",
    );
    assert.ok(!/Not pasted/.test(ctx.dom.text()));
  });
});

test("Paste refuses several lines while the shell would run each, and says so", async () => {
  await withApp({ viewport: { width: 900, height: 600 }, shellAnswers: { codify_terminal_open: () => "clip-term-c" } }, async (ctx) => {
    await pasteSomething(ctx, "cd build\nmake -j4");
    await ctx.dom.click(ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
    await ctx.settle();
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "cd build\nmake -j4"), "Paste into the terminal"));
    await ctx.settle();
    assert.deepEqual(writes(ctx), [], "a multi-line clip reached a shell that would have run it");
    assert.match(ctx.dom.text(), /each line would run/);
  });
});

test("Paste sends several lines once the shell has asked for bracketed paste, wrapped so they are held back", async () => {
  await withApp({ viewport: { width: 900, height: 600 }, shellAnswers: { codify_terminal_open: () => "clip-term-d" } }, async (ctx) => {
    await pasteSomething(ctx, "cd build\nmake -j4");
    await ctx.dom.click(ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
    await ctx.settle();
    await ctx.emit("terminal-output", { id: "clip-term-d", data: "\x1b[?2004h" });
    await ctx.settle();
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "cd build\nmake -j4"), "Paste into the terminal"));
    await ctx.settle();
    const sent = writes(ctx).map((w) => String(w.data)).join("");
    assert.ok(sent.startsWith("\x1b[200~") && sent.endsWith("\x1b[201~"), `not bracketed: ${JSON.stringify(sent)}`);
    assert.match(sent, /cd build\rmake -j4/, "the lines were not sent");
  });
});

test("Paste into a shell that has exited is not offered, and the pane is not written to", async () => {
  await withApp({ viewport: { width: 900, height: 600 }, shellAnswers: { codify_terminal_open: () => "clip-term-e" } }, async (ctx) => {
    await pasteSomething(ctx, "ls");
    await ctx.dom.click(ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
    await ctx.settle();
    await ctx.emit("terminal-exit", { id: "clip-term-e", code: 0 });
    await ctx.settle();
    await open(ctx);
    const paste = inRow(rowOf(ctx, "ls"), "Paste into the terminal");
    assert.equal(paste.disabled, true, "a finished shell was offered a paste");
    assert.match(paste.title, /exited/);
    assert.deepEqual(writes(ctx), []);
  });
});

test("a browser tab has neither a message box nor a terminal, and says so", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    await pasteSomething(ctx, "x");
    await ctx.dom.click(ctx.dom.container.querySelector('button[title^="Browser"]') as HTMLElement);
    await ctx.settle();
    await open(ctx);
    const row = rowOf(ctx, "x");
    assert.equal(inRow(row, "Insert into the message box").disabled, true);
    assert.equal(inRow(row, "Paste into the terminal").disabled, true);
    assert.equal(inRow(row, "Copy again").disabled, false);
  });
});

test("Pin keeps a clip first and Delete removes it, and both are remembered", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    await pasteSomething(ctx, "a");
    await pasteSomething(ctx, "b");
    await pasteSomething(ctx, "c");
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "a"), "Pin clip"));
    assert.deepEqual(rowTexts(ctx), ["a", "c", "b"], "a pinned clip is not first");
    await ctx.dom.click(inRow(rowOf(ctx, "c"), "Delete clip"));
    assert.deepEqual(rowTexts(ctx), ["a", "b"]);
    const kept = JSON.parse(stored(ctx)) as { clips?: Array<{ text: string }> } | Array<{ text: string }>;
    const list = Array.isArray(kept) ? kept : kept.clips ?? [];
    assert.deepEqual(list.map((c) => c.text).sort(), ["a", "b"], "the deleted clip is still in storage");
  });
});

test("Clear unpinned empties the list but for what is pinned", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    await pasteSomething(ctx, "keep");
    await pasteSomething(ctx, "lose 1");
    await pasteSomething(ctx, "lose 2");
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "keep"), "Pin clip"));
    await ctx.dom.click(ctx.dom.byLabel("Clear unpinned clips"));
    assert.deepEqual(rowTexts(ctx), ["keep"]);
  });
});

// ── messages go when they are no longer true ───────────────────────────────

const failCopies = (ctx: AppContext): void => {
  stubClipboard(ctx, async () => {
    throw new Error("denied");
  });
  (ctx.dom.window.document as unknown as { execCommand?: unknown }).execCommand = undefined;
};

test("a message about a failed copy goes when the next copy works", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    failCopies(ctx);
    await pasteSomething(ctx, "one");
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "one"), "Copy again"));
    await ctx.settle();
    assert.match(ctx.dom.text(), /Could not copy/);
    stubClipboard(ctx, async () => {});
    await ctx.dom.click(inRow(rowOf(ctx, "one"), "Copy again"));
    await ctx.settle();
    assert.doesNotMatch(ctx.dom.text(), /Could not copy/, "the message outlived the copy that worked");
  });
});

test("a message about a failed copy goes when something is inserted instead", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    failCopies(ctx);
    await pasteSomething(ctx, "one");
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "one"), "Copy again"));
    await ctx.settle();
    assert.match(ctx.dom.text(), /Could not copy/);
    await ctx.dom.click(inRow(rowOf(ctx, "one"), "Insert into the message box"));
    await ctx.settle();
    assert.doesNotMatch(ctx.dom.text(), /Could not copy/);
  });
});

test("a refused paste's message goes when a paste that is allowed goes through", async () => {
  await withApp({ viewport: { width: 900, height: 600 }, shellAnswers: { codify_terminal_open: () => "clip-term-g" } }, async (ctx) => {
    await pasteSomething(ctx, "cd a\nls");
    await pasteSomething(ctx, "ls");
    await ctx.dom.click(ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
    await ctx.settle();
    await open(ctx);
    await ctx.dom.click(inRow(rowOf(ctx, "cd a\nls"), "Paste into the terminal"));
    assert.match(ctx.dom.text(), /each line would run/);
    await ctx.dom.click(inRow(rowOf(ctx, "ls"), "Paste into the terminal"));
    await ctx.settle();
    assert.doesNotMatch(ctx.dom.text(), /each line would run/, "the refusal outlived the paste that worked");
    assert.deepEqual(writes(ctx).map((w) => w.data), ["ls"]);
  });
});

test("the pane itself refuses a paste once its shell has exited, whoever asks", async () => {
  const { pasteIntoTerminal } = await import("../src/terminalPaste.ts");
  await withApp({ viewport: { width: 900, height: 600 }, shellAnswers: { codify_terminal_open: () => "clip-term-h" } }, async (ctx) => {
    await ctx.dom.click(ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
    await ctx.settle();
    assert.equal(pasteIntoTerminal("clip-term-h", "ls"), "pasted", "the pane was not reachable at all");
    await ctx.emit("terminal-exit", { id: "clip-term-h", code: 0 });
    await ctx.settle();
    assert.equal(pasteIntoTerminal("clip-term-h", "ls"), "exited");
    assert.deepEqual(writes(ctx).map((w) => w.data), ["ls"], "a finished shell was written to");
  });
});

test("a pane that is closed is no longer reachable", async () => {
  const { pasteIntoTerminal } = await import("../src/terminalPaste.ts");
  await withApp({ viewport: { width: 900, height: 600 }, shellAnswers: { codify_terminal_open: () => "clip-term-i" } }, async (ctx) => {
    await ctx.dom.click(ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
    await ctx.settle();
    assert.equal(pasteIntoTerminal("clip-term-i", "ls"), "pasted");
    await ctx.dom.click(ctx.dom.container.querySelector('button[title^="Browser"]') as HTMLElement);
    await ctx.settle();
    assert.equal(pasteIntoTerminal("clip-term-i", "ls"), "no-terminal", "a pane that is not on screen was still being written to");
  });
});
