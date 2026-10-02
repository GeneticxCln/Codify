/**
 * A DOM you can click.
 *
 * ## What this is for
 *
 * Most of this suite renders a component and reads the string it produced. That
 * says a sentence is on screen; it cannot say a button *does* anything.
 * `renderToStaticMarkup` runs no effects and no events, so a control wired to
 * nothing produces perfect markup. Three gaps in this suite were exactly that
 * — the radiogroup's arrow keys, the tint picker's colour wells, and the scheme
 * import's paste box — and each was being covered by a test that read the
 * **source** and matched a regex, which is a test of the file rather than of the
 * feature.
 *
 * So this installs a real DOM and gives the suite the one thing a static render
 * cannot: a way to press something and see what happens.
 *
 * ## Why it is separate from `tsxLoader.ts`
 *
 * `tsxLoader` already installs a `localStorage` stub, and its own comment says
 * why it stops there — "a new module-scope read will say so loudly, which is the
 * right way to find out". Folding jsdom in there would have replaced that
 * loudness with a blanket: every existing render test would silently gain a
 * `window` and a `document`, and nobody would know which of them had been
 * relying on not having one. This file is opt-in, so a test that gets a DOM is a
 * test that asked for one.
 *
 * ## Why the component is imported *inside* `withDom`
 *
 * `react-dom/client` reads `document` and `window` while it is being
 * **imported**, not when it is used. So does any module graph that reaches it.
 * A static `import` of a component at the top of a test file is hoisted above
 * the jsdom setup, React's client build finds no document, and the harness
 * mounts nothing while every assertion passes. Loading the component from
 * inside the callback is the only ordering that cannot be got wrong, so the API
 * is shaped to make that the path of least resistance rather than a rule to
 * remember.
 *
 * ## What the browser still refuses to do
 *
 * jsdom is a DOM, not a browser, and a harness that pretends otherwise gets
 * tests that pass for the wrong reason. Three of its gaps are answered here
 * and each answer is the honest one:
 *
 * - **Scrolling.** `Element.prototype.scrollIntoView` does not exist, and a
 *   document with no layout has no scroll position. The element that was
 *   scrolled to is recorded in `dom.scrolls` and nothing moves — "the second
 *   edit was brought into view" is a claim about *which* element was named, and
 *   that is all a test can legitimately make.
 * - **The network.** `api.ts` calls the bare global `fetch` with the engine's
 *   port, so a component that fetches on mount would otherwise open a real
 *   socket to `127.0.0.1`. The installed `fetch` records the attempt in
 *   `dom.fetches` and rejects, naming the URL. A test that wants an answer
 *   assigns `globalThis.fetch` itself; a test that wants to know what was
 *   *asked* for only reads the list.
 * - **Layout-dependent senses.** Sizes, `matchMedia` and the observers are
 *   answered above, in `installEnvironment`, and answered with fixed answers,
 *   because there is no honest alternative and pretending otherwise is worse
 *   than a constant.
 */
import type React from "react";
import { withoutHooks } from "./tsxLoader.ts";

/** The globals a jsdom window owns that `src/` may read. */
const GLOBALS = [
  "window",
  "document",
  "navigator",
  "location",
  "history",
  "localStorage",
  "sessionStorage",
  "Node",
  "Element",
  "HTMLElement",
  "HTMLInputElement",
  "HTMLTextAreaElement",
  "HTMLButtonElement",
  "Event",
  "CustomEvent",
  "MouseEvent",
  "KeyboardEvent",
  "FocusEvent",
  "getComputedStyle",
  "requestAnimationFrame",
  "cancelAnimationFrame",
  "DOMParser",
  "SVGElement",
  "HTMLCanvasElement",
  "HTMLElement",
  "HTMLInputElement",
  "HTMLTextAreaElement",
  "MouseEvent",
  "File",
  "fetch",
  // A text editor (`@codemirror/view`) watches its own DOM and reads the selection.
  "MutationObserver",
  "Range",
  "Selection",
  // Deliberately not `Window`. CodeMirror asks `elt instanceof Window` on every measure; defined, the measure runs to the end,
  // and with no layout in jsdom it settles on a two-line viewport and stops drawing the lines the tests are looking at. Undefined,
  // the measure throws inside a timer (a line on stderr, nothing else) and the view keeps drawing the whole document.
] as const;

/** One document, and everything a test can do to it. */
export interface Dom {
  /** The tree this test owns. Query *this* rather than `document`. */
  container: HTMLElement;
  /** The window, for constructing events with the right realm's constructors. */
  window: Window & typeof globalThis;
  /** Mount an element, replacing anything already mounted. */
  render(element: React.ReactElement): Promise<void>;
  /** Click, and let React finish before returning. */
  click(element: Element): Promise<void>;
  /** Set a controlled input's value the way a person would, then let React see it. */
  fill(element: HTMLInputElement | HTMLTextAreaElement, value: string): Promise<void>;
  /** Choose a file on a file input, as a person double-clicking one would. */
  selectFile(element: HTMLInputElement, file: { name: string; text: string }): Promise<void>;
  /** Press a mouse button down, without the click that usually follows it. */
  mousedown(element: Element): Promise<void>;
  /**
   * Let an answer the test is holding land, inside `act`.
   *
   * `act` already drains microtasks and 0-delay timers, so a component that
   * finishes a tick after it mounts needs nothing here. What it cannot drain is
   * a promise that has not resolved yet — an answer the test controls, which is
   * what a real fetch is. Without this, that update lands between two `act`s
   * and React prints a warning that looks like a failure and is not one.
   */
  settle(): Promise<void>;
  /** Dispatch a keydown/keyup pair. */
  press(element: Element, key: string, init?: Record<string, unknown>): Promise<void>;
  /** The first element with this accessible name. Throws, naming what was there. */
  byLabel(name: string): HTMLElement;
  /** The first element whose text content contains this. Throws if absent. */
  byText(text: string): HTMLElement;
  /** Every element with this accessible name. */
  allByLabel(name: string): HTMLElement[];
  /** The button whose visible text is exactly this. Throws, naming what was there. */
  byButton(text: string): HTMLElement;
  /**
   * The form field this label names — the wrapping `<label>`, `aria-label` or
   * placeholder, whichever the component used.
   *
   * Separate from `byLabel` because the two mean different things: an
   * `aria-label` is a name on the control, while `<label>Title</label>` is
   * text *beside* the control, and a component that only has the second is not
   * missing an accessible name.
   */
  byField(name: string): HTMLElement;
  /** The text of the whole tree, for asserting on a sentence. */
  text(): string;
  /**
   * Every network call the component made, in order, whether it was answered
   * or not. The installed `fetch` records here and *rejects*; a test that wants
   * an answer assigns `globalThis.fetch` itself, and this list is then the
   * record of what it was asked for.
   */
  fetches: FetchAttempt[];
  /**
   * Every element something asked the browser to scroll to, in order.
   *
   * Recorded rather than dropped, because "scroll the second edit into view" is
   * a claim about *which* element moved, and a no-op stub cannot answer it. A
   * document with no layout has no scroll position at all, so this is the only
   * honest way to say what a jump did.
   */
  scrolls: Element[];
}

/** One `fetch` the component attempted, answered or not. */
export interface FetchAttempt {
  url: string;
  method: string;
  body: string | null;
}

/** jsdom has no layout, so the things `src/` asks the browser for must be answered. */
function installEnvironment(
  win: Window & typeof globalThis,
  fetches: FetchAttempt[],
  scrolls: Element[],
): void {
  // On **both** the node globals and the jsdom window, because "the browser" is
  // two objects here. A component that reads a bare `matchMedia` finds
  // `globalThis`; a library that reaches for
  // `element.ownerDocument.defaultView.matchMedia` finds the window — and xterm
  // does exactly that, so with the stub on `globalThis` alone the terminal pane
  // could not open a shell and the failure read as a broken pane rather than a
  // missing global. One answer, installed on both, or the same component is
  // unmountable depending only on how it asks for the browser.
  const sense = (name: string, value: unknown): void => {
    for (const target of [globalThis, win] as unknown as Record<string, unknown>[]) {
      Object.defineProperty(target, name, {
        value,
        configurable: true,
        writable: true,
      });
    }
  };
  sense("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
  // The canvases size themselves from a `ResizeObserver`; without one they draw
  // at zero and the `painter`-style assertions cannot see them. A no-op is the
  // right answer for "how big is it" in a document with no layout.
  for (const name of ["ResizeObserver", "IntersectionObserver"]) {
    sense(name, class {
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() {
        return [];
      }
    });
  }
  installRangeGeometry(win);
  installCanvas(win);
  installNetwork(fetches);
  installScrolling(win, scrolls);
  void win;
}

/**
 * The geometry of a text range, answered as "nowhere".
 *
 * jsdom has no layout, so `Range.getClientRects` and `getBoundingClientRect` do not exist at all, and an editor that
 * asks where a character is — to scroll it into view, or to place the cursor — dies with a `TypeError` that reads as a
 * broken editor. An empty answer is the honest one for "where is this" in a document with no layout, and it is the
 * same kind of constant `ResizeObserver` is given above. **Nothing here can be used to assert a position.**
 */
function installRangeGeometry(win: Window & typeof globalThis): void {
  const zero = { x: 0, y: 0, width: 0, height: 0, top: 0, right: 0, bottom: 0, left: 0, toJSON: () => ({}) };
  Object.defineProperty(win.Range.prototype, "getClientRects", {
    value: () => [],
    configurable: true,
    writable: true,
  });
  Object.defineProperty(win.Range.prototype, "getBoundingClientRect", {
    value: () => zero,
    configurable: true,
    writable: true,
  });
}

/**
 * A `fetch` that records the call and refuses it.
 *
 * Refusing is the point. `api.ts` builds its URL from the engine's port and
 * boot token and calls the bare global `fetch`, so a component that fetches on
 * mount would otherwise open a real socket to `127.0.0.1` — the developer's
 * engine, or a stranger's. A test that is allowed to reach the network is a
 * test whose result depends on what happens to be listening on a port, and
 * `tests/hermetic.py`'s rule for the Python suite says the same thing in one
 * line: state goes under `CODIFY_HOME`, never a real `~/.codify`.
 *
 * It rejects rather than hangs, and the message names the URL, so the failure
 * says which call went unstubbed instead of surfacing as an empty card. The
 * attempt is still recorded, so a test can assert on what a component *asked*
 * for without answering it.
 */
function installNetwork(fetches: FetchAttempt[]): void {
  Object.defineProperty(globalThis, "fetch", {
    value: (input: unknown, init?: { method?: string; body?: unknown }) => {
      const url = typeof input === "string" ? input : String((input as { url?: string })?.url ?? input);
      fetches.push({
        url,
        method: init?.method ?? "GET",
        body: init?.body === undefined ? null : String(init.body),
      });
      return Promise.reject(
        new Error(
          `withDom: unstubbed fetch to ${url}. A test that expects it must assign globalThis.fetch itself.`,
        ),
      );
    },
    configurable: true,
    writable: true,
  });
}

/**
 * `scrollIntoView`, recorded.
 *
 * jsdom has no scroll position, no layout and — before this — no
 * `Element.prototype.scrollIntoView` at all, so the call is a `TypeError` and
 * every component that reaches for it cannot mount. What a component wants
 * from that call is "the browser brought this element into view", which in a
 * document with no layout is a statement about *which element was named* and
 * nothing else. So the element is recorded and the call is a no-op; a test that
 * wants to know what was painted belongs in `painter.test.ts`, and a test that
 * wants to know where something scrolled reads `dom.scrolls`.
 */
function installScrolling(win: Window & typeof globalThis, scrolls: Element[]): void {
  Object.defineProperty(win.Element.prototype, "scrollIntoView", {
    value: function scrollIntoView(this: Element) {
      scrolls.push(this);
    },
    configurable: true,
    writable: true,
  });
}

/**
 * A canvas that accepts drawing and shows nothing.
 *
 * jsdom has no 2D context: `getContext("2d")` returns `null` and logs a
 * "Not implemented" error for every canvas, so a component that draws anything
 * produces a line of noise per test that looks like a failure and is not one.
 * Returning `null` instead silences it — and every effect in this app guards
 * with `if (!ctx) return`, so a null context is a *supported* state here rather
 * than a broken one.
 *
 * Why a stub rather than `null`, then: a null context is only survivable for
 * code that happens to check, and a harness whose components must all be
 * defensive before they will mount is a harness that will hide a real crash.
 *
 * **This stub draws nothing and asserts nothing about pixels.** It exists so a
 * component can mount. A test that wants to know what was painted belongs in
 * `painter.test.ts`, which drives each painter against a *recording* context and
 * makes claims about the output — and a claim made against this stub would be a
 * claim about nothing.
 */
function installCanvas(win: Window & typeof globalThis): void {
  const noop = (): void => {};
  const context = {
    canvas: null as unknown,
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    lineCap: "butt",
    lineJoin: "miter",
    globalAlpha: 1,
    font: "",
    textBaseline: "alphabetic",
    globalCompositeOperation: "source-over",
    shadowBlur: 0,
    shadowColor: "",
    save: noop,
    restore: noop,
    scale: noop,
    rotate: noop,
    translate: noop,
    transform: noop,
    setTransform: noop,
    resetTransform: noop,
    clearRect: noop,
    fillRect: noop,
    strokeRect: noop,
    beginPath: noop,
    closePath: noop,
    moveTo: noop,
    lineTo: noop,
    bezierCurveTo: noop,
    quadraticCurveTo: noop,
    arc: noop,
    arcTo: noop,
    rect: noop,
    ellipse: noop,
    fill: noop,
    stroke: noop,
    clip: noop,
    setLineDash: noop,
    getLineDash: () => [] as number[],
    fillText: noop,
    strokeText: noop,
    measureText: () => ({ width: 0 } as TextMetrics),
    createLinearGradient: () => ({ addColorStop: noop }),
    createRadialGradient: () => ({ addColorStop: noop }),
    createPattern: () => null,
    getImageData: () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 }),
    putImageData: noop,
    drawImage: noop,
  };
  Object.defineProperty(win.HTMLCanvasElement.prototype, "getContext", {
    value: () => context,
    configurable: true,
    writable: true,
  });
  // A canvas with no layout reports zero for every dimension, and an effect
  // that divides by width draws nothing. Not fatal, but it means every canvas
  // in a test is a 0x0 one, which is not what the component will meet in a real
  // window. `clientWidth` is a getter on the prototype; this is the smallest
  // override that makes a mounted canvas believe it has a size.
  for (const prop of ["clientWidth", "clientHeight"] as const) {
    Object.defineProperty(win.HTMLElement.prototype, prop, {
      value: 1440,
      configurable: true,
    });
  }
  Object.defineProperty(win.HTMLElement.prototype, "getBoundingClientRect", {
    value: () => ({ x: 0, y: 0, top: 0, left: 0, right: 1440, bottom: 900, width: 1440, height: 900 }),
    configurable: true,
  });
}

/**
 * Install a DOM, call `body`, and tear it down again.
 *
 * A callback rather than a setup/teardown pair, because the teardown is the part
 * that gets forgotten. A suite that leaks a `document` between files has tests
 * that pass alone and fail together, which is the worst order-dependent bug to
 * chase; `finally` here is the guarantee, and `render` unmounts on the way out
 * so React's effects do not outlive the document they were attached to.
 */
export async function withDom<T>(body: (dom: Dom) => Promise<T> | T): Promise<T> {
  // With the loader's hooks off: on Node 22 a `load` hook breaks jsdom's require of
  // an ES module. See `withoutHooks` for the measurement.
  const { JSDOM: Ctor } = await withoutHooks(() => import("jsdom"));
  const instance = new Ctor("<!doctype html><html><body></body></html>", {
    url: "http://localhost/",
    pretendToBeVisual: true,
  });
  const win = instance.window as unknown as Window & typeof globalThis;

  const fetches: FetchAttempt[] = [];
  const scrolls: Element[] = [];
  const saved = new Map<string, unknown>();
  for (const name of GLOBALS) {
    saved.set(name, (globalThis as Record<string, unknown>)[name]);
    const value = (win as unknown as Record<string, unknown>)[name];
    if (value !== undefined) {
      Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
    }
  }
  installEnvironment(win, fetches, scrolls);

  const React = await import("react");
  const { createRoot } = await import("react-dom/client");
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

  const container = win.document.createElement("div");
  win.document.body.appendChild(container);
  const root = createRoot(container);
  const act = React.act as (body: () => Promise<void> | void) => Promise<void>;

  const dom: Dom = {
    container,
    window: win,
    fetches,
    scrolls,
    async render(element) {
      await act(async () => {
        root.render(element);
      });
    },
    async click(element) {
      await act(async () => {
        element.dispatchEvent(new win.MouseEvent("click", { bubbles: true, cancelable: true }));
      });
    },
    async fill(element, value) {
      // React tracks the last value it wrote on a controlled input and ignores
      // a `value` it recognises as its own. Going through the *prototype's*
      // setter is what makes this a change from React's point of view; setting
      // `element.value` directly is the version of this that silently does
      // nothing, and it is the reason most hand-rolled React tests are a lie.
      const proto =
        element.tagName === "TEXTAREA"
          ? win.HTMLTextAreaElement.prototype
          : win.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      await act(async () => {
        setter?.call(element, value);
        // Both, because React listens for `input` on text-like elements and
        // `change` on everything else — and `<input type="color">` is the
        // second kind while `<input type="text">` is the first.
        element.dispatchEvent(new win.Event("input", { bubbles: true }));
        element.dispatchEvent(new win.Event("change", { bubbles: true }));
      });
    },
    async selectFile(element, file) {
      // `input.files` is read-only in the DOM, and so it should be: a component
      // cannot fabricate a user's file. A *test* can, and the standard way is a
      // one-property redefine — which is honest about the asymmetry rather than
      // pretending the browser exposes a setter it does not.
      //
      // A `FileList`-shaped object rather than a real one: jsdom 30 does not
      // implement `DataTransfer`, which is how you would otherwise populate it,
      // and nothing here needs it. What a component does with a chosen file is
      // read `files[0]` and call `text()` on it, so that is exactly what this
      // provides — no more, and no less than the surface being claimed.
      const handle = new win.File([file.text], file.name, { type: "application/json" });
      const list = { 0: handle, length: 1, item: (i: number) => (i === 0 ? handle : null) };
      Object.defineProperty(element, "files", { value: list, configurable: true });
      await act(async () => {
        element.dispatchEvent(new win.Event("change", { bubbles: true }));
      });
    },
    async press(element, key, init = {}) {
      await act(async () => {
        const options = { bubbles: true, cancelable: true, key, ...init };
        element.dispatchEvent(new win.KeyboardEvent("keydown", options));
        element.dispatchEvent(new win.KeyboardEvent("keyup", options));
      });
    },
    async mousedown(element) {
      // Its own method because `click` cannot stand in for it: an overlay that
      // closes on `onMouseDown` (so a drag out of it cancels) never sees a
      // synthetic click's mousedown phase, and a panel that *cancels* the
      // mousedown to keep focus is only testable through the event it cancels.
      await act(async () => {
        element.dispatchEvent(
          new win.MouseEvent("mousedown", { bubbles: true, cancelable: true }),
        );
      });
    },
    async settle() {
      // Two turns of the event loop: one for whatever the component awaited,
      // one for the `setState` that follows it in its own `.then`.
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    },
    byLabel(name) {
      const found = all(win.document, `[aria-label="${cssEscape(name)}"]`);
      if (found.length === 0) {
        const available = [...win.document.querySelectorAll("[aria-label]")]
          .map((el) => el.getAttribute("aria-label"))
          .filter(Boolean);
        throw new Error(
          `nothing here is labelled "${name}". The labels on the page are: ` +
            (available.length ? available.join(", ") : "(none)"),
        );
      }
      return found[0] as HTMLElement;
    },
    byText(text) {
      const found = [...win.document.querySelectorAll("button, h3, h4, p, span, label")].filter(
        (el) => (el.textContent ?? "").includes(text),
      );
      if (found.length === 0) throw new Error(`no element's text contains "${text}"`);
      return found[0] as HTMLElement;
    },
    allByLabel(name) {
      return all(win.document, `[aria-label="${cssEscape(name)}"]`);
    },
    byButton(text) {
      const buttons = all(win.document, "button");
      const found = buttons.filter((b) => (b.textContent ?? "").trim() === text);
      if (found.length === 0) {
        const available = buttons
          .map((b) => (b.textContent ?? "").trim())
          .filter((t) => t.length > 0);
        throw new Error(
          `no button reads exactly "${text}". The buttons here are: ` +
            (available.length ? available.map((t) => `"${t}"`).join(", ") : "(none)"),
        );
      }
      return found[0];
    },
    byField(name) {
      const fields = all(win.document, "input, textarea, select");
      const matches = (field: HTMLElement): boolean => {
        // `labels` is the DOM's own answer to "what is this field called", and
        // it covers a wrapping `<label>` exactly as a screen reader reads it.
        const labels = (field as HTMLInputElement).labels;
        if (labels && labels.length > 0) {
          return [...labels].some((l) => (l.textContent ?? "").trim() === name);
        }
        return (
          field.getAttribute("aria-label") === name ||
          field.getAttribute("placeholder") === name
        );
      };
      const found = fields.filter(matches);
      if (found.length === 0) {
        const available = fields
          .map((f) => {
            const labels = (f as HTMLInputElement).labels;
            const named = labels && labels.length > 0 ? labels[0].textContent : null;
            return (named ?? f.getAttribute("aria-label") ?? f.getAttribute("placeholder") ?? "")
              .trim();
          })
          .filter((t) => t.length > 0);
        throw new Error(
          `no field is labelled "${name}". The named fields here are: ` +
            (available.length ? available.map((t) => `"${t}"`).join(", ") : "(none)"),
        );
      }
      return found[0];
    },
    text() {
      return container.textContent ?? "";
    },
  };

  try {
    return await body(dom);
  } finally {
    try {
      await act(async () => {
        root.unmount();
      });
    } catch {
      // Already unmounted, which is the normal case for a test that called it.
    }
    container.remove();
    for (const [name, value] of saved) {
      if (value === undefined) delete (globalThis as Record<string, unknown>)[name];
      else
        Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
    }
    delete (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT;
    instance.window.close();
  }
}

function all(root: ParentNode, selector: string): HTMLElement[] {
  return [...root.querySelectorAll(selector)] as HTMLElement[];
}

/** Enough CSS.escape to make a label usable in a selector. */
function cssEscape(value: string): string {
  return value.replace(/["\\]/g, "\\$&");
}
