"""The engine's own death, against a real engine: bounded, and nothing left behind.

`tests/test_watchdog.py` drives the stop machinery with fakes — a polling loop that is
told its parent is gone, a deadline whose `exit_fn` is a `Mock`. That pins the
decisions; it cannot reach the defect this module exists for, which lived in the seam
between those decisions and a real process:

* a stop request is not handled by the engine at all, it is handed to uvicorn, and
  uvicorn's shutdown waits for in-flight work with `timeout_graceful_shutdown=None` —
  *forever* — while closing the listening socket first. So a `SIGTERM`ed engine
  released its port within about a second and then sat on the SQLite file for as long
  as a turn or an event stream ran, and a second `SIGTERM` changed nothing;
* the deadline the fix arms is only reachable if a real process actually gets there,
  and the teardown checkpoint underneath it (`PRAGMA wal_checkpoint(TRUNCATE)`, waiting
  on a reader a cancelled turn leaves behind) was a second way to hold the exit. Only a
  real boot with a real stream finds that one.

So this boots `python3 -m engine` as a real subprocess — real uvicorn, real sockets,
real HTTP auth, one genuine WebSocket — and asserts only what a user could see:

1. a `SIGTERM` with an event stream open ends the engine within the grace
   (`engine/watchdog.py::HARD_EXIT_GRACE_S`), without this test killing anything;
2. it ends *itself*: the port is bindable again and the stream the test was holding is
   closed by the engine rather than abandoned;
3. the same shape when the stop comes from a dying parent instead: an engine whose
   shell is killed dies with it, bounded the same way, and says which pid it was
   watching — the case where nothing is left to ask a second time, and where the
   engine has to notice for itself.

The goal created here exists only so the stream has an id to attach to; the run it
spawns is not what is under test, and nothing waits for it. `docs/04` §6.1 is the
contract; this is its end-to-end half. Nothing skips itself out of a guarantee:
`websockets` is a declared dependency (uvicorn serves no WebSocket without one), and
every engine here runs against a throwaway `CODIFY_HOME`.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, NamedTuple

import httpx
import websockets

from engine import watchdog

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The bound under test is the engine's own grace plus room for a loaded machine. The
# assertion is that nothing *unbounded* happens: the behaviour this file pins let the
# engine outlive the test itself, so any budget catches a regression.
DEATH_BUDGET_S = watchdog.HARD_EXIT_GRACE_S + 3.0
# A watched engine learns of its parent's death on a poll, so its death is the poll
# plus the same budget.
PARENT_DEATH_BUDGET_S = watchdog.POLL_INTERVAL_S + DEATH_BUDGET_S

# A shell's shape, in one process: launch the engine, say which pid, then sit there
# until something kills it. The engine watches *this* pid, and the child is left
# orphaned exactly as it is when a window dies without running its exit handler. The
# engine inherits the launcher's stdout, so the boot handshake arrives on this pipe
# along with the pid line printed first.
_LAUNCHER_SOURCE = f"""
import os
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-m", "engine"],
    cwd={str(PROJECT_ROOT)!r},
    env=dict(os.environ, CODIFY_PARENT_PID=str(os.getpid())),
)
print("ENGINE_PID", child.pid, flush=True)
time.sleep(300)
"""


class _Boot(NamedTuple):
    """A booted engine and the two pipes its death will be read off."""

    proc: subprocess.Popen[bytes]
    token: str
    port: int
    stdout_lines: list[bytes]
    stderr_lines: list[bytes]


def _free_port() -> int:
    """An ephemeral port, released for the boot path to bind.

    `main()` really binds and listens, so a hard-coded port makes the suite unrunnable
    for anyone with the app open. The probe-release-rebind gap is a TOCTOU this
    harness cannot remove, which is why `_boot` retries with a fresh port.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _pump(stream: IO[bytes], sink: Callable[[bytes], None]) -> None:
    """Drain a pipe on a daemon thread, one line at a time, EOF as an empty line.

    A plain `readline()` blocks until a line arrives — fine for a healthy boot, a
    wedged test for a silent one. Draining on a thread also keeps the engine from ever
    blocking on a full pipe, which would be a stall this test could not explain.
    """

    def run() -> None:
        try:
            for line in stream:
                sink(line)
        finally:
            sink(b"")

    threading.Thread(target=run, daemon=True).start()


def _spawn_engine(home: Path, port: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "engine"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "CODIFY_HOME": str(home), "CODIFY_PORT": str(port)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _spawn_behind_a_parent(home: Path, port: int) -> subprocess.Popen[bytes]:
    """The desktop topology: a parent that launches the engine and is not asked."""
    return subprocess.Popen(
        [sys.executable, "-c", _LAUNCHER_SOURCE],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "CODIFY_HOME": str(home), "CODIFY_PORT": str(port)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _spawner(home: Path, behind_a_parent: bool) -> Callable[[int], subprocess.Popen[bytes]]:
    """The port-taking spawner `_boot` drives, for either of the two topologies."""
    spawn = _spawn_behind_a_parent if behind_a_parent else _spawn_engine
    return lambda port: spawn(home, port)


def _handshake(lines: list[bytes]) -> str | None:
    for raw in lines:
        text = raw.decode(errors="replace").strip()
        if text.startswith("CODIFY_ENGINE"):
            return text
    return None


def _engine_pid(lines: list[bytes]) -> int:
    for raw in lines:
        text = raw.decode(errors="replace").strip()
        if text.startswith("ENGINE_PID"):
            return int(text.split()[1])
    raise AssertionError(f"the launcher never said which engine it started: {_tail(lines)}")


def _tail(lines: list[bytes], limit: int = 24) -> str:
    text = [raw.decode(errors="replace").rstrip() for raw in lines if raw]
    return "\n".join(text[-limit:])


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL whatever is left, reap it, and close its pipes.

    Deliberately harsher than the app: a closed window asks the engine to stop and
    kills it only if it will not (`docs/09` §5.4.1), whereas a teardown has to be
    certain, and a failed assertion must never leave an engine holding a port or a
    database.
    """
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None and not stream.closed:
            stream.close()


async def _boot(
    spawn: Callable[[int], subprocess.Popen[bytes]], timeout: float = 20.0
) -> _Boot:
    """Boot a real engine through `spawn` and read `CODIFY_ENGINE token=… port=…`.

    A boot death — or a handshake that never arrives — is retried once with a fresh
    process and a fresh port; a second failure is reported with the engine's own
    stderr, which is all a dead boot leaves behind.
    """
    detail = ""
    for _ in (1, 2):
        port = _free_port()
        proc = spawn(port)
        assert proc.stdout is not None and proc.stderr is not None, "spawned with pipes"
        out: list[bytes] = []
        err: list[bytes] = []
        _pump(proc.stdout, out.append)
        _pump(proc.stderr, err.append)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = _handshake(out)
            if line is not None:
                fields = dict(p.split("=", 1) for p in line.split()[1:])
                return _Boot(proc, fields["token"], int(fields["port"]), out, err)
            if proc.poll() is not None or b"" in out:
                break  # died at boot, or its pipe closed: no line is coming
            await asyncio.sleep(0.05)
        _reap(proc)
        detail = _tail(err)
    raise AssertionError(f"the engine never printed its boot handshake in {timeout:.0f}s:\n{detail}")


def _port_free(port: int) -> bool:
    """Whether `port` can be bound again, which means the engine's socket is gone.

    `SO_REUSEADDR` is not optional here: the engine sets it (`main()`), and without it
    a bind also fails against a connection this test left in TIME_WAIT — which would
    report a released port as a held one.
    """
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _alive(pid: int) -> bool:
    """Whether `pid` is still a process doing something.

    An orphaned engine that has already exited can sit as a zombie until whoever
    inherited it reaps it: dead, holding no socket and no database lock, but still
    present in `/proc`. Reading the state letter is what tells those two apart.
    """
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split(") ", 1)[1][0]
    except (FileNotFoundError, IndexError):
        return False
    return state != "Z"


async def _await_death(proc: subprocess.Popen[bytes], err: list[bytes], budget: float) -> float:
    """How long the engine took to end on its own, or a failure carrying its stderr."""
    started = time.monotonic()
    while time.monotonic() - started < budget:
        if proc.poll() is not None:
            return time.monotonic() - started
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"the engine (pid {proc.pid}) was still alive {budget:.1f}s after it was told to "
        f"stop — a stop request with no bound behind it:\n{_tail(err)}"
    )


async def _open_stream(port: int, token: str, home: Path) -> tuple[str, Any]:
    """A goal to attach a stream to, plus the stream, proven live.

    The goal exists only so the stream has an id to attach to — the run it spawns is
    not what is under test and nothing waits for it. The client is closed again
    before this returns, so the only thing left on the wire is the stream: what the
    engine is about to be asked to wait for has to be its own event stream, not this
    test's HTTP keep-alive.

    Auth is the browser shape — the token is an auth *message*, since a browser cannot
    set headers — and a pong is the receipt that the connection is served rather than
    merely accepted.
    """
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    ) as client:
        workspace = (
            await client.post("/workspaces", json={"name": "ws", "root_path": str(home / "ws")})
        ).json()
        goal = (
            await client.post(
                "/goals",
                json={
                    "workspace_id": workspace["id"],
                    "title": "shutdown",
                    "description": "a run nobody waits for: the goal exists so the "
                    "stream has an id to attach to",
                },
            )
        ).json()
        wire = await websockets.connect(f"ws://127.0.0.1:{port}/ws/goals/{goal['id']}")
        await wire.send(json.dumps({"type": "auth", "token": token}))
        pong = await asyncio.wait_for(wire.ping(), timeout=5.0)
        await asyncio.wait_for(pong, timeout=5.0)
    return str(goal["id"]), wire


async def _await_wire_close(wire: Any, timeout: float = 5.0) -> None:
    """The engine is gone, so the stream has to end — one way or another.

    Every close path raises (a closed frame, then EOF, then the connection closing),
    so the receipt is "it stopped arriving", and a stream that keeps arriving fails on
    the timeout instead of passing quietly.
    """

    async def drain() -> None:
        try:
            while True:
                await wire.recv()
        except Exception:  # the close itself is the signal, however it arrives
            return

    await asyncio.wait_for(drain(), timeout=timeout)


class EngineDeathE2E(unittest.IsolatedAsyncioTestCase):
    """A stop request ends the process. Bounded, and with nothing left holding on."""

    async def test_a_signal_with_a_stream_open_ends_the_engine_within_the_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / "ws").mkdir(parents=True)
            boot = await _boot(_spawner(home, behind_a_parent=False))
            err: list[bytes] = boot.stderr_lines
            wire: Any = None
            try:
                _, wire = await _open_stream(boot.port, boot.token, home)
                self.assertFalse(
                    _port_free(boot.port), "the engine is holding the port before the signal"
                )
                boot.proc.send_signal(signal.SIGTERM)
                elapsed = await _await_death(boot.proc, err, DEATH_BUDGET_S)
                self.assertNotEqual(
                    -signal.SIGKILL,
                    boot.proc.returncode,
                    f"the engine must end on its own; nothing here kills it:\n{_tail(err)}",
                )
                self.assertTrue(
                    _port_free(boot.port),
                    f"the port was still held {elapsed:.1f}s after the stop:\n{_tail(err)}",
                )
                await _await_wire_close(wire)
            finally:
                if wire is not None:
                    await wire.close()
                _reap(boot.proc)

    async def test_the_engine_dies_with_the_parent_it_watches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / "ws").mkdir(parents=True)
            boot = await _boot(_spawner(home, behind_a_parent=True))
            err = boot.stderr_lines
            engine_pid = _engine_pid(boot.stdout_lines)
            wire: Any = None
            try:
                self.assertTrue(
                    _alive(engine_pid), f"the launcher started no engine:\n{_tail(err)}"
                )
                # The same shape as above — a stream the engine is still serving — so
                # this asks the harder question: is a shell's death bounded too, or
                # only a signal the engine can answer for itself?
                _, wire = await _open_stream(boot.port, boot.token, home)
                # What a crash looks like: the parent cannot tidy up, run its exit
                # handler, or kill anything on the way out.
                boot.proc.send_signal(signal.SIGKILL)
                boot.proc.wait(timeout=10)
                started = time.monotonic()
                while time.monotonic() - started < PARENT_DEATH_BUDGET_S and _alive(engine_pid):
                    await asyncio.sleep(0.05)
                self.assertFalse(
                    _alive(engine_pid),
                    f"the engine outlived the shell by more than "
                    f"{PARENT_DEATH_BUDGET_S:.1f}s:\n{_tail(err, limit=200)}",
                )
                self.assertIn(
                    f"parent process {boot.proc.pid} is gone",
                    _tail(err, limit=200),
                    "the engine has to notice the parent itself — nothing else can tell it",
                )
                self.assertTrue(
                    _port_free(boot.port),
                    f"the port outlived the engine:\n{_tail(err, limit=200)}",
                )
                await _await_wire_close(wire)
            finally:
                if wire is not None:
                    await wire.close()
                if _alive(engine_pid):
                    os.kill(engine_pid, signal.SIGKILL)
                _reap(boot.proc)
