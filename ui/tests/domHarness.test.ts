/**
 * The harness, tested before anything relies on it.
 *
 * A test harness that mounts nothing passes every assertion written against it
 * if those assertions are about absence — "no crash", "the panel is not open" —
 * and that is a suite which reports success while testing nothing. So the first
 * file to use this one has to prove the four things every other file will lean
 * on: it mounts a real tree, a click reaches a real handler, a controlled input
 * reports the value that was typed, and the DOM is gone afterwards.
 *
 * These use a component defined *here* rather than one from `src/`, deliberately.
 * The claim under test is about the harness, and a failure should point at the
 * harness rather than at whichever app component happened to be involved.
 *
 * The last third of the file covers the *refusals* — the network and scrolling —
 * because a stub nobody tested is a stub nobody can trust: an installed `fetch`
 * that quietly returned `undefined` would leave every component that calls it
 * reporting "no data", and the tests written on top of it would still be green.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const React = (await import("react")).default;
const { withDom } = await import("./dom.ts");
const h = React.createElement;

/**
 * A component with the three things every test in the suite will reach for.
 *
 * `React.createElement` rather than JSX, because this is a `.ts` file: node's
 * type stripping refuses JSX outside `.tsx`, and the suite's glob is
 * `tests/*.test.ts`. Every other test here is written the same way, and a
 * harness that needed a different file extension would be a harness nobody used.
 */
function Probe({ onPick, onKey }: { onPick?: (v: string) => void; onKey?: (k: string) => void }) {
  const [clicks, setClicks] = React.useState(0);
  const [text, setText] = React.useState("");
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);
  return h(
    "div",
    null,
    h("p", { "data-testid": "effects" }, `effects: ${mounted ? "ran" : "pending"}`),
    h("p", { "data-testid": "clicks" }, `clicks: ${clicks}`),
    h("p", { "data-testid": "text" }, `text: ${text}`),
    h(
      "button",
      { type: "button", "aria-label": "Bump", onClick: () => setClicks((n) => n + 1) },
      "Bump",
    ),
    h("input", {
      "aria-label": "Field",
      value: text,
      onChange: (e: React.ChangeEvent<HTMLInputElement>) => {
        setText(e.target.value);
        onPick?.(e.target.value);
      },
    }),
    h(
      "div",
      {
        role: "group",
        tabIndex: 0,
        "aria-label": "Keys",
        onKeyDown: (e: React.KeyboardEvent<HTMLDivElement>) => onKey?.(e.key),
        onClick: () => setClicks((n) => n + 100),
        onMouseDown: () => onKey?.("mousedown"),
      },
      "keys",
    ),
    // A field named by a *wrapping* label rather than an `aria-label`, because
    // half the components in this app are written that way and `byField` has to
    // reach them.
    h(
      "label",
      null,
      h("span", null, "Nickname"),
      h("input", { defaultValue: "codify" }),
    ),
  );
}

/** Fetches the way `api.ts` does — the bare global — and reports what came back. */
function NetProbe() {
  const [outcome, setOutcome] = React.useState("pending");
  React.useEffect(() => {
    let live = true;
    fetch("http://127.0.0.1:7430/goals/g1/usage")
      .then(() => live && setOutcome("answered"))
      .catch((err: Error) => live && setOutcome(`refused: ${err.message}`));
    return () => {
      live = false;
    };
  }, []);
  return h("p", null, outcome);
}

/** Asks the browser to scroll, which jsdom cannot do. */
function ScrollProbe() {
  const target = React.useRef<HTMLParagraphElement>(null);
  return h(
    "div",
    null,
    h("button", { type: "button", onClick: () => target.current?.scrollIntoView() }, "Jump"),
    h("p", { ref: target }, "the entry that was scrolled to"),
  );
}

/**
 * Finishes only when the test says so — the shape `settle()` exists for.
 *
 * Not a timer: `act` already drains microtasks and 0-delay timers, so a
 * component that finishes a tick after it mounts needs nothing. What it cannot
 * drain is a promise that has *not resolved yet* — an answer the test controls,
 * which is what an async fetch really is.
 */
let releaseGate: (() => void) | null = null;
const gate = new Promise<void>((resolve) => {
  releaseGate = resolve;
});

function GatedProbe() {
  const [value, setValue] = React.useState("pending");
  React.useEffect(() => {
    let live = true;
    gate.then(() => {
      if (live) setValue("landed");
    });
    return () => {
      live = false;
    };
  }, []);
  return h("p", null, value);
}

test("it mounts a real tree, and effects run", async () => {
  await withDom(async (dom) => {
    await dom.render(React.createElement(Probe));
    // `renderToStaticMarkup` cannot produce this line at all, which is the
    // whole reason the harness exists.
    assert.match(dom.text(), /effects: ran/, "the effect did not run, so this is not a real mount");
  });
});

test("a click reaches a real handler and the result is visible when click() returns", async () => {
  await withDom(async (dom) => {
    await dom.render(React.createElement(Probe));
    assert.match(dom.text(), /clicks: 0/);
    await dom.click(dom.byLabel("Bump"));
    // No `settle()` here on purpose. If this ever needs one, `act` has stopped
    // flushing and every interaction test in the suite is quietly racy.
    assert.match(dom.text(), /clicks: 1/, "the click did not land, or the update was not flushed");
  });
});

test("a controlled input reports what was typed, not what React last wrote", async () => {
  // The failure this guards is silent and total: setting `element.value`
  // directly and dispatching `change` leaves React believing the value never
  // moved, so the input snaps back and the test asserts on a stale tree. Every
  // "the picker records the colour I picked" test would pass against a picker
  // that records nothing.
  const seen: string[] = [];
  await withDom(async (dom) => {
    await dom.render(React.createElement(Probe, { onPick: (v: string) => seen.push(v) }));
    await dom.fill(dom.byLabel("Field") as HTMLInputElement, "crimson");
    assert.deepEqual(seen, ["crimson"], "the change handler never saw the value");
    assert.match(dom.text(), /text: crimson/, "and the controlled value did not follow it");
  });
});

test("a keydown reaches the element, and does not also fire click", async () => {
  const keys: string[] = [];
  await withDom(async (dom) => {
    await dom.render(React.createElement(Probe, { onKey: (k: string) => keys.push(k) }));
    await dom.press(dom.byLabel("Keys"), "ArrowDown");
    assert.deepEqual(keys, ["ArrowDown"]);
    // The two paths are separate, and a `press` that also clicked would make
    // every keyboard assertion in the suite a lie about what it exercised.
    assert.match(dom.text(), /clicks: 0/, "a keydown also produced a click");
  });
});

test("the DOM is torn down, and the globals go back to what they were", async () => {
  // Without this, a suite leaks a document between files and gets tests that
  // pass alone and fail together — the hardest kind of order dependence to
  // chase, and one this file is the only thing that can prevent.
  const before = Object.getOwnPropertyDescriptor(globalThis, "window");
  assert.equal(before, undefined, "sanity: the harness is what puts a window on globalThis");

  await withDom(async (dom) => {
    await dom.render(React.createElement(Probe));
    assert.ok(dom.container.isConnected);
  });

  const after = Object.getOwnPropertyDescriptor(globalThis, "window");
  assert.equal(after, undefined, "the window outlived withDom");
  assert.equal(
    Object.getOwnPropertyDescriptor(globalThis, "document"),
    undefined,
    "and so did the document",
  );
});

test("a byLabel miss names what *is* on the page", async () => {
  // A missing element in a test is almost always a wrong selector rather than a
  // missing feature, and the useful information is the list of labels that
  // exist. "nothing matched" sends you to the source; this sends you to the
  // markup.
  await withDom(async (dom) => {
    await dom.render(React.createElement(Probe));
    assert.throws(
      () => dom.byLabel("Nonesuch"),
      /The labels on the page are: Bump, Field, Keys/,
      "the failure did not list the labels that do exist",
    );
  });
});

test("a button is found by its visible text, and a miss lists the buttons", async () => {
  // The controls in this app are named by their words far more often than by an
  // `aria-label`, so "press the button that says Start" has to be expressible.
  // Exact rather than partial: "Start" must not find "Start recording", which is
  // the bug a substring match walks straight into.
  await withDom(async (dom) => {
    await dom.render(React.createElement(Probe));
    assert.equal(dom.byButton("Bump").getAttribute("aria-label"), "Bump");
    assert.throws(() => dom.byButton("Bump "), /The buttons here are: "Bump", "Jump"|"Bump"/);
  });
});

test("a field is found by the label beside it, or by the name on it", async () => {
  await withDom(async (dom) => {
    await dom.render(React.createElement(Probe));
    // A wrapping `<label>`: text next to the control, which is how most of this
    // app's forms are written.
    const wrapped = dom.byField("Nickname") as HTMLInputElement;
    assert.equal(wrapped.value, "codify", "byField found the wrong field");
    // And an `aria-label`, which is the same claim made the other way.
    assert.equal(dom.byField("Field").getAttribute("aria-label"), "Field");
    assert.throws(() => dom.byField("Nonesuch"), /The named fields here are: /);
  });
});

test("mousedown is its own event, so a backdrop and a click can be told apart", async () => {
  const events: string[] = [];
  await withDom(async (dom) => {
    await dom.render(React.createElement(Probe, { onKey: (k) => events.push(k) }));
    await dom.mousedown(dom.byLabel("Keys"));
    assert.deepEqual(events, ["mousedown"]);
    // No click, because no click was dispatched: an overlay that closes on
    // `onMouseDown` — so a drag out of it cancels — is only reachable this way.
    assert.match(dom.text(), /clicks: 0/, "a mousedown also produced a click");
  });
});

test("settle waits for an answer the test itself is holding", async () => {
  await withDom(async (dom) => {
    await dom.render(React.createElement(GatedProbe));
    assert.match(dom.text(), /pending/, "sanity: nothing has answered yet");
    releaseGate!();
    // Without this, the answer lands between two `act`s — a state update React
    // is told to expect inside one — and the suite prints a warning per test
    // rather than a failure.
    await dom.settle();
    assert.match(dom.text(), /landed/, "settle did not let the queued update through");
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// The refusals
// ─────────────────────────────────────────────────────────────────────────────

test("an unstubbed fetch is refused by name, and recorded", async () => {
  // Two halves, and the second is the one that would otherwise be lost. A
  // component that fetches on mount must not open a real socket to
  // `127.0.0.1` from a test; and the attempt still has to be visible, so a test
  // can say what a component *asked* for without answering it.
  await withDom(async (dom) => {
    await dom.render(React.createElement(NetProbe));
    await dom.settle();
    assert.match(dom.text(), /refused: /, "the call was answered by something");
    assert.match(dom.text(), /127\.0\.0\.1:7430\/goals\/g1\/usage/, "the refusal did not name the URL");
    assert.match(dom.text(), /assign globalThis\.fetch/, "the refusal did not say what to do about it");
    assert.deepEqual(
      dom.fetches.map((f) => [f.method, f.url]),
      [["GET", "http://127.0.0.1:7430/goals/g1/usage"]],
      "the attempt was not recorded",
    );
  });
});

test("a test can answer the fetch itself, and then the attempt is only in its own hands", async () => {
  const asked: string[] = [];
  await withDom(async (dom) => {
    // Installed from inside, because that is when the harness's own `fetch` is
    // in place — the mistake this pins is a stub assigned from outside, which
    // the harness then overwrites.
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      asked.push(String(input));
      return { ok: true, json: async () => ({ tokens: 1 }) } as unknown as Response;
    }) as unknown as typeof fetch;
    await dom.render(React.createElement(NetProbe));
    await dom.settle();
    assert.match(dom.text(), /answered/);
    assert.deepEqual(dom.fetches, [], "the harness saw a call it did not make");
  });
  assert.equal(asked.length, 1, "the stub was overwritten, or called twice");
});

test("the real fetch is put back when the DOM goes", async () => {
  // Without `fetch` in the saved set, every test that installed a stub would
  // hand the next one a `127.0.0.1` caller it did not choose.
  const before = globalThis.fetch;
  await withDom(async (dom) => {
    globalThis.fetch = (async () => ({ ok: true, json: async () => ({}) })) as unknown as typeof fetch;
    assert.notEqual(globalThis.fetch, before);
    void dom;
  });
  assert.equal(globalThis.fetch, before, "a test's fetch outlived its DOM");
});

test("scrolling is answered, and what it was asked to bring into view is recorded", async () => {
  // `Element.prototype.scrollIntoView` does not exist in jsdom, so a component
  // that reaches for it cannot even mount without this. A document with no
  // layout has no scroll position, so the element is recorded and nothing moves
  // — which is the most that can honestly be claimed.
  await withDom(async (dom) => {
    await dom.render(React.createElement(ScrollProbe));
    const entry = dom.byText("the entry that was scrolled to");
    await dom.click(dom.byButton("Jump"));
    assert.deepEqual(
      dom.scrolls,
      [entry],
      "the scroll was not recorded against the element it named",
    );
  });
});
