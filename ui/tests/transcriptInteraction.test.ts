/**
 * The transcript, pressed.
 *
 * `turnTranscript.test.ts` renders `ChatTimeline` to a string and reads the
 * words. That is the right test for "a benign turn says nothing about the gate"
 * — a claim about wording, which markup can carry. But the transcript is mostly
 * *controls*: Start, Execute Plan, Cancel, Delete, Edit step, the issues-only
 * filter, three audit badges, a recording panel and an audit export.
 * `renderToStaticMarkup` runs no effects and no events, so all of them render
 * identically whether they are wired up or not, and a button that has been
 * sitting there doing nothing for a year looks exactly like one that works.
 *
 * The gaps this file fills are specific, and each is a way the component could
 * be *correct as markup and wrong as a surface*:
 *
 * - **the version every goal action carries.** `onStartGoal(id, version)` is the
 *   optimistic-concurrency check; a card that passed anything else would 409 on
 *   the second click, and no amount of reading the markup would say so;
 * - **the audit badges cycling.** A count is a claim and the jump is the
 *   evidence behind it: the badge has to walk *every* match, flash the one it
 *   landed on, and come back round — and has to do it inside its own goal's
 *   card, since a transcript can hold several;
 * - **the plan step editor sending only what changed**, staying open with the
 *   user's text when the engine refuses, and not troubling the engine at all
 *   when nothing did;
 * - **two fetches that were silent on failure.** The recording panel and the
 *   audit export. A button that appears to do nothing is a bug report with no
 *   detail in it.
 *
 * ## The network
 *
 * This component reaches `api.ts`, which holds the engine's port and boot token
 * and calls the bare global `fetch`. The harness installs a `fetch` that
 * *records* the call and rejects, naming the URL — so mounting a finished run
 * gets a recorded attempt and a visible failure rather than a real socket to
 * `127.0.0.1`. Tests that want an answer install their own, and because they
 * install their own, the URLs they were asked for are assertions rather than
 * side effects.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
import type { Dom } from "./dom.ts";

// The id the skip link and the composer both name. Imported rather than
// written out, so this file cannot pass by agreeing with a second copy of the
// same typo — which is the failure the skip link itself exists to prevent.
const { COMPOSER_ANCHOR_ID } = await import("../src/composerAnchor.ts");

import type { ChatMessage, Event, Goal, PlanStep, TraceSummary } from "../src/types.ts";

// ── fixtures ────────────────────────────────────────────────────────────

const event = (
  sequence: number,
  type: Event["type"],
  payload: Record<string, any>,
): Event => ({
  id: `e${sequence}`,
  goal_id: "g1",
  step_id: null,
  type,
  payload,
  timestamp: sequence,
  sequence,
});

const step = (over: Partial<PlanStep> = {}): PlanStep => ({
  id: "s1",
  goal_id: "g1",
  ordinal: 0,
  title: "Add the tint picker",
  description: "One colour well per tintable variable.",
  suggested_paths: ["ui/src/tint.ts"],
  status: "PENDING",
  review_notes: null,
  commit_message: null,
  last_agent_role: null,
  ...over,
});

/** A run, not a conversation: `mode: "normal"` is what keeps the card whole. */
const goal = (over: Partial<Goal> = {}): Goal => ({
  id: "g1",
  workspace_id: "w1",
  conversation_id: "c1",
  title: "Ship the tint picker",
  description: "Ship the tint picker",
  status: "PENDING",
  dry_run: false,
  plan_only: false,
  parallel: false,
  mode: "normal",
  trace: false,
  version: 3,
  created_at: 0,
  updated_at: 0,
  steps: [step()],
  ...over,
});

const assistant = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "a1",
  role: "assistant",
  content: "",
  timestamp: 1,
  goal: goal(),
  events: [],
  ...over,
});

/** One edit to the plan, which is what the `edits` badge counts. */
const planEdited = (sequence: number, title: string): Event =>
  event(sequence, "plan_updated", {
    step_id: "s1",
    changes: { title: { before: `${title}!`, after: title } },
  });

const React = (await import("react")).default;
const h = React.createElement;

/** Every callback the transcript was handed, recorded in order. */
interface Recorder {
  started: Array<[string, number]>;
  enabled: Array<[string, number]>;
  cancelled: Array<[string, number]>;
  deleted: Array<[string, string]>;
  edits: Array<{ goalId: string; stepId: string; version: number; patch: Record<string, unknown> }>;
  imported: number;
}

const recorder = (): Recorder => ({
  started: [],
  enabled: [],
  cancelled: [],
  deleted: [],
  edits: [],
  imported: 0,
});

/**
 * Mount the transcript over these messages.
 *
 * `editResult` is what the engine answers to a plan edit: `true` for a save
 * that landed, `false` for the version conflict the editor's own comment is
 * about. "Refused" and "succeeded" are the same button, so a test that wants
 * the refusal has to ask for it.
 */
async function withTranscript(
  options: { messages: ChatMessage[]; editResult?: boolean },
  body: (dom: Dom, io: Recorder) => Promise<void>,
): Promise<void> {
  const io = recorder();
  await withDom(async (dom) => {
    // Imported inside the DOM: `react-dom/client` reads `document` while it is
    // being imported, and `api.ts` reads `localStorage` while *it* is. A
    // hoisted static import would be resolved before this DOM exists.
    const { ChatTimeline } = await import("../src/components/ChatTimeline.tsx");
    await dom.render(
      h(ChatTimeline, {
        messages: options.messages,
        pinnedContracts: {},
        onStartGoal: (id, v) => io.started.push([id, v]),
        onEnableExecution: (id, v) => io.enabled.push([id, v]),
        onApplyGoal: () => {},
        onEditStep: (id, stepId, version, patch) => {
          io.edits.push({ goalId: id, stepId, version, patch });
          return options.editResult ?? true;
        },
        onPauseGoal: () => {},
        onCancelGoal: (id, v) => io.cancelled.push([id, v]),
        onSetGoalTrace: () => {},
        onDeleteGoal: (id, title) => io.deleted.push([id, title]),
        onRetryStep: () => {},
        onOpenSettings: () => {},
        onImportAudit: () => {
          io.imported += 1;
        },
        onPinDesignContract: async () => {},
      }),
    );
    // A finished run's card asks the engine for its token usage the moment it
    // mounts. Letting that rejection land here, inside `act`, is what keeps the
    // suite free of "not wrapped in act" warnings from a component doing
    // something entirely ordinary.
    await dom.settle();
    await body(dom, io);
  });
}

/**
 * A `fetch` that answers everything with one document, recording what it was asked.
 *
 * Installed *inside* the DOM rather than before it: the harness puts its own
 * refusing `fetch` in place as it sets up, so a stub assigned from outside is
 * overwritten before the component ever runs. The recording is worth having even
 * so — a test can then say which endpoint it expected to be called, rather than
 * only what it got back.
 */
function answering(asked: string[], document: unknown): void {
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    asked.push(String(input));
    return {
      ok: true,
      status: 200,
      json: async () => document,
      text: async () => JSON.stringify(document),
    } as unknown as Response;
  }) as unknown as typeof fetch;
}

/** The same, for a call that must fail. */
function failing(message: string): void {
  globalThis.fetch = (async () => {
    throw new Error(message);
  }) as unknown as typeof fetch;
}

/** A recording with something in it, in the shape the panel reads. */
const recording = (over: Partial<TraceSummary> = {}): TraceSummary => ({
  goal_id: "g1",
  calls: 3,
  by_role: { librarian: 1, fixer: 1, scribe: 1 },
  prompts_kept: false,
  recording_error: null,
  recorded: [],
  ...over,
});

/**
 * The audit badge whose name starts this.
 *
 * Not `byLabel`: the accessible name of these buttons *changes* — it grows a
 * "— showing 2nd of 3" once one has been shown, which is the point of the
 * test — so a test about the cycling needs to find it by its stable prefix and
 * then read the whole name.
 */
const badge = (dom: Dom, prefix: string, index = 0): HTMLElement => {
  const found = [...dom.container.querySelectorAll(`button[aria-label^="${prefix}"]`)] as HTMLElement[];
  if (found.length <= index) {
    throw new Error(
      `no badge #${index} starts with "${prefix}". The badges here are: ` +
        (found.length ? found.map((b) => `"${b.getAttribute("aria-label")}"`).join(", ") : "(none)"),
    );
  }
  return found[index];
};

const edits = (dom: Dom): HTMLElement[] =>
  [...dom.container.querySelectorAll('[data-audit-marker="edits"]')] as HTMLElement[];

const name = (element: Element | null): string => element?.getAttribute("aria-label") ?? "";

// ─────────────────────────────────────────────────────────────────────────────
// The empty window
// ─────────────────────────────────────────────────────────────────────────────

test("the empty transcript's one control opens a file picker", async () => {
  await withTranscript({ messages: [] }, async (dom, io) => {
    // A wordmark, a skip link, and the import button. Nothing else: an empty
    // window that explains itself is a window asking to be read before it has
    // been used. The link exists because the button is the wrong *only* control
    // in it — a keyboard user who tabbed into the middle of this would have
    // nowhere to go but Escape.
    const skip = dom.container.querySelector('a[href^="#"]') as HTMLAnchorElement | null;
    assert.ok(skip, "the empty window has no skip link");
    assert.equal(skip.getAttribute("href"), `#${COMPOSER_ANCHOR_ID}`);
    assert.match(skip.textContent ?? "", /Skip to the prompt/);

    await dom.click(dom.byButton("Import audit report…"));
    assert.equal(io.imported, 1, "the one control in an empty window did nothing");
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Goal actions carry the goal's version
// ─────────────────────────────────────────────────────────────────────────────

test("Start asks for the goal and the version it was rendered from", async () => {
  // The version is the whole point: the engine refuses a stale one, so a card
  // that passed anything else would 409 on the second click — behind a button
  // that looks exactly as enabled as it did the first time.
  await withTranscript(
    { messages: [assistant({ goal: goal({ status: "PENDING", version: 7 }) })] },
    async (dom, io) => {
      await dom.click(dom.byButton("Start"));
      assert.deepEqual(io.started, [["g1", 7]]);
    },
  );
});

test("a plan-only goal offers Execute Plan instead, and it is a different call", async () => {
  // Two different engine routes. A card that offered the wrong one would lift
  // the plan-only guard by the wrong door, or start a run the user asked not to
  // start — and the markup for both is a button with a triangle on it.
  await withTranscript(
    { messages: [assistant({ goal: goal({ status: "PENDING", plan_only: true, version: 2 }) })] },
    async (dom, io) => {
      assert.throws(() => dom.byButton("Start"), /no button reads/, "a plan-only goal also offered Start");
      await dom.click(dom.byButton("Execute Plan"));
      assert.deepEqual(io.enabled, [["g1", 2]]);
      assert.deepEqual(io.started, [], "the plan-only guard was lifted by starting the goal");
    },
  );
});

test("a planning goal can be cancelled, and a finished one can be deleted", async () => {
  // Cancel is offered while there is still something to cancel — PLANNING
  // included, since a planning goal has written nothing yet. Delete is offered
  // only once the goal has stopped, because a live coroutine is writing files
  // and "delete" must never be how a person stops work in progress.
  await withTranscript(
    { messages: [assistant({ goal: goal({ status: "PLANNING" }) })] },
    async (dom, io) => {
      await dom.click(dom.byLabel("Cancel goal"));
      assert.deepEqual(io.cancelled, [["g1", 3]]);
      assert.deepEqual(io.deleted, [], "a running goal was also offered Delete");
    },
  );

  await withTranscript(
    { messages: [assistant({ goal: goal({ status: "COMPLETED" }) })] },
    async (dom, io) => {
      assert.deepEqual(dom.allByLabel("Cancel goal"), [], "a finished goal was still cancellable");
      await dom.click(dom.byLabel("Delete goal"));
      assert.deepEqual(io.deleted, [["g1", "Ship the tint picker"]]);
    },
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// The issues-only filter
// ─────────────────────────────────────────────────────────────────────────────

test("issues only hides the healthy telemetry, and can be undone", async () => {
  // Long runs render hundreds of telemetry entries and the question is usually
  // "what went wrong". A filter that cannot be switched off is a dead end.
  await withTranscript(
    {
      messages: [
        assistant({
          events: [
            event(1, "log", { level: "info", message: "conductor called read_file" }),
            event(2, "log", { level: "warn", message: "risk score 2.00/2 — destructive" }),
            event(3, "log", { level: "info", message: "librarian opened 12 files" }),
          ],
        }),
      ],
    },
    async (dom) => {
      assert.match(dom.text(), /librarian opened 12 files/);
      await dom.click(dom.byButton("Issues only"));
      assert.doesNotMatch(
        dom.text(),
        /librarian opened 12 files/,
        "the filter kept a healthy log line",
      );
      assert.match(dom.text(), /risk score 2\.00\/2/, "the filter dropped the warning it exists for");

      await dom.click(dom.byButton("Issues only"));
      assert.match(dom.text(), /librarian opened 12 files/);
    },
  );
});

test("the filter is only offered on a run that has something to filter", async () => {
  // A pill that filters nothing is noise, not a control. This also pins what
  // "an issue" means to the filter — a warning or a failure, not any event at
  // all — which is the difference between a useful button and a dead one.
  await withTranscript(
    { messages: [assistant({ events: [event(1, "log", { level: "info", message: "all quiet" })] })] },
    async (dom) => {
      assert.throws(() => dom.byButton("Issues only"), /no button reads/);
    },
  );
  await withTranscript(
    {
      messages: [
        assistant({
          events: [
            event(1, "log", { level: "info", message: "all quiet" }),
            event(2, "log", { level: "warn", message: "risk score 2.00/2" }),
          ],
        }),
      ],
    },
    async (dom) => {
      assert.equal(dom.byButton("Issues only").textContent?.trim(), "Issues only");
    },
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// Audit badges
// ─────────────────────────────────────────────────────────────────────────────

test("an audit badge walks its matches, flashes each one, and comes back round", async () => {
  // Three claims, none of which a static render can make: the badge cycles
  // rather than landing on the first match forever, the element it moved to is
  // the *Nth* match, and it wraps. The cursor behind this is module state, so
  // the fixture gives this test a message id of its own — a shared id would let
  // one test's clicks decide where the next one starts.
  await withTranscript(
    {
      messages: [
        assistant({
          id: "badge-1",
          events: [planEdited(1, "Add the tint picker"), planEdited(2, "Guard the pick")],
        }),
      ],
    },
    async (dom) => {
      const first = badge(dom, "Plan steps were edited");
      assert.match(first.textContent ?? "", /^2 edits$/, "the badge did not count both edits");
      assert.doesNotMatch(
        name(first),
        /showing 1st/,
        "the badge claims to have shown a match before it was pressed",
      );

      await dom.click(first);
      assert.match(name(first), /showing 1st of 2/);
      assert.equal(
        edits(dom)[0].classList.contains("audit-flash"),
        true,
        "the first edit was not flashed",
      );
      assert.equal(
        dom.scrolls.at(-1),
        edits(dom)[0],
        "the transcript scrolled to something other than the edit it just counted",
      );

      await dom.click(first);
      assert.match(name(first), /showing 2nd of 2/);
      assert.equal(
        edits(dom)[1].classList.contains("audit-flash"),
        true,
        "the second edit was not flashed",
      );
      assert.equal(dom.scrolls.at(-1), edits(dom)[1], "the jump did not move to the second edit");

      await dom.click(first);
      assert.match(name(first), /showing 1st of 2/, "the badge stopped after one pass instead of cycling");
    },
  );
});

test("a badge jumps inside its own goal's card, not the first one on screen", async () => {
  // A transcript can hold several runs. An unscoped query lands on whichever
  // card rendered first, so a person presses a badge on the run they are
  // reading and is taken to a different run entirely — with the count they just
  // clicked now pointing somewhere else.
  await withTranscript(
    {
      messages: [
        assistant({
          id: "card-a",
          goal: goal({ id: "g-a", title: "First run" }),
          events: [planEdited(1, "First edit")],
        }),
        assistant({
          id: "card-b",
          goal: goal({ id: "g-b", title: "Second run" }),
          events: [planEdited(2, "Second edit")],
        }),
      ],
    },
    async (dom) => {
      await dom.click(badge(dom, "Plan steps were edited", 1));
      const flashed = edits(dom).filter((m) => m.classList.contains("audit-flash"));
      assert.equal(flashed.length, 1, "one edit was not lit at all");
      assert.match(
        flashed[0].textContent ?? "",
        /Second edit/,
        "the second card's badge jumped into the first card's transcript",
      );
    },
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// The plan step editor
// ─────────────────────────────────────────────────────────────────────────────

test("editing a step sends only what changed, and the goal's version with it", async () => {
  // The patch is what the engine's `plan_updated` event names, so a save that
  // re-sent the whole step reannounces fields that never moved — and a version
  // from anywhere but the goal is a 409 waiting to happen.
  await withTranscript(
    { messages: [assistant({ goal: goal({ plan_only: true, version: 4 }) })] },
    async (dom, io) => {
      await dom.click(dom.byButton("Edit"));
      assert.equal(
        (dom.byField("Title") as HTMLInputElement).value,
        "Add the tint picker",
        "the editor opened blank rather than on the step",
      );

      await dom.fill(dom.byField("Title") as HTMLInputElement, "Add the tint picker, guarded");
      await dom.fill(
        dom.byField("Target Paths (comma-separated)") as HTMLInputElement,
        "ui/src/tint.ts, ui/tests/tint.test.ts",
      );
      await dom.click(dom.byButton("Save Step"));

      assert.equal(io.edits.length, 1);
      assert.deepEqual(
        { goalId: io.edits[0].goalId, stepId: io.edits[0].stepId, version: io.edits[0].version },
        { goalId: "g1", stepId: "s1", version: 4 },
      );
      assert.deepEqual(
        io.edits[0].patch,
        {
          title: "Add the tint picker, guarded",
          suggested_paths: ["ui/src/tint.ts", "ui/tests/tint.test.ts"],
        },
        "the untouched description was re-sent as a change",
      );
      // And the editor is gone, because the save landed.
      assert.throws(() => dom.byButton("Save Step"), /no button reads/);
    },
  );
});

test("a save with nothing changed closes without troubling the engine", async () => {
  // An empty patch would only earn a 422.
  await withTranscript(
    { messages: [assistant({ goal: goal({ plan_only: true }) })] },
    async (dom, io) => {
      await dom.click(dom.byButton("Edit"));
      await dom.click(dom.byButton("Save Step"));
      assert.deepEqual(io.edits, [], "an unchanged step was sent to the engine anyway");
      assert.throws(() => dom.byButton("Save Step"), /no button reads/);
    },
  );
});

test("a refused save keeps the editor open, with the text the person was saving", async () => {
  // The version conflict is the case the editor's own comment names: closing on
  // a refusal throws away the exact words being saved, and the only record of
  // them is gone.
  await withTranscript(
    { messages: [assistant({ goal: goal({ plan_only: true }) })], editResult: false },
    async (dom, io) => {
      await dom.click(dom.byButton("Edit"));
      await dom.fill(dom.byField("Title") as HTMLInputElement, "Half-written");
      await dom.click(dom.byButton("Save Step"));
      assert.equal(io.edits.length, 1, "the save never reached the engine");
      assert.equal(
        (dom.byField("Title") as HTMLInputElement).value,
        "Half-written",
        "the editor closed and took the edit with it",
      );
    },
  );
});

test("cancelling an edit asks the engine for nothing", async () => {
  await withTranscript(
    { messages: [assistant({ goal: goal({ plan_only: true }) })] },
    async (dom, io) => {
      await dom.click(dom.byButton("Edit"));
      await dom.fill(dom.byField("Title") as HTMLInputElement, "Never mind");
      await dom.click(dom.byButton("Cancel"));
      assert.deepEqual(io.edits, [], "cancelling an edit still sent it");
      assert.throws(() => dom.byField("Title"), /no field is labelled/);
    },
  );
});

test("an edit is only offered where the engine will accept one", async () => {
  // Plan edits are a plan-only, awaiting-approval affordance. Offering one on a
  // run that is already executing is a button that 409s.
  await withTranscript(
    { messages: [assistant({ goal: goal({ status: "RUNNING" }) })] },
    async (dom) => {
      assert.throws(() => dom.byButton("Edit"), /no button reads/);
    },
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// A finished reply
// ─────────────────────────────────────────────────────────────────────────────

test("a finished reply starts closed, and one click opens it", async () => {
  // Measured before this existed: one goal filled 937px of a 654px panel, and
  // 456px of it was two unexpanded JSON documents nobody had asked to read.
  await withTranscript(
    {
      messages: [
        assistant({
          id: "replies-1",
          goal: goal({ status: "COMPLETED" }),
          events: [
            event(1, "model_delta", {
              role: "scribe",
              text: '{"summary": "one sentence", "files": [1, 2, 3]}',
              final: true,
            }),
          ],
        }),
      ],
    },
    async (dom) => {
      assert.equal(
        dom.byText("scribe replied").getAttribute("aria-expanded"),
        "false",
        "a finished reply arrived open",
      );
      assert.doesNotMatch(dom.text(), /"files": \[1, 2, 3\]/, "the payload is on screen unasked");

      await dom.click(dom.byText("scribe replied"));
      assert.equal(dom.byText("scribe replied").getAttribute("aria-expanded"), "true", "the reply did not open");
      assert.match(dom.text(), /"files": \[1, 2, 3\]/);

      await dom.click(dom.byText("scribe replied"));
      assert.equal(
        dom.byText("scribe replied").getAttribute("aria-expanded"),
        "false",
        "the reply could not be closed again",
      );
    },
  );
});

test("a reply still arriving is open, because it is the thing changing on screen", async () => {
  await withTranscript(
    {
      messages: [
        assistant({
          id: "replies-2",
          goal: goal({ status: "RUNNING" }),
          events: [event(1, "model_delta", { role: "scribe", text: "working on it", final: false })],
        }),
      ],
    },
    async (dom) => {
      assert.match(dom.text(), /working on it/);
      assert.equal(dom.byText("scribe replied").getAttribute("aria-expanded"), "true");
    },
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// The recording panel
// ─────────────────────────────────────────────────────────────────────────────

test("Show recording opens that goal's recording, and asks the engine for it", async () => {
  await withTranscript(
    { messages: [assistant({ goal: goal({ trace: true, status: "COMPLETED" }) })] },
    async (dom) => {
      const asked: string[] = [];
      answering(asked, recording());
      await dom.click(dom.byLabel("Show recording"));
      await dom.settle();
      assert.ok(
        asked.some((u) => u.includes("/goals/g1/trace")),
        `the panel opened without asking for the recording; it asked for ${JSON.stringify(asked)}`,
      );
      // What the panel says about the recording is the panel's own claim, read
      // from the document the engine sent rather than from the goal card.
      assert.match(dom.text(), /3 calls recorded/);
      assert.match(dom.text(), /librarian × 1/);
      // Deleting a recording is offered here and nowhere else, and only when
      // there is one to delete.
      assert.equal(dom.allByLabel("Delete recording").length, 1);
    },
  );
});

test("closing the recording panel puts the card back as it was", async () => {
  await withTranscript(
    { messages: [assistant({ goal: goal({ trace: true, status: "COMPLETED" }) })] },
    async (dom) => {
      const asked: string[] = [];
      answering(asked, recording());
      await dom.click(dom.byLabel("Show recording"));
      await dom.settle();
      assert.equal(dom.allByLabel("Close recording panel").length, 1);

      await dom.click(dom.byLabel("Close recording panel"));
      assert.deepEqual(dom.allByLabel("Close recording panel"), [], "the panel stayed open");
      // And the control that opened it is offered again, since the recording is
      // still there.
      assert.equal(dom.allByLabel("Show recording").length, 1);
    },
  );
});

test("a recording that cannot be read says so, instead of rendering an empty panel", async () => {
  await withTranscript(
    { messages: [assistant({ goal: goal({ trace: true, status: "COMPLETED" }) })] },
    async (dom) => {
      failing("HTTP 500");
      await dom.click(dom.byLabel("Show recording"));
      await dom.settle();
      assert.match(dom.text(), /HTTP 500/, "a failed read is silent, which is a bug report with no detail");
    },
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// The audit export
// ─────────────────────────────────────────────────────────────────────────────

test("Export audit trail downloads that goal's audit", async () => {
  // The whole claim is a file: an anchor is created, pointed at a blob and
  // clicked, and then it is gone. The blob is the only part of that a test can
  // still hold afterwards, so it is captured where it is made.
  const asked: string[] = [];
  const blobs: Blob[] = [];
  const saved = URL.createObjectURL;
  URL.createObjectURL = ((blob: Blob) => {
    blobs.push(blob);
    return "blob:audit";
  }) as typeof URL.createObjectURL;
  try {
    await withTranscript(
      { messages: [assistant({ goal: goal({ title: "Ship the tint picker" }) })] },
      async (dom) => {
        answering(asked, { goal_id: "g1", edits: 2 });
        const downloads: Array<{ name: string; href: string }> = [];
        // The component's anchor never enters the document, so its `click()` is
        // recorded here. That is also how jsdom's "not implemented: navigation"
        // stays out of the suite: a download link in a document with nowhere to
        // download to is not a thing to navigate.
        dom.window.HTMLAnchorElement.prototype.click = function recordDownload(
          this: HTMLAnchorElement,
        ) {
          downloads.push({ name: this.download, href: this.href });
        };

        await dom.click(dom.byLabel("Export audit trail"));
        await dom.settle();

        assert.ok(
          asked.some((u) => u.includes("/goals/g1/audit")),
          `the export never asked for the audit; it asked for ${JSON.stringify(asked)}`,
        );
        assert.equal(downloads.length, 1, "the export produced no file");
        assert.equal(downloads[0].href, "blob:audit");
        assert.match(
          downloads[0].name,
          /^audit-ship-the-tint-picker-\d{4}-\d{2}-\d{2}T/,
          downloads[0].name,
        );
        assert.equal(blobs.length, 1);
        assert.match(await blobs[0].text(), /"goal_id": "g1"/);
      },
    );
  } finally {
    URL.createObjectURL = saved;
  }
});

test("an export that fails says so on the card, rather than in the console", async () => {
  await withTranscript({ messages: [assistant({})] }, async (dom) => {
    failing("HTTP 500");
    await dom.click(dom.byLabel("Export audit trail"));
    await dom.settle();
    assert.match(dom.text(), /HTTP 500/, "the button appears to do nothing and explains why nowhere");
  });
});
