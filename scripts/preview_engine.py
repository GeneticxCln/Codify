"""Start the engine, wait for the boot handshake, print what a browser needs.

**Why this exists.** The browser preview (`make dev-ui`, then a tab on
localhost:5173) needs two values the engine mints at boot: the port it bound
and the bearer token every request must carry. The desktop shell gets them by
parsing the engine's stdout; a browser tab has no shell to parse, so a
developer did it by hand — tail the log, copy the 64-character hex out of the
handshake line, paste both into the tab's localStorage, reload. That is a
chore with no margin for error, repeated on every engine restart, because the
token rotates on every boot.

This script is the chore, done once:

    make run-engine-preview

It starts `python3 -m engine` (the same entry point the desktop shell uses),
relays its output, waits for the boot line, and prints the two assignments the
browser console needs. It stays in the foreground for the same reason
`run-engine` does: the engine arms a parent watchdog (`CODIFY_PARENT_PID`) when
the shell sets it, so a helper that started the engine and exited could be the
parent whose death kills the engine a second later. Ctrl-C stops the engine.

The values also land in an env file as shell assignments (`source` it in
another terminal). The file holds the credential for a loopback-only server
and is written 0600 — the same exposure as the handshake line this script
prints, and no wider. `CODIFY_PREVIEW_ENV_FILE` moves it (tests do); it
defaults to `/tmp/codify-engine-preview.env`.

`CODIFY_HOME` and friends pass through untouched, so scratch isolation
composes: `CODIFY_HOME=/tmp/codify-scratch make run-engine-preview`.
`CODIFY_PREVIEW_TIMEOUT_S` bounds the boot wait (default 20; the engine's own
`hard_exit_s` is 6, so three of those without a handshake is a wedged boot,
not a slow one).
"""
from __future__ import annotations

import atexit
import os
import re
import signal
import stat
import subprocess
import sys
import threading
from types import FrameType

HANDSHAKE = re.compile(r"^CODIFY_ENGINE token=(?P<token>[0-9a-f]+) port=(?P<port>\d+)")

BOOT_TIMEOUT_S = 20.0

ENV_FILE = "/tmp/codify-engine-preview.env"
ENV_FILE_OVERRIDE = "CODIFY_PREVIEW_ENV_FILE"
TIMEOUT_OVERRIDE = "CODIFY_PREVIEW_TIMEOUT_S"


def env_file_path() -> str:
    """Where the env file goes, resolved per call so tests can move it."""
    return os.environ.get(ENV_FILE_OVERRIDE, ENV_FILE)


def read_handshake(line: str) -> tuple[str, int] | None:
    """The boot line's `(token, port)`, or None for anything else on stdout."""
    match = HANDSHAKE.match(line.strip())
    if not match:
        return None
    return match.group("token"), int(match.group("port"))


def paste_lines(token: str, port: int) -> list[str]:
    """What a developer pastes into the browser console, one line per value."""
    return [
        f'localStorage.setItem("CODIFY_PORT", "{port}");',
        f'localStorage.setItem("CODIFY_TOKEN", "{token}");',
    ]


def write_env_file(token: str, port: int) -> str:
    """Shell assignments for a second terminal; 0600, because it holds a token."""
    path = env_file_path()
    body = f"CODIFY_PORT={port}\nCODIFY_TOKEN={token}\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, body.encode())
    finally:
        os.close(descriptor)
    # os.open's mode is a floor, not a promise, when the file already existed
    # with wider permissions.
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def boot_timeout_s() -> float:
    raw = os.environ.get(TIMEOUT_OVERRIDE, "").strip()
    if not raw:
        return BOOT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError(f"invalid {TIMEOUT_OVERRIDE}={raw!r}: must be a number of seconds") from None
    if value <= 0:
        raise RuntimeError(f"invalid {TIMEOUT_OVERRIDE}={raw!r}: must be positive")
    return value


def run(argv: list[str], env: dict[str, str], timeout_s: float | None = None) -> int:
    """Start the engine, relay its output, and hand over the handshake values.

    Returns the engine's exit status. The parent watchdog means this process
    staying alive *is* the engine's lease on life, so the loop runs until the
    engine ends one way or another.
    """
    bound = timeout_s if timeout_s is not None else boot_timeout_s()
    engine = subprocess.Popen(  # noqa: S603 — a fixed argv, no shell; see the spawn-guard entry
        argv,
        env=env,
        # The engine says everything it has to say on stderr except the one
        # line the shell parses from stdout. The relay keeps both visible, so
        # this is `make run-engine` with a reader on the pipe, not a quieter
        # engine.
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
    )
    state = {"handed_over": False, "stopped_by_us": False}

    def boot_deadline() -> None:
        # A wedged boot has no line to relay and no exit to report; without
        # this the script would wait on stdout forever and look hung itself.
        # The engine is killed rather than left behind: a helper that gives up
        # on the boot but leaves the process is how orphaned engines happen.
        if not state["handed_over"] and engine.poll() is None:
            print(
                f"no boot line within {bound:g}s — killing the engine; "
                "its relayed output above says how far it got",
                file=sys.stderr,
                flush=True,
            )
            state["stopped_by_us"] = True
            engine.kill()

    timer = threading.Timer(bound, boot_deadline)
    timer.daemon = True
    timer.start()

    def stop(signum: int, _frame: FrameType | None) -> None:
        # Ctrl-C reaches the whole foreground session — the engine gets the
        # signal too — so terminating here as well makes one Ctrl-C one clean
        # stop rather than a race between two handlers.
        state["stopped_by_us"] = True
        engine.terminate()
        raise KeyboardInterrupt(f"signal {signum}")

    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, stop)
    atexit.register(engine.terminate)

    exit_status = 1
    try:
        assert engine.stdout is not None
        for line in engine.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if state["handed_over"]:
                continue
            handshake = read_handshake(line)
            if handshake is None:
                continue
            token, port = handshake
            state["handed_over"] = True
            written = write_env_file(token, port)
            print(
                "\nEngine ready. For the browser preview on localhost:5173, paste:\n"
                "  " + "\n  ".join(paste_lines(token, port))
                + f"\n(also written to {written} — `source` it in another terminal)\n",
                flush=True,
            )
    except KeyboardInterrupt:
        pass
    finally:
        timer.cancel()
        if engine.poll() is None:
            state["stopped_by_us"] = True
            engine.terminate()
            try:
                engine.wait(timeout=10)
            except subprocess.TimeoutExpired:
                engine.kill()
                engine.wait()
        else:
            engine.wait()
        exit_status = engine.returncode if engine.returncode is not None else 1
        if state["handed_over"] and state["stopped_by_us"]:
            # A requested stop is the foreground contract working, not a
            # failure: exit 0 even though the engine's own stop path ends via
            # os._exit with the signal byte (241 for SIGTERM). The developer
            # asked it to stop; it stopped.
            exit_status = 0
        if not state["handed_over"]:
            # No handshake is a failure whatever the engine's own exit said:
            # SIGKILL from the boot deadline arrives as -9, a clean exit
            # without a boot arrives as 0, and both mean this script did not
            # do the one thing it exists to do.
            how = (
                "exited cleanly"
                if exit_status == 0
                else f"ended with status {exit_status}"
            )
            print(
                f"no boot handshake — the engine {how}; see the output above",
                file=sys.stderr,
                flush=True,
            )
            exit_status = 1
    return exit_status


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Arguments pass through to the engine untouched, exactly as `make
    # run-engine` would take them; the environment passes through untouched,
    # which is what makes CODIFY_HOME-style isolation compose.
    env = dict(os.environ)
    # One deliberate addition: point the engine's parent watchdog at this
    # process, the way the desktop shell points it at itself. This script's
    # own cleanup covers Ctrl-C, the boot deadline and normal exit — but a
    # SIGKILL to the launcher (a test timeout, an OOM) has no finally to run,
    # and without the watchdog the engine it started sits orphaned forever.
    # The first run of the test suite for this script demonstrated that
    # exactly: two killed launchers, two immortal engines. With the variable
    # set, the engine dies within its own poll interval of this process.
    env["CODIFY_PARENT_PID"] = str(os.getpid())
    return run([sys.executable, "-m", "engine", *argv], env)


if __name__ == "__main__":
    raise SystemExit(main())
