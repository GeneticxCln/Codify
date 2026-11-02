/**
 * The inbox section in the notifications drawer, and the Settings card, mounted for real.
 *
 * What a person sees and what a press sends: Send is available only for the project that is selected, Snooze and
 * Dismiss say what they do, and the CI switch is off by default with its network behaviour written beside it.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { Suggestion } from "../src/types.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

const offer = (over: Partial<Suggestion> = {}): Suggestion => ({
  id: "s1",
  workspace_id: "w1",
  kind: "paused_goal",
  title: "“Add the banner” has been paused for 7 h",
  detail: "The critic asked for changes and stopped the run.",
  evidence: [],
  prompt: "My goal is paused. Do not change anything.",
  status: "open",
  snoozed_until: null,
  goal_id: null,
  created_at: 1,
  updated_at: 1,
  ...over,
});

async function section(props: Record<string, unknown>, body: (dom: Dom) => Promise<void>): Promise<void> {
  await withDom(async (dom) => {
    const { SuggestionsSection } = await import("../src/components/SuggestionsSection.tsx");
    await dom.render(
      h(SuggestionsSection, {
        items: [offer()],
        snoozed: 0,
        busyId: null,
        error: null,
        selectedWorkspaceId: "w1",
        workspaceNames: { w1: "Codify" },
        onSend: () => {},
        onSnooze: () => {},
        onDismiss: () => {},
        onScan: () => {},
        ...props,
      }),
    );
    await dom.settle();
    await body(dom);
  });
}

test("an offer shows its kind, project, title and why, and Send is ready for the selected project", async () => {
  await section({}, async (dom) => {
    assert.match(dom.text(), /Paused goal/);
    assert.match(dom.text(), /Codify/);
    assert.match(dom.text(), /has been paused for 7 h/);
    assert.match(dom.text(), /critic asked for changes/);
    assert.equal(dom.byButton("Send to chat").hasAttribute("disabled"), false);
  });
});

test("Send, Snooze and Dismiss each report which offer was pressed", async () => {
  const seen: string[] = [];
  await section(
    {
      onSend: (s: Suggestion) => seen.push(`send:${s.id}`),
      onSnooze: (id: string) => seen.push(`snooze:${id}`),
      onDismiss: (id: string) => seen.push(`dismiss:${id}`),
      onScan: () => seen.push("scan"),
    },
    async (dom) => {
      await dom.click(dom.byButton("Send to chat"));
      await dom.click(dom.byButton("Snooze 24 h"));
      await dom.click(dom.byButton("Dismiss"));
      await dom.click(dom.byButton("Check now"));
      assert.deepEqual(seen, ["send:s1", "snooze:s1", "dismiss:s1", "scan"]);
    },
  );
});

test("an offer about another project cannot be sent, and says why instead of going quiet", async () => {
  await section({ selectedWorkspaceId: "w2" }, async (dom) => {
    assert.equal(dom.byButton("Send to chat").hasAttribute("disabled"), true);
    assert.match(dom.text(), /different project/);
    assert.equal(dom.byButton("Dismiss").hasAttribute("disabled"), false, "it can still be put away");
  });
});

test("an empty inbox says what is watched, and a snoozed one says how many are asleep", async () => {
  await section({ items: [] }, async (dom) => {
    assert.match(dom.text(), /Nothing to suggest/);
    assert.match(dom.text(), /command that keeps failing/);
  });
  await section({ items: [], snoozed: 2 }, async (dom) => {
    assert.match(dom.text(), /2 snoozed/);
  });
});

test("a failed read is said, and the offers already on screen stay", async () => {
  await section({ error: "Could not load suggestions: HTTP 500" }, async (dom) => {
    assert.match(dom.text(), /Could not load suggestions/);
    assert.match(dom.text(), /has been paused for 7 h/);
  });
});

// ── the Settings card ────────────────────────────────────────────────────────────────────────────────────────────

interface Call {
  method: string;
  path: string;
  body: unknown;
}

async function card(
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
    const { SuggestionsCard } = await import("../src/components/SuggestionsCard.tsx");
    await dom.render(h(SuggestionsCard));
    await dom.settle();
    await body(dom, calls);
  });
}

const LOOK = "Let Codify point out things worth your attention";
const CI = "Also check GitHub for a failing CI run";
const on = (dom: Dom, label: string): boolean => dom.byButton(label).getAttribute("aria-checked") === "true";

test("an engine without the settings shows no card", async () => {
  await card({ parallel_width: { value: 4, min: 1, max: 16 } }, () => json({}), async (dom) => {
    assert.doesNotMatch(dom.text(), /Suggestions/);
  });
});

test("a fresh install looks but does not touch GitHub, and says so in plain words", async () => {
  await card(
    { suggestions: { value: 1, min: 0, max: 1 }, suggest_ci: { value: 0, min: 0, max: 1 } },
    (body) => json({ saved: body }),
    async (dom) => {
      assert.equal(on(dom, LOOK), true);
      assert.equal(on(dom, CI), false);
      assert.match(dom.text(), /makes no model call and never starts\s+anything/);
      assert.match(dom.text(), /contacts another machine on its own/);
      assert.match(dom.text(), /api\.github\.com/);
      assert.match(dom.text(), /no\s+credential/);
      assert.match(dom.text(), /every 30 minutes/);
    },
  );
});

test("turning on the CI check saves only that, and is stored as the engine reports it", async () => {
  await card(
    { suggestions: { value: 1, min: 0, max: 1 }, suggest_ci: { value: 0, min: 0, max: 1 } },
    (body) => json({ saved: { ...(body as object), suggestions: 1 } }),
    async (dom, calls) => {
      await dom.click(dom.byButton(CI));
      await dom.settle();
      assert.deepEqual(calls.filter((c) => c.method === "PUT").map((c) => c.body), [{ suggest_ci: true }]);
      assert.equal(on(dom, CI), true);
    },
  );
});

test("with suggestions off the CI switch cannot be turned on", async () => {
  await card(
    { suggestions: { value: 0, min: 0, max: 1 }, suggest_ci: { value: 0, min: 0, max: 1 } },
    (body) => json({ saved: body }),
    async (dom) => {
      assert.equal(dom.byButton(CI).hasAttribute("disabled"), true);
    },
  );
});

test("a refused save does not leave a switch the engine did not store", async () => {
  await card(
    { suggestions: { value: 1, min: 0, max: 1 }, suggest_ci: { value: 0, min: 0, max: 1 } },
    () => json({ message: "settings unavailable" }, 503),
    async (dom) => {
      await dom.click(dom.byButton(CI));
      await dom.settle();
      assert.equal(on(dom, CI), false);
      assert.match(dom.text(), /settings unavailable/);
    },
  );
});
