"""Run one spawned command under a guard that dies with the engine.

Both ends of that contract live here: the launcher side (`guarded_argv`/`guarded_env`,
used by the sandbox and by git) and the guard itself, which runs as the command's
parent. A caller that builds `[python, spawn_guard.py, …]` by hand is a caller that can
get the pid handover or the group leadership wrong, so no caller does.

A verifier's command gets a session of its own, so a timeout can signal the whole tree
without touching the engine. The other direction has no such mechanism: kill the
engine — a closed window SIGKILLs it, the watchdog TERMs it, a crash ends it — and the
command keeps its session, keeps its port, and keeps writing to a workspace nobody
supervises any more. No kernel facility reaps an orphaned process group, so this file
does it: the engine spawns it as the command's parent and session leader, and it takes
the whole group down when the engine that spawned it is gone.

Two detectors, because neither is enough alone:

* `prctl(PR_SET_PDEATHSIG)` (Linux) — the kernel signals this process when the thread
  that spawned it dies. Instant, but it is Linux-only *and* it is armed on a thread,
  so the poll below is what makes the outcome independent of which thread forked.
* a parent check on every wait tick (everywhere) — `getppid()` changes the instant the
  engine dies, whatever killed it. Checking on the *wait tick* rather than in a thread
  keeps this process single-threaded, and the pid handed over in the environment closes
  the fork/exec window: a guard that starts after the engine already died sees a parent
  it was not told to expect and kills the group immediately.

The command's own outcome is reproduced exactly. A normal exit is re-reported as the
same code, and a command killed by signal N is re-raised on this process, so the engine
still reads the negative `Popen.returncode` it read before this guard existed — the
verifier's prompt quotes that number. A parent death is the one case the command does
not get to choose: that is a group kill.

**Resource limits.** A command can also be started under `Limits` (address space, CPU seconds, file size, core size),
which the guard applies to *itself* with `setrlimit` just before it starts the command, so the command inherits
them and cannot raise them past the hard limit. They are opt-in per spawn site (`guarded_env(env, limits=...)`) because
the right numbers differ: a Python worker has a sane address-space cap, a JVM or Node reserves far more virtual memory than
it uses and must be capped by its own heap flag instead, and a build tool writes large files. Nothing is applied that the
engine's own limits already forbid: a request above the inherited hard limit is clamped to it, never raised.

Stdin, stdout, stderr, the working directory and the environment are all inherited from
the engine, unchanged: the guard passes its own handles to the command and prints
nothing on the success path, so the captured output is the command's own.
"""

from __future__ import annotations

import ctypes
import json
import os
import resource
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# The engine's own pid, handed to every guard. Absent only for a guard started by hand.
ENV_PARENT_PID = "CODIFY_SANDBOX_PARENT_PID"

# The guard script, by absolute path. Never `-m engine.spawn_guard`: the command runs
# with the workspace (or the repository) as its working directory, which is not on
# `sys.path`, and with an environment that may carry no PYTHONPATH at all.
SPAWN_GUARD = str(Path(__file__).resolve())


# The limits a spawn site asked for, as JSON, for the guard to apply to itself before it starts the command.
ENV_LIMITS = "CODIFY_SPAWN_LIMITS"


@dataclass(frozen=True)
class Limits:
    """What a spawned command may not exceed. `None` leaves that resource as the engine has it.

    * `address_space` — bytes of virtual memory (`RLIMIT_AS`). A process that asks for more gets `MemoryError` /
      `ENOMEM`. Not for a JVM or Node: they reserve far more than they use.
    * `cpu_seconds` — CPU time (`RLIMIT_CPU`): SIGXCPU at the soft limit, SIGKILL shortly after. A second line behind the
      wall-clock timeout for a process that is alive but burning a core in a loop that ignores TERM.
    * `file_bytes` — the largest file it may write (`RLIMIT_FSIZE`): the write fails with SIGXFSZ / `EFBIG`. A disk filler
      stops at this, not at the disk.
    * `core_bytes` — core dump size. 0 is the useful value: a crashing child leaves no multi-GiB file behind.
    """

    address_space: int | None = None
    cpu_seconds: int | None = None
    file_bytes: int | None = None
    core_bytes: int | None = None

    def to_env(self) -> str:
        return json.dumps({k: v for k, v in (
            ("as", self.address_space), ("cpu", self.cpu_seconds), ("fsize", self.file_bytes), ("core", self.core_bytes),
        ) if v is not None})


_RESOURCES = {
    "as": resource.RLIMIT_AS,
    "cpu": resource.RLIMIT_CPU,
    "fsize": resource.RLIMIT_FSIZE,
    "core": resource.RLIMIT_CORE,
}


def limits_from_env(env: Mapping[str, str] | None = None) -> dict[str, int]:
    """The limits the launcher asked for, validated: known names, non-negative integers. Anything else is ignored."""
    raw = (os.environ if env is None else env).get(ENV_LIMITS, "").strip()
    if not raw:
        return {}
    try:
        asked = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(asked, dict):
        return {}
    return {
        name: value for name, value in asked.items()
        if name in _RESOURCES and isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }


def apply_limits(asked: Mapping[str, int]) -> dict[str, int]:
    """Set each limit on this process (soft and hard), never above what it already has; return what was set.

    A request above the inherited hard limit is clamped to it. A resource the kernel refuses to lower is skipped, and the
    guard says so on stderr: a limit that silently did not apply is the failure this exists to prevent.
    """
    applied: dict[str, int] = {}
    for name, wanted in asked.items():
        which = _RESOURCES[name]
        _soft, hard = resource.getrlimit(which)
        value = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError) as exc:
            print(f"[spawn-guard] could not apply the {name} limit {wanted}: {exc}", file=sys.stderr, flush=True)
            continue
        applied[name] = value
    return applied


def guarded_argv(argv: list[str]) -> list[str]:
    """`argv` one process deeper: the guard leads, the command follows.

    The caller still owns the two process facts the guard cannot infer for itself:
    `start_new_session=True` (the guard must lead the group it kills) and
    `guarded_env`.
    """
    if argv[:2] == [sys.executable, SPAWN_GUARD]:
        # Already guarded (a caller that wrapped it, handing it to a helper that wraps at the spawn):
        # a guard in front of a guard would put the inner one in a group it does not lead.
        return list(argv)
    return [sys.executable, SPAWN_GUARD, *argv]


def guarded_env(env: Mapping[str, str] | None = None, limits: Limits | None = None) -> dict[str, str]:
    """`env` (or this process's environment) carrying this process's pid, and the limits the command runs under.

    The pid is read at call time, never cached: the process spawning the guard is the
    one whose death the guard must notice, and that is not the process that imported
    this module. `limits` is applied by the guard to the command and nothing else; without it the
    command runs with the limits the engine has (a stale `CODIFY_SPAWN_LIMITS` in `env` is dropped).
    """
    out = {**(os.environ if env is None else env), ENV_PARENT_PID: str(os.getpid())}
    out.pop(ENV_LIMITS, None)
    if limits is not None:
        asked = limits.to_env()
        if asked != "{}":
            out[ENV_LIMITS] = asked
    return out

# Half a second: the poll only decides how long a *hard-killed* engine's commands
# linger — on Linux the death signal has already fired by then — and cheap is the
# point, because this ticks for as long as the command runs.
POLL_INTERVAL_S = 0.5

# prctl(2) option number for "signal me when my parent dies". Not exposed by any
# stdlib module; the call is here because there is no portable spelling of it.
PR_SET_PDEATHSIG = 1


def parent_pid_from_env(env: dict[str, str] | None = None) -> int | None:
    """The engine pid the guard was handed, or `None` when there is none to trust."""
    raw = (os.environ if env is None else env).get(ENV_PARENT_PID, "").strip()
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def arm_parent_death_signal() -> bool:
    """Ask the kernel to TERM this process when its parent dies. Linux only.

    SIGTERM rather than SIGKILL because the handler has work to do: it is the whole
    point of this process that it outlives the engine long enough to kill the group.
    Returns whether the request was made, so the tests can tell the two situations
    apart instead of guessing.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
    except (OSError, AttributeError):
        # No libc handle, or no prctl in it: the poll is the whole detector.
        return False
    try:
        prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except (OSError, ValueError):
        return False
    return True


class Guard:
    """The command's session leader, and the engine's death its exit condition."""

    def __init__(self, expected_parent: int | None = None) -> None:
        self.expected_parent = expected_parent
        # Who the engine (or whoever launched the guard) actually is right now. The
        # comparison below is against this, not against the environment: a guard
        # started by hand must still die with whoever started it.
        self.parent = os.getppid()

    def engine_is_gone(self) -> bool:
        """Whether the process this guard was spawned by has exited.

        `getppid()` is the kernel saying we were reparented — the one answer that
        stays true even if the engine's pid is reused by someone else a moment later.
        """
        return os.getppid() != self.parent

    def started_after_the_engine_died(self) -> bool:
        """Whether the engine was already gone before this guard could be armed.

        The fork/exec window is small but real, and a death in it is invisible to
        both detectors: no signal was delivered (nothing was armed yet) and nothing
        will ever change `getppid()` again. The pid the engine passed is what makes
        that window visible at all.
        """
        return self.expected_parent is not None and self.parent != self.expected_parent

    def kill_group(self) -> None:
        """SIGKILL every process in this guard's group — itself included.

        The guard is the session leader (`start_new_session` in the engine's Popen),
        so the group is the command and everything it spawned. SIGKILL rather than
        TERM: the engine is gone, so there is nobody left to escalate, and a command
        that ignores TERM would outlive us all. This never returns.

        **The leadership check is the whole reason this is safe to call at all.**
        `guarded_argv` cannot supply `start_new_session` itself — it is the
        caller's flag on the Popen — so a guard spawned without it shares the
        engine's process group, and `killpg(getpgrp(), SIGKILL)` would then be a
        SIGKILL to the engine, the desktop shell, and every sibling in that group:
        the guard killing the thing it exists to outlive. Comparing the group to
        our own pid is the only question that answers "is this group mine?", and a
        group we do not lead is a group we must not signal. We still leave — the
        command's pipes have to stop being held — but we leave alone.

        The refusal is announced rather than silent, because in production it means
        a spawn site has lost `start_new_session=True` and that is a bug worth
        seeing in the log rather than inferring from a missing kill.
        """
        group = os.getpgrp()
        if group == os.getpid():
            try:
                os.killpg(group, signal.SIGKILL)
            finally:
                # Belt for the impossible case: SIGKILL is not blockable, but if
                # this process is somehow still here, holding the command's pipes
                # open and doing nothing is the one outcome that must not happen.
                self._leave(1)
        else:
            print(
                f"[spawn-guard] pid {os.getpid()} does not lead process group {group}; "
                "refusing to signal it and leaving the command to its own timeout",
                file=sys.stderr,
                flush=True,
            )
            self._leave(1)

    def _leave(self, code: int) -> None:
        """End this guard. A seam so `kill_group` is callable from a test.

        `os._exit` through the module attribute rather than a bound default, so
        the suite's standing `patch.object(os, "_exit")` is what intercepts it —
        the same substitution every other exit site in the engine relies on, and
        the reason this one is not written as `os._exit` inline.
        """
        os._exit(code)

    def on_term(self, _signum: int, _frame: object) -> None:
        """The engine's TERM: a group timeout, or the engine's death. Tell them apart.

        A timeout signals the whole group, this guard included, and escalating to
        SIGKILL there would cut short the grace the engine deliberately leaves for a
        test runner to shut its workers down — so a TERM whose sender is still alive
        forwards nothing and the command answers it. A TERM that arrives because the
        engine died is the one case that gets the group kill.
        """
        if self.engine_is_gone():
            self.kill_group()

    def wait_for(self, command: subprocess.Popen[bytes]) -> int:
        """Wait for the command, watching for the engine's death while we wait."""
        while True:
            try:
                return command.wait(timeout=POLL_INTERVAL_S)
            except subprocess.TimeoutExpired:
                if self.engine_is_gone():
                    self.kill_group()


def reproduce(command_returncode: int) -> int:
    """The command's outcome in the shape the engine has always read.

    A normal exit is that code. A signal death is re-raised on this process: the
    engine reads `Popen.returncode`, so a guard exiting 137 would tell it "the command
    exited 137" where the truth is "the command was killed by 9" — and the verifier's
    verdict text quotes that number.
    """
    if command_returncode < 0:
        signum = -command_returncode
        # SIGKILL and SIGSTOP are the two signals with no disposition to reset: the
        # kernel always acts on them, and calling `signal.signal` for either raises
        # "Invalid argument". A command the OOM killer takes is exactly this case, and
        # a guard that died on that OSError would report exit 1 where the truth is
        # "killed by 9".
        if signum not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    return command_returncode


def main(argv: list[str]) -> int:
    if not argv:
        print("[spawn-guard] no command given", file=sys.stderr, flush=True)
        return 2
    guard = Guard(parent_pid_from_env())
    if guard.started_after_the_engine_died():
        guard.kill_group()
    arm_parent_death_signal()
    signal.signal(signal.SIGTERM, guard.on_term)
    # After the death signal is armed, before the command exists: the command is born under them.
    apply_limits(limits_from_env())
    try:
        # No new session, no new group, no env, no cwd: the command runs exactly where
        # and how the engine asked for it, one process deeper.
        command = subprocess.Popen(argv)  # noqa: S603 — the guard's own child: the argv it was handed, already validated by whoever guarded it
    except OSError as exc:
        # `shutil.which` in the engine resolved this binary, so this is ENOEXEC or a
        # missing loader rather than a typo. 127 is the shell's "cannot execute".
        print(f"[spawn-guard] could not start {argv[0]}: {exc}", file=sys.stderr, flush=True)
        return 127
    return reproduce(guard.wait_for(command))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
