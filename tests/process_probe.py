"""What a stray leaves behind: a process wearing its marker, and a file still growing.

Every "nothing survives the engine" test asks the same two questions — "is anything
still running that carries this marker?" and "did that stop changing the workspace?" —
from the ends of the same guarantee: `test_sandbox.py` kills a *stand-in engine* (one
Python process that calls `SandboxService().run_command` directly), `test_git.py` does
the same for a commit's hook tree, and `test_sandbox_orphans_e2e.py` SIGKILLs a real
`python3 -m engine` subprocess driven through the API with a fake provider. The
markers differ (a probe string in a grandchild's argv, a unique script name that
catches both a command and the guard in front of it, a commit message that names the
git process); the questions do not, so the vocabulary lives here exactly once.

`pgrep -f` reads `/proc/<pid>/cmdline`, so a marker is found without knowing pids and
without any cooperation from the code under test — which is the point: what is
asserted is what the kernel sees, not what the engine claims it did. The file half is
the same idea from the other side: a process that is gone cannot append another line.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path


def pids_matching(pattern: str) -> list[str]:
    """pids of live processes whose command line matches `pattern`.

    The calling process is filtered out: a test that greps for a marker its own
    command line carries would otherwise never see the process table go quiet.
    """
    found = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True, check=False
    ).stdout.split()
    return [pid for pid in found if pid != str(os.getpid())]


def wait_until(pattern: str, *, matches: bool, timeout: float = 10.0) -> bool:
    """Wait until `pattern` matches (or no longer matches) some live process.

    Returns whether the wanted state was reached, so a caller asserting it does not
    have to invert the question — the first draft of this helper returned "the state
    you asked for is here", and a test asserting `assertFalse(...)` on it passed for
    exactly the wrong reason. Callers that assert the *absence* of a process should
    still prove the pattern matched before the event they are testing; otherwise
    "gone afterwards" is true for a process that was never there.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bool(pids_matching(pattern)) is matches:
            return True
        time.sleep(0.1)
    return bool(pids_matching(pattern)) is matches


def file_text(path: Path) -> str:
    """The file's text, or "" while it does not exist (yet)."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def wait_for_text(path: Path, timeout: float = 10.0) -> str:
    """Wait until `path` has content, and return its text.

    The assertion that writing *stopped* is only meaningful once writing started, so
    every caller wants this as a precondition on the kill rather than a nicety.
    Missing files and empty ones read the same (""), which is what an unfinished
    command looks like from here either way.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = file_text(path)
        if text:
            return text
        time.sleep(0.05)
    return file_text(path)


def sigkill_matching(pattern: str) -> None:
    """Best-effort cleanup: SIGKILL whatever still matches `pattern`.

    Test teardown only. A failed assertion must not leave a stray writer behind —
    a survivor from an earlier run writes to a workspace nobody is watching and
    makes the next run's probes lie.
    """
    for pid in pids_matching(pattern):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass
