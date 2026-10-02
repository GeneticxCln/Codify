/**
 * A change the assistant made, and a way to look at the file it made it in.
 *
 * The run card draws each `diff` event as a unified diff. The editor gives that card somewhere to go: an **Open** link that
 * puts the file in an editor tab, so the person can read the whole file around the change, and edit it, without leaving the
 * conversation to find it. `docs/09` §13.
 *
 * The link is the card's only new control and its only new dependency, so it is optional. A transcript mounted without a
 * handler (the printable views, the tests that read words) draws the diff exactly as before, with no link that does nothing.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
import type { Dom } from "./dom.ts";
import type { ChatMessage, Event, Goal } from "../src/types.ts";

const React = (await import("react")).default;
const h = React.createElement;

const DIFF = ["--- a/src/main.py", "+++ b/src/main.py", "@@ -1,2 +1,2 @@", " def main():", "-    return 1", "+    return 2"].join("\n");

const diffEvent = (sequence: number, path: string): Event => ({
  id: `e${sequence}`,
  goal_id: "g1",
  step_id: "s1",
  type: "diff",
  payload: { path, unified_diff: DIFF },
  timestamp: sequence,
  sequence,
});

const goal = (): Goal => ({
  id: "g1",
  workspace_id: "w1",
  conversation_id: "c1",
  title: "Change main",
  description: "Change main",
  status: "COMPLETED",
  dry_run: false,
  plan_only: false,
  parallel: false,
  mode: "normal",
  trace: false,
  version: 3,
  created_at: 0,
  updated_at: 0,
  steps: [],
});

const message = (events: Event[]): ChatMessage => ({ id: "a1", role: "assistant", content: "", timestamp: 1, goal: goal(), events });

async function withTranscript(
  events: Event[],
  onOpenFile: ((path: string) => void) | undefined,
  body: (dom: Dom) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const { ChatTimeline } = await import("../src/components/ChatTimeline.tsx");
    await dom.render(
      h(ChatTimeline, {
        messages: [message(events)],
        pinnedContracts: {},
        onStartGoal: () => {},
        onEnableExecution: () => {},
        onApplyGoal: () => {},
        onEditStep: () => true,
        onPauseGoal: () => {},
        onCancelGoal: () => {},
        onSetGoalTrace: () => {},
        onDeleteGoal: () => {},
        onRetryStep: () => {},
        onOpenSettings: () => {},
        onImportAudit: () => {},
        onPinDesignContract: async () => {},
        onOpenFile,
      }),
    );
    await dom.settle();
    await body(dom);
  });
}

const openLinks = (dom: Dom): HTMLElement[] =>
  [...dom.container.querySelectorAll<HTMLElement>("button")].filter((b) => (b.getAttribute("aria-label") ?? "").startsWith("Open "));

test("a diff card offers to open its file, and pressing it names that path", async () => {
  const opened: string[] = [];
  await withTranscript([diffEvent(1, "src/main.py")], (path) => opened.push(path), async (dom) => {
    assert.match(dom.container.textContent ?? "", /return 2/, "the diff was not drawn");

    const links = openLinks(dom);
    assert.equal(links.length, 1);
    assert.equal(links[0]!.getAttribute("aria-label"), "Open src/main.py in the editor");

    await dom.click(links[0]!);
    assert.deepEqual(opened, ["src/main.py"]);
  });
});

test("each diff card opens its own file", async () => {
  const opened: string[] = [];
  await withTranscript([diffEvent(1, "src/main.py"), diffEvent(2, "src/util.py")], (path) => opened.push(path), async (dom) => {
    const links = openLinks(dom);
    assert.deepEqual(links.map((l) => l.getAttribute("aria-label")), ["Open src/main.py in the editor", "Open src/util.py in the editor"]);

    await dom.click(links[1]!);
    assert.deepEqual(opened, ["src/util.py"]);
  });
});

test("without a handler the diff is drawn as before, with no link that does nothing", async () => {
  await withTranscript([diffEvent(1, "src/main.py")], undefined, async (dom) => {
    assert.match(dom.container.textContent ?? "", /return 2/);
    assert.equal(openLinks(dom).length, 0);
  });
});
