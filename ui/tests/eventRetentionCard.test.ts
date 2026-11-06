/**
 * Settings → Engine timeline retention, mounted against the route the card uses.
 *
 * `event_retention_days` deletes the timelines of ended goals when Codify starts, and until this card it had no
 * control: a doc said it lived in Settings → Engine and nothing there could read or change it. The card must show the
 * engine's own number, save exactly what was typed, show what the engine stored when it clamps, and say plainly what
 * is deleted and what never is.
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

async function withCard(
  settings: unknown,
  put: (body: unknown) => Response,
  body: (dom: Dom, calls: Call[]) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const calls: Call[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const method = (init?.method ?? "GET").toUpperCase();
      const parsed = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      calls.push({ method, path: url.pathname, body: parsed });
      if (method === "GET" && url.pathname === "/settings/engine") return json(settings);
      if (method === "PUT" && url.pathname === "/settings/engine") return put(parsed);
      throw new Error(`no answer for ${method} ${url.pathname}`);
    }) as typeof fetch;
    const { EventRetentionCard } = await import("../src/components/EventRetentionCard.tsx");
    await dom.render(h(EventRetentionCard));
    await dom.settle();
    await body(dom, calls);
  });
}

const LABEL = "Keep ended goals' timelines for (days)";
const field = (dom: Dom): HTMLInputElement => dom.byField(LABEL) as HTMLInputElement;
const save = (dom: Dom): HTMLElement => dom.byLabel(`Save ${LABEL}`);
const SETTINGS = { event_retention_days: { value: 365, min: 0, max: 3650 } };

test("an engine without the setting shows no card", async () => {
  await withCard({ parallel_width: { value: 4, min: 1, max: 16 } }, () => json({}), async (dom) => {
    assert.doesNotMatch(dom.text(), /Goal timelines/);
  });
});

test("the card shows the engine's number and says what is deleted and what never is", async () => {
  await withCard(SETTINGS, (body) => json({ saved: body }), async (dom) => {
    assert.equal(field(dom).value, "365");
    assert.match(dom.text(), /prompts, diffs and command output/);
    assert.match(dom.text(), /ended.*\(completed, failed or cancelled\)/s);
    assert.match(dom.text(), /0 keeps everything/);
    assert.match(dom.text(), /running, paused\s+or waiting for your approval is never touched/);
  });
});

test("saving sends only the number typed, and says when the deletion happens", async () => {
  await withCard(SETTINGS, (body) => json({ saved: body }), async (dom, calls) => {
    await dom.fill(field(dom), "90");
    await dom.click(save(dom));
    await dom.settle();

    assert.deepEqual(
      calls.filter((call) => call.method === "PUT").map((call) => call.body),
      [{ event_retention_days: 90 }],
    );
    assert.match(dom.text(), /more than 90 days ago are deleted the next time Codify starts/);
  });
});

test("zero is saved as 'keep everything', in those words", async () => {
  await withCard(SETTINGS, (body) => json({ saved: body }), async (dom) => {
    await dom.fill(field(dom), "0");
    await dom.click(save(dom));
    await dom.settle();

    assert.match(dom.text(), /timelines are kept until you delete the goal/);
  });
});

test("a number the engine clamps is shown as what was stored", async () => {
  await withCard(SETTINGS, () => json({ saved: { event_retention_days: 3650 } }), async (dom) => {
    await dom.fill(field(dom), "99999");
    await dom.click(save(dom));
    await dom.settle();

    assert.equal(field(dom).value, "3650");
    assert.match(dom.text(), /Clamped to 3650 \(allowed 0–3650\)/);
  });
});

test("a refused save says why", async () => {
  await withCard(SETTINGS, () => json({ message: "settings unavailable" }, 503), async (dom) => {
    await dom.fill(field(dom), "30");
    await dom.click(save(dom));
    await dom.settle();

    assert.match(dom.text(), /settings unavailable/);
  });
});
