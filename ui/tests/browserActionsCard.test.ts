/**
 * Settings → Engine browser-action consent, mounted against the same settings route the card uses.
 *
 * This is intentionally separate from the Web pages card: reading a page and acting on one have different
 * consequences, so the card must save its own switch and the UI test must exercise that control.
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
    const { BrowserActionsCard } = await import("../src/components/BrowserActionsCard.tsx");
    await dom.render(h(BrowserActionsCard));
    await dom.settle();
    await body(dom, calls);
  });
}

const switchControl = (dom: Dom): HTMLInputElement =>
  dom.byField("Allow the assistant to navigate, click and type in browser pages") as HTMLInputElement;

const toggle = async (dom: Dom, enabled: boolean): Promise<void> => {
  const input = switchControl(dom);
  await dom.click(input);
  assert.equal(input.checked, enabled, "the click did not toggle the checkbox");
};

test("an engine without browser-action consent has no toggle to save", async () => {
  await withCard({
    "GET /settings/engine": () => json({ parallel_width: { value: 4, min: 1, max: 16 } }),
  }, async (dom) => {
    assert.doesNotMatch(dom.text(), /Browser actions/);
  });
});

test("the fresh-install permission is off and the person can explicitly enable it", async () => {
  const settings = {
    browser_actions: { value: 0, min: 0, max: 1 },
  };
  await withCard({
    "GET /settings/engine": () => json(settings),
    "PUT /settings/engine": (body) => json({ saved: body }),
  }, async (dom, calls) => {
    assert.equal(switchControl(dom).checked, false);
    assert.match(dom.text(), /Off by default/);
    assert.match(dom.text(), /logged-in account/);

    await toggle(dom, true);
    await dom.settle();

    assert.deepEqual(
      calls.filter((call) => call.method === "PUT").map((call) => call.body),
      [{ browser_actions: true }],
    );
    assert.equal(switchControl(dom).checked, true);
    assert.match(dom.text(), /Saved — this applies to the next turn/);
  });
});

test("a failed save does not display consent that the engine did not store", async () => {
  await withCard({
    "GET /settings/engine": () => json({ browser_actions: { value: 0, min: 0, max: 1 } }),
    "PUT /settings/engine": () => json({ message: "settings unavailable" }, 503),
  }, async (dom, calls) => {
    await dom.click(switchControl(dom));
    await dom.settle();

    assert.equal(calls.filter((call) => call.method === "PUT").length, 1);
    assert.equal(switchControl(dom).checked, false, "a refused save left consent enabled");
    assert.match(dom.text(), /settings unavailable/);
  });
});
