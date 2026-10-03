/**
 * The assistant's eyes and hands on a machine, answered from the screens.
 *
 * Run against the real headless emulator (`machineScreens.ts`) and a **simulated clock and shell**: the fake shell answers a
 * write with output at chosen virtual delays, `sleep` advances time and delivers whatever is due, and so "settled", "still
 * going" and "never answered" are exact and take no real time.
 *
 * What is asserted, and why it is the part that matters:
 *  - **Typing needs a target that is not a guess.** With several machines and none in view, input asks for a name and writes
 *    nothing; looking may guess, typing may not.
 *  - **Nothing outside this workspace's machine tabs is reachable.** Not another workspace's machine, and not a terminal's id
 *    (the person's own shell), which must never reach the write.
 *  - **The window is the last hop before a shell.** It refuses a control character, an unknown key and an exited machine again
 *    after the engine has, and says why.
 *  - **A command that is still going is not waited for.** The answer says it is unsettled, and the machine keeps going.
 *  - **The twelve key names are the engine's twelve**, held by reading its file.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { createMachineScreens, loadHeadless } = await import("../src/machineScreens.ts");
const { bytesForKey, createMachineSurface, fitRows, KEY_BYTES, DEFAULT_TIMING } = await import("../src/machineSurface.ts");
type MachineScreens = import("../src/machineScreens.ts").MachineScreens;
type MachineInfo = import("../src/machineSurface.ts").MachineInfo;
type SurfaceReply = import("../src/machineSurface.ts").SurfaceReply;

const WS = "w1";
const info = (id: string, over: Partial<MachineInfo> = {}): MachineInfo => ({
  id,
  title: "Machine",
  workspaceId: WS,
  exited: false,
  network: false,
  ...over,
});

interface Step {
  /** Virtual milliseconds after the write. */
  after: number;
  out: string;
}

/** A window with machines in it, a shell that can be scripted, and a clock that only moves when something sleeps. */
function world(initial: MachineInfo[] = [info("mach-1")]) {
  let now = 0;
  const due: Array<{ at: number; fn: () => void }> = [];
  const screens: MachineScreens = createMachineScreens(loadHeadless);
  const written: Array<{ id: string; data: string }> = [];
  const state = {
    machines: initial,
    shown: initial.map((m) => m.id).slice(0, 1),
    focused: (initial[0]?.id ?? null) as string | null,
    failWrites: null as string | null,
  };
  const shells = new Map<string, (data: string) => Step[]>();

  const advance = async (ms: number): Promise<void> => {
    now += ms;
    for (;;) {
      due.sort((a, b) => a.at - b.at);
      const next = due[0];
      if (!next || next.at > now) break;
      due.shift();
      next.fn();
    }
    await Promise.resolve();
  };

  const host = {
    view: () => ({ machines: state.machines, shown: state.shown, focused: state.focused }),
    async write(id: string, data: string): Promise<void> {
      written.push({ id, data });
      if (state.failWrites) throw new Error(state.failWrites);
      for (const step of shells.get(id)?.(data) ?? []) due.push({ at: now + step.after, fn: () => screens.write(id, step.out) });
    },
    async sleep(ms: number): Promise<void> {
      await advance(ms);
    },
    now: () => now,
  };
  const surface = createMachineSurface(screens, host, DEFAULT_TIMING);
  const ask = (op: string, args: Record<string, unknown> = {}, workspace = WS): Promise<SurfaceReply> =>
    surface({ id: "q", surface: "machine", op, workspace_id: workspace, args });
  for (const m of initial) screens.open(m.id);
  return { screens, host, state, shells, written, ask, advance, clock: () => now };
}

const ok = (reply: SurfaceReply): any => {
  assert.equal(reply.ok, true, reply.ok ? "" : (reply as { error: string }).error);
  return (reply as { result: unknown }).result;
};
const refused = (reply: SurfaceReply): string => {
  assert.equal(reply.ok, false, "the question was answered and should have been refused");
  return (reply as { error: string }).error;
};

/** A shell that echoes the command, prints `lines`, then prints a prompt, a little after each. */
const echoing = (lines: string[], first = 30, gap = 10): ((data: string) => Step[]) => (data) => {
  const typed = data.replace(/\r$/, "").replace(/\r/g, "\r\n");
  const steps: Step[] = [{ after: first, out: `${typed}\r\n` }];
  lines.forEach((line, i) => steps.push({ after: first + gap * (i + 1), out: `${line}\r\n` }));
  steps.push({ after: first + gap * (lines.length + 2), out: "[machine] /work $ " });
  return steps;
};

// ── the key table is the engine's ────────────────────────────────────────────

test("the twelve key names are exactly the engine's, read from its file", () => {
  const py = readFileSync(fileURLToPath(new URL("../../engine/surface_machine.py", import.meta.url)), "utf8");
  const line = py.split("\n").find((l) => l.startsWith("KEYS = ("));
  assert.ok(line, "engine/surface_machine.py no longer defines KEYS");
  const engine = [...line.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  const window = [...Object.keys(KEY_BYTES), "Up", "Down", "Left", "Right"];
  assert.deepEqual([...window].sort(), [...engine].sort(), "the window and the engine disagree about which keys exist");
  assert.equal(engine.length, 12);
});

test("each key is the bytes a terminal sends for it, and an arrow is the one the program asked for", () => {
  assert.deepEqual(
    ["Enter", "Tab", "Escape", "Backspace", "Ctrl-C", "Ctrl-D", "Ctrl-L", "Ctrl-Z"].map((k) => bytesForKey(k, false)),
    ["\r", "\t", "\x1b", "\x7f", "\x03", "\x04", "\x0c", "\x1a"],
  );
  assert.deepEqual(["Up", "Down", "Right", "Left"].map((k) => bytesForKey(k, false)), ["\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"]);
  assert.deepEqual(["Up", "Down", "Right", "Left"].map((k) => bytesForKey(k, true)), ["\x1bOA", "\x1bOB", "\x1bOC", "\x1bOD"]);
  for (const bad of ["enter", "Ctrl-X", "F5", "Alt-F4", "", "\x03", "toString", "constructor", "__proto__", "hasOwnProperty"]) {
    assert.equal(bytesForKey(bad, false), null, `${JSON.stringify(bad)} is a key`);
  }
});

test("a tall screen is cut at the top and keeps the cursor's row, and padding below the cursor is dropped", () => {
  assert.deepEqual(fitRows(["a", "b", "", ""], 1), { rows: ["a", "b"], cursorRow: 1 });
  assert.deepEqual(fitRows(["a", "", "", ""], 2), { rows: ["a", "", ""], cursorRow: 2 }, "the cursor's own row was dropped");
  const tall = Array.from({ length: 100 }, (_, i) => `r${i}`);
  const fitted = fitRows(tall, 99);
  assert.equal(fitted.rows.length, 60);
  assert.equal(fitted.rows[59], "r99", "the bottom of a tall screen, where the prompt is, was cut");
  assert.equal(fitted.cursorRow, 59);
  assert.equal(fitRows(tall, 10).cursorRow, 0, "a cursor above the kept part is clamped to its top");
});

// ── looking ──────────────────────────────────────────────────────────────────

test("read lists this workspace's machines and shows the screen of the one in view", async () => {
  const w = world([info("mach-1", { network: true }), info("mach-2")]);
  w.state.shown = ["mach-2"];
  w.state.focused = "mach-2";
  w.screens.write("mach-1", "one");
  w.screens.write("mach-2", "[machine] /work $ ls\r\na.py\r\n[machine] /work $ ");

  const result = ok(await w.ask("read"));

  assert.deepEqual(
    result.machines.map((m: any) => [m.id, m.network, m.in_view, m.focused, m.alive]),
    [
      ["mach-1", true, false, false, true],
      ["mach-2", false, true, true, true],
    ],
  );
  assert.equal(result.screen.id, "mach-2");
  assert.deepEqual(result.screen.rows, ["[machine] /work $ ls", "a.py", "[machine] /work $"]);
  assert.equal(result.screen.cursor_row, 2);
  assert.equal(result.screen.cursor_col, "[machine] /work $ ".length);
});

test("with no name, read picks the focused machine in view, then the first in view, then the only one, then the latest", async () => {
  const w = world([info("a"), info("b"), info("c")]);
  for (const id of ["a", "b", "c"]) w.screens.write(id, `I am ${id}`);
  const which = async (): Promise<string> => ok(await w.ask("read")).screen.id;

  w.state.shown = ["a", "b"];
  w.state.focused = "b";
  assert.equal(await which(), "b", "the focused machine was not preferred");

  w.state.focused = null;
  assert.equal(await which(), "a", "the first in view was not chosen");

  w.state.shown = [];
  assert.equal(await which(), "c", "with nothing in view, looking should settle on the most recently opened");

  w.state.machines = [info("only")];
  w.screens.write("only", "just me");
  assert.equal(await which(), "only");
});

test("a machine in a tab nobody is looking at is read as readily as one in front", async () => {
  const w = world([info("front"), info("behind")]);
  w.state.shown = ["front"];
  w.screens.write("behind", "a build is running\r\nstep 3 of 9");

  const result = ok(await w.ask("read", { machine: "behind" }));

  assert.equal(result.screen.id, "behind");
  assert.deepEqual(result.screen.rows, ["a build is running", "step 3 of 9"]);
  assert.equal(result.machines.find((m: any) => m.id === "behind").in_view, false);
});

test("read brings back the scrollback it was asked for, and a default of forty", async () => {
  const w = world();
  w.screens.write("mach-1", Array.from({ length: 100 }, (_, i) => `row ${i}`).join("\r\n"));

  const some = ok(await w.ask("read", { scrollback: 3 }));
  const dflt = ok(await w.ask("read"));

  assert.deepEqual(some.screen.scrollback, ["row 73", "row 74", "row 75"]);
  assert.equal(dflt.screen.scrollback.length, 40);
});

test("no machine open is an empty answer and not an error, and a machine that has said nothing yet has an empty screen", async () => {
  const none = world([]);
  assert.deepEqual(ok(await none.ask("read")), { machines: [], screen: null });

  const quiet = world();
  const result = ok(await quiet.ask("read"));
  assert.equal(result.screen.id, "mach-1");
  assert.ok(result.screen.rows.every((row: string) => row === ""), "a machine that has said nothing has text on its screen");
  assert.equal(result.screen.cursor_row, 0);
});

test("an exited machine is still read, and says its shell has gone", async () => {
  const w = world([info("m", { exited: true })]);
  w.screens.write("m", "last words");
  w.screens.exit("m");

  const result = ok(await w.ask("read"));

  assert.equal(result.machines[0].alive, false);
  assert.equal(result.screen.alive, false);
  assert.deepEqual(result.screen.rows, ["last words"]);
});

test("only this workspace's machines are listed or reachable, however they are named", async () => {
  const w = world([info("mine"), info("theirs", { workspaceId: "w2" })]);
  w.screens.write("theirs", "SECRET of the other workspace");

  const listed = ok(await w.ask("read"));
  const asked = refused(await w.ask("read", { machine: "theirs" }));
  const other = ok(await w.ask("read", {}, "w2"));

  assert.deepEqual(listed.machines.map((m: any) => m.id), ["mine"]);
  assert.match(asked, /no machine theirs in this workspace/);
  assert.ok(!asked.includes("SECRET"));
  assert.deepEqual(other.machines.map((m: any) => m.id), ["theirs"]);
});

test("a terminal's id is not a machine, for looking or for typing, and nothing is written", async () => {
  const w = world();
  w.screens.write("term-1", "the person's own shell");

  assert.match(refused(await w.ask("read", { machine: "term-1" })), /no machine term-1/);
  assert.match(refused(await w.ask("run", { command: "ls", machine: "term-1" })), /no machine term-1/);
  assert.match(refused(await w.ask("key", { key: "Enter", machine: "term-1" })), /no machine term-1/);
  assert.deepEqual(w.written, [], "something was typed into a terminal");
});

test("a tall screen is cut at the top and keeps the prompt", async () => {
  const w = world();
  w.screens.resize("mach-1", 80, 80);
  w.screens.write("mach-1", Array.from({ length: 70 }, (_, i) => `r${i}`).join("\r\n") + "\r\n[machine] $ ");

  const result = ok(await w.ask("read"));

  assert.equal(result.screen.rows.length, 60);
  assert.equal(result.screen.rows[result.screen.cursor_row], "[machine] $", "the cursor no longer points at the prompt");
});

// ── typing a command ─────────────────────────────────────────────────────────

test("run types the command and Enter, and brings back what it printed from the line it was typed on", async () => {
  const w = world();
  w.screens.write("mach-1", "older output\r\n[machine] /work $ ");
  w.shells.set("mach-1", echoing(["a.py  b.py"]));

  const result = ok(await w.ask("run", { command: "ls" }));

  assert.deepEqual(w.written, [{ id: "mach-1", data: "ls\r" }]);
  assert.deepEqual(result.output, ["[machine] /work $ ls", "a.py  b.py", "[machine] /work $"]);
  assert.equal(result.settled, true);
  assert.equal(result.alive, true);
  assert.equal(result.truncated, false);
  assert.ok(!result.output.includes("older output"), "what was on the screen before the command is not its output");
});

test("a command that spans lines is typed as Enter after each", async () => {
  const w = world();
  w.shells.set("mach-1", echoing([]));

  ok(await w.ask("run", { command: "echo a\necho b" }));

  assert.deepEqual(w.written, [{ id: "mach-1", data: "echo a\recho b\r" }]);
});

test("output that arrives in pieces, with gaps shorter than the quiet period, is one answer", async () => {
  const w = world();
  w.shells.set("mach-1", () => [
    { after: 50, out: "building 1\r\n" },
    { after: 450, out: "building 2\r\n" },
    { after: 850, out: "building 3\r\n" },
    { after: 1250, out: "done\r\n[machine] $ " },
  ]);

  const result = ok(await w.ask("run", { command: "make" }));

  assert.deepEqual(result.output, ["building 1", "building 2", "building 3", "done", "[machine] $"]);
  assert.equal(result.settled, true);
});

test("settling needs a quiet period: the answer waits for it, and no longer", async () => {
  const w = world();
  w.shells.set("mach-1", () => [{ after: 20, out: "ok\r\n$ " }]);

  const before = w.clock();
  ok(await w.ask("run", { command: "true" }));
  const took = w.clock() - before;

  assert.ok(took >= DEFAULT_TIMING.quietMs, `settled after ${took}ms, before the output had been quiet for ${DEFAULT_TIMING.quietMs}`);
  assert.ok(took < DEFAULT_TIMING.quietMs + 300, `waited ${took}ms for output that stopped after 20`);
});

test("a command still going when the wait ends is not waited for: the answer says it is unsettled and the machine carries on", async () => {
  const w = world();
  w.shells.set("mach-1", () =>
    Array.from({ length: 40 }, (_, i) => ({ after: 100 * (i + 1), out: `tick ${i}\r\n` })),
  );

  const result = ok(await w.ask("run", { command: "ticker", wait_s: 1 }));

  assert.equal(result.settled, false);
  assert.ok(result.output.length >= 5 && result.output.length < 20, `${result.output.length} lines`);
  assert.ok(w.clock() >= 1000 && w.clock() < 1300, `waited ${w.clock()}ms for a 1s wait`);
  await w.advance(10_000);
  const later = ok(await w.ask("read", { scrollback: 0 }));
  assert.ok(later.screen.rows.includes("tick 39"), "the machine did not keep running after the call returned");
});

test("a command that never prints anything is not settled, and says so after its wait", async () => {
  const w = world();

  const result = ok(await w.ask("run", { command: "sleep 100", wait_s: 0.5 }));

  assert.equal(result.settled, false);
  assert.ok(w.clock() >= 500);
});

test("more output than the engine will take is cut to the last of it and says so", async () => {
  const w = world();
  w.shells.set("mach-1", () => [
    { after: 20, out: Array.from({ length: 450 }, (_, i) => `line ${i}`).join("\r\n") + "\r\n$ " },
  ]);

  const result = ok(await w.ask("run", { command: "yes | head -450" }));

  assert.equal(result.output.length, 300);
  assert.equal(result.output[299], "$");
  assert.equal(result.output[0], "line 151");
  assert.equal(result.truncated, true);
});

test("output that outran the scrollback is reported as cut", async () => {
  const w = world();
  w.shells.set("mach-1", () => [
    { after: 20, out: Array.from({ length: 2400 }, (_, i) => `n${i}`).join("\r\n") + "\r\n$ " },
  ]);

  const result = ok(await w.ask("run", { command: "seq 2400" }));

  assert.equal(result.truncated, true);
  assert.equal(result.output[result.output.length - 1], "$");
});

test("a shell that exits while the command runs is reported as no longer alive", async () => {
  const w = world();
  w.shells.set("mach-1", () => [
    { after: 20, out: "bye\r\n" },
    { after: 30, out: "" },
  ]);
  w.shells.set("mach-1", () => {
    setTimeout(() => w.screens.exit("mach-1"), 0);
    return [{ after: 20, out: "bye\r\n" }];
  });

  const result = ok(await w.ask("run", { command: "exit" }));
  await new Promise((resolve) => setTimeout(resolve, 5));
  const again = refused(await w.ask("run", { command: "ls" }));

  assert.equal(typeof result.alive, "boolean");
  assert.match(again, /has exited/);
});

// ── typing is refused unless it is right ─────────────────────────────────────

test("the window refuses what the engine should already have: empty, control characters, too long", async () => {
  const w = world();

  assert.match(refused(await w.ask("run", { command: "" })), /no command/);
  assert.match(refused(await w.ask("run", { command: "   " })), /no command/);
  for (const bad of ["ls\x03", "\x1b[A", "a\x00b", "a\rb", "a\x7fb", "ls\t", "a\x08b"]) {
    assert.match(refused(await w.ask("run", { command: bad })), /control character/, JSON.stringify(bad));
  }
  assert.match(refused(await w.ask("run", { command: "x".repeat(4001) })), /longer than/);
  assert.match(refused(await w.ask("run", {})), /no command/);
  assert.deepEqual(w.written, [], "something was typed");
});

test("input with several machines and none in view asks for a name, writes nothing, and looking may still guess", async () => {
  const w = world([info("a"), info("b")]);
  w.state.shown = [];
  w.state.focused = null;
  w.screens.write("b", "I am b");

  const run = refused(await w.ask("run", { command: "ls" }));
  const key = refused(await w.ask("key", { key: "Enter" }));
  const looked = ok(await w.ask("read"));

  assert.match(run, /none is in view/);
  assert.match(run, /a, b/, "the refusal did not name the machines to choose from");
  assert.match(key, /none is in view/);
  assert.deepEqual(w.written, []);
  assert.equal(looked.screen.id, "b");
});

test("input with exactly one machine, in view or not, goes to it; with a name it goes where it was told", async () => {
  const lone = world([info("only")]);
  lone.state.shown = [];
  lone.shells.set("only", echoing([]));
  ok(await lone.ask("run", { command: "ls" }));
  assert.equal(lone.written[0].id, "only");

  const two = world([info("a"), info("b")]);
  two.state.shown = ["a"];
  two.shells.set("b", echoing([]));
  ok(await two.ask("run", { command: "ls", machine: "b" }));
  assert.deepEqual(two.written.map((x) => x.id), ["b"], "a named machine was not the one typed into");

  const inView = world([info("a"), info("b")]);
  inView.state.shown = ["b"];
  inView.state.focused = "b";
  inView.shells.set("b", echoing([]));
  ok(await inView.ask("run", { command: "ls" }));
  assert.deepEqual(inView.written.map((x) => x.id), ["b"], "the machine in view was not the one typed into");
});

test("no machine open is a refusal that says the assistant cannot open one", async () => {
  const w = world([]);

  assert.match(refused(await w.ask("run", { command: "ls" })), /you cannot/);
  assert.match(refused(await w.ask("key", { key: "Enter" })), /you cannot/);
});

test("a machine whose shell has exited is not typed into, and the refusal says what to do", async () => {
  const w = world([info("m", { exited: true })]);

  assert.match(refused(await w.ask("run", { command: "ls" })), /has exited.*open a new machine/);
  assert.match(refused(await w.ask("key", { key: "Enter" })), /has exited/);
  assert.deepEqual(w.written, []);

  const stale = world([info("m")]);
  stale.screens.write("m", "x");
  stale.screens.exit("m");
  assert.match(refused(await stale.ask("run", { command: "ls" })), /has exited/, "the screen's own word that the shell ended was ignored");
});

test("a write the shell refuses is the refusal's own words, and the machine is not waited on", async () => {
  const w = world();
  w.state.failWrites = "no such machine: mach-1";

  const reply = refused(await w.ask("run", { command: "ls" }));
  const key = refused(await w.ask("key", { key: "Enter" }));

  assert.match(reply, /refused the keystrokes: no such machine/);
  assert.match(key, /refused the key: no such machine/);
  assert.ok(w.clock() < 100, `waited ${w.clock()}ms on a write that failed`);
});

// ── pressing a key ───────────────────────────────────────────────────────────

test("key sends exactly the key's bytes and brings back the screen as it then reads", async () => {
  const w = world();
  w.shells.set("mach-1", (data) => (data === "\x03" ? [{ after: 20, out: "^C\r\n[machine] $ " }] : []));
  w.screens.write("mach-1", "[machine] $ sleep 100");

  const result = ok(await w.ask("key", { key: "Ctrl-C" }));

  assert.deepEqual(w.written, [{ id: "mach-1", data: "\x03" }]);
  assert.equal(result.key, "Ctrl-C");
  assert.deepEqual(result.rows, ["[machine] $ sleep 100^C", "[machine] $"]);
  assert.equal(result.alive, true);
});

test("an arrow is the one the program asked for", async () => {
  const w = world();
  w.screens.write("mach-1", "x");

  ok(await w.ask("key", { key: "Up" }));
  w.screens.write("mach-1", "\x1b[?1h");
  ok(await w.ask("key", { key: "Up" }));

  assert.deepEqual(w.written.map((x) => x.data), ["\x1b[A", "\x1bOA"]);
});

test("a key nothing answers is given a short grace and no more, and one that is answered waits for the answer to go quiet", async () => {
  const silent = world();
  silent.screens.write("mach-1", "$ ");
  const t0 = silent.clock();
  ok(await silent.ask("key", { key: "Tab" }));
  const grace = silent.clock() - t0;
  assert.ok(grace >= DEFAULT_TIMING.keyGraceMs && grace < DEFAULT_TIMING.keyGraceMs + 200, `waited ${grace}ms for nothing`);

  const chatty = world();
  chatty.shells.set("mach-1", () => [
    { after: 20, out: "a" },
    { after: 150, out: "b" },
    { after: 280, out: "c" },
  ]);
  const result = ok(await chatty.ask("key", { key: "Enter" }));
  assert.deepEqual(result.rows, ["abc"], "the answer was returned before the output went quiet");

  const endless = world();
  endless.shells.set("mach-1", () => Array.from({ length: 100 }, (_, i) => ({ after: 100 * (i + 1), out: "." })));
  const t1 = endless.clock();
  ok(await endless.ask("key", { key: "Enter" }));
  assert.ok(endless.clock() - t1 <= DEFAULT_TIMING.keyMaxMs + 100, "a key that kept the output going was waited on without end");
});

test("an unknown key is refused, and nothing is typed", async () => {
  const w = world();

  for (const key of ["enter", "Ctrl-X", "F5", "", "\x03"]) {
    assert.match(refused(await w.ask("key", { key })), /not a key that can be pressed/, JSON.stringify(key));
  }
  assert.match(refused(await w.ask("key", {})), /not a key/);
  assert.deepEqual(w.written, []);
});

// ── what it never does ───────────────────────────────────────────────────────

test("an operation the surface does not have is refused by name, and nothing is typed", async () => {
  const w = world();

  for (const op of ["open", "close", "resize", "network", "spawn", "exec"]) {
    assert.match(refused(await w.ask(op)), new RegExp(`no operation called ${op}`));
  }
  assert.deepEqual(w.written, []);
});

test("looking writes nothing, and typing writes only to a machine tab", async () => {
  const w = world([info("mach-1"), info("mach-2")]);
  w.shells.set("mach-1", echoing(["x"]));
  w.shells.set("mach-2", echoing(["y"]));

  ok(await w.ask("read"));
  ok(await w.ask("read", { machine: "mach-2" }));
  assert.equal(w.written.length, 0, "reading typed something");

  ok(await w.ask("run", { command: "a", machine: "mach-1" }));
  ok(await w.ask("key", { key: "Enter", machine: "mach-2" }));
  assert.ok(w.written.every((x) => /^mach-\d$/.test(x.id)), JSON.stringify(w.written));
});

test("a host that throws something that is not an error is still a refusal and not a crash", async () => {
  const w = world();
  w.host.write = async () => {
    throw "a string";
  };

  assert.match(refused(await w.ask("run", { command: "ls" })), /refused the keystrokes: a string/);
});
