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

Two holes the first version left, both found by reading the killed window as a
*process* rather than as a person:

* The handler's first act was a stderr print — and the engine's stderr is a pipe
  read by the shell. After a hard shell death that read end is gone, so the print
  raised `BrokenPipeError` and the thread died holding the news: no deadline armed,
  no SIGTERM sent, an engine that outlived the shell forever — the very orphan this
  module exists to prevent, and invisible in the log, because the log *was* the
  pipe that died. The handler now ends the process first and explains itself
  afterwards, with the explanation guarded against the pipe it may find dead.
* It armed only in `serve()`, which runs after the heavy imports; a shell dying
  while the engine was still importing left an engine with nothing armed. The
  watch therefore starts at the entrypoint, before the first import — see
  `engine/__main__.py` — and `arm_death_signal` is the handler both arming
  points share.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping

ENV_PARENT_PID = "CODIFY_PARENT_PID"

# The production-shaped watcher this process already has, if any. The entrypoint
# starts the watch before the heavy imports, and `serve()` calls in again
# afterwards; without this the second call would run two pollers and arm two
# deadlines on the same death. Only watchers with the default poll and predicate
# are memoized — the tests drive the loop with custom intervals and predicates
# and must be able to start one repeatedly.
_watchdog_thread: threading.Thread | None = None

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


def start_hard_deadline_timer(
    grace_s: float = HARD_EXIT_GRACE_S,
    *,
    exit_fn: Callable[[], None] | None = None,
) -> threading.Timer:
    """The daemon timer behind both arming points: the shutdown deadline and death.

    If anything on those paths wants a different bound, there is one number to
    change — not two that are free to drift apart.
    """
    def fired() -> None:
        try:
            print(
                f"[engine] shutdown unfinished after {grace_s:g}s — exiting anyway",
                file=sys.stderr,
                flush=True,
            )
        except OSError:
            # The same dead pipe `terminate` guards: by deadline time the shell's
            # reader may be long gone, and the exit is the part that must land.
            pass
        hard_exit()

    timer = threading.Timer(grace_s, exit_fn if exit_fn is not None else fired)
    timer.daemon = True
    timer.start()
    return timer


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

    Kept as the whole shape (`start_hard_deadline_timer` is only its engine room) —
    a test that replaces this function wholesale is asserting on the wiring the
    handlers actually use, not on a private detail.
    """
    return start_hard_deadline_timer(grace_s, exit_fn=exit_fn)


def arm_death_signal() -> bool:
    """SIGTERM ourselves on a deadline, before anything can print on a dead pipe.

    The caller is a handler whose parent just died — likely hard, with the shell's
    end of the stderr pipe gone with it — so the deadline is armed and the signal
    sent *before* any write. Nothing has installed a SIGTERM handler before the
    server starts, so its default action ends the process at once; after that, the
    handler is free to say why.

    Inert for a run with no parent-watch contract in the environment: a standalone
    `python3 -m engine` is deliberately left unwatched (`make run-engine`, the
    browser build), and this must not turn that into a boot that kills itself.
    The check happens here rather than at the callers so there is one place that
    knows the gate.

    `False` when the timer could not be started (fd exhaustion) or the signal
    could not be sent: the caller then says so instead of promising a bound it
    does not have. Returns `True` when there is nothing to arm (standalone) or
    the process has asked itself to stop.

    The entrypoint arms the watch before the heavy imports
    (`engine/__main__.py`), because `serve()` arms it only after them — a
    window in which a dying shell left an engine that was not yet watching
    anything. Death cannot wait on imports.
    """
    if parent_pid_from_env() is None:
        return True
    try:
        start_hard_deadline_timer()
    except (OSError, RuntimeError):
        return False
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except OSError:
        return False
    return True


def on_parent_gone(parent_pid: int) -> None:
    """The production death handler: end the engine, then say why if it still can.

    Two arming points want this handler — the entrypoint, before the heavy
    imports, and `serve()` once the server is up — and neither wants to repeat
    the adaptation. `_watch` hands the pid to the handler and ignores whatever
    comes back; `arm_death_signal` takes no argument and reports whether it
    actually armed, which is the answer its own caller wants and this one has
    no use for. So the pid is dropped and the bool discarded, once, here.

    The receipt comes *after* `arm_death_signal` on purpose, and guarded: with
    the server up, uvicorn's SIGTERM handler keeps the process alive through
    its graceful shutdown, so the line lands — this is the log's only proof the
    engine noticed the death itself, which is exactly what a lone orphaned
    engine can never show. With the shell dead the pipe has no reader, and the
    write is the hazard that caused the orphan in the first place; the guard
    makes a log line unable to reverse an already-delivered stop.
    """
    arm_death_signal()
    try:
        print(
            f"[engine] parent process {parent_pid} is gone — exiting",
            file=sys.stderr,
            flush=True,
        )
    except OSError:
        pass


def terminate(parent_pid: int) -> None:
    """The default death handler: ask ourselves to stop on a clock, then say why.

    The order is the fix. The first version printed first, and after a SIGKILLed
    shell that print is a `BrokenPipeError` — the shell's end of the stderr pipe
    died with it — which killed this thread with the deadline unarmed and the
    SIGTERM unsent. The engine outlived the shell forever: the exact orphan this
    module exists to prevent, caused by the module's own log line. So the deadline
    is armed and the signal delivered *first*; the explanation follows, guarded
    against the pipe it may find dead.

    SIGTERM rather than `os._exit` first, because with the server up uvicorn turns
    it into a normal shutdown: the socket is closed and the lifespan hooks run.
    Before the server is up nothing has installed a handler, and the signal's default
    action ends the process just as promptly. Either way the port is released by the
    kernel. The deadline is armed before the signal rather than after it because
    this is the path a killed window takes, nobody is left to ask a second time,
    and a shutdown waiting on in-flight work would otherwise run for as long as the
    work does.
    """
    arm_hard_deadline()
    os.kill(os.getpid(), signal.SIGTERM)
    try:
        print(
            f"[engine] parent process {parent_pid} is gone — exiting",
            file=sys.stderr,
            flush=True,
        )
    except OSError:
        # The shell died hard, so its end of the stderr pipe died with it and this
        # line has nowhere to go. The process has already asked itself to stop with
        # a deadline behind it; a log line must not be able to reverse that.
        pass


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
    """Arm the watchdog; `None` when there is no parent to watch or one is armed.

    The thread is a daemon, so it can never be the reason the interpreter stays up.
    Stderr, not stdout: stdout is the boot handshake the desktop shell parses line by
    line, and this line is not part of that contract.

    One production-shaped watcher per process. `engine/__main__.py` starts the
    watch before the heavy imports — the boot window is exactly when a dying
    shell is otherwise invisible — and `serve()` then calls this again on the
    general principle that the server must not depend on its entrypoint having
    done so. The second call finds a watcher alive and leaves it: two pollers
    would arm two deadlines on death, and a re-armed poller would forget the
    first thread's silence. "Production-shaped" means the default poll interval
    and predicate; callers passing their own (the tests) always get a thread.
    """
    global _watchdog_thread
    existing = _watchdog_thread
    if (
        interval_s == POLL_INTERVAL_S
        and is_gone is parent_is_gone
        and existing is not None
        and existing.is_alive()
    ):
        return None
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
    _watchdog_thread = thread
    return thread
