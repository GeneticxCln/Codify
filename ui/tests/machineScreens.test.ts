/**
 * What a machine's screen holds, with a real terminal emulator behind it.
 *
 * These run against `@xterm/headless` itself and not a stand-in: the point of the store is that it parses a terminal's stream
 * the way a terminal does (a cursor that moves, a line that wraps, a program that takes over the screen), and a fake that
 * split on newlines would pass every test here while being wrong about all three.
 *
 * What is asserted, and why:
 *  - **The screen is a screen**, not a transcript: rows and a cursor, scrollback separately, the alternate buffer of a
 *    full-screen program with none.
 *  - **Nothing is lost to a race.** Output that arrives before the screen was asked for, or while the emulator is still
 *    loading, is parsed in order; output for a machine that has been closed does not bring it back.
 *  - **A mark stays true as lines scroll away beneath it**, which is what makes `run`'s "what it printed" right for a long
 *    build, and says so when the start has gone.
 *  - **A pane that mounts later arrives at the same screen** (the replay), and says when the front of the stream was cut.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const screens = await import("../src/machineScreens.ts");
const { createMachineScreens, loadHeadless, MAX_LOG_CHARS, SCROLLBACK_LINES } = screens;
type MachineScreens = import("../src/machineScreens.ts").MachineScreens;

const make = (): MachineScreens => createMachineScreens(loadHeadless);
const nl = (...lines: string[]): string => lines.join("\r\n");

test("a screen is rows and a cursor, as a terminal would draw them, with trailing blanks cut", async () => {
  const s = make();
  s.write("m1", nl("[machine] /work $ ls", "a.py  b.py", "[machine] /work $ "));

  const snap = await s.read("m1", 40);

  assert.ok(snap);
  assert.equal(snap.rows.length, 24, "the screen has the grid's rows");
  assert.deepEqual(snap.rows.slice(0, 3), ["[machine] /work $ ls", "a.py  b.py", "[machine] /work $"]);
  assert.equal(snap.rows[10], "", "an empty row is empty");
  assert.equal(snap.cursorRow, 2);
  assert.equal(snap.cursorCol, "[machine] /work $ ".length, "the cursor sits after the prompt, past the trailing space");
  assert.equal(snap.exited, false);
  assert.equal(snap.alternate, false);
});

test("a carriage return and an escape sequence are interpreted and not shown", async () => {
  const s = make();
  s.write("m1", "downloading 10%\rdownloading 90%\x1b[31m!\x1b[0m\x1b[K");

  const snap = await s.read("m1", 0);

  assert.equal(snap?.rows[0], "downloading 90%!", "a progress line is what it ends as, not every state it passed through");
});

test("what scrolled off the top is scrollback, oldest first, and limited to what was asked for", async () => {
  const s = make();
  s.write("m1", Array.from({ length: 40 }, (_, i) => `line ${i}`).join("\r\n"));

  const all = await s.read("m1", 500);
  const few = await s.read("m1", 5);
  const none = await s.read("m1", 0);

  assert.ok(all && few && none);
  assert.equal(all.scrollback[0], "line 0");
  assert.equal(all.scrollback.length, 40 - 24, "everything that left the 24-row screen");
  assert.deepEqual(few.scrollback, ["line 11", "line 12", "line 13", "line 14", "line 15"], "the five nearest the screen, in order");
  assert.deepEqual(none.scrollback, []);
  assert.equal(all.rows[23], "line 39");
});

test("a program that takes over the screen is seen as it draws, with no scrollback to speak of", async () => {
  const s = make();
  s.write("m1", nl("before", "the program"));
  s.write("m1", "\x1b[?1049h\x1b[2J\x1b[1;1HFULL SCREEN\x1b[3;1Hstatus line");

  const during = await s.read("m1", 50);

  assert.equal(during?.alternate, true);
  assert.equal(during?.rows[0], "FULL SCREEN");
  assert.equal(during?.rows[2], "status line");
  assert.deepEqual(during?.scrollback, [], "a full-screen program has no scrollback, and what is behind it is not shown as such");

  s.write("m1", "\x1b[?1049l");
  const after = await s.read("m1", 50);
  assert.equal(after?.alternate, false);
  assert.equal(after?.rows[0], "before", "the screen comes back as it was");
});

test("whether the program wants application cursor keys is reported, so an arrow key can be the right one", async () => {
  const s = make();
  s.write("m1", "x");
  assert.equal((await s.read("m1", 0))?.applicationCursorKeys, false);

  s.write("m1", "\x1b[?1h");
  assert.equal((await s.read("m1", 0))?.applicationCursorKeys, true);

  s.write("m1", "\x1b[?1l");
  assert.equal((await s.read("m1", 0))?.applicationCursorKeys, false);
});

test("an id nobody has opened has no screen, and reading it says so", async () => {
  const s = make();

  assert.equal(await s.read("nope", 10), null);
  assert.equal(await s.mark("nope"), null);
  assert.equal(s.has("nope"), false);
  assert.equal(s.version("nope"), 0);
  assert.deepEqual(s.replay("nope"), { data: "", truncated: false, exited: false });
});

// ── nothing is lost to a race ────────────────────────────────────────────────

test("output that arrives before the screen was asked for opens it, and is all there", async () => {
  const s = make();
  s.write("m1", "first prompt $ ");
  s.open("m1");
  s.write("m1", "typed");

  assert.equal((await s.read("m1", 0))?.rows[0], "first prompt $ typed");
});

test("output that arrives while the emulator is still loading is held and parsed in order", async () => {
  let release: (() => void) | undefined;
  const slow = createMachineScreens(async () => {
    await new Promise<void>((resolve) => {
      release = resolve;
    });
    return loadHeadless();
  });
  slow.write("m1", "one ");
  slow.write("m1", "two ");
  const reading = slow.read("m1", 0);
  slow.write("m1", "three");
  release?.();

  assert.equal((await reading)?.rows[0], "one two three", "a chunk written during the load was dropped or came out of order");
});

test("a read waits for everything written so far, so it never shows a half-parsed screen", async () => {
  const s = make();
  for (let i = 0; i < 200; i += 1) s.write("m1", `row ${i}\r\n`);

  const snap = await s.read("m1", 1000);

  assert.equal(snap?.scrollback.length, 201 - 24 + 0, "some of the 200 chunks were not parsed when the read happened");
  assert.equal(snap?.scrollback[0], "row 0");
});

test("a machine that has been closed stays closed: late output does not bring its screen back", async () => {
  const s = make();
  s.write("m1", "alive");
  await s.flushed("m1");

  s.close("m1");
  s.write("m1", "a straggler from a jail that is gone");
  s.open("m1");

  assert.equal(s.has("m1"), false);
  assert.equal(await s.read("m1", 0), null);
  assert.equal(s.version("m1"), 0);
});

test("closing one machine leaves another's screen alone", async () => {
  const s = make();
  s.write("m1", "one");
  s.write("m2", "two");

  s.close("m1");

  assert.equal((await s.read("m2", 0))?.rows[0], "two");
  assert.equal(s.has("m1"), false);
});

test("an exited shell is marked, and the screen stays", async () => {
  const s = make();
  s.write("m1", "bye");
  let told = 0;
  s.watch("m1", () => {
    told += 1;
  });

  await s.flushed("m1");
  await new Promise((resolve) => setTimeout(resolve, 20));
  const afterTheChunk = told;
  s.exit("m1");
  s.exit("m1");

  const snap = await s.read("m1", 0);
  assert.equal(snap?.exited, true);
  assert.equal(snap?.rows[0], "bye");
  assert.equal(told, afterTheChunk + 1, "an exit said twice was announced twice");
  assert.equal(s.replay("m1").exited, true);
});

test("a resize between two chunks lands between them", async () => {
  const s = make();
  s.write("m1", "x".repeat(100));
  s.resize("m1", 40, 10);
  s.write("m1", "\r\nshort");

  const snap = await s.read("m1", 100);

  assert.equal(snap?.rows.length, 10);
  // 100 characters at 40 columns are three rows (40, 40, 20); the new line after them is the fourth.
  assert.deepEqual(snap?.rows.slice(0, 4), ["x".repeat(40), "x".repeat(40), "x".repeat(20), "short"], "the resize landed before or after the wrong chunk");
  s.resize("m1", 0, 10);
  assert.equal((await s.read("m1", 0))?.rows.length, 10, "a zero-width resize was applied");
});

// ── a mark stays true as lines scroll away beneath it ────────────────────────

test("a mark returns what was printed since, as logical lines, and not what was there before", async () => {
  const s = make();
  s.write("m1", nl("old line 1", "old line 2", "[machine] $ "));
  const mark = await s.mark("m1");
  assert.ok(mark);

  s.write("m1", nl("ls", "a.py", "b.py", "[machine] $ "));
  await s.flushed("m1");

  const since = mark.since();
  assert.deepEqual(since, { lines: ["[machine] $ ls", "a.py", "b.py", "[machine] $"], cut: false });
});

test("a long line that wrapped is one line, and the screen's padding is not output", async () => {
  const s = make();
  s.open("m1");
  const mark = await s.mark("m1");
  assert.ok(mark);

  s.write("m1", "w".repeat(200) + "\r\ndone");
  await s.flushed("m1");

  assert.deepEqual(mark.since()?.lines, ["w".repeat(200), "done"], "a wrapped line was split in two, or blank rows were counted");
});

test("a space where a line wrapped belongs to the line, and a trailing one does not", async () => {
  const s = make();
  s.open("m1", { cols: 10, rows: 5 });
  const mark = await s.mark("m1");
  assert.ok(mark);

  s.write("m1", "aaaaaaaa b" + "ccc   \r\n");
  await s.flushed("m1");

  assert.deepEqual(mark.since()?.lines, ["aaaaaaaa bccc"], "the line was rejoined without its inner space, or kept its trailing ones");
});

test("a mark stays right while the output outgrows the screen and scrolls the start into scrollback", async () => {
  const s = make();
  s.write("m1", "prompt $ ");
  const mark = await s.mark("m1");
  assert.ok(mark);

  s.write("m1", Array.from({ length: 100 }, (_, i) => `out ${i}`).join("\r\n"));
  await s.flushed("m1");

  const since = mark.since();
  assert.ok(since);
  assert.equal(since.cut, false, "the start is still in the buffer, and was reported lost");
  assert.equal(since.lines[0], "prompt $ out 0");
  assert.equal(since.lines.length, 100);
  assert.equal(since.lines[99], "out 99");
});

test("a mark whose start has been lost says so, and gives what there is", async () => {
  const s = make();
  s.write("m1", "start $ ");
  const mark = await s.mark("m1");
  assert.ok(mark);

  // More than the whole scrollback and the screen, so the marked line is gone.
  s.write("m1", Array.from({ length: SCROLLBACK_LINES + 200 }, (_, i) => `n${i}`).join("\r\n"));
  await s.flushed("m1");

  const since = mark.since();
  assert.ok(since);
  assert.equal(since.cut, true, "output that outran the scrollback was reported complete");
  assert.ok(since.lines.length > 0);
  assert.equal(since.lines[since.lines.length - 1], `n${SCROLLBACK_LINES + 199}`);
});

test("a full-screen program that starts after a mark makes the whole screen the answer, and says it is cut", async () => {
  const s = make();
  s.write("m1", "$ ");
  const mark = await s.mark("m1");
  assert.ok(mark);

  s.write("m1", "\x1b[?1049h\x1b[2J\x1b[1;1HEDITOR");
  await s.flushed("m1");

  const since = mark.since();
  assert.ok(since);
  assert.equal(since.cut, true);
  assert.equal(since.lines[0], "EDITOR");
});

test("a mark of a machine that has since been closed gives nothing", async () => {
  const s = make();
  s.write("m1", "x");
  const mark = await s.mark("m1");

  s.close("m1");

  assert.equal(mark?.since(), null);
  mark?.dispose();
});

// ── a pane that mounts later ─────────────────────────────────────────────────

test("a pane replays the log into its own terminal and arrives at the same screen", async () => {
  const s = make();
  s.write("m1", nl("one", "two", "three $ "));
  const { data, truncated } = s.replay("m1");
  assert.equal(truncated, false);

  const Terminal = await loadHeadless();
  const pane = new Terminal({ cols: 80, rows: 24, scrollback: 100, allowProposedApi: true });
  await new Promise<void>((resolve) => pane.write(data, resolve));

  const screen = await s.read("m1", 0);
  const rows = Array.from({ length: 24 }, (_, y) => (pane.buffer.active.getLine(y)?.translateToString(true) ?? "").trimEnd());
  assert.deepEqual(rows, screen?.rows, "the pane and the screen disagree about what is on screen");
});

test("a log that outgrows its cap is cut at the front and says so", async () => {
  const s = make();
  const chunk = "y".repeat(64 * 1024) + "\r\n";
  for (let i = 0; i < 6; i += 1) s.write("m1", chunk);

  const { data, truncated } = s.replay("m1");

  assert.equal(truncated, true);
  assert.equal(data.length, MAX_LOG_CHARS, "the log is not held to its cap");
  assert.ok(data.endsWith(chunk), "the cut took the end and not the front");
});

test("a reset brings an exited machine back and a machine that did not exit is left alone", async () => {
  const s = make();
  s.write("m1", "before\r\n");
  await s.read("m1", 0);
  await new Promise((resolve) => setTimeout(resolve, 0));
  let told = 0;
  s.watch("m1", () => {
    told += 1;
  });
  const quiet = told;

  s.revive("m1");
  assert.equal(told, quiet, "reviving a machine that never exited told the watchers");

  s.exit("m1");
  assert.equal(s.replay("m1").exited, true);
  const afterExit = told;
  s.revive("m1");

  assert.equal(s.replay("m1").exited, false, "a reset machine is still exited");
  assert.equal((await s.read("m1", 0))?.exited, false);
  assert.equal(told, afterExit + 1, "the watchers were not told the machine is running again");
  s.revive("nope");
  s.close("m1");
  s.revive("m1");
  assert.equal(s.has("m1"), false, "reviving a closed machine brought its screen back");
});

test("the screen's grid is what a reset gives the new terminal, and an unknown machine has none", async () => {
  const s = make();
  s.open("m1", { cols: 100, rows: 30 });
  assert.deepEqual(s.size("m1"), { cols: 100, rows: 30 });

  s.resize("m1", 140, 45);
  assert.deepEqual(s.size("m1"), { cols: 140, rows: 45 });
  s.resize("m1", 0, 10);
  assert.deepEqual(s.size("m1"), { cols: 140, rows: 45 }, "a grid with no columns replaced a real one");

  assert.equal(s.size("nope"), null);
  s.close("m1");
  assert.equal(s.size("m1"), null);
});

test("a terminal reset in the stream clears the screen and what is written after it is all there is", async () => {
  const s = make();
  s.write("m1", nl("[machine] /work $ junk", "more junk", "and more"));

  s.write("m1", "\x1bc[machine reset: a clean project, nothing from before]\r\n[machine] /work $ ");
  const snap = await s.read("m1", 40);

  assert.ok(snap);
  assert.deepEqual(snap.rows.slice(0, 2), ["[machine reset: a clean project, nothing from before]", "[machine] /work $"]);
  assert.equal(snap.rows.filter((r) => r.includes("junk")).length, 0, "what was there before the reset survived it");
  assert.deepEqual(snap.scrollback, [], "a reset left scrollback behind");
});

test("a follower hears each live chunk and the exit, and stops when it unsubscribes", async () => {
  const s = make();
  s.open("m1");
  const heard: string[] = [];
  let exits = 0;
  const off = s.follow("m1", (c) => heard.push(c), () => {
    exits += 1;
  });

  s.write("m1", "a");
  s.write("m1", "b");
  off();
  s.write("m1", "c");
  s.exit("m1");

  assert.deepEqual(heard, ["a", "b"]);
  assert.equal(exits, 0, "an unsubscribed follower heard the exit");
});

test("a watcher hears after a chunk has been parsed, and the version counts chunks", async () => {
  const s = make();
  s.open("m1");
  let seen = "";
  const off = s.watch("m1", () => {
    void s.read("m1", 0).then((snap) => {
      seen = snap?.rows[0] ?? "";
    });
  });

  s.write("m1", "hello");
  assert.equal(s.version("m1"), 1);
  await s.flushed("m1");
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.equal(seen, "hello");
  off();
  s.write("m1", "!");
  assert.equal(s.version("m1"), 2);
});
