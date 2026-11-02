/**
 * Settings → Engine deep-review switch, mounted against the same settings route the card uses.
 *
 * Deep review spends extra model calls on every reviewed step, so the switch is a person's and must save only what
 * the person chose. An engine without the setting shows no card at all.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Route = (body: unknown) => Response;

async function withCard(
  routes: Record<string, Route>,
  body: (dom: Dom, calls: Call[]) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const calls: Call[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const method = (init?.method ?? "GET").toUpperCase();
      const call: Call = {
        method,
        path: url.pathname,
        body: typeof init?.body === "string" ? JSON.parse(init.body) : null,
      };
      calls.push(call);
      const route = routes[`${method} ${url.pathname}`];
      if (!route) throw new Error(`no answer for ${method} ${url.pathname}`);
      return route(call.body);
    }) as typeof fetch;
    const { DeepReviewCard } = await import("../src/components/DeepReviewCard.tsx");
    await dom.render(h(DeepReviewCard));
    await dom.settle();
    await body(dom, calls);
  });
}

const switchControl = (dom: Dom): HTMLElement =>
  dom.byButton("Have two fresh readers review each step before the critic decides");

/** The control's own answer to "am I on", which is what a screen reader reads. */
const isOn = (dom: Dom): boolean => switchControl(dom).getAttribute("aria-checked") === "true";

const toggle = async (dom: Dom, enabled: boolean): Promise<void> => {
  await dom.click(switchControl(dom));
  assert.equal(isOn(dom), enabled, "the click did not toggle the checkbox");
};

test("an engine without the deep-review setting has no toggle to save", async () => {
  await withCard({
    "GET /settings/engine": () => json({ parallel_width: { value: 4, min: 1, max: 16 } }),
  }, async (dom) => {
    assert.doesNotMatch(dom.text(), /Deep review/);
  });
});

test("a fresh install has deep review off and the person can switch it on", async () => {
  const settings = {
    review_deep: { value: 0, min: 0, max: 1 },
  };
  await withCard({
    "GET /settings/engine": () => json(settings),
    "PUT /settings/engine": (body) => json({ saved: body }),
  }, async (dom, calls) => {
    assert.equal(isOn(dom), false);
    assert.match(dom.text(), /Off by default/);
    assert.match(dom.text(), /never the research the author/);

    await toggle(dom, true);
    await dom.settle();

    assert.deepEqual(
      calls.filter((call) => call.method === "PUT").map((call) => call.body),
      [{ review_deep: true }],
    );
    assert.equal(isOn(dom), true);
    assert.match(dom.text(), /Saved — this applies to the next reviewed step/);
  });
});

test("a failed save does not show a switch the engine did not store", async () => {
  await withCard({
    "GET /settings/engine": () => json({ review_deep: { value: 0, min: 0, max: 1 } }),
    "PUT /settings/engine": () => json({ message: "settings unavailable" }, 503),
  }, async (dom, calls) => {
    await dom.click(switchControl(dom));
    await dom.settle();

    assert.equal(calls.filter((call) => call.method === "PUT").length, 1);
    assert.equal(isOn(dom), false, "a refused save left deep review shown as on");
    assert.match(dom.text(), /settings unavailable/);
  });
});
