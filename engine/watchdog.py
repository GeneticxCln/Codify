"""Take the engine down when the desktop shell that spawned it goes down.

The Tauri shell kills its child on the way out and when its `Child` handle is
dropped. A signal does neither: `kill` the window and the engine keeps running,
holding the port it announced and the SQLite file behind it — and the next launch
then has to fight it for both. The shell cannot fix that from its side (by the time
the engine is orphaned, the shell is gone), so the engine watches the shell instead:
the launcher passes its own pid in `CODIFY_PARENT_PID`.

Only the desktop shell arms this. A standalone `python3 -m engine` (`make
run-engine`, the browser build) is deliberately left unwatched, because its parent
is a shell the user may close while meaning to leave the engine up — the engine
surviving its terminal is the behavior every run had before this existed.

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


def terminate(parent_pid: int) -> None:
    """The default death handler: say why, then ask ourselves to stop.

    SIGTERM rather than `os._exit`: with the server up, uvicorn's handler turns it
    into a normal shutdown — the socket is closed and the event loop unwinds. Before
    the server is up nothing has installed a handler, and the signal's default action
    ends the process just as promptly. Either way the port is released by the kernel.
    """
    print(
        f"[engine] parent process {parent_pid} is gone — exiting",
        file=sys.stderr,
        flush=True,
    )
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
