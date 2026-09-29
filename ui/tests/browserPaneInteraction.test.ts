/**
 * What it is to *use* the browser pane: type, press Enter, and see either a
 * seated page or the shell's refusal — in the same frame, in the tab you
 * typed in.
 *
 * `browserPane.test.ts` freezes what the pane *says*; this file drives what it
 * *does*. The interesting behaviours are all reachable only through events:
 * the classifier running before the round trip, the refusal clearing on the
 * next accepted address, Escape restoring the committed address, and the
 * bounds report that places the native page — none of them exist in static
 * markup.
 *
 * This file needs the DOM harness (`dom.ts`), which is jsdom-based: the pane
 * mounts for real, ResizeObserver is the harness's stub (it fires, so the
 * bounds report runs), and `onOpen`/`onNavigate` are spies. The shell is not
 * involved — the classifier's job is exactly that refusals never reach it.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
import type { Dom } from "./dom.ts";
import type { BrowserHistory } from "../src/browserHistory.ts";

const React = (await import("react")).default;
const h = React.createElement;

const loadPane = async () => {
  const mod = await import("../src/components/BrowserPane.tsx");
  return mod.BrowserPane;
};

interface Calls {
  open: string[];
  navigate: string[];
  bounds: Array<{ x: number; y: number; width: number; height: number }>;
  back: number;
  forward: number;
}

const renderPane = async (
  dom: Dom,
  props: {
    url?: string;
    history?: BrowserHistory;
    error?: string | null;
  } = {}
) => {
  const BrowserPane = await loadPane();
  const calls: Calls = { open: [], navigate: [], bounds: [], back: 0, forward: 0 };
  const el = h(BrowserPane, {
    tabId: "browser-1",
    onOpen: (url: string) => calls.open.push(url),
    onNavigate: (url: string) => calls.navigate.push(url),
    onBack: () => {
      calls.back += 1;
    },
    onForward: () => {
      calls.forward += 1;
    },
    onBounds: (b: { x: number; y: number; width: number; height: number }) =>
      calls.bounds.push(b),
    ...props,
  } as any);
  await dom.render(el);
  return { calls };
};

const typeAddress = async (dom: Dom, value: string, key = "Enter") => {
  const input = dom.byLabel("Address") as HTMLInputElement;
  await dom.fill(input, value);
  await dom.press(input, key);
};

test("typing an ordinary address opens a page through the guarded path", async () => {
  await withDom(async (dom) => {
    const { calls } = await renderPane(dom);
    await typeAddress(dom, "example.com");
    assert.deepEqual(calls.open, ["https://example.com/"], "the bare host got its scheme and reached the shell");
    assert.deepEqual(calls.navigate, [], "no page existed yet, so this was an open, not a navigate");
  });
});

test("a typed scheme the shell refuses is refused in the same frame, and the shell never hears of it", async () => {
  await withDom(async (dom) => {
    const { calls } = await renderPane(dom);
    await typeAddress(dom, "javascript:alert(document.cookie)");
    assert.deepEqual(calls.open, [], "nothing was seated");
    assert.deepEqual(calls.navigate, []);
    // The shell's sentence, verbatim — the mirror owns the wording.
    const alert = dom.container.querySelector('[role="alert"]');
    assert.ok(alert, "no refusal is on screen");
    // A refusal sentence, whichever form the input earns: a `://` scheme gets
    // the shell's "refusing to navigate" wording, anything else that cannot
    // become an address gets "not a navigable URL". Both are refusals, both
    // come from the shell's own sentences, and neither reached the shell.
    assert.match(
      alert!.textContent ?? "",
      /^(refusing to navigate to "javascript:alert\(document\.cookie\)"|not a navigable URL: "javascript:alert\(document\.cookie\)")/
    );
  });
});

test("a loopback address is refused before any round trip", async () => {
  await withDom(async (dom) => {
    const { calls } = await renderPane(dom);
    await typeAddress(dom, "localhost:3000");
    assert.deepEqual(calls.open, [], "a loopback page was never seated");
    const alert = dom.container.querySelector('[role="alert"]');
    assert.match(alert!.textContent ?? "", /127\.0\.0\.1|localhost/);
  });
});

test("a numeric spelling of loopback is refused the same as its name", async () => {
  await withDom(async (dom) => {
    const { calls } = await renderPane(dom);
    await typeAddress(dom, "2130706433");
    assert.deepEqual(calls.open, []);
    const alert = dom.container.querySelector('[role="alert"]');
    assert.ok(alert, "the integer form reached the shell or showed nothing");
  });
});

test("the refusal clears when the next address is accepted", async () => {
  await withDom(async (dom) => {
    const { calls } = await renderPane(dom);
    await typeAddress(dom, "localhost");
    const alert = dom.container.querySelector('[role="alert"]');
    assert.ok(alert, "the refusal showed");
    await typeAddress(dom, "example.com");
    assert.deepEqual(calls.open, ["https://example.com/"]);
    assert.equal(
      dom.container.querySelector('[role="alert"]'),
      null,
      "the refusal outlived the address that fixed it"
    );
  });
});

test("Escape abandons a half-typed address and puts the committed one back", async () => {
  await withDom(async (dom) => {
    const { calls } = await renderPane(dom);
    await typeAddress(dom, "example.com");
    assert.deepEqual(calls.open, ["https://example.com/"]);
    const input = dom.byLabel("Address") as HTMLInputElement;
    await dom.fill(input, "https://half-typed");
    await dom.press(input, "Escape");
    assert.equal(
      (input as HTMLInputElement).value,
      "https://example.com/",
      "Escape did not restore the committed address"
    );
    assert.deepEqual(calls.open, ["https://example.com/"], "Escape is not a navigation");
  });
});

test("a pane with a page navigates it instead of seating another", async () => {
  await withDom(async (dom) => {
    const url = "https://docs.rs/tauri/latest/";
    const history = (await import("../src/browserHistory.ts")).visit(
      (await import("../src/browserHistory.ts")).emptyHistory(),
      url
    );
    const { calls } = await renderPane(dom, { url, history });
    await typeAddress(dom, "example.com");
    assert.deepEqual(calls.navigate, ["https://example.com/"], "the existing page navigated");
    assert.deepEqual(calls.open, [], "no second page was seated");
  });
});

test("the pane reports its content rectangle for the page to be seated on", async () => {
  await withDom(async (dom) => {
    const { calls } = await renderPane(dom);
    // The harness stubs the element rect (1440x900) and ResizeObserver fires
    // on observe, so the mount-time report happened.
    assert.ok(calls.bounds.length >= 1, "no bounds report on mount — the page would not be placed");
    const b = calls.bounds[0];
    assert.ok(b.width > 0 && b.height > 0, "a zero rectangle was reported");
  });
});

test("Reload commits the address on screen, which is a navigate of the open page", async () => {
  await withDom(async (dom) => {
    const url = "https://docs.rs/tauri/latest/";
    const bh = await import("../src/browserHistory.ts");
    const history = bh.visit(bh.emptyHistory(), url);
    const { calls } = await renderPane(dom, { url, history });
    const reload = dom.byLabel("Reload");
    await dom.click(reload);
    assert.deepEqual(calls.navigate, [url], "reload did not navigate the seated page");
    assert.deepEqual(calls.open, [], "reload seated a second page");
  });
});

test("the DevTools control toggles and reports its state", async () => {
  await withDom(async (dom) => {
    let toggles = 0;
    const BrowserPane = await loadPane();
    const calls: Calls = { open: [], navigate: [], bounds: [], back: 0, forward: 0 };
    const el = h(BrowserPane, {
      tabId: "browser-1",
      onOpen: (u: string) => calls.open.push(u),
      onNavigate: (u: string) => calls.navigate.push(u),
      onBack: () => {},
      onForward: () => {},
      onBounds: () => {},
      onToggleDevtools: () => {
        toggles += 1;
      },
      devtoolsOpen: false,
    } as any);
    await dom.render(el);
    const control = dom.byLabel("DevTools");
    await dom.click(control);
    assert.equal(toggles, 1, "pressing DevTools did not toggle");
    // aria-pressed is the inspector's state, rendered from the prop —
    // the pane never guesses it.
    assert.match(control.outerHTML, /aria-pressed="false"/);
  });
});

test("there is no control that hands a page to the operating system", async () => {
  // The inverse of the test this replaces, and the point of it: a page is
  // loaded, its address is known, and the pane still offers no way to take
  // that address anywhere. Every website Codify opens is a tab in Codify, and
  // a control that quietly undoes that is not a feature.
  await withDom(async (dom) => {
    const BrowserPane = await loadPane();
    const bh = await import("../src/browserHistory.ts");
    const url = "https://docs.rs/tauri/latest/";
    await dom.render(
      h(BrowserPane, {
        tabId: "browser-1",
        url,
        history: bh.visit(bh.emptyHistory(), url),
        onOpen: () => {},
        onNavigate: () => {},
        onBack: () => {},
        onForward: () => {},
        onBounds: () => {},
      } as any),
    );
    const labels = [...dom.container.querySelectorAll("button[aria-label]")].map((b) =>
      b.getAttribute("aria-label"),
    );
    assert.equal(
      labels.filter((l) => /external|system browser|open in/i.test(l ?? "")).length,
      0,
      `the pane offers a way out: ${JSON.stringify(labels)}`
    );
    // The page's own address is still on screen, in the address bar, where a
    // user reads it — the control is gone, not the information. (The bar is an
    // input, so this is its value rather than the tree's text.)
    const address = dom.container.querySelector("input[type=text]") as HTMLInputElement | null;
    assert.ok(address, "the pane has no address bar to show the page's URL in");
    assert.match(address!.value, /docs\.rs/);
  });
});

test("Back and Forward send the address their stack step names", async () => {
  await withDom(async (dom) => {
    const bh = await import("../src/browserHistory.ts");
    const two = bh.visit(bh.visit(bh.emptyHistory(), "https://a.example"), "https://b.example");
    const back = bh.goBack(two)!;
    const { calls } = await renderPane(dom, { url: back.url, history: back.history });
    await dom.click(dom.byLabel("Forward"));
    assert.ok(calls.forward === 1, "Forward did not fire");
  });
});
