"""Prove tab restoration end to end: seed the engine's strip, watch it come back.

**Why this exists.** Every unit test of the shared strip stops one step short of
the whole: `tests/test_shell_tabs.py` proves the engine's three routes, and the
UI suites (`tabPersistence`, `layoutSync`) prove the mirror, the merge and the
restore against fakes. What none of them can claim is the chain a user's second
window performs without thinking — engine row → pull → adopt → restore → seat —
because its middle lives in a React effect that only runs in a real window, and
its two ends live in two different processes.

`make smoke-tabs` runs this script. It builds the shell if needed, launches it
with `CODEIFY_TABS_SMOKE=1` — the shell's own mode, which starts the engine as
an ordinary boot does, seeds `PUT /shell/tabs` with one browser row, and waits
for the UI to seat a webview for it — and relays the output. The verdict is
read from the child's stdout lines and nothing else:

* `tabs-smoke: mode engaged` — the binary entered the mode (an older binary
  prints nothing and launches the whole app; see the stale-binary story below);
* `tabs-smoke: engine is up on <port>` — the boot handshake landed;
* `tabs-smoke: seeded <key>` — the engine accepted the seed through the same
  authenticated route a window writes through;
* `tabs-smoke: restored <kind> <key>` — one per row of the strip, read back
  from the engine after the seat, so the run records the strip the window is
  showing;
* `tabs-smoke: probe {…}` — the window's own page, sampled live while the run
  waits: its tab count, which engine port its localStorage names, its
  visibility, and any error-banner text. Diagnosis only, printed rather than
  parsed — a run that fails names its stage in the FAILED line, and the probes
  are the story of *why* beside it;
* `tabs-smoke: PASS` — the child's own verdict, printed only after the webview
  labelled `browser-<seed key>` existed. That label is the whole proof: the
  seed's key is one the UI's own key factory cannot mint (`k_tabs_smoke_seed`
  fails `tabKey`'s time+counter shape, and the freeze in `src-tauri/src/lib.rs`
  holds that), so a seat for it means the engine row was pulled, adopted as a
  stranger, restored into the strip, and seated — the full chain, in a real
  window, with a real engine;
* `tabs-smoke: FAILED <why>` — the child names the stage that did not happen,
  and the harness quotes it.

The exit code is deliberately narrow: 0 means *the engine's seed came back as a
restored, seated tab in a real window*, nothing more. It does not say the page
painted (the embed smoke's subject), nor that a second window's strip agrees
with a first's (the merge rules' unit tests), nor that anything survived a
crash (the mirror's round-trip tests).

**Isolation is not optional.** The smoke writes rows into an engine's database,
so the harness runs the whole thing under a throwaway `CODIFY_HOME`: without
it, the seed would land in the developer's real `~/.codify/codify.db` and their
next real boot would open a tab called "the tabs smoke". Everything else about
the environment passes through untouched — session bus, `WAYLAND_DISPLAY`/
`DISPLAY`, any user-set DMABUF override — for the same reason the embed smoke
gives: a sanitised environment would diagnose a machine nobody is on.

Like `scripts/embed_smoke.py`, this is deliberately not part of `check`/`ci`:
it needs a real display and a real engine boot, and no CI leg here has either.
"""

from __future__ import annotations

import argparse
import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import FrameType
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_TAURI = PROJECT_ROOT / "src-tauri"
SHELL_BINARY = SRC_TAURI / "target" / "debug" / "codify-desktop"

# The contract with the shell half (src-tauri/src/lib.rs). The Rust freeze test
# `the_tabs_smoke_lines_name_the_reader_that_waits_for_them` reads this file and
# asserts these strings appear in it, so a rename here without a rename there
# fails `cargo test` instead of failing a smoke at 2am.
TABS_SMOKE_ENV = "CODEIFY_TABS_SMOKE"
LINE = "tabs-smoke: "
ENGAGED_LINE = f"{LINE}mode engaged\n"
PASS_LINE = f"{LINE}PASS\n"
SEED_MARK = "seeded "
RESTORED_RE = re.compile(rf"^{re.escape(LINE)}restored (\S+) (\S+)$")
FAILED_RE = re.compile(rf"^{re.escape(LINE)}FAILED (?P<why>.*)$")

# The child fails itself 75s after engaging (TABS_SMOKE_DEADLINE_SECS), so the
# harness bound needs slack past it — for the boot before engagement and the
# teardown after the verdict.
DEFAULT_TIMEOUT_S = 100.0
# A binary that never announces the mode is either stale or blocked by the
# single-instance guard; 20s distinguishes them, as in the embed smoke.
ENGAGE_TIMEOUT_S = 20.0

# A silent exit-0 inside a few seconds is the single-instance guard showing
# the launch the door — most often the *previous* run of this smoke, whose
# window's D-Bus name is still held while its process is reaped. Retried with
# backoff rather than failed, because that is what a person at the terminal
# does, and because the refusal says nothing about the feature under test.
GUARD_RETRIES = 4
GUARD_BACKOFF_S = 3.0


def cargo_binary() -> str | None:
    found = shutil.which("cargo")
    if found is not None:
        return found
    return os.environ.get("CARGO")


def build_shell() -> int:
    """`cargo build` in src-tauri, streamed. Returns its exit status."""
    cargo = cargo_binary()
    if cargo is None:
        print(
            "tabs-smoke: cargo is not on PATH and $CARGO is unset — cannot build "
            "the shell. Install Rust, or point $CARGO at it.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print("tabs-smoke: build: starting (cargo build)…", flush=True)
    status = subprocess.run(  # noqa: S603 — a fixed argv, no shell; see the spawn-guard entry
        [cargo, "build"],
        cwd=SRC_TAURI,
    ).returncode
    print(f"tabs-smoke: build: {'ok' if status == 0 else 'failed'}", flush=True)
    return status


class SmokeRun(NamedTuple):
    """One launch of the shell: how it ended, and what it was seen to do.

    The return code alone cannot name a guard refusal. The single-instance guard
    exits 0 *silently, inside `Builder::build()`*, which is the same exit status
    a run that engaged and passed leaves — so the retry policy in `main` needs
    the shape as well as the status: `engaged` (did the mode line ever appear?)
    and `elapsed_s` (how long did the child live?).

    These two facts used to be attributes set on the function object itself,
    which no type checker can see and which the repo's mypy leg correctly
    refused. Returning them is also the honest shape: they are the result of
    the launch, not a property of the launcher.
    """

    code: int
    engaged: bool
    passed: bool
    elapsed_s: float


def run_smoke(argv: list[str], env: dict[str, str], timeout_s: float) -> SmokeRun:
    """Launch the shell in tabs-smoke mode, relay output, judge the verdict.

    `code` is 0 only when the mode was engaged, the child printed its own PASS,
    the child exited 0, and the restored strip read back through the engine
    actually contains the seed. Anything else is a story worth telling, and the
    verdict below tells it — alongside the two facts `main`'s retry policy needs
    to recognise a launch that never happened. See `SmokeRun`.
    """
    state: dict[str, object] = {"engaged": False, "passed": False, "seeded": False, "failed": None}
    restored: list[tuple[str, str]] = []
    started = time.monotonic()

    child = subprocess.Popen(  # noqa: S603 — a fixed argv, no shell; see the spawn-guard entry
        argv,
        env=env,
        # Both streams piped: stdout carries the verdict, and stderr is the
        # engine's boot log and WebKit's complaints — relayed live, because a
        # run that fails at the seat is diagnosed by the engine lines above it.
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def relay_stderr() -> None:
        stream = child.stderr
        if stream is None:  # pragma: no cover — PIPE above guarantees a pipe
            return
        for raw in stream:
            sys.stderr.write(raw)
            sys.stderr.flush()

    stderr_thread = threading.Thread(target=relay_stderr, daemon=True)
    stderr_thread.start()

    def deadline() -> None:
        if child.poll() is None:
            print(
                f"\ntabs-smoke: no verdict within {timeout_s:g}s — stopping the shell",
                file=sys.stderr,
                flush=True,
            )
            child.terminate()

    def engagement_deadline() -> None:
        # Nothing after the short bound: this binary never entered the mode —
        # stale, or the single-instance guard exited it silently (on Linux the
        # newcomer exits 0 inside Builder::build(), before setup runs). Stop it
        # either way: it is a full window and an engine on the desktop.
        if state["engaged"] is not True and child.poll() is None:
            print(
                f"\ntabs-smoke: the shell never announced the mode within "
                f"{ENGAGE_TIMEOUT_S:g}s — stopping it",
                file=sys.stderr,
                flush=True,
            )
            child.terminate()

    def stop(signum: int, _frame: FrameType | None) -> None:
        if child.poll() is None:
            child.terminate()
        raise KeyboardInterrupt(f"signal {signum}")

    def terminate_if_running() -> None:
        if child.poll() is None:
            child.terminate()

    timer = threading.Timer(timeout_s, deadline)
    timer.daemon = True
    timer.start()
    engage_timer = threading.Timer(ENGAGE_TIMEOUT_S, engagement_deadline)
    engage_timer.daemon = True
    engage_timer.start()
    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, stop)
    atexit.register(terminate_if_running)

    pipe = child.stdout
    if pipe is None:
        raise RuntimeError("the smoke child was launched without a stdout pipe")
    try:
        for line in pipe:
            sys.stdout.write(line)
            sys.stdout.flush()
            if line == ENGAGED_LINE:
                state["engaged"] = True
                engage_timer.cancel()
            elif line == PASS_LINE:
                state["passed"] = True
            elif line.startswith(f"{LINE}seeded "):
                state["seeded"] = True
            elif line.startswith(f"{LINE}restored "):
                match = RESTORED_RE.match(line.rstrip("\n"))
                if match is not None:
                    restored.append((match.group(1), match.group(2)))
            elif line.startswith(f"{LINE}FAILED"):
                match = FAILED_RE.match(line.rstrip("\n"))
                if match is not None:
                    state["failed"] = match.group("why")
    except KeyboardInterrupt:
        pass
    finally:
        timer.cancel()
        engage_timer.cancel()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        else:
            child.wait()

    engaged = state["engaged"] is True
    passed = state["passed"] is True
    seeded = state["seeded"] is True
    failed_line = state["failed"]
    elapsed = time.monotonic() - started
    seed = "k_tabs_smoke_seed"
    keys = {key for _kind, key in restored}

    if engaged and passed and child.returncode == 0 and seed in keys:
        print(
            f"\ntabs-smoke: PASS in {elapsed:.1f}s — the engine's seed came back "
            f"as a restored tab ({len(restored)} row(s) in the strip), and the "
            "window seated a webview for it",
            flush=True,
        )
        return SmokeRun(code=0, engaged=engaged, passed=passed, elapsed_s=elapsed)

    print(
        f"\ntabs-smoke: FAIL after {elapsed:.1f}s (child exit status {child.returncode})",
        file=sys.stderr,
        flush=True,
    )
    if not engaged:
        # Silence on the mode line has exactly two stories, and stdout cannot
        # tell them apart — both reach the boot diagnostics (which this build
        # always prints) and then stop. Name both, and let the developer's
        # desktop settle which one held.
        print(
            f"  {SHELL_BINARY} never announced the mode. Either it predates this "
            "smoke and launched the whole app instead (rebuild: make smoke-tabs "
            "SMOKE_TABS_ARGS=--rebuild), or the single-instance guard showed this "
            "launch the door — another Codify instance is running (on Linux the "
            "newcomer exits 0 inside Builder::build(), before setup can say "
            "anything).",
            file=sys.stderr,
            flush=True,
        )
    if failed_line is not None:
        print(f"  the shell named the stage: {failed_line}", file=sys.stderr, flush=True)
    if engaged and not seeded:
        print(
            "  the engine never accepted the seed: the shell names the stage in "
            "its FAILED line above when it knows, and the engine's stderr names "
            "its own refusals.",
            file=sys.stderr,
            flush=True,
        )
    if engaged and seeded and not restored:
        print(
            "  the strip read back empty: the seed never became a row, or the "
            "read was taken before it did. The engine's stderr above names its "
            "own refusals.",
            file=sys.stderr,
            flush=True,
        )
    if restored and seed not in keys:
        print(
            f"  the strip has {len(restored)} row(s) but not the seed — the seed "
            "write may have been refused or overwritten. The shell's seed line "
            "above says which.",
            file=sys.stderr,
            flush=True,
        )
    if passed and child.returncode != 0:
        print(
            "  the shell announced PASS but exited nonzero — a teardown fault "
            "after the verdict; the restoration itself happened.",
            file=sys.stderr,
            flush=True,
        )
    return SmokeRun(code=1, engaged=engaged, passed=passed, elapsed_s=elapsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test tab restoration end to end (make smoke-tabs).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=(
            "whole-run bound in seconds; the shell fails itself at 75s, so this "
            f"needs slack (default {DEFAULT_TIMEOUT_S:g})"
        ),
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="run `cargo build` even if the binary already exists",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 80:
        parser.error("--timeout must exceed the shell's own 75s deadline")

    if args.rebuild or not SHELL_BINARY.exists():
        if build_shell() != 0:
            return 1
    if not SHELL_BINARY.exists():
        print(
            f"tabs-smoke: FAIL — {SHELL_BINARY} does not exist even after the "
            "build. Build it by hand once to see cargo's real complaint.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    env = os.environ.copy()
    env[TABS_SMOKE_ENV] = "1"
    # The whole run under a throwaway home: the smoke writes rows into an
    # engine's database, and the engine inherits this environment on spawn, so
    # this is the one place isolation is enforced. Without it the seed would
    # land in the developer's real strip and come back on their next boot.
    with tempfile.TemporaryDirectory(prefix="codify-tabs-smoke-") as home:
        env["CODIFY_HOME"] = home
        # …and the **webview's** storage, which `CODIFY_HOME` does not touch.
        #
        # Measured the hard way. The window's `localStorage` holds the cached
        # engine address (`CODIFY_PORT`/`CODIFY_TOKEN`, read at module load in
        # `ui/src/api.ts`) and the layout mirror (`CODIFY_TABS`); it lives in
        # the WebKit profile; and a run shares that profile with the
        # developer's own window — same binary, same identifier, and in dev the
        # same origin, because the UI is one dev server. So the cache can name
        # a *live* engine belonging to somebody else, and a health check cannot
        # tell it apart from ours: the token in that cache is a real one and
        # the other engine answers as authenticated. This smoke then wrote its
        # own tab into the developer's real strip — 137 rows of
        # `https://example.com/tabs-smoke` were found in `~/.codify/codify.db`,
        # one per run, each under a key the UI had minted.
        #
        # `CODIFY_HOME` cannot prevent that (the engine is not the party
        # choosing the target) and neither can the single-instance guard. An
        # empty profile can: no cached address, no mirror, and the window is
        # left with the engine its own shell hands it.
        env["XDG_DATA_HOME"] = str(Path(home) / "data")
        env["XDG_CACHE_HOME"] = str(Path(home) / "cache")
        for xdg in (env["XDG_DATA_HOME"], env["XDG_CACHE_HOME"]):
            Path(xdg).mkdir(parents=True, exist_ok=True)
        # A guard refusal is not a verdict: on Linux the newcomer exits 0,
        # silently, inside Builder::build() — and the most common reason is a
        # previous run of this very smoke whose D-Bus name is still held while
        # its widow is reaped. A person would try again; the harness does too,
        # a bounded number of times, and only when the shape fits (exit 0,
        # nothing on the mode line, under five seconds — a real failure is
        # louder, later, or both).
        for attempt in range(1, GUARD_RETRIES + 1):
            run = run_smoke([str(SHELL_BINARY)], env, args.timeout)
            refusal = run.code != 0 and not run.engaged and run.elapsed_s < 5.0
            if not refusal:
                return run.code
            print(
                f"tabs-smoke: launch refused (single-instance guard?) — retry "
                f"{attempt}/{GUARD_RETRIES} after {GUARD_BACKOFF_S:g}s",
                flush=True,
            )
            time.sleep(GUARD_BACKOFF_S)
        # Every attempt was refused, which is *not* a verdict: the smoke never
        # ran, so nothing was proven and nothing may be reported as passed.
        # Returning the refusal's exit status (0, since the guard exits 0) made
        # this the one path that printed no verdict and still passed the gate —
        # a green `make smoke-tabs` for a run that never opened a window.
        print(
            f"tabs-smoke: FAILED every launch was refused "
            f"({GUARD_RETRIES}/{GUARD_RETRIES}, {run.elapsed_s:.1f}s each) — another "
            "Codify instance is holding the single-instance name. Close it, or "
            "wait for its widow to be reaped, and run again.",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
