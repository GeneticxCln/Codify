/**
 * What the mounted App does about a terminal.
 *
 * `terminalPane.test.ts` renders the pane's markup; what it cannot see is the
 * App and the pane working together. These tests replace a set that read
 * `App.tsx` and `TerminalPane.tsx` as text. xterm loads in the harness's DOM, so
 * a terminal here is a real pane: they open shells, emit the shell's output and
 * exit events, switch tabs and close them, and look at what xterm shows, what
 * the strip badges and what the app asked the shell to do.
 *
 * The terminal store (`terminalBuffer.ts`, `terminalHistory.ts`) is module state
 * that outlives an app, as it does in production, so every test names its own
 * shells.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp } = await import("./appHarness.ts");
import type { AppContext, AppOptions } from "./appHarness.ts";

const VIEWPORT = { width: 900, height: 600 };

/** A shell that hands out these ids, one per `codify_terminal_open`, in order. */
const shells = (...ids: string[]): AppOptions["shellAnswers"] => {
  let next = 0;
  return { codify_terminal_open: () => ids[Math.min(next++, ids.length - 1)] };
};

const commands = (shell: AppContext["shell"], name: string) =>
  shell.calls.flatMap((c, i) => (c === name ? [shell.args[i]] : []));

const terminalButton = (root: HTMLElement) => root.querySelector('button[title^="Terminal"]') as HTMLElement;
const browserButton = (root: HTMLElement) => root.querySelector('button[title^="Browser"]') as HTMLElement;
const pane = (root: HTMLElement) => root.querySelector('section[aria-label="Terminal"]') as HTMLElement | null;
const unreadDots = (root: HTMLElement) => root.querySelectorAll('[data-testid="terminal-unread-dot"]').length;
const closers = (root: HTMLElement) => [...root.querySelectorAll('button[aria-label^="Close"]')] as HTMLElement[];

/** What xterm is showing in the pane on screen. */
const screen = (root: HTMLElement): string => pane(root)?.querySelector(".xterm-rows")?.textContent ?? "";

async function openShell(ctx: AppContext): Promise<void> {
  await ctx.dom.click(terminalButton(ctx.dom.container));
  await ctx.settle();
}

test("a terminal tab replaces the transcript, named by the shell, and is drawn by a pane for that shell", async () => {
  // Dropping the terminal branch would leave a terminal tab showing a chat
  // transcript with no way to type into it, and the pane's own render tests
  // would stay green because they render the pane directly.
  await withApp({ viewport: VIEWPORT, shellAnswers: shells("wire-a1") }, async (ctx) => {
    await openShell(ctx);
    assert.equal(ctx.tabs().length, 1);
    assert.ok(pane(ctx.dom.container), "no terminal pane for the terminal tab");
    assert.equal(pane(ctx.dom.container)?.getAttribute("data-terminal-id"), "wire-a1", "the pane was not given the shell's own id");
    assert.ok(ctx.dom.container.querySelector("textarea[placeholder]") === null, "the chat composer is still on screen under the terminal");
  });
});

test("a shell that will not start is reported where a user will see it, and opens no tab", async () => {
  // A terminal that never opened has no pane to report into, so the banner is the
  // only surface that is always on screen. The shell's refusal is a Rust
  // `Err(String)`, not an Error.
  await withApp(
    { viewport: VIEWPORT, shellFails: { codify_terminal_open: "no pty available" } },
    async (ctx) => {
      await openShell(ctx);
      assert.equal(ctx.tabs().length, 0, "a shell that never started got a tab");
      assert.match(ctx.dom.text(), /no pty available/, "the shell's own reason never reached the screen");
    },
  );
});

test("output the shell emits reaches xterm, and only the terminal it came from", async () => {
  await withApp({ viewport: VIEWPORT, shellAnswers: shells("wire-b1", "wire-b2") }, async (ctx) => {
    await openShell(ctx);
    await ctx.emit("terminal-output", { id: "wire-b1", data: "first-shell-says-hello\r\n" });
    await ctx.emit("terminal-output", { id: "wire-b2", data: "someone-elses-secret\r\n" });
    await ctx.settle();
    assert.match(screen(ctx.dom.container), /first-shell-says-hello/, "the shell's output never reached xterm");
    assert.doesNotMatch(screen(ctx.dom.container), /someone-elses-secret/, "another terminal's output was drawn here");
  });
});

test("a resize is sent to the terminal the pane is drawing, not the last one opened", async () => {
  // With two shells up, dragging the window used to resize the other one, and a
  // terminal restored from scrollback was never resized at all.
  await withApp({ viewport: VIEWPORT, shellAnswers: shells("wire-c1", "wire-c2") }, async (ctx) => {
    await openShell(ctx);
    await openShell(ctx);
    const resized = commands(ctx.shell, "codify_terminal_resize").map((a) => a.terminalId);
    assert.ok(resized.includes("wire-c1"), "the first pane never sized its own shell");
    assert.ok(resized.includes("wire-c2"), "the second pane never sized its own shell");
    // Each pane measures once when it mounts: c1's mount comes before c2's, and
    // nothing addressed to c1 may follow c2's arrival because c2 was "last".
    const lastC1 = resized.lastIndexOf("wire-c1");
    const firstC2 = resized.indexOf("wire-c2");
    assert.ok(lastC1 < firstC2, "the first shell was resized after the second was opened, by a pane that is not showing it");
  });
});

test("the pane is keyed by terminal id, so two shells are two terminals", async () => {
  // Without a key React reuses one pane, one xterm and one subscription across
  // shells: the second shell's prompt lands in the first one's scrollback.
  await withApp({ viewport: VIEWPORT, shellAnswers: shells("wire-d1", "wire-d2") }, async (ctx) => {
    await openShell(ctx);
    await ctx.emit("terminal-output", { id: "wire-d1", data: "only-in-d1\r\n" });
    await ctx.settle();
    const first = pane(ctx.dom.container);
    await openShell(ctx);
    const second = pane(ctx.dom.container);
    assert.equal(second?.getAttribute("data-terminal-id"), "wire-d2");
    assert.ok(second !== first, "switching shells reused the same pane");
    // (Not the screen's text: a new terminal in the same workspace restores the
    // workspace's scrollback on purpose. The claim is about the xterm instance.)
    assert.ok(second?.querySelector(".xterm") !== first?.querySelector(".xterm"), "one xterm was reused across two shells");
    await ctx.dom.click(ctx.tabs()[0]);
    await ctx.settle();
    assert.equal(pane(ctx.dom.container)?.getAttribute("data-terminal-id"), "wire-d1");
    assert.match(screen(ctx.dom.container), /only-in-d1/, "the first shell lost what it had said");
  });
});

test("output a background shell prints is kept, and shown when its tab comes back, once", async () => {
  // The recorder and the pane are a relay: the pane records what it shows, the app
  // records what nobody shows, and a chunk must be displayed exactly once.
  await withApp({ viewport: VIEWPORT, shellAnswers: shells("wire-e1") }, async (ctx) => {
    await openShell(ctx);
    await ctx.emit("terminal-output", { id: "wire-e1", data: "seen-live\r\n" });
    await ctx.dom.click(browserButton(ctx.dom.container));
    await ctx.settle();
    assert.ok(pane(ctx.dom.container) === null);
    await ctx.emit("terminal-output", { id: "wire-e1", data: "built-while-away\r\n" });
    await ctx.settle();
    await ctx.dom.click(ctx.tabs()[0]);
    await ctx.settle();
    const shown = screen(ctx.dom.container);
    assert.equal(shown.split("built-while-away").length - 1, 1, "output from the gap was lost or shown twice");
    assert.match(shown, /seen-live/, "what was on screen before the gap did not come back");
    assert.ok(
      shown.indexOf("seen-live") < shown.indexOf("output missed while this pane was away") &&
        shown.indexOf("output missed while this pane was away") < shown.indexOf("built-while-away"),
      "the gap is not marked between what was shown and what was missed, so the two read as one unbroken stream",
    );
  });
});

test("a keystroke the shell refuses is reported in the pane, and a healthy pane shows no alert", async () => {
  // The pane has no error prop: a refused write and a renderer that would not load
  // are its own state, and a failed *open* goes to the app banner instead.
  await withApp(
    { viewport: VIEWPORT, shellAnswers: shells("wire-key"), shellFails: { codify_terminal_write: "pty is gone" } },
    async (ctx) => {
      await openShell(ctx);
      assert.ok(pane(ctx.dom.container)?.querySelector('[role="alert"]') === null, "a healthy pane is showing an alert");
      const input = pane(ctx.dom.container)?.querySelector("textarea") as HTMLTextAreaElement;
      assert.ok(input, "xterm has no input to type into");
      await ctx.act(async () => {
        input.dispatchEvent(
          new ctx.dom.window.KeyboardEvent("keypress", { key: "a", charCode: 97, keyCode: 97, bubbles: true, cancelable: true }),
        );
      });
      await ctx.settle();
      assert.ok(commands(ctx.shell, "codify_terminal_write").length > 0, "the keystroke never reached the shell");
      assert.match(pane(ctx.dom.container)?.textContent ?? "", /pty is gone/, "a refused write was not reported in the pane");
    },
  );
});

test("output on the terminal being watched writes to xterm and never re-renders the app", async () => {
  // A PTY emits faster than a frame. A chunk that set React state would re-render
  // the pane and everything above it once per chunk, so `ls` in a big directory
  // would take the app with it.
  let commits = 0;
  await withApp({ viewport: VIEWPORT, shellAnswers: shells("wire-perf"), onCommit: () => commits++ }, async (ctx) => {
    await openShell(ctx);
    await ctx.settle();
    const before = commits;
    for (let i = 0; i < 100; i++) await ctx.emit("terminal-output", { id: "wire-perf", data: `line ${i}\r\n` });
    await ctx.settle();
    assert.match(screen(ctx.dom.container), /line 99/, "the output never reached xterm");
    assert.equal(commits - before, 0, `${commits - before} app commits for 100 chunks of terminal output`);
  });
});

test("the badge follows output nobody watched: never the tab being read, cleared by looking, gone with the tab", async () => {
  await withApp({ viewport: VIEWPORT, shellAnswers: shells("wire-f1") }, async (ctx) => {
    await openShell(ctx);
    await ctx.emit("terminal-output", { id: "wire-f1", data: "on-screen\r\n" });
    await ctx.settle();
    assert.equal(unreadDots(ctx.dom.container), 0, "the badge landed on the tab the user is reading");

    await ctx.dom.click(browserButton(ctx.dom.container));
    await ctx.settle();
    await ctx.emit("terminal-output", { id: "wire-f1", data: "finished-in-background\r\n" });
    await ctx.settle();
    assert.equal(unreadDots(ctx.dom.container), 1, "output nobody saw did not badge the tab");

    await ctx.dom.click(ctx.tabs()[0]);
    await ctx.settle();
    assert.equal(unreadDots(ctx.dom.container), 0, "showing the tab left the badge up");

    await ctx.dom.click(ctx.tabs()[1]);
    await ctx.emit("terminal-output", { id: "wire-f1", data: "more\r\n" });
    await ctx.settle();
    assert.equal(unreadDots(ctx.dom.container), 1);
    await ctx.dom.click(closers(ctx.dom.container)[0]);
    await ctx.settle();
    assert.equal(unreadDots(ctx.dom.container), 0, "a closed tab kept its badge");
  });
});

test("a shell that talks before its tab exists does not leave the new tab badged as unread", async () => {
  // A shell's first prompt is nearly always printed as `open` returns, before the
  // tab is on the strip. The recorder keeps that chunk and badges the tab, and the
  // tab then opens active with the pane replaying those very bytes.
  let release!: (id: string) => void;
  const gate = new Promise<string>((resolve) => (release = resolve));
  await withApp({ viewport: VIEWPORT, shellAnswers: { codify_terminal_open: () => gate } }, async (ctx) => {
    await ctx.dom.click(terminalButton(ctx.dom.container));
    await ctx.emit("terminal-output", { id: "wire-early", data: "printed-before-the-tab-existed\r\n" });
    await ctx.act(async () => release("wire-early"));
    await ctx.settle();
    assert.match(screen(ctx.dom.container), /printed-before-the-tab-existed/, "the early output was lost");
    assert.equal(unreadDots(ctx.dom.container), 0, "the tab on screen is badged as having unread output");
  });
});

test("what a shell prints while the renderer is still loading is not lost", async () => {
  // The pane subscribes to output before xterm exists, buffers what arrives, and
  // flushes it once there is somewhere to write. A listener that went up after
  // the import would miss the first prompt and every terminal would open blank,
  // as would a buffer nobody drained. The chunk is aimed at the gap: it lands the
  // moment the pane's own subscription is registered, before xterm has loaded.
  let emitNow: AppContext["emit"] | undefined;
  let opened = false;
  let fired = false;
  await withApp(
    {
      viewport: VIEWPORT,
      shellAnswers: {
        codify_terminal_open: () => {
          opened = true;
          return "wire-race";
        },
      },
      onShell: (cmd, a) => {
        if (!opened || fired || cmd !== "plugin:event|listen" || a.event !== "terminal-output") return;
        fired = true;
        queueMicrotask(() => void emitNow?.("terminal-output", { id: "wire-race", data: "first-prompt-in-the-gap$ \r\n" }));
      },
    },
    async (ctx) => {
      emitNow = ctx.emit;
      await openShell(ctx);
      assert.ok(fired, "the pane never subscribed to the shell's output, so the race was not exercised");
      const shown = screen(ctx.dom.container);
      assert.equal(shown.split("first-prompt-in-the-gap").length - 1, 1, `the first prompt was lost or shown twice: ${JSON.stringify(shown)}`);
    },
  );
});

test("a restored session is written above the live one, and is filed as it arrives, not when the tab closes", async () => {
  // The restored bytes are older than this shell's, so writing the live prompt above
  // them would read as the new shell quoting the old one. And the terminal that
  // said them is never closed here — the common case — so anything it left behind
  // was filed on the output path.
  await withApp({ viewport: VIEWPORT, shellAnswers: shells("wire-r1", "wire-r2") }, async (ctx) => {
    await openShell(ctx);
    await ctx.emit("terminal-output", { id: "wire-r1", data: "older-session-line\r\n" });
    await ctx.settle();
    await openShell(ctx);
    await ctx.emit("terminal-output", { id: "wire-r2", data: "live-session-line\r\n" });
    await ctx.settle();
    const shown = screen(ctx.dom.container);
    assert.ok(shown.includes("older-session-line"), "the earlier session was not restored into the new pane");
    assert.ok(shown.includes("live-session-line"));
    assert.ok(shown.indexOf("older-session-line") < shown.indexOf("live-session-line"), "the live session was written above the restored one");
    assert.equal(shown.split("older-session-line").length - 1, 1, "the earlier session was restored twice");
  });
});

test("a shell that exits in the background keeps its badge; one that exits on screen does not gain one", async () => {
  // A background exit is when the badge matters most: the shell finished while the
  // user was elsewhere and the output is unseen.
  await withApp({ viewport: VIEWPORT, shellAnswers: shells("wire-g1", "wire-g2") }, async (ctx) => {
    await openShell(ctx);
    await openShell(ctx);
    // wire-g2 is on screen, wire-g1 is in the background.
    await ctx.emit("terminal-output", { id: "wire-g1", data: "bg-output\r\n" });
    await ctx.emit("terminal-exit", { id: "wire-g1" });
    await ctx.settle();
    assert.equal(unreadDots(ctx.dom.container), 1, "a background exit silenced the badge that says something happened");
    await ctx.emit("terminal-exit", { id: "wire-g2" });
    await ctx.settle();
    assert.equal(unreadDots(ctx.dom.container), 1, "an exit the user watched added a badge");
    assert.match(ctx.dom.text(), /process exited/, "the pane on screen did not say its shell finished");
  });
});

test("closing a background tab files what its shell said, so the next terminal in the workspace restores it", async () => {
  // The pane that would have claimed that backlog will never exist. What a
  // terminal said is part of what the workspace restores, including a tab the
  // user has since closed.
  await withApp({ viewport: VIEWPORT, shellAnswers: shells("wire-h1", "wire-h2") }, async (ctx) => {
    await openShell(ctx);
    await ctx.dom.click(browserButton(ctx.dom.container));
    await ctx.settle();
    await ctx.emit("terminal-output", { id: "wire-h1", data: "said-while-unmounted\r\n" });
    await ctx.settle();
    await ctx.dom.click(closers(ctx.dom.container)[0]);
    await ctx.settle();
    assert.equal(commands(ctx.shell, "codify_terminal_close")[0]?.terminalId, "wire-h1", "the shell was not closed with its tab");
    await openShell(ctx);
    assert.match(screen(ctx.dom.container), /said-while-unmounted/, "what a closed background shell said was lost");
  });
});
