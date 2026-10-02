/**
 * What the clipboard history sees, mounted.
 *
 * The model (`clipboardHistory.test.ts`) decides what is kept; this file is about where the text comes from
 * and what the window does with it: the document's copy / cut / paste events, the Copy buttons that write
 * through `navigator.clipboard` (which fires no event at all), the fields that are never read, the note that
 * says why something was not kept, and that the list survives a restart without trusting what it reads.
 *
 * jsdom has no `ClipboardEvent`, so events are built by hand with the one property the code reads.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { ClipboardApi } from "../src/useClipboardHistory.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;
const { act } = React as unknown as { act: (body: () => Promise<void> | void) => Promise<void> };

const { useClipboardHistory } = await import("../src/useClipboardHistory.ts");
const { CLIPBOARD_KEY, MAX_CLIP_CHARS } = await import("../src/clipboardHistory.ts");
const { ClipboardRecorderContext } = await import("../src/clipboardContext.ts");
const { Markdown } = await import("../src/components/Markdown.tsx");
const { ApiKeyField } = await import("../src/components/ApiKeyField.tsx");
const { copySchemeText } = await import("../src/scheme.ts");

/**
 * The hook, mounted bare, or beside something to copy from. `api()` is whatever the last render returned.
 * `render` replaces the whole tree, so anything that must share the window with the hook is passed here and
 * not rendered afterwards (which would unmount the hook and leave the test listening to nothing).
 */
async function mountHistory(dom: Dom, beside?: React.ReactElement): Promise<() => ClipboardApi> {
  let latest: ClipboardApi | undefined;
  const Probe = (): null => {
    latest = useClipboardHistory();
    return null;
  };
  await dom.render(beside ? h(React.Fragment, null, h(Probe), beside) : h(Probe));
  return () => {
    assert.ok(latest, "the probe never rendered");
    return latest;
  };
}

/** A clipboard event as the browser builds it: a data store the handlers can write to and read back. */
function clipboardEvent(dom: Dom, type: "copy" | "cut" | "paste", text?: string): Event {
  const event = new dom.window.Event(type, { bubbles: true, cancelable: true });
  const store = new Map<string, string>();
  if (text !== undefined) store.set("text/plain", text);
  Object.defineProperty(event, "clipboardData", {
    value: {
      getData: (kind: string) => store.get(kind) ?? "",
      setData: (kind: string, value: string) => void store.set(kind, value),
    },
  });
  return event;
}

async function fire(target: EventTarget, event: Event): Promise<void> {
  await act(async () => {
    target.dispatchEvent(event);
  });
}

/** Put a sentence on the page, select all of it, and return the element holding it. */
function selectedParagraph(dom: Dom, text: string): HTMLElement {
  const p = dom.window.document.createElement("p");
  p.textContent = text;
  dom.window.document.body.appendChild(p);
  dom.window.getSelection()?.selectAllChildren(p);
  return p;
}

const texts = (api: ClipboardApi): string[] => api.clips.map((c) => c.text);

test("copying selected text on the page keeps it", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const p = selectedParagraph(dom, "the parser drops trailing commas");
    await fire(p, clipboardEvent(dom, "copy"));
    assert.deepEqual(texts(api()), ["the parser drops trailing commas"]);
    assert.equal(api().clips[0].source, "selection");
    assert.equal(api().clips[0].pinned, false);
  });
});

test("a copy in a text field keeps the selected slice, not the whole field", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const box = dom.window.document.createElement("textarea");
    dom.window.document.body.appendChild(box);
    box.value = "fix the parser bug";
    box.setSelectionRange(8, 14);
    await fire(box, clipboardEvent(dom, "copy"));
    assert.deepEqual(texts(api()), ["parser"]);
  });
});

test("a cut is kept the same way as a copy", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const box = dom.window.document.createElement("input");
    dom.window.document.body.appendChild(box);
    box.value = "alpha beta";
    box.setSelectionRange(0, 5);
    await fire(box, clipboardEvent(dom, "cut"));
    assert.deepEqual(texts(api()), ["alpha"]);
    assert.equal(api().clips[0].source, "selection");
  });
});

test("text an inner handler put on the clipboard is what is kept, wherever the selection was", async () => {
  // The terminal draws its own selection, so the window has none; it writes the text through the event.
  // The history listens after it, so the clip is exactly what the person has on their clipboard.
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const term = dom.window.document.createElement("div");
    dom.window.document.body.appendChild(term);
    term.addEventListener("copy", (e) => {
      (e as unknown as { clipboardData: { setData(k: string, v: string): void } }).clipboardData.setData(
        "text/plain",
        "npm run build",
      );
      e.preventDefault();
    });
    dom.window.getSelection()?.removeAllRanges();
    await fire(term, clipboardEvent(dom, "copy"));
    assert.deepEqual(texts(api()), ["npm run build"]);
  });
});

test("a paste is kept as a paste, from the text on the clipboard", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const box = dom.window.document.createElement("textarea");
    dom.window.document.body.appendChild(box);
    await fire(box, clipboardEvent(dom, "paste", "git rebase --onto main"));
    assert.deepEqual(texts(api()), ["git rebase --onto main"]);
    assert.equal(api().clips[0].source, "paste");
  });
});

test("a paste is seen even where a handler stops the event from travelling", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const box = dom.window.document.createElement("textarea");
    dom.window.document.body.appendChild(box);
    box.addEventListener("paste", (e) => e.stopPropagation());
    await fire(box, clipboardEvent(dom, "paste", "still seen"));
    assert.deepEqual(texts(api()), ["still seen"]);
  });
});

test("a copy with nothing selected, and a paste with no text in it, leave no trace", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    dom.window.getSelection()?.removeAllRanges();
    await fire(dom.window.document.body, clipboardEvent(dom, "copy"));
    await fire(dom.window.document.body, clipboardEvent(dom, "paste", ""));
    await fire(dom.window.document.body, clipboardEvent(dom, "paste", "   \n  "));
    assert.deepEqual(texts(api()), []);
    assert.equal(api().lastSkip, null, "an empty copy is not worth a note");
  });
});

test("a password field is never read, in either direction, and says nothing about it", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const box = dom.window.document.createElement("input");
    box.type = "password";
    dom.window.document.body.appendChild(box);
    box.value = "correct horse";
    box.setSelectionRange(0, 7);
    await fire(box, clipboardEvent(dom, "copy"));
    await fire(box, clipboardEvent(dom, "cut"));
    await fire(box, clipboardEvent(dom, "paste", "pasted into a password box"));
    assert.deepEqual(texts(api()), []);
    assert.equal(api().lastSkip, null);
  });
});

test("anything inside a data-clipboard=off region is never read", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const region = dom.window.document.createElement("div");
    region.setAttribute("data-clipboard", "off");
    const inner = dom.window.document.createElement("p");
    inner.textContent = "a note the person asked us not to keep";
    region.appendChild(inner);
    dom.window.document.body.appendChild(region);
    dom.window.getSelection()?.selectAllChildren(inner);
    await fire(inner, clipboardEvent(dom, "copy"));
    await fire(inner, clipboardEvent(dom, "paste", "pasted in there"));
    assert.deepEqual(texts(api()), []);
  });
});

test("an event on a text node inside an off region is still read as inside it", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const region = dom.window.document.createElement("div");
    region.setAttribute("data-clipboard", "off");
    region.textContent = "hidden words";
    dom.window.document.body.appendChild(region);
    dom.window.getSelection()?.selectAllChildren(region);
    const textNode = region.firstChild as Node;
    await fire(textNode, clipboardEvent(dom, "copy"));
    assert.deepEqual(texts(api()), []);
  });
});

test("a key is refused, the person is told once, and the next thing kept clears the note", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const box = dom.window.document.createElement("textarea");
    dom.window.document.body.appendChild(box);

    await fire(box, clipboardEvent(dom, "paste", "sk-proj-AbCdEf0123456789AbCdEf0123456789"));
    assert.deepEqual(texts(api()), [], "a secret reached the history");
    assert.equal(api().lastSkip, "secret");

    await fire(box, clipboardEvent(dom, "paste", "an ordinary sentence"));
    assert.deepEqual(texts(api()), ["an ordinary sentence"]);
    assert.equal(api().lastSkip, null, "the note outlived the thing it was about");
  });
});

test("the note can be dismissed", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    await fire(dom.window.document.body, clipboardEvent(dom, "paste", "ghp_AbCdEf0123456789AbCdEf0123456789abcd"));
    assert.equal(api().lastSkip, "secret");
    await act(async () => api().dismissSkip());
    assert.equal(api().lastSkip, null);
  });
});

test("a clip over the limit is refused with a note, never cut", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    await fire(dom.window.document.body, clipboardEvent(dom, "paste", "word ".repeat(MAX_CLIP_CHARS)));
    assert.deepEqual(texts(api()), []);
    assert.equal(api().lastSkip, "too-long");
  });
});

test("a Copy button reports through record, and a repeat moves the clip up rather than adding one", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    await act(async () => api().record("const x = 1;", "code"));
    await act(async () => api().record("const y = 2;", "code"));
    await act(async () => api().record("const x = 1;", "code"));
    assert.deepEqual(texts(api()), ["const x = 1;", "const y = 2;"]);
    assert.equal(api().clips[0].source, "code");
  });
});

test("record refuses a secret as the events do", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    await act(async () => api().record("nvapi-AbCdEf0123456789AbCdEf0123456789", "code"));
    assert.deepEqual(texts(api()), []);
    assert.equal(api().lastSkip, "secret");
  });
});

test("pin, remove and clear work on the list the hook holds, and survive being called together", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    await act(async () => {
      api().record("one", "code");
      api().record("two", "code");
      api().record("three", "code");
    });
    assert.deepEqual(texts(api()), ["three", "two", "one"], "two records in one tick lost one");

    const two = api().clips.find((c) => c.text === "two")!;
    await act(async () => api().togglePin(two.id));
    assert.equal(api().clips.find((c) => c.text === "two")!.pinned, true);

    const one = api().clips.find((c) => c.text === "one")!;
    await act(async () => api().remove(one.id));
    assert.deepEqual(texts(api()).sort(), ["three", "two"]);

    await act(async () => api().clearUnpinned());
    assert.deepEqual(texts(api()), ["two"], "clearing the unpinned took the pinned one too");
  });
});

test("the list is remembered, and what comes back is checked", async () => {
  await withDom(async (dom) => {
    const first = await mountHistory(dom);
    await act(async () => first().record("kept across a restart", "code"));
    const stored = dom.window.localStorage.getItem(CLIPBOARD_KEY) ?? "";
    assert.match(stored, /kept across a restart/);
    await dom.render(h("div"));

    // Someone edited the stored list by hand and put a key in it. It is read, and refused.
    const parsed = JSON.parse(stored) as { clips: Array<Record<string, unknown>> } | Array<Record<string, unknown>>;
    const list = Array.isArray(parsed) ? parsed : parsed.clips;
    list.push({
      id: "planted",
      text: "sk-proj-AbCdEf0123456789AbCdEf0123456789",
      at: Date.now(),
      source: "paste",
      pinned: true,
    });
    dom.window.localStorage.setItem(CLIPBOARD_KEY, JSON.stringify(parsed));

    const second = await mountHistory(dom);
    assert.deepEqual(texts(second()), ["kept across a restart"]);
  });
});

test("a window that will not let it read or write storage still works", async () => {
  await withDom(async (dom) => {
    Object.defineProperty(dom.window, "localStorage", {
      get() {
        throw new Error("blocked");
      },
      configurable: true,
    });
    const api = await mountHistory(dom);
    await act(async () => api().record("still here", "code"));
    assert.deepEqual(texts(api()), ["still here"]);
  });
});

test("it listens once, however often the window re-renders, and stops when it goes", async () => {
  await withDom(async (dom) => {
    const doc = dom.window.document;
    // A listener is its type, the function and the capture flag: removing one with a different flag removes
    // nothing in a browser, so a count of calls alone would call a leak balanced.
    const live = new Set<string>();
    const ids = new WeakMap<object, number>();
    let issued = 0;
    const key = (type: string, fn: unknown, options: unknown): string => {
      const target = fn as object;
      if (!ids.has(target)) ids.set(target, (issued += 1));
      const capture = typeof options === "boolean" ? options : Boolean((options as { capture?: boolean } | undefined)?.capture);
      return `${type}:${ids.get(target)}:${capture}`;
    };
    const add = doc.addEventListener.bind(doc) as (...a: unknown[]) => void;
    const remove = doc.removeEventListener.bind(doc) as (...a: unknown[]) => void;
    doc.addEventListener = ((type: string, fn: unknown, options?: unknown) => {
      if (["copy", "cut", "paste"].includes(type)) live.add(key(type, fn, options));
      return add(type, fn, options);
    }) as typeof doc.addEventListener;
    doc.removeEventListener = ((type: string, fn: unknown, options?: unknown) => {
      if (["copy", "cut", "paste"].includes(type)) live.delete(key(type, fn, options));
      return remove(type, fn, options);
    }) as typeof doc.removeEventListener;
    const heard = (): string[] => [...live].map((k) => k.split(":")[0]).sort();

    const api = await mountHistory(dom);
    for (let n = 0; n < 5; n += 1) await act(async () => api().record(`clip ${n}`, "code"));
    assert.deepEqual(heard(), ["copy", "cut", "paste"], "a listener per render, or none at all");

    await dom.render(h("div"));
    assert.deepEqual(heard(), [], "listeners left behind after the window went");
  });
});

test("a list that would not load is not written over on the way in", async () => {
  await withDom(async (dom) => {
    dom.window.localStorage.setItem(CLIPBOARD_KEY, JSON.stringify({ version: 1, clips: [] }));
    const writes: string[] = [];
    const proto = dom.window.Storage.prototype;
    const original = proto.setItem;
    proto.setItem = function (name: string, value: string) {
      writes.push(name);
      return original.call(this, name, value);
    };
    try {
      await mountHistory(dom);
      assert.deepEqual(writes, [], "opening the window wrote the history back, which can only lose it");
    } finally {
      proto.setItem = original;
    }
  });
});

test("a code block's Copy button records the code once the copy has worked", async () => {
  await withDom(async (dom) => {
    const written: string[] = [];
    Object.defineProperty(dom.window.navigator, "clipboard", {
      value: { writeText: async (t: string) => void written.push(t) },
      configurable: true,
    });
    const seen: Array<[string, string]> = [];
    const record = (text: string, source: string): void => void seen.push([text, source]);
    await dom.render(
      h(ClipboardRecorderContext.Provider, { value: record as never }, h(Markdown, { text: "```ts\nconst x = 1;\n```" })),
    );
    await dom.click(dom.byLabel("Copy code"));
    await dom.settle();
    assert.deepEqual(written, ["const x = 1;"]);
    assert.deepEqual(seen, [["const x = 1;", "code"]]);
  });
});

test("a Copy that failed is not in the history, as it is not on the clipboard", async () => {
  await withDom(async (dom) => {
    Object.defineProperty(dom.window.navigator, "clipboard", {
      value: {
        writeText: async () => {
          throw new Error("denied");
        },
      },
      configurable: true,
    });
    // No `execCommand` either: both ways of copying fail.
    (dom.window.document as unknown as { execCommand?: unknown }).execCommand = undefined;
    const seen: string[] = [];
    await dom.render(
      h(
        ClipboardRecorderContext.Provider,
        { value: ((t: string) => void seen.push(t)) as never },
        h(Markdown, { text: "```ts\nconst x = 1;\n```" }),
      ),
    );
    await dom.click(dom.byLabel("Copy code"));
    await dom.settle();
    assert.deepEqual(seen, []);
    assert.match(dom.text(), /Copy failed/);
  });
});

test("a code block outside any provider still copies, and records nothing", async () => {
  await withDom(async (dom) => {
    const written: string[] = [];
    Object.defineProperty(dom.window.navigator, "clipboard", {
      value: { writeText: async (t: string) => void written.push(t) },
      configurable: true,
    });
    await dom.render(h(Markdown, { text: "```\nplain\n```" }));
    await dom.click(dom.byLabel("Copy code"));
    await dom.settle();
    assert.deepEqual(written, ["plain"]);
  });
});

test("the fallback copy goes through a field the history ignores, so a Copy button is recorded once and as itself", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const doc = dom.window.document;
    // The browser's own behaviour: execCommand("copy") fires `copy` at the selected field.
    (doc as unknown as { execCommand: (name: string) => boolean }).execCommand = (name: string) => {
      const field = doc.querySelector("textarea");
      assert.equal(name, "copy");
      assert.ok(field, "no field was selected for the copy");
      field.dispatchEvent(clipboardEvent(dom, "copy"));
      return true;
    };
    let outcome = "";
    await act(async () => {
      outcome = await copySchemeText("kept by a button", { document: doc });
    });
    assert.equal(outcome, "copied-by-fallback");
    assert.deepEqual(texts(api()), [], "the button's own scratch field was read as the person's selection");
  });
});

test("an API key field is never read, masked or shown", async () => {
  await withDom(async (dom) => {
    const api = await mountHistory(dom, h(ApiKeyField, { onChange: () => {} }));
    const input = dom.container.querySelector("input") as HTMLInputElement;

    // A control, so an empty history below means "refused" and not "was never listening".
    const plain = dom.window.document.createElement("input");
    dom.window.document.body.appendChild(plain);
    plain.value = "control";
    plain.setSelectionRange(0, 7);
    await fire(plain, clipboardEvent(dom, "copy"));
    assert.deepEqual(texts(api()), ["control"]);

    for (const show of [false, true]) {
      if (show) await dom.click(dom.container.querySelector("button") as HTMLButtonElement);
      assert.equal(input.type, show ? "text" : "password");
      await dom.fill(input, "plain looking value");
      input.setSelectionRange(0, 5);
      await fire(input, clipboardEvent(dom, "copy"));
      await fire(input, clipboardEvent(dom, "paste", "plain looking value"));
    }
    assert.deepEqual(texts(api()), ["control"], "the key field was read once it was shown");
  });
});

test("the stale-token banner's fallback copy is not read as a selection either", async () => {
  const { StaleAuthBanner } = await import("../src/components/StaleAuthBanner.tsx");
  await withDom(async (dom) => {
    const api = await mountHistory(dom);
    const doc = dom.window.document;
    Object.defineProperty(dom.window.navigator, "clipboard", { value: undefined, configurable: true });
    (doc as unknown as { execCommand: (name: string) => boolean }).execCommand = () => {
      doc.querySelector("textarea")?.dispatchEvent(clipboardEvent(dom, "copy"));
      return true;
    };
    const banner = document.createElement("div");
    dom.window.document.body.appendChild(banner);
    const { createRoot } = await import("react-dom/client");
    const root = createRoot(banner);
    await act(async () => {
      root.render(h(StaleAuthBanner, { port: 7421, onRetry: () => {}, onDismiss: () => {} }));
    });
    const button = [...banner.querySelectorAll("button")].find((b) => /Copy commands/.test(b.textContent ?? ""));
    assert.ok(button, "no Copy commands button");
    await dom.click(button);
    await dom.settle();
    assert.match(banner.textContent ?? "", /Copied/, "the copy did not run, so this proved nothing");
    assert.deepEqual(texts(api()), []);
    await act(async () => root.unmount());
  });
});

test("a provider's key field is never read, masked or shown", async () => {
  const { ProviderRow } = await import("../src/components/ProviderRow.tsx");
  await withDom(async (dom) => {
    const row = h(ProviderRow, {
      keyStatus: {
        provider: "nvidia",
        protocol: "openai_compat",
        base_url: "https://integrate.api.nvidia.com/v1",
        has_key: false,
        needs_key: true,
        storage: "keyring",
        storage_detail: "OS keychain",
      } as never,
      models: [],
      roleProviders: [],
      onSaveKey: async () => {},
      onApplyModel: async () => {},
    });
    const api = await mountHistory(dom, row);
    const input = dom.container.querySelector("input") as HTMLInputElement;

    const plain = dom.window.document.createElement("input");
    dom.window.document.body.appendChild(plain);
    plain.value = "control";
    plain.setSelectionRange(0, 7);
    await fire(plain, clipboardEvent(dom, "copy"));
    assert.deepEqual(texts(api()), ["control"]);

    await dom.fill(input, "a value that is not shaped like a key");
    input.setSelectionRange(0, 7);
    await fire(input, clipboardEvent(dom, "paste", "a value that is not shaped like a key"));
    await dom.click(dom.byLabel("Show the key"));
    assert.equal(input.type, "text", "the key was not shown, so this proved nothing about a shown key");
    input.setSelectionRange(0, 7);
    await fire(input, clipboardEvent(dom, "copy"));
    await fire(input, clipboardEvent(dom, "cut"));
    assert.deepEqual(texts(api()), ["control"], "the key field was read");
  });
});
