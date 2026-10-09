/**
 * The clipboard drawer, drawn on its own: what it shows, what each button hands back, and what it will not do.
 *
 * It is a view: the list, the note and the actions come in as props, and every press goes out as a call, so
 * these tests mount it with recorded handlers. `clipboardApp.test.ts` proves the App connects them.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { Clip } from "../src/clipboardHistory.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;
const { ClipboardDrawer } = await import("../src/components/ClipboardDrawer.tsx");
const { SKIP_MESSAGES, MAX_PINNED } = await import("../src/clipboardHistory.ts");

// One timestamp for every clip a test builds. Read per clip, `Date.now()` can roll over to the next millisecond
// between two clips, and the later one then counts as newer and sorts first, which flipped the order a test
// asserts (the history is newest first, and equal timestamps keep the order the clips were given in).
const FIVE_MINUTES_AGO = Date.now() - 5 * 60_000;
const clip = (id: string, text: string, over: Partial<Clip> = {}): Clip => ({
  id,
  text,
  at: FIVE_MINUTES_AGO,
  source: "selection",
  pinned: false,
  ...over,
});

interface Calls {
  copy: string[];
  insert: string[];
  terminal: string[];
  pin: string[];
  remove: string[];
  clear: number;
  dismiss: number;
  dismissNotice: number;
  close: number;
}

type Props = Partial<React.ComponentProps<typeof ClipboardDrawer>>;

async function mount(dom: Dom, clips: Clip[], props: Props = {}): Promise<Calls> {
  const calls: Calls = { copy: [], insert: [], terminal: [], pin: [], remove: [], clear: 0, dismiss: 0, dismissNotice: 0, close: 0 };
  await dom.render(
    h(ClipboardDrawer, {
      clips,
      lastSkip: null,
      notice: null,
      canInsert: true,
      canPasteToTerminal: true,
      onCopy: (c: Clip) => void calls.copy.push(c.id),
      onInsert: (c: Clip) => void calls.insert.push(c.id),
      onPasteToTerminal: (c: Clip) => void calls.terminal.push(c.id),
      onPin: (id: string) => void calls.pin.push(id),
      onRemove: (id: string) => void calls.remove.push(id),
      onClearUnpinned: () => void (calls.clear += 1),
      onDismissSkip: () => void (calls.dismiss += 1),
      onDismissNotice: () => void (calls.dismissNotice += 1),
      onClose: () => void (calls.close += 1),
      ...props,
    }),
  );
  return calls;
}

const rows = (dom: Dom): HTMLElement[] => [...dom.container.querySelectorAll<HTMLElement>("li")];
const rowOf = (dom: Dom, text: string): HTMLElement => {
  const found = rows(dom).find((r) => r.querySelector("pre")?.textContent === text);
  if (!found) throw new Error(`no clip "${text}"`);
  return found;
};
const inRow = (row: HTMLElement, label: string): HTMLButtonElement => {
  const found = row.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`);
  if (!found) throw new Error(`no "${label}" in the row: ${row.textContent}`);
  return found;
};

test("an empty drawer says what it keeps and what it never will", async () => {
  await withDom(async (dom) => {
    await mount(dom, []);
    const said = dom.text();
    assert.match(said, /Nothing yet/);
    assert.match(said, /copy, cut or paste/i);
    assert.match(said, /only in this window/i, "it does not say where the history lives");
    assert.match(said, /never kept/i, "it does not say what is refused");
    assert.match(said, /password/i);
    assert.deepEqual(rows(dom), []);
  });
});

test("the standing note is there with clips too, not only when empty", async () => {
  await withDom(async (dom) => {
    await mount(dom, [clip("a", "one")]);
    assert.match(dom.text(), /never kept/i);
    assert.match(dom.text(), /only in this window/i);
  });
});

test("clips are listed in the order given, with where they came from and how long ago", async () => {
  await withDom(async (dom) => {
    await mount(dom, [
      clip("a", "pinned one", { pinned: true, source: "code" }),
      clip("b", "second", { source: "paste" }),
      clip("c", "third", { at: Date.now() - 3 * 3600_000 }),
    ]);
    const list = rows(dom);
    assert.equal(list.length, 3);
    assert.match(list[0].textContent ?? "", /pinned one/);
    assert.match(list[0].textContent ?? "", /Code block/);
    assert.match(list[1].textContent ?? "", /Pasted/);
    assert.match(list[1].textContent ?? "", /5 min ago/);
    assert.match(list[2].textContent ?? "", /Copied/);
    assert.match(list[2].textContent ?? "", /3 h ago/);
  });
});

test("a long clip shows its first lines and says how much it left out", async () => {
  await withDom(async (dom) => {
    const text = Array.from({ length: 9 }, (_, n) => `line ${n + 1}`).join("\n");
    await mount(dom, [clip("a", text)]);
    const row = rows(dom)[0];
    assert.match(row.textContent ?? "", /line 4/);
    assert.doesNotMatch(row.textContent ?? "", /line 5/, "the preview showed more than its lines");
    assert.match(row.textContent ?? "", /5 more lines/);
    assert.match(row.textContent ?? "", /\b\d+ characters\b|\b\d+ chars\b/, "no length");
  });
});

test("a clip is drawn as text and as nothing else", async () => {
  await withDom(async (dom) => {
    await mount(dom, [clip("a", '<img src=x onerror="alert(1)"><script>alert(2)</script> [link](javascript:alert(3))')]);
    assert.ok(dom.container.querySelector("img") === null);
    assert.ok(dom.container.querySelector("script") === null);
    assert.ok(dom.container.querySelector("a") === null);
    assert.match(rows(dom)[0].textContent ?? "", /<img src=x onerror="alert\(1\)">/);
  });
});

test("indentation survives in the preview", async () => {
  await withDom(async (dom) => {
    await mount(dom, [clip("a", "def f():\n    return 1")]);
    const pre = rows(dom)[0].querySelector("pre") as HTMLElement;
    assert.equal(pre.textContent, "def f():\n    return 1");
    assert.match(pre.className, /whitespace-pre/);
  });
});

test("search narrows the list, says when nothing matches, and is itself never read", async () => {
  await withDom(async (dom) => {
    await mount(dom, [clip("a", "git rebase main"), clip("b", "npm test"), clip("c", "Git status")]);
    const box = dom.byField("Search clipboard history") as HTMLInputElement;
    assert.equal(box.closest('[data-clipboard="off"]') !== null || box.getAttribute("data-clipboard") === "off", true, "a pasted search term would be kept as a clip");
    await dom.fill(box, "git");
    assert.deepEqual(rows(dom).map((r) => /git (rebase|status)/i.exec(r.textContent ?? "")?.[0]), ["git rebase", "Git status"]);
    await dom.fill(box, "zzz");
    assert.deepEqual(rows(dom), []);
    assert.match(dom.text(), /No clips match/);
    assert.doesNotMatch(dom.text(), /Nothing yet/, "an empty search was described as an empty history");
  });
});

test("each button hands back the clip it sits on", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom, [clip("a", "first"), clip("b", "second")]);
    const [one, two] = rows(dom);
    await dom.click(inRow(two, "Copy again"));
    await dom.click(inRow(one, "Insert into the message box"));
    await dom.click(inRow(two, "Paste into the terminal"));
    await dom.click(inRow(one, "Pin clip"));
    await dom.click(inRow(two, "Delete clip"));
    assert.deepEqual(calls.copy, ["b"]);
    assert.deepEqual(calls.insert, ["a"]);
    assert.deepEqual(calls.terminal, ["b"]);
    assert.deepEqual(calls.pin, ["a"]);
    assert.deepEqual(calls.remove, ["b"]);
  });
});

test("a pinned clip says so, and its button unpins", async () => {
  await withDom(async (dom) => {
    await mount(dom, [clip("a", "keep", { pinned: true }), clip("b", "loose")]);
    const [kept, loose] = rows(dom);
    assert.equal(inRow(kept, "Unpin clip").getAttribute("aria-pressed"), "true");
    assert.equal(inRow(loose, "Pin clip").getAttribute("aria-pressed"), "false");
  });
});

test("at the pin limit a clip cannot be pinned, and says why, while pinned ones can still be unpinned", async () => {
  await withDom(async (dom) => {
    const full = Array.from({ length: MAX_PINNED }, (_, n) => clip(`p${n}`, `pinned ${n}`, { pinned: true }));
    await mount(dom, [...full, clip("x", "loose")]);
    const loose = inRow(rowOf(dom, "loose"), "Pin clip");
    assert.equal(loose.disabled, true, "a press that does nothing is live");
    assert.match(loose.title, new RegExp(`${MAX_PINNED}`));
    assert.match(loose.title, /unpin/i);
    assert.equal(inRow(rowOf(dom, "pinned 0"), "Unpin clip").disabled, false);
  });
});

test("one under the limit, pinning is allowed", async () => {
  await withDom(async (dom) => {
    const nearly = Array.from({ length: MAX_PINNED - 1 }, (_, n) => clip(`p${n}`, `pinned ${n}`, { pinned: true }));
    await mount(dom, [...nearly, clip("x", "loose")]);
    assert.equal(inRow(rowOf(dom, "loose"), "Pin clip").disabled, false);
  });
});

test("Insert and Paste say why they cannot be used, and do nothing when pressed", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom, [clip("a", "x")], { canInsert: false, canPasteToTerminal: false });
    const row = rows(dom)[0];
    const insert = inRow(row, "Insert into the message box");
    const paste = inRow(row, "Paste into the terminal");
    assert.equal(insert.disabled, true);
    assert.equal(paste.disabled, true);
    assert.match(insert.title, /chat/i, "no reason on the disabled Insert");
    assert.match(paste.title, /terminal/i, "no reason on the disabled Paste");
    await dom.click(insert);
    await dom.click(paste);
    assert.deepEqual([calls.insert, calls.terminal], [[], []]);
    assert.equal(inRow(row, "Copy again").disabled, false, "Copy needs neither a composer nor a terminal");
  });
});

test("a finished shell is its own reason, not a missing terminal", async () => {
  await withDom(async (dom) => {
    await mount(dom, [clip("a", "x")], { canPasteToTerminal: false, terminalExited: true });
    const paste = inRow(rows(dom)[0], "Paste into the terminal");
    assert.equal(paste.disabled, true);
    assert.match(paste.title, /exited/);
    assert.doesNotMatch(paste.title, /Open a terminal tab/);
  });
});

test("when a key was refused the drawer says so, once, and the note can be dismissed", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom, [], { lastSkip: "secret" });
    const note = dom.container.querySelector('[role="status"]') as HTMLElement;
    assert.ok(note, "no note");
    assert.equal(note.textContent?.includes(SKIP_MESSAGES.secret), true);
    await dom.click(dom.byLabel("Dismiss note"));
    assert.equal(calls.dismiss, 1);
  });
});

test("what an action could not do is said in the same place, and can be dismissed on its own", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom, [clip("a", "x")], { notice: "Not pasted: that shell has exited." });
    const notes = [...dom.container.querySelectorAll('[role="status"]')];
    assert.equal(notes.length, 1);
    assert.match(notes[0].textContent ?? "", /that shell has exited/);
    await dom.click(dom.byLabel("Dismiss message"));
    assert.deepEqual([calls.dismissNotice, calls.dismiss], [1, 0], "dismissing the message dismissed the refusal note");
  });
});

test("a refusal and a message can both be showing", async () => {
  await withDom(async (dom) => {
    await mount(dom, [], { lastSkip: "secret", notice: "Open a terminal tab to paste into it." });
    assert.equal(dom.container.querySelectorAll('[role="status"]').length, 2);
  });
});

test("no refusal, no note", async () => {
  await withDom(async (dom) => {
    await mount(dom, [clip("a", "x")], { lastSkip: null });
    assert.ok(dom.container.querySelector('[role="status"]') === null);
  });
});

test("Clear unpinned is offered only when there is something to clear, and says what it spares", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom, [clip("a", "keep", { pinned: true })]);
    const clear = dom.byLabel("Clear unpinned clips") as HTMLButtonElement;
    assert.equal(clear.disabled, true, "nothing to clear, and the button is live");
    assert.match(clear.title, /pinned/i);

    await mount(dom, [clip("a", "keep", { pinned: true }), clip("b", "go")], {
      onClearUnpinned: () => void (calls.clear += 1),
    });
    const live = dom.byLabel("Clear unpinned clips") as HTMLButtonElement;
    assert.equal(live.disabled, false);
    await dom.click(live);
    assert.equal(calls.clear, 1);
  });
});

test("Close closes", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom, []);
    await dom.click(dom.byLabel("Close clipboard"));
    assert.equal(calls.close, 1);
  });
});

test("the drawer is labelled for what it is, like the others", async () => {
  await withDom(async (dom) => {
    await mount(dom, []);
    const aside = dom.container.querySelector("aside") as HTMLElement;
    assert.equal(aside.getAttribute("aria-label"), "Clipboard");
    assert.match(aside.className, /\bw-80\b/);
    assert.match(aside.className, /max-w-\[40%\]/);
    assert.match(aside.className, /\bshrink-0\b/);
  });
});
