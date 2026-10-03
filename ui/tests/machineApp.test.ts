/**
 * A machine, through the whole App: the person opens one, it shows its screen, the assistant reads and types into it
 * over the surface bridge, and none of it is written down or reaches anything but a machine.
 *
 * The pure parts are held elsewhere (`machineTab`, `machineScreens`, `machineSurface`). What only the App can show is the
 * part that is *about the window*, and each test below is one way the wiring could be wrong while every piece is right:
 * output that reaches the store only while a pane is mounted, a first prompt lost to the race with the open's reply, a
 * closed machine brought back by a straggling chunk, a terminal's id reaching a write, a jail the strip does not say has a
 * network, a layout that remembers a live jail.
 *
 * Real xterm and the real App over a fake shell. **The fake shell never spawns anything**: `codify_machine_*` are answered
 * by this file, and the PTY's output is `ctx.emit("machine-output", …)`, walking the same event path Rust's does.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  SEEDED,
  WIDE,
  beat,
  chord,
  click,
  clipRow,
  commands,
  focusedSide,
  id,
  openThread,
  pane,
  panes,
  pasteSomething,
  selectedTab,
  split,
  tabNamed,
  withApp,
} from "./splitHarness.ts";
import type { AppContext, AppOptions } from "./splitHarness.ts";
import { openPalette, paletteOption, queryPalette } from "./editorHarness.ts";

const STORAGE_KEY = "CODIFY_TABS";
const WS = "ws-a";

/** A shell that hands out these machine ids in order, and says what each write does through `onWrite`. */
function shell(ids: string[], onWrite?: (args: Record<string, unknown>) => void): { answers: NonNullable<AppOptions["shellAnswers"]> } {
  let next = 0;
  return {
    answers: {
      codify_machine_open: () => ids[Math.min(next++, ids.length - 1)],
      codify_machine_write: (a: Record<string, unknown>) => {
        onWrite?.(a);
        return null;
      },
      codify_machine_resize: () => null,
      codify_machine_close: () => null,
    },
  };
}

/** Say what a machine printed, the way the shell does. */
const say = (ctx: AppContext, machine: string, data: string): Promise<void> => ctx.emit("machine-output", { id: machine, data });

const machineButton = (ctx: AppContext): HTMLElement => ctx.dom.container.querySelector('button[title^="Machine"]') as HTMLElement;
const machinePane = (ctx: AppContext, machine: string): HTMLElement | null =>
  ctx.dom.container.querySelector<HTMLElement>(`[data-machine-id="${machine}"]`);
const screenOf = (ctx: AppContext, machine: string): string =>
  machinePane(ctx, machine)?.querySelector(".xterm-rows")?.textContent ?? "";
const networkBadge = (ctx: AppContext): string =>
  ctx.dom.container.querySelector('[data-testid="machine-network"]')?.textContent ?? "";

async function openMachine(ctx: AppContext): Promise<void> {
  await click(ctx, machineButton(ctx));
  await ctx.settle();
  await beat(40);
}

/** A person's keystrokes into a pane, as `terminalEndToEnd` types: keydown, then keypress unless the keydown was cancelled. */
function typeInto(ctx: AppContext, machine: string, text: string): void {
  const textarea = machinePane(ctx, machine)?.querySelector("textarea");
  assert.ok(textarea, `no xterm textarea in the ${machine} pane to type into`);
  for (const ch of text) {
    const keyCode = ch === " " ? 32 : /[a-z]/i.test(ch) ? ch.toUpperCase().charCodeAt(0) : ch.charCodeAt(0);
    const down = new ctx.dom.window.KeyboardEvent("keydown", { key: ch, keyCode, which: keyCode, bubbles: true, cancelable: true });
    textarea.dispatchEvent(down);
    if (down.defaultPrevented) continue;
    textarea.dispatchEvent(
      new ctx.dom.window.KeyboardEvent("keypress", { key: ch, keyCode, which: keyCode, charCode: keyCode, bubbles: true, cancelable: true }),
    );
  }
}

const run = (ctx: AppContext, args: Record<string, unknown>, workspace = WS) =>
  ctx.surface.ask({ surface: "machine", op: "run", workspace_id: workspace, args });
const look = (ctx: AppContext, args: Record<string, unknown> = {}, workspace = WS) =>
  ctx.surface.ask({ surface: "machine", op: "read", workspace_id: workspace, args });

// ── opening one ──────────────────────────────────────────────────────────────

test("the sidebar opens a machine in the selected workspace with no network, and shows it in front", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openMachine(ctx);

    assert.deepEqual(commands(ctx, "codify_machine_open"), [{ workspaceId: WS, cols: 80, rows: 24, network: false }]);
    assert.match(selectedTab(ctx), /^Machine: Machine/);
    assert.ok(machinePane(ctx, "mach-1"), "the machine's pane is not in the centre column");
    assert.equal(networkBadge(ctx), "No network");
    assert.doesNotMatch(selectedTab(ctx), /network/i);
  });
});

test("the palette opens one with a network, and the tab and the pane both say so", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openPalette(ctx);
    await queryPalette(ctx, "with network");
    const row = paletteOption(ctx, "New machine with network");
    assert.ok(row, "the palette has no row that opens a machine with a network");
    await ctx.dom.click(row);
    await ctx.settle();
    await beat(40);

    assert.deepEqual(commands(ctx, "codify_machine_open"), [{ workspaceId: WS, cols: 80, rows: 24, network: true }]);
    assert.match(selectedTab(ctx), /Machine: Machine · network.*has network access/);
    assert.equal(networkBadge(ctx), "Network on");
  });
});

test("the first row in the palette opens a machine with no network, so a network is never one Enter away", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openPalette(ctx);
    await queryPalette(ctx, "machine");
    const first = ctx.dom.container.querySelector<HTMLElement>('[role="option"]');
    assert.match(first?.textContent ?? "", /New machine: a jailed shell, no network/);
    await ctx.dom.click(first as HTMLElement);
    await ctx.settle();
    await beat(40);

    assert.equal((commands(ctx, "codify_machine_open")[0] as { network: boolean }).network, false);
  });
});

test("a shell that cannot make a jail is said in its own words, and nothing is opened", async () => {
  await withApp(
    {
      viewport: WIDE,
      ...SEEDED,
      shellFails: { codify_machine_open: "bubblewrap (bwrap) is not installed, and a machine cannot be isolated without it" },
    },
    async (ctx) => {
      await openMachine(ctx);

      const alert = [...ctx.dom.container.querySelectorAll('[role="alert"]')].map((a) => a.textContent).join(" ");
      assert.match(alert, /bubblewrap \(bwrap\) is not installed/);
      assert.equal(ctx.tabs().filter((t) => (t.getAttribute("aria-label") ?? "").startsWith("Machine")).length, 0);
      assert.equal(machinePane(ctx, "mach-1"), null);
    },
  );
});

// ── its screen ───────────────────────────────────────────────────────────────

test("what a machine prints is on its pane's screen", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openMachine(ctx);

    await say(ctx, "mach-1", "[machine] /work $ ls\r\na.py  b.py\r\n[machine] /work $ ");
    await beat(40);

    assert.match(screenOf(ctx, "mach-1"), /a\.py\s+b\.py/);
  });
});

test("output before the open's reply landed is not lost: the first prompt is there when the tab is", async () => {
  let ctxRef: AppContext | null = null;
  const answers: AppOptions["shellAnswers"] = {
    // The reader thread starts at once, and the reply naming the machine loses the race with its first prompt.
    codify_machine_open: () => {
      void say(ctxRef as AppContext, "mach-1", "[machine] /work $ first prompt");
      return "mach-1";
    },
    codify_machine_resize: () => null,
  };
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: answers }, async (ctx) => {
    ctxRef = ctx;
    await openMachine(ctx);
    await beat(40);

    assert.match(screenOf(ctx, "mach-1"), /first prompt/);
  });
});

test("a machine behind another tab keeps its output, and the pane that comes back shows all of it", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openThread(ctx, "c1");
    await openMachine(ctx);
    await click(ctx, tabNamed(ctx, "Conversation:"));
    assert.equal(machinePane(ctx, "mach-1"), null, "the pane should be unmounted while its tab is behind another");

    await say(ctx, "mach-1", "a build ran while nobody was looking\r\nstep 9 of 9 done");
    await click(ctx, tabNamed(ctx, "Machine:"));
    await beat(60);

    assert.match(screenOf(ctx, "mach-1"), /a build ran while nobody was looking/);
    assert.match(screenOf(ctx, "mach-1"), /step 9 of 9 done/);
  });
});

test("a machine whose shell exits says so, and the pane no longer sends what is typed", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openMachine(ctx);
    await say(ctx, "mach-1", "last words");

    await ctx.emit("machine-exit", { id: "mach-1" });
    await ctx.settle();
    await beat(40);
    typeInto(ctx, "mach-1", "ls");
    await beat(30);

    assert.match(machinePane(ctx, "mach-1")?.textContent ?? "", /shell has exited/);
    assert.match(screenOf(ctx, "mach-1"), /last words/, "the screen went with the shell");
    assert.equal(commands(ctx, "codify_machine_write").length, 0, "keystrokes were sent to a shell that had exited");
    // The assistant is told the same: its read says the shell is gone, from the store's own record of the exit.
    const read = await look(ctx);
    assert.equal(read.result.machines[0].alive, false);
    assert.equal(read.result.screen.alive, false, "the screen store was not told the shell exited");
    const typed = await run(ctx, { command: "ls" });
    assert.equal(typed.ok, false);
    assert.match(typed.error ?? "", /has exited/);
    assert.equal(commands(ctx, "codify_machine_write").length, 0, "the assistant typed into a shell that had exited");
  });
});

test("the pane tells its machine the size it measured, and the screen the assistant reads is that size", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openMachine(ctx);

    const sizes = commands(ctx, "codify_machine_resize");
    assert.ok(sizes.length >= 1, "the shell was never told how big the pane is, so the shell in the jail keeps wrapping at 80");
    assert.equal(sizes[0].machineId, "mach-1");
    const cols = sizes[0].cols as number;
    assert.ok(cols > 80, `the pane measured ${cols} columns, which is no more than the default`);

    // One line that is longer than the default width and shorter than the measured one is one row, if the screen the
    // assistant reads was resized with the PTY, and three if it kept the 80 columns it was opened with.
    await say(ctx, "mach-1", "x".repeat(200));
    await beat(40);
    const read = await look(ctx);
    assert.deepEqual(read.result.screen.rows, ["x".repeat(200)], "the store's screen was not resized with the machine");
  });
});

test("a shell that refuses what is typed is said on the pane, not swallowed", async () => {
  const s = shell(["mach-1"]);
  await withApp(
    { viewport: WIDE, ...SEEDED, shellAnswers: s.answers, shellFails: { codify_machine_write: "no machine mach-1 is open" } },
    async (ctx) => {
      await openMachine(ctx);

      await ctx.act(async () => {
        typeInto(ctx, "mach-1", "l");
        await beat(60);
      });

      const alert = machinePane(ctx, "mach-1")?.querySelector('[role="alert"]')?.textContent ?? "";
      assert.match(alert, /no machine mach-1 is open/);
    },
  );
});

test("with no workspace there is nothing to mount, so no machine is opened and the person is told", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, workspaces: [], shellAnswers: s.answers }, async (ctx) => {
    await click(ctx, machineButton(ctx));
    await ctx.settle();

    assert.equal(commands(ctx, "codify_machine_open").length, 0, "a machine was opened with no project to put at /work");
    const alert = [...ctx.dom.container.querySelectorAll('[role="alert"]')].map((a) => a.textContent).join(" ");
    assert.match(alert, /Pick a workspace/);
  });
});

/**
 * Which machine panes asked for the keyboard, by machine id, while `body` ran. `term.focus()` ends in the textarea's own
 * `focus()`, and that call is the one thing a pane does to take the keyboard that jsdom lets a test count.
 */
async function whoTookTheKeyboard(ctx: AppContext, body: () => Promise<void>): Promise<string[]> {
  const proto = ctx.dom.window.HTMLElement.prototype;
  const focus = proto.focus;
  const took: string[] = [];
  proto.focus = function (this: HTMLElement, ...rest: Parameters<HTMLElement["focus"]>) {
    const owner = this.closest("[data-machine-id]")?.getAttribute("data-machine-id");
    if (owner && this.tagName === "TEXTAREA") took.push(owner);
    return focus.apply(this, rest);
  };
  try {
    await body();
  } finally {
    proto.focus = focus;
  }
  return took;
}

test("a machine opened in front takes the keyboard, so what the person types next goes to it", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    const took = await whoTookTheKeyboard(ctx, () => openMachine(ctx));

    assert.ok(took.includes("mach-1"), "a machine that opened in front did not take the keyboard");
  });
});

test("the machine in the half of a split that is not focused does not take the keyboard from the other half", async () => {
  const s = shell(["mach-1", "mach-2"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openMachine(ctx);
    await openMachine(ctx);
    // mach-2 is in front; the palette offers mach-1 to sit beside it.
    await openPalette(ctx);
    await queryPalette(ctx, "beside");
    await ctx.dom.click(paletteOption(ctx, "Show beside: Machine") as HTMLElement);
    await ctx.settle();
    await beat(80);
    assert.equal(panes(ctx).length, 2, "the split was not made");
    const inPane = (side: 0 | 1) => pane(ctx, side).querySelector("[data-machine-id]")?.getAttribute("data-machine-id");
    const focusedSideNow = focusedSide(ctx) === "1" ? 1 : 0;
    const focused = inPane(focusedSideNow);
    const other = inPane(focusedSideNow === 1 ? 0 : 1);
    assert.ok(focused && other && focused !== other, `two machines are not side by side: ${focused} / ${other}`);

    // Close the split (the machine that was beside is now in front, and rightly takes the keyboard), then make it again
    // and count only that: each half is mounted afresh, and only the focused one may ask for the keyboard.
    await split(ctx);
    assert.equal(panes(ctx).length, 0);
    const took = await whoTookTheKeyboard(ctx, async () => {
      await openPalette(ctx);
      await queryPalette(ctx, "beside");
      await ctx.dom.click(paletteOption(ctx, "Show beside: Machine") as HTMLElement);
      await ctx.settle();
      await beat(80);
    });

    assert.ok(took.length >= 1, "no pane asked for the keyboard when the split was drawn, so this test cannot tell");
    const nowFocused = pane(ctx, focusedSide(ctx) === "1" ? 1 : 0).querySelector("[data-machine-id]")?.getAttribute("data-machine-id");
    const nowOther = pane(ctx, focusedSide(ctx) === "1" ? 0 : 1).querySelector("[data-machine-id]")?.getAttribute("data-machine-id");
    assert.ok(!took.includes(nowOther as string), `the machine in the unfocused half (${nowOther}) took the keyboard from ${nowFocused} (${took.join(", ")})`);
  });
});

// ── its keyboard ─────────────────────────────────────────────────────────────

test("what a person types in a machine's pane goes to that machine, by its own id", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openMachine(ctx);

    typeInto(ctx, "mach-1", "ls");
    await beat(40);

    const writes = commands(ctx, "codify_machine_write");
    assert.deepEqual(writes.map((w) => w.machineId), ["mach-1", "mach-1"]);
    assert.equal(writes.map((w) => w.data).join(""), "ls");
    assert.equal(commands(ctx, "codify_terminal_write").length, 0, "a machine's keystrokes went to the terminal's command");
  });
});

// ── closing one ──────────────────────────────────────────────────────────────

/**
 * What the headless emulators are told, counted on the real class: the store is private to the App, and "its screen is
 * freed and stays freed" is a claim about the emulator, which is the only place it can be seen from outside.
 */
async function watchEmulators(): Promise<{ disposed: () => number; written: () => number; stop: () => void }> {
  type Emulator = { prototype: { dispose(): void; write(data: string, done?: () => void): void } };
  // A CommonJS package under ESM: `Terminal` is on `default`, which is where `loadHeadless` finds it too.
  const mod = (await import("@xterm/headless")) as unknown as { Terminal?: Emulator; default?: { Terminal?: Emulator } };
  const Terminal = mod.Terminal ?? mod.default?.Terminal;
  assert.ok(Terminal, "@xterm/headless exports no Terminal");
  const proto = Terminal.prototype;
  const dispose = proto.dispose;
  const write = proto.write;
  let disposed = 0;
  let written = 0;
  proto.dispose = function (this: unknown) {
    disposed += 1;
    return dispose.call(this);
  };
  proto.write = function (this: unknown, data: string, done?: () => void) {
    written += 1;
    return write.call(this, data, done);
  };
  return {
    disposed: () => disposed,
    written: () => written,
    stop: () => {
      proto.dispose = dispose;
      proto.write = write;
    },
  };
}

test("closing a machine's tab ends its jail and frees its screen, and output still in flight does not bring it back", async () => {
  const s = shell(["mach-1"]);
  const seen = await watchEmulators();
  try {
    await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
      await openMachine(ctx);
      await say(ctx, "mach-1", "before");
      await beat(40);
      assert.equal(seen.disposed(), 0, "the screen was freed while the machine was open");
      const close = ctx.dom.container.querySelector('button[aria-label^="Close machine"]') as HTMLElement;
      await click(ctx, close);
      await ctx.settle();

      assert.deepEqual(commands(ctx, "codify_machine_close"), [{ machineId: "mach-1" }]);
      assert.equal(machinePane(ctx, "mach-1"), null);
      assert.equal(seen.disposed(), 1, "closing the tab did not free the machine's screen");

      const writes = seen.written();
      await say(ctx, "mach-1", "a straggler from a jail that is gone");
      await ctx.settle();
      await beat(40);
      assert.equal(seen.written(), writes, "output from a closed machine was parsed into a screen");
      assert.equal(seen.disposed(), 1);
      assert.deepEqual((await look(ctx)).result, { machines: [], screen: null }, "a closed machine came back");
    });
  } finally {
    seen.stop();
  }
});

// ── the assistant's eyes and hands, through the window ───────────────────────

test("the assistant reads a machine that no pane is showing, and only this workspace's", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openThread(ctx, "c1");
    await openMachine(ctx);
    await click(ctx, tabNamed(ctx, "Conversation:"));
    await say(ctx, "mach-1", "[machine] /work $ make\r\nbuilding...");

    const mine = await look(ctx);
    const theirs = await look(ctx, {}, "ws-other");

    assert.equal(mine.ok, true, mine.error ?? "");
    assert.equal(mine.result.machines[0].id, "mach-1");
    assert.equal(mine.result.machines[0].in_view, false, "a machine behind another tab was reported as in view");
    assert.deepEqual(mine.result.screen.rows, ["[machine] /work $ make", "building..."]);
    assert.deepEqual(theirs.result, { machines: [], screen: null }, "another workspace's assistant saw this machine");
  });
});

test("the assistant types a command, waits for it to settle, and gets what it printed", async () => {
  let ctxRef: AppContext | null = null;
  const s = shell(["mach-1"], (a) => {
    // The shell echoes what was typed and answers it, a little later, the way a real one does.
    const typed = String(a.data).replace(/\r$/, "");
    setTimeout(() => void say(ctxRef as AppContext, String(a.machineId), `${typed}\r\na.py  b.py\r\n[machine] /work $ `), 30);
  });
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    ctxRef = ctx;
    await openMachine(ctx);
    await say(ctx, "mach-1", "[machine] /work $ ");

    const reply = await run(ctx, { command: "ls" });

    assert.equal(reply.ok, true, reply.error ?? "");
    assert.deepEqual(commands(ctx, "codify_machine_write").map((w) => [w.machineId, w.data]), [["mach-1", "ls\r"]]);
    assert.equal(reply.result.settled, true);
    assert.deepEqual(reply.result.output, ["[machine] /work $ ls", "a.py  b.py", "[machine] /work $"]);
  });
});

test("the assistant presses a named key and reads the screen after it", async () => {
  let ctxRef: AppContext | null = null;
  const s = shell(["mach-1"], (a) => {
    if (a.data === "\x03") setTimeout(() => void say(ctxRef as AppContext, String(a.machineId), "^C\r\n[machine] $ "), 20);
  });
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    ctxRef = ctx;
    await openMachine(ctx);
    await say(ctx, "mach-1", "[machine] $ sleep 100");

    const reply = await ctx.surface.ask({ surface: "machine", op: "key", workspace_id: WS, args: { key: "Ctrl-C" } });

    assert.equal(reply.ok, true, reply.error ?? "");
    assert.deepEqual(commands(ctx, "codify_machine_write").map((w) => w.data), ["\x03"]);
    assert.deepEqual(reply.result.rows, ["[machine] $ sleep 100^C", "[machine] $"]);
  });
});

test("a terminal's id is not a machine: the assistant cannot type into the person's own shell", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: { ...s.answers, codify_terminal_open: () => id("term-1") } }, async (ctx) => {
    await click(ctx, ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
    await ctx.settle();
    await openMachine(ctx);

    const reply = await run(ctx, { command: "rm -rf /", machine: id("term-1") });

    assert.equal(reply.ok, false);
    assert.match(reply.error ?? "", /no machine/);
    assert.equal(commands(ctx, "codify_machine_write").length, 0);
    assert.equal(commands(ctx, "codify_terminal_write").length, 0, "something was typed into a terminal");
  });
});

test("the window has no way to open, close or resize a machine for the assistant", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openMachine(ctx);
    const before = ctx.shell.calls.length;

    for (const op of ["open", "close", "resize", "network", "spawn"]) {
      const reply = await ctx.surface.ask({ surface: "machine", op, workspace_id: WS, args: {} });
      assert.equal(reply.ok, false, `${op} was answered`);
      assert.match(reply.error ?? "", /no operation/);
    }

    const after = ctx.shell.calls.slice(before).filter((c) => c.startsWith("codify_machine_"));
    assert.deepEqual(after, [], "the assistant's questions reached the shell");
  });
});

test("a machine in front, alone, is the one the assistant means: in view, focused, and the one it is typing into", async () => {
  const s = shell(["mach-1", "mach-2"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openMachine(ctx);
    await openMachine(ctx);

    const read = await look(ctx);

    assert.deepEqual(
      read.result.machines.map((m: { id: string; in_view: boolean; focused: boolean }) => [m.id, m.in_view, m.focused]),
      [["mach-1", false, false], ["mach-2", true, true]],
    );
    assert.equal(read.result.screen.id, "mach-2", "the read was not of the machine in front");
  });
});

test("the clipboard drawer offers a machine neither the message box nor the terminal paste", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await pasteSomething(ctx, "ls -la");
    await openMachine(ctx);
    await ctx.dom.click(ctx.dom.byLabel("Clipboard history"));
    const row = clipRow(ctx, "ls -la");
    const button = (label: string) => row.querySelector(`[aria-label="${label}"]`) as HTMLButtonElement;

    assert.equal(button("Insert into the message box").disabled, true, "a clip can be inserted under a machine, where there is no message box");
    assert.equal(button("Paste into the terminal").disabled, true, "a clip can be pasted into a machine as if it were the person's terminal");
    assert.equal(button("Copy again").disabled, false);
  });
});

// ── beside a chat, and never written down ────────────────────────────────────

test("a machine can sit beside the chat, with the machine focused and the chat on the left", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openThread(ctx, "c1");
    await openMachine(ctx);
    await click(ctx, tabNamed(ctx, "Conversation:"));

    assert.equal(await split(ctx), true, "the chord was not claimed");

    assert.equal(panes(ctx).length, 2);
    assert.ok(pane(ctx, 1).querySelector('[data-machine-id="mach-1"]'), "the machine is not in the right pane");
    assert.ok(pane(ctx, 0).querySelector('textarea[aria-label="Chat prompt"]'), "the chat is not in the left pane");
    const read = await look(ctx);
    assert.equal(read.result.machines[0].in_view, true);
    assert.equal(focusedSide(ctx), "1");
    assert.equal(read.result.machines[0].focused, true);
  });
});

test("a machine tab is not in the strip's storage, and the engine is never told about it", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openThread(ctx, "c1");
    await openMachine(ctx);
    await say(ctx, "mach-1", "secret output");
    await beat(80);
    await ctx.settle();

    const stored = ctx.dom.window.localStorage.getItem(STORAGE_KEY) ?? "";
    assert.doesNotMatch(stored, /mach-1|machine|secret output/, "a machine reached CODIFY_TABS");
    assert.deepEqual(ctx.engineTabs().map((row) => row.kind), ["chat"]);
  });
});

test("Ctrl+W on a machine's tab closes its jail like the tab's own button does", async () => {
  const s = shell(["mach-1"]);
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: s.answers }, async (ctx) => {
    await openMachine(ctx);

    assert.equal(await chord(ctx, "w", "KeyW"), true, "the chord was not claimed");
    await ctx.settle();

    assert.deepEqual(commands(ctx, "codify_machine_close"), [{ machineId: "mach-1" }]);
    assert.equal(machinePane(ctx, "mach-1"), null);
  });
});
