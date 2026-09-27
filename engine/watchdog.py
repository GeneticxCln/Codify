"""Take the engine down when the desktop shell that spawned it goes down.

The Tauri shell asks its child to stop on the way out — `SIGTERM`, then `SIGKILL`
if this module's deadline is not what ends it (`09` §5.4.1). A signal does
neither: `kill` the window and the engine keeps running, holding the port it
announced and the SQLite file behind it — and the next launch then has to fight
it for both. Nor does a shell that dies mid-request, which never gets to send the
signal at all. The shell cannot fix that from its side (by the time the engine is
orphaned, the shell is gone), so the engine watches the shell instead: the
launcher passes its own pid in `CODIFY_PARENT_PID`.

Only the desktop shell arms this. A standalone `python3 -m engine` (`make
run-engine`, the browser build) is deliberately left unwatched, because its parent
is a shell the user may close while meaning to leave the engine up — the engine
surviving its terminal is the behavior every run had before this existed.

Watching is not enough on its own. A stop request only *starts* a shutdown, and
that shutdown is uvicorn's: it waits for in-flight work with no deadline unless it
is given one, and this engine's in-flight work is a turn against a model (minutes,
on a local one) or an event stream a window holds open. So the observed orphan was
not a missing signal — the port was released within a second — but a process that
had shut the socket down and was still waiting to hear back from a model, holding
the SQLite file the whole time. Both ways in (the parent dying, and a SIGTERM from
anyone else) now arm a deadline: if the graceful path has not finished when the
grace runs out, the process ends regardless, through `hard_exit` and not through
interpreter finalization.

Testable without killing anything: the polling loop takes both its "are they gone"
predicate and its "they are gone" handler as arguments, so the suite drives the
whole state machine with fakes and exercises the real predicates against real pids.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping

ENV_PARENT_PID = "CODIFY_PARENT_PID"

# One second: the check is a `kill(pid, 0)` and a `getppid()`, so being prompt costs
# nothing worth trading for — and the alternative to being prompt is a stale engine
# holding the port across a relaunch.
POLL_INTERVAL_S = 1.0

# uvicorn's own window for in-flight work. Its default is "wait forever", which is
# the defect this constant exists for: a shutdown that waits on an open event stream
# holds the database for as long as the stream is open, and the next launch finds the
# port free and the file behind it still held. Whole seconds because uvicorn's own
# `timeout_graceful_shutdown` is typed as one.
GRACEFUL_SHUTDOWN_S = 3

# The deadline has to outlast that window, or it would cut off a shutdown that was
# about to finish on its own. Past it nothing in this process is trusted to end it.
#
# Announced to the shell on the boot handshake (`CODIFY_ENGINE … hard_exit_s=…`),
# which is how the desktop shell knows how long to wait after it asks this process
# to stop before escalating to SIGKILL. One number, two languages: a copy in the
# shell would be free to drift, and drift here means a turn killed mid-record.
HARD_EXIT_GRACE_S = GRACEFUL_SHUTDOWN_S * 2


def parent_pid_from_env(env: Mapping[str, str] | None = None) -> int | None:
    """The pid to watch, or `None` when this run is not the desktop shell's child.

    `None` covers both "the variable is absent" and "it is garbage" — neither is
    worth refusing to boot over, since an unwatched engine is exactly what every
    run did before this module existed.
    """
    raw = (os.environ if env is None else env).get(ENV_PARENT_PID, "").strip()
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def process_exists(pid: int) -> bool:
    """Whether `pid` names a live process. Signal 0 checks without sending anything."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, and not ours to signal. Only reachable for a pid handed in from
        # outside, which is still not a reason to take the engine down.
        return True
    return True


def parent_is_gone(
    parent_pid: int,
    *,
    parent_now: Callable[[], int] = os.getppid,
    exists: Callable[[int], bool] = process_exists,
) -> bool:
    """Whether the process we were told to watch has exited.

    Two questions, because each has a hole the other covers. `getppid()` is the
    kernel saying we were reparented, which happens the instant our parent dies —
    and keeps saying it even if the pid is reused, which is precisely the case a
    pid-existence check gets wrong. `kill(pid, 0)` answers about the pid directly,
    which is what remains if this process was never that pid's child (the variable
    is inherited, so a grandchild can hold it).
    """
    return parent_now() != parent_pid or not exists(parent_pid)


def hard_exit(code: int = 0) -> None:
    """End this process without letting the interpreter tidy up first.

    `os._exit`, not a return from `main()`: finalization joins every non-daemon
    thread, and the threads left here are exactly the ones nobody can cancel —
    `asyncio.to_thread` workers waiting on a sandboxed command or the native folder
    picker. That join is where an orphan came from: the signal was delivered, the
    socket was closed, and the process stayed in the join. `os._exit` skips it, so
    the deadline cannot be defeated by anything the process is still waiting on.

    The streams are flushed by hand because this path leaves no other chance to, and
    a broken pipe is not a reason to keep holding the database.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)


def arm_hard_deadline(
    grace_s: float = HARD_EXIT_GRACE_S,
    *,
    exit_fn: Callable[[], None] | None = None,
) -> threading.Timer:
    """Start the countdown that ends this process even if nothing else does.

    The timer is a daemon, so it can never itself be the reason a run lingers; it is
    returned so a caller can cancel a grace it no longer needs. The default handler
    says one line on the way out: an engine that gives up in silence is
    indistinguishable, in the shell's log, from one that was killed.
    """

    def fired() -> None:
        print(
            f"[engine] shutdown unfinished after {grace_s:g}s — exiting anyway",
            file=sys.stderr,
            flush=True,
        )
        hard_exit()

    timer = threading.Timer(grace_s, exit_fn if exit_fn is not None else fired)
    timer.daemon = True
    timer.start()
    return timer


def terminate(parent_pid: int) -> None:
    """The default death handler: say why, then ask ourselves to stop — on a clock.

    SIGTERM rather than `os._exit` first, because with the server up uvicorn turns it
    into a normal shutdown: the socket is closed and the lifespan hooks run. Before
    the server is up nothing has installed a handler, and the signal's default action
    ends the process just as promptly. Either way the port is released by the kernel.

    The deadline is armed before the signal rather than after it, and that is the
    load-bearing half: this is the path a killed window takes, nobody is left to ask
    a second time, and a shutdown waiting on in-flight work would otherwise run for as
    long as the work does.
    """
    print(
        f"[engine] parent process {parent_pid} is gone — exiting",
        file=sys.stderr,
        flush=True,
    )
    arm_hard_deadline()
    os.kill(os.getpid(), signal.SIGTERM)


def watch(
    parent_pid: int,
    *,
    interval_s: float = POLL_INTERVAL_S,
    on_gone: Callable[[int], None],
    is_gone: Callable[[int], bool] = parent_is_gone,
) -> None:
    """Poll the watched pid and hand over to `on_gone` once it is gone."""
    while not is_gone(parent_pid):
        time.sleep(interval_s)
    on_gone(parent_pid)


def start_parent_watchdog(
    parent_pid: int | None = None,
    *,
    interval_s: float = POLL_INTERVAL_S,
    on_parent_death: Callable[[int], None] | None = None,
    is_gone: Callable[[int], bool] = parent_is_gone,
) -> threading.Thread | None:
    """Arm the watchdog; `None` when there is no parent to watch.

    The thread is a daemon, so it can never be the reason the interpreter stays up.
    Stderr, not stdout: stdout is the boot handshake the desktop shell parses line by
    line, and this line is not part of that contract.
    """
    if parent_pid is None:
        parent_pid = parent_pid_from_env()
    if parent_pid is None:
        return None
    print(
        f"[engine] parent watchdog armed on pid {parent_pid} (poll {interval_s:g}s)",
        file=sys.stderr,
        flush=True,
    )
    thread = threading.Thread(
        target=watch,
        args=(parent_pid,),
        kwargs={
            "interval_s": interval_s,
            "on_gone": on_parent_death or terminate,
            "is_gone": is_gone,
        },
        name="parent-watchdog",
        daemon=True,
    )
    thread.start()
    return thread
