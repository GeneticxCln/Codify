/**
 * Settings → Engine standing notes, mounted against the routes the card uses.
 *
 * Standing notes are the one memory that crosses a workspace (docs/10 §8) and, until this card, nothing in the app
 * could turn them on: the switch had no control, so the feature was reachable only by calling `PUT /settings/engine`
 * by hand. These tests hold the two acts apart (the switch and the notes), and hold the card to saving only what the
 * person chose and to showing the engine's refusal rather than a note the engine did not keep.
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
    const { NotesCard } = await import("../src/components/NotesCard.tsx");
    await dom.render(h(NotesCard));
    await dom.settle();
    await body(dom, calls);
  });
}

const note = (id: string, text: string, source: "person" | "imported" = "person") => ({
  id,
  text,
  source,
  created_at: 1,
});

const SWITCH = "Show my standing notes to the assistant";
const isOn = (dom: Dom): boolean => dom.byButton(SWITCH).getAttribute("aria-checked") === "true";
const writes = (calls: Call[]): Call[] => calls.filter((call) => call.method !== "GET");

const engine = (value: number) => ({ cross_workspace_notes: { value, min: 0, max: 1 } });

test("an engine without the notes setting shows no card", async () => {
  await withCard({
    "GET /settings/engine": () => json({ parallel_width: { value: 4, min: 1, max: 16 } }),
    "GET /settings/notes": () => json({ notes: [], enabled: false, max_notes: 50 }),
  }, async (dom) => {
    assert.doesNotMatch(dom.text(), /Standing notes/);
  });
});

test("a fresh install has notes off, and only the switch is saved when the person turns it on", async () => {
  await withCard({
    "GET /settings/engine": () => json(engine(0)),
    "GET /settings/notes": () => json({ notes: [], enabled: false, max_notes: 50 }),
    "PUT /settings/engine": (body) => json({ saved: body }),
  }, async (dom, calls) => {
    assert.equal(isOn(dom), false);
    assert.match(dom.text(), /Off by default/);
    assert.match(dom.text(), /preferences, not commands/);
    assert.match(dom.text(), /No notes yet/);

    await dom.click(dom.byButton(SWITCH));
    await dom.settle();

    assert.deepEqual(writes(calls).map((call) => [call.method, call.path, call.body]), [
      ["PUT", "/settings/engine", { cross_workspace_notes: true }],
    ]);
    assert.equal(isOn(dom), true);
    assert.match(dom.text(), /Saved — this applies to the next turn/);
  });
});

test("a refused switch does not show notes as on", async () => {
  await withCard({
    "GET /settings/engine": () => json(engine(0)),
    "GET /settings/notes": () => json({ notes: [], enabled: false, max_notes: 50 }),
    "PUT /settings/engine": () => json({ code: "settings_unavailable", message: "settings unavailable" }, 503),
  }, async (dom) => {
    await dom.click(dom.byButton(SWITCH));
    await dom.settle();

    assert.equal(isOn(dom), false, "a refused save left notes shown as on");
    assert.match(dom.text(), /settings unavailable/);
  });
});

test("the notes are listed, and one a profile brought is marked as not the person's own", async () => {
  await withCard({
    "GET /settings/engine": () => json(engine(1)),
    "GET /settings/notes": () =>
      json({ notes: [note("a", "Prefer small commits"), note("b", "Use tabs", "imported")], enabled: true, max_notes: 50 }),
  }, async (dom) => {
    assert.equal(isOn(dom), true);
    assert.match(dom.text(), /Prefer small commits/);
    assert.match(dom.text(), /Use tabs\(imported from a profile\)/);
    assert.doesNotMatch(dom.text(), /Prefer small commits\(imported/, "a person's own note was marked as imported");
    assert.match(dom.text(), /2 of 50 kept/);
  });
});

test("adding a note sends only its text, lists what the engine kept, and clears the box", async () => {
  await withCard({
    "GET /settings/engine": () => json(engine(1)),
    "GET /settings/notes": () => json({ notes: [], enabled: true, max_notes: 50 }),
    "POST /settings/notes": (body) => json(note("n1", String((body as { text: string }).text)), 201),
  }, async (dom, calls) => {
    const box = dom.byField("New note") as HTMLTextAreaElement;
    await dom.fill(box, "  Answer in short sentences  ");
    await dom.click(dom.byButton("Add note"));
    await dom.settle();

    assert.deepEqual(writes(calls).map((call) => [call.method, call.path, call.body]), [
      ["POST", "/settings/notes", { text: "Answer in short sentences" }],
    ]);
    assert.match(dom.text(), /Answer in short sentences/);
    assert.equal((dom.byField("New note") as HTMLTextAreaElement).value, "", "the box kept a note that was stored");
    assert.match(dom.text(), /1 of 50 kept/);
  });
});

test("a refused note shows the engine's sentence and keeps the text to edit", async () => {
  await withCard({
    "GET /settings/engine": () => json(engine(1)),
    "GET /settings/notes": () => json({ notes: [note("a", "Prefer small commits")], enabled: true, max_notes: 50 }),
    "POST /settings/notes": () => json({ code: "note_exists", message: "that note is already kept" }, 409),
  }, async (dom) => {
    await dom.fill(dom.byField("New note") as HTMLTextAreaElement, "Prefer small commits");
    await dom.click(dom.byButton("Add note"));
    await dom.settle();

    assert.match(dom.text(), /that note is already kept/);
    assert.equal((dom.byField("New note") as HTMLTextAreaElement).value, "Prefer small commits");
    assert.match(dom.text(), /1 of 50 kept/, "a refused note was listed as kept");
  });
});

test("deleting a note removes that one and no other", async () => {
  await withCard({
    "GET /settings/engine": () => json(engine(1)),
    "GET /settings/notes": () =>
      json({ notes: [note("a", "Prefer small commits"), note("b", "Use tabs")], enabled: true, max_notes: 50 }),
    "DELETE /settings/notes/a": () => new Response(null, { status: 204 }),
  }, async (dom, calls) => {
    await dom.click(dom.byLabel("Delete note: Prefer small commits"));
    await dom.settle();

    assert.deepEqual(writes(calls).map((call) => [call.method, call.path]), [["DELETE", "/settings/notes/a"]]);
    assert.doesNotMatch(dom.text(), /Prefer small commits/);
    assert.match(dom.text(), /Use tabs/);
    assert.match(dom.text(), /Deleted/);
  });
});

test("a full list says so and cannot take another note", async () => {
  const notes = Array.from({ length: 3 }, (_, i) => note(`n${i}`, `note ${i}`));
  await withCard({
    "GET /settings/engine": () => json(engine(1)),
    "GET /settings/notes": () => json({ notes, enabled: true, max_notes: 3 }),
  }, async (dom, calls) => {
    assert.match(dom.text(), /The list is full/);
    assert.equal((dom.byButton("Add note") as HTMLButtonElement).disabled, true);
    assert.equal((dom.byField("New note") as HTMLTextAreaElement).disabled, true);
    assert.deepEqual(writes(calls), []);
  });
});

test("a note over the engine's length cannot be sent", async () => {
  await withCard({
    "GET /settings/engine": () => json(engine(1)),
    "GET /settings/notes": () => json({ notes: [], enabled: true, max_notes: 50 }),
  }, async (dom, calls) => {
    await dom.fill(dom.byField("New note") as HTMLTextAreaElement, "x".repeat(501));
    assert.equal((dom.byButton("Add note") as HTMLButtonElement).disabled, true);
    assert.match(dom.text(), /501 \/ 500/);
    assert.deepEqual(writes(calls), []);
  });
});
