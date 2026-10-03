/**
 * The assistant's eyes and hands on a machine, answered from the screens.
 *
 * The engine asks a question (`engine/surfaces.py`: `machine` surface, ops `read`, `run`, `key`), the window polls it and
 * answers it here, and the answer is checked against a fixed shape on the engine's side before the model sees a word of it
 * (`engine/surface_machine.py`). This module is the window's half, written against `MachineScreens` and a small
 * `MachineHost` standing in for the App, so all of it runs with no pane and no shell.
 *
 *  - **`read` sees what the person would see**: the screen as drawn, a cursor, and what scrolled off. It answers from the
 *    store, so a machine in a tab nobody is looking at is read as readily as one in front.
 *  - **`run` types a command and waits for the output to go quiet**, then says what was printed since the line it was typed
 *    on. A command still going when the wait ends is *not* waited for: the answer says it is unsettled and the machine
 *    keeps running.
 *  - **`key` presses one named key.** The names are the window's table from a name to the bytes it stands for, so the
 *    engine never sends a byte, and an arrow is the one the program asked for (a full-screen program that switched to
 *    application cursor keys gets `ESC O A`, not `ESC [ A`).
 *
 * **What it will not do is as much of the point.** Every question is about one workspace, the one the run belongs to: another
 * workspace's machines are not listed and not reachable. It writes to a machine **tab** and to nothing else, so a terminal's
 * id (the person's own shell) is refused here as it is refused by the shell's separate id space. It cannot open, close or
 * resize a machine, or change its network: there is no function here that does. And typing into the wrong place is worse
 * than not typing, so *input* needs a target that is not a guess: with several machines open and none in view it asks for
 * a name rather than choosing.
 */
import type { MachineScreens, ScreenSnapshot } from "./machineScreens";
import type { SurfaceRequest } from "./types";

/** The engine's caps, repeated here so the two cut in the same place. */
const MAX_ROWS = 60;
const MAX_OUTPUT_LINES = 300;
const MAX_COMMAND_CHARS = 4_000;

export interface MachineInfo {
  id: string;
  title: string;
  workspaceId: string;
  /** The shell in it has exited. */
  exited: boolean;
  network: boolean;
}

export interface MachineHost {
  /** The machine tabs, in strip order, which are on screen right now, and the one with focus. */
  view(): { machines: readonly MachineInfo[]; shown: readonly string[]; focused: string | null };
  /** Type into a machine through the shell (`codify_machine_write`). Rejects with the shell's reason. */
  write(id: string, data: string): Promise<void>;
  sleep(ms: number): Promise<void>;
  now(): number;
}

export interface MachineTiming {
  /** How long the output has to be silent for a command to count as settled. */
  quietMs: number;
  /** How often the screen is looked at while waiting. */
  pollMs: number;
  /** After a key: how long to wait for any answer at all, and how long for the answer to go quiet, and the most. */
  keyGraceMs: number;
  keyQuietMs: number;
  keyMaxMs: number;
}

export const DEFAULT_TIMING: MachineTiming = { quietMs: 600, pollMs: 40, keyGraceMs: 400, keyQuietMs: 250, keyMaxMs: 1_500 };

/** The keys the assistant may press: a name, and the bytes it stands for. The same twelve the engine's `Key` type lists. */
export const KEY_BYTES: Readonly<Record<string, string>> = {
  Enter: "\r",
  Tab: "\t",
  Escape: "\x1b",
  Backspace: "\x7f",
  "Ctrl-C": "\x03",
  "Ctrl-D": "\x04",
  "Ctrl-L": "\x0c",
  "Ctrl-Z": "\x1a",
};

/** The arrows, which are the one place the bytes depend on what the program asked for. */
const ARROWS: Readonly<Record<string, string>> = { Up: "A", Down: "B", Right: "C", Left: "D" };

export function bytesForKey(key: string, applicationCursorKeys: boolean): string | null {
  if (Object.hasOwn(KEY_BYTES, key)) return KEY_BYTES[key];
  if (Object.hasOwn(ARROWS, key)) return (applicationCursorKeys ? "\x1bO" : "\x1b[") + ARROWS[key];
  return null;
}

export type SurfaceReply = { ok: true; result: unknown } | { ok: false; error: string };

const refuse = (error: string): SurfaceReply => ({ ok: false, error });
const done = (result: unknown): SurfaceReply => ({ ok: true, result });

/**
 * The rows to send: the screen down to where it is used, and if that is still taller than the engine will take, the part
 * nearest the cursor, with the cursor's row moved to match. A cut that dropped the *bottom* of a tall screen would drop the
 * prompt the assistant is looking for.
 */
export function fitRows(rows: readonly string[], cursorRow: number): { rows: string[]; cursorRow: number } {
  let end = rows.length;
  while (end > 0 && end - 1 > cursorRow && rows[end - 1] === "") end -= 1;
  const used = rows.slice(0, end);
  if (used.length <= MAX_ROWS) return { rows: used, cursorRow };
  const offset = used.length - MAX_ROWS;
  return { rows: used.slice(offset), cursorRow: Math.max(0, cursorRow - offset) };
}

export function createMachineSurface(
  screens: MachineScreens,
  host: MachineHost,
  timing: MachineTiming = DEFAULT_TIMING,
): (request: SurfaceRequest) => Promise<SurfaceReply> {
  const mine = (workspaceId: string): MachineInfo[] => host.view().machines.filter((m) => m.workspaceId === workspaceId);

  const names = (machines: readonly MachineInfo[]): string => machines.map((m) => m.id).join(", ");

  /**
   * Which machine a question is about. A named machine must be one of this workspace's machine tabs. With no name, the one
   * in view and focused, else the first in view; and where nothing is in view, *looking* may guess (the most recent) but
   * *typing* may not, unless there is only one to mean.
   */
  const pick = (
    workspaceId: string,
    requested: unknown,
    forInput: boolean,
  ): { machine: MachineInfo } | { error: string } => {
    const candidates = mine(workspaceId);
    if (typeof requested === "string" && requested !== "") {
      const found = candidates.find((m) => m.id === requested);
      if (found) return { machine: found };
      return {
        error:
          candidates.length === 0
            ? `There is no machine ${requested}: no machine is open in this workspace.`
            : `There is no machine ${requested} in this workspace. The machines open are: ${names(candidates)}.`,
      };
    }
    if (candidates.length === 0) {
      return { error: "No machine is open in this workspace. The person opens one; you cannot." };
    }
    const { shown, focused } = host.view();
    const inView = candidates.filter((m) => shown.includes(m.id));
    const chosen = inView.find((m) => m.id === focused) ?? inView[0];
    if (chosen) return { machine: chosen };
    if (candidates.length === 1) return { machine: candidates[0] };
    if (forInput) {
      return {
        error: `${candidates.length} machines are open and none is in view, so there is no telling which you mean. Name one with \`machine\`: ${names(candidates)}.`,
      };
    }
    return { machine: candidates[candidates.length - 1] };
  };

  const describe = (m: MachineInfo, shown: readonly string[], focused: string | null) => ({
    id: m.id,
    title: m.title,
    alive: !m.exited,
    network: m.network,
    in_view: shown.includes(m.id),
    focused: focused === m.id,
  });

  const read = async (workspaceId: string, args: Record<string, unknown>): Promise<SurfaceReply> => {
    const machines = mine(workspaceId);
    const { shown, focused } = host.view();
    const listed = machines.map((m) => describe(m, shown, focused));
    if (machines.length === 0) return done({ machines: [], screen: null });

    const target = pick(workspaceId, args.machine, false);
    if ("error" in target) return refuse(target.error);
    const scrollback = typeof args.scrollback === "number" ? args.scrollback : 40;
    const snap = await screens.read(target.machine.id, scrollback);
    const alive = !(snap?.exited ?? target.machine.exited);
    if (!snap) {
      return done({
        machines: listed,
        screen: { id: target.machine.id, alive, rows: [], cursor_row: 0, cursor_col: 0, scrollback: [] },
      });
    }
    const fitted = fitRows(snap.rows, snap.cursorRow);
    return done({
      machines: listed,
      screen: {
        id: target.machine.id,
        alive,
        rows: fitted.rows,
        cursor_row: fitted.cursorRow,
        cursor_col: snap.cursorCol,
        scrollback: snap.scrollback,
      },
    });
  };

  /** Everything that has to be true before a keystroke is sent. The last place that can say no before the shell. */
  const writable = (m: MachineInfo): string | null => {
    if (m.exited || screens.replay(m.id).exited) {
      return `The shell in ${m.id} has exited, so nothing typed into it would be read. The person can open a new machine.`;
    }
    return null;
  };

  const type = async (id: string, data: string): Promise<string | null> => {
    try {
      await host.write(id, data);
      return null;
    } catch (error) {
      return error instanceof Error ? error.message : String(error);
    }
  };

  const run = async (workspaceId: string, args: Record<string, unknown>): Promise<SurfaceReply> => {
    const command = typeof args.command === "string" ? args.command : "";
    // The engine checked these. The window checks them again, because it is the last hop before a shell.
    if (command.trim() === "") return refuse("There is no command to type.");
    if (command.length > MAX_COMMAND_CHARS) return refuse(`That command is longer than ${MAX_COMMAND_CHARS} characters.`);
    // eslint-disable-next-line no-control-regex -- a control character is exactly what is being refused
    if (/[\x00-\x09\x0b-\x1f\x7f]/.test(command)) {
      return refuse("That command holds a control character. Press keys with key_in_machine instead.");
    }
    const target = pick(workspaceId, args.machine, true);
    if ("error" in target) return refuse(target.error);
    const m = target.machine;
    const refusal = writable(m);
    if (refusal) return refuse(refusal);
    const waitMs = Math.round((typeof args.wait_s === "number" ? args.wait_s : 5) * 1000);

    const mark = await screens.mark(m.id);
    if (!mark) return refuse(`${m.id} has no screen to type into yet. Try again in a moment.`);
    const before = screens.version(m.id);
    const failed = await type(m.id, command.replace(/\n/g, "\r") + "\r");
    if (failed) {
      mark.dispose();
      return refuse(`The shell refused the keystrokes: ${failed}`);
    }

    // Settled is "something was printed, and then nothing for a while". Output that never starts is not settled either.
    const started = host.now();
    let lastVersion = before;
    let lastChange = started;
    let settled = false;
    while (host.now() - started < waitMs) {
      await host.sleep(timing.pollMs);
      const now = host.now();
      const version = screens.version(m.id);
      if (version !== lastVersion) {
        lastVersion = version;
        lastChange = now;
      } else if (lastVersion !== before && now - lastChange >= timing.quietMs) {
        settled = true;
        break;
      }
    }
    await screens.flushed(m.id);
    const since = mark.since();
    mark.dispose();
    const after = await screens.read(m.id, 0);
    const lines = since?.lines ?? [];
    const truncated = Boolean(since?.cut) || lines.length > MAX_OUTPUT_LINES;
    return done({
      id: m.id,
      output: lines.slice(-MAX_OUTPUT_LINES),
      settled,
      alive: !(after?.exited ?? false),
      truncated,
    });
  };

  const key = async (workspaceId: string, args: Record<string, unknown>): Promise<SurfaceReply> => {
    const name = typeof args.key === "string" ? args.key : "";
    const target = pick(workspaceId, args.machine, true);
    if ("error" in target) return refuse(target.error);
    const m = target.machine;
    const refusal = writable(m);
    if (refusal) return refuse(refusal);

    const snap = await screens.read(m.id, 0);
    const bytes = bytesForKey(name, snap?.applicationCursorKeys ?? false);
    if (bytes === null) return refuse(`${JSON.stringify(name)} is not a key that can be pressed here.`);
    await screens.flushed(m.id);
    const before = screens.version(m.id);
    const failed = await type(m.id, bytes);
    if (failed) return refuse(`The shell refused the key: ${failed}`);

    const started = host.now();
    let lastVersion = before;
    let lastChange = started;
    while (host.now() - started < timing.keyMaxMs) {
      await host.sleep(timing.pollMs);
      const now = host.now();
      const version = screens.version(m.id);
      if (version !== lastVersion) {
        lastVersion = version;
        lastChange = now;
      } else if (lastVersion !== before ? now - lastChange >= timing.keyQuietMs : now - started >= timing.keyGraceMs) {
        break;
      }
    }
    const after: ScreenSnapshot | null = await screens.read(m.id, 0);
    const fitted = fitRows(after?.rows ?? [], after?.cursorRow ?? 0);
    return done({ id: m.id, key: name, rows: fitted.rows, alive: !(after?.exited ?? false) });
  };

  return async (request) => {
    try {
      switch (request.op) {
        case "read":
          return await read(request.workspace_id, request.args);
        case "run":
          return await run(request.workspace_id, request.args);
        case "key":
          return await key(request.workspace_id, request.args);
        default:
          return refuse(`A machine has no operation called ${request.op}.`);
      }
    } catch (error) {
      return refuse(error instanceof Error ? error.message : String(error));
    }
  };
}
