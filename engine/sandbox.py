from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

from engine.fs import FileSystemService, PathEscapeError
# The guard that makes "this command dies with the engine" true (see
# engine/spawn_guard.py), including how it is launched: the argv shape and the pid
# handover are the guard's own contract, so they are written down once, next to it.
from engine.spawn_guard import guarded_argv, guarded_env
from typing import Any

PYTEST_FLAGS = {"-q", "-v", "--tb=short", "--no-header"}
MAXFAIL_RE = re.compile(r"^--maxfail=\d+$")
NPM_SCRIPT_RE = re.compile(r"^[A-Za-z0-9_:-]+$")
CARGO_FLAGS = {"--", "--lib", "--bins", "--quiet"}


class CommandNotAllowed(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.code = "command_not_allowed"

def _is_workspace_path(fs: FileSystemService, token: str) -> bool:
    if token.startswith("-"):
        return False
    try:
        fs.resolve(token)
        return True
    except PathEscapeError:
        return False


# ── read-only mode ────────────────────────────────────────────────────────────
# The librarian inspects the workspace and must never change it. This is the
# allowlist for that mode: three binaries that cannot write, and a git subcommand
# allowlist with the flags that would redirect git at another directory or another
# file removed. Kept here, next to the test-mode allowlist, so there is exactly
# one place that decides what a sub-agent may execute.
READ_ONLY_BINARIES = {"ls", "wc", "git"}
READ_ONLY_GIT_SUBCOMMANDS = {
    "status", "diff", "log", "show", "blame", "ls-files", "rev-parse",
    "describe", "shortlog", "grep", "branch", "tag", "cat-file", "show-ref",
}
LS_FLAGS = {"-l", "-a", "-h", "-1", "-la", "-al", "-lh", "-lah", "-alh", "-s", "-t"}
WC_FLAGS = {"-l", "-w", "-c", "-m", "-L"}
MAX_READ_ONLY_ARGS = 8
# Stored command output is capped so a verbose suite cannot bloat memory/DB.
MAX_COMMAND_OUTPUT_CHARS = 200_000


def _cap_output(text: str) -> str:
    if len(text) > MAX_COMMAND_OUTPUT_CHARS:
        return text[:MAX_COMMAND_OUTPUT_CHARS] + "\n… (output truncated)"
    return text


def _signal_alone(pid: int, sig: int) -> None:
    """Signal one pid, escalating nothing. A pid that is already gone is success."""
    try:
        os.kill(pid, signal.SIGKILL if sig == signal.SIGKILL else sig)
    except ProcessLookupError:
        pass

# `-C` / `--git-dir` point git somewhere else; `--output` writes a file; `-o` is
# shorthand for it; `--ext-diff` and `--no-index` run external readers.
# `-d`/`-D`/`--delete` remove refs; `-m`/`-M`/`-f`/`--force` let the other
# mutating subcommand flags through; `-O`/`--open-files-in-pager` and
# `--pager` make `git grep` execute an arbitrary pager binary. Every flag here
# has appeared on a nominally read-only subcommand.
DANGEROUS_GIT_FLAGS = (
    "-C", "--git-dir", "--work-tree", "--output", "-o", "--ext-diff", "--no-index",
    "-d", "-D", "--delete", "-m", "-M", "-f", "--force",
    "-O", "--open-files-in-pager", "--pager", "--exec", "--exec-path",
    # Long forms that mutate without a name to give them away: `-u`/`-m`/`-c`
    # take a *value* the positional rule below would refuse, but the attached
    # form has no separate token, and `--edit-description` takes no argument at
    # all and opens an editor instead of returning.
    "--set-upstream-to", "--unset-upstream", "--edit-description", "--copy", "--move",
)
# `branch` and `tag` **read only in their listing forms**. `git branch NAME`
# creates a ref, `git tag NAME` tags a commit, `git branch -c NAME` copies one,
# and `git tag -a NAME` opens an editor and hangs for the whole timeout — every
# one of them arrives as a bare word that no flag denylist can see, because it
# is not a flag. So for these two, a positional is refused unless a listing flag
# is what the caller asked with: those flags mean "show me", and their presence
# is the difference between a pattern and a name.
# Per subcommand, not shared: `-a` lists for one and *annotates* for the other,
# so a single set would let `git tag -a NAME` through as a listing (measured —
# that is how the first version of this rule failed).
GIT_LISTING_FLAGS: dict[str, frozenset[str]] = {
    "branch": frozenset({
        "-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
        "--contains", "--merged", "--no-merged", "--points-at", "--sort", "--format",
        "--column",
    }),
    "tag": frozenset({
        "-l", "--list", "-n", "-v", "--verify", "--verbose", "--contains", "--merged",
        "--no-merged", "--points-at", "--sort", "--format", "--column",
    }),
}
GIT_LISTING_SUBCOMMANDS = frozenset(GIT_LISTING_FLAGS)


def _is_listing_token(tok: str) -> bool:
    """One token, as a possible listing flag (`-n5` counts as `-n`)."""
    head = tok.split("=", 1)[0]
    return head in GIT_LISTING_FLAGS["tag"] and head.startswith("-n") and head[2:].isdigit()
# Letters that are dangerous in any combined short cluster (see the check in
# `_validate_read_only`). Kept separate from DANGEROUS_GIT_FLAGS because the
# flag meanings above are subcommand-dependent; these letters are unsafe in
# every read-only context this allowlist admits.
DANGEROUS_GIT_SHORT = "dfmMO"


def _validate_read_only(argv: list[str], fs: FileSystemService) -> None:
    cmd = argv[0]
    rest = argv[1:]
    if cmd not in READ_ONLY_BINARIES:
        raise CommandNotAllowed(
            f"{cmd} is not a read-only command (the librarian may not change the workspace)"
        )
    if len(rest) > MAX_READ_ONLY_ARGS:
        raise CommandNotAllowed("too many arguments for a read-only command")
    if cmd == "git":
        if not rest or rest[0].startswith("-"):
            raise CommandNotAllowed("git requires a read-only subcommand")
        if rest[0] not in READ_ONLY_GIT_SUBCOMMANDS:
            raise CommandNotAllowed(f"git {rest[0]} is not a read-only git command")
        allowed_listing = GIT_LISTING_FLAGS.get(rest[0], frozenset())
        listing = any(
            tok.split("=", 1)[0] in allowed_listing or _is_listing_token(tok)
            for tok in rest[1:]
        )
        mutating_form = rest[0] in GIT_LISTING_SUBCOMMANDS and not listing
        for tok in rest:
            if any(tok == flag or tok.startswith(flag + "=") for flag in DANGEROUS_GIT_FLAGS):
                raise CommandNotAllowed(f"git flag not allowed: {tok}")
            # Short flags combine (`-dO`) and take attached values with no
            # separator (`-Oless` runs `less` as grep's pager), so bare
            # equality is not enough: reject any short cluster carrying a
            # dangerous letter.
            if tok.startswith("-") and not tok.startswith("--"):
                if any(ch in DANGEROUS_GIT_SHORT for ch in tok[1:]):
                    raise CommandNotAllowed(f"git flag not allowed: {tok}")
            elif not tok.startswith("-") and tok != rest[0] and mutating_form:
                # The argument is a *name*: `git branch NAME` and `git tag NAME`
                # are writes, whatever they are called, and the model is the
                # caller. Listing forms keep their patterns.
                raise CommandNotAllowed(
                    f"git {rest[0]} {tok} creates a ref; only the listing forms "
                    f"of {rest[0]} are read-only"
                )
        return
    flags = LS_FLAGS if cmd == "ls" else WC_FLAGS
    for tok in rest:
        if tok.startswith("-"):
            if tok not in flags:
                raise CommandNotAllowed(f"{cmd} flag not allowed: {tok}")
            continue
        if not _is_workspace_path(fs, tok):
            raise CommandNotAllowed(f"{cmd} path not allowed: {tok}")


def validate_argv(argv: list[str], fs: FileSystemService, mode: str = "test") -> None:
    if not argv:
        raise CommandNotAllowed("empty argv")
    if "/" in argv[0] or "\\" in argv[0]:
        raise CommandNotAllowed("argv[0] must be a basename")
    cmd = argv[0]
    rest = argv[1:]
    if mode == "read_only":
        _validate_read_only(argv, fs)
        return
    if cmd == "pytest":
        for tok in rest:
            if tok.startswith("-"):
                if tok not in PYTEST_FLAGS and not MAXFAIL_RE.match(tok):
                    raise CommandNotAllowed(f"pytest flag not allowed: {tok}")
                continue
            if not _is_workspace_path(fs, tok):
                raise CommandNotAllowed(f"pytest path not allowed: {tok}")
        return
    if cmd in {"python", "python3"}:
        if rest[:2] == ["-m", "pytest"]:
            validate_argv(["pytest", *rest[2:]], fs)
            return
        if len(rest) == 1 and rest[0].endswith(".py") and _is_workspace_path(fs, rest[0]):
            return
        raise CommandNotAllowed(f"{cmd} only allows -m pytest or one workspace .py script")
    if cmd in {"npm", "pnpm"}:
        if rest and rest[0] == "test" and len(rest) == 1:
            return
        if len(rest) == 2 and rest[0] == "run" and NPM_SCRIPT_RE.match(rest[1]):
            return
        raise CommandNotAllowed(f"{cmd} argv not allowed")
    if cmd == "cargo":
        if not rest or rest[0] != "test":
            raise CommandNotAllowed("cargo only allows test")
        for tok in rest[1:]:
            if tok not in CARGO_FLAGS:
                raise CommandNotAllowed(f"cargo arg not allowed: {tok}")
        return
    if cmd == "go":
        if not rest or rest[0] != "test":
            raise CommandNotAllowed("go only allows test")
        for tok in rest[1:]:
            # Relative recursive patterns (./pkg/...) pass the path check
            # below: resolve() is lexical, the ./ prefix and /... suffix add
            # no .. or leading /, so only true escapes are refused here.
            if not _is_workspace_path(fs, tok):
                raise CommandNotAllowed(f"go arg not allowed: {tok}")
        return
    if cmd == "git":
        if rest == ["status"] or rest == ["diff"] or rest == ["log", "-1"]:
            return
        raise CommandNotAllowed("git write commands are forbidden")
    raise CommandNotAllowed(f"binary not allowed: {cmd}")


class SandboxService:
    def run_command(
        self, root_path: str, argv: list[str], timeout_s: int = 120, mode: str = "test",
    ) -> dict[str, Any]:
        """Run one allowlisted command. `mode="read_only"` narrows the allowlist
        to commands that cannot change the workspace (used by the librarian).

        The command runs in its own process group and a timeout kills the whole
        group, not just the direct child: a test command that spawns children
        (pytest spawning workers, npm spawning node) must not leave strays
        behind holding ports or writing files after the engine moved on.

        That group outlives the engine only as long as its leader lets it: the leader
        is `engine/spawn_guard.py`, which kills the group when the engine that
        spawned it is gone. A command still running when the window is closed is the
        same stray as a timed-out one, and the timeout cannot catch it.
        """
        if not isinstance(timeout_s, (int, float)) or timeout_s <= 0 or timeout_s > 600:
            raise CommandNotAllowed(f"invalid timeout: {timeout_s!r}")
        fs = FileSystemService(root_path)
        validate_argv(argv, fs, mode=mode)
        resolved = shutil.which(argv[0])
        if not resolved:
            raise CommandNotAllowed(f"{argv[0]} not found on PATH")
        env = guarded_env({k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TERM", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME") if k in os.environ})
        try:
            proc = subprocess.Popen(
                # One process deeper than the command itself: the guard leads the new
                # session (below) and is what removes the command's *whole tree* when
                # this engine is gone. A timeout reads exactly as it did before — the
                # guard is in the group being signalled and reproduces the command's
                # own exit status.
                guarded_argv([resolved, *argv[1:]]),
                cwd=str(Path(root_path).resolve()),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                # A session of its own: the child becomes process-group leader, so
                # everything it spawns joins a group the engine can signal as one.
                start_new_session=True,
            )
        except OSError as exc:
            # `which` resolved the binary but exec failed (permissions, ENOEXEC,
            # resource limits): a refusal with a reason, not a raw traceback in
            # the middle of a step.
            raise CommandNotAllowed(f"failed to start {argv[0]}: {exc.strerror or exc}") from exc
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            # The direct child is not enough — kill the entire group. A gentle
            # TERM first, then KILL for anything still alive a moment later:
            # a test runner that handles TERM to shut its workers down cleanly
            # gets the chance to.
            self._kill_group(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_group(proc.pid, sig=signal.SIGKILL)
                stdout, stderr = proc.communicate()
            stderr = (stderr or "") + f"\n[timed out after {timeout_s}s — the whole process group was killed]"
        # Contract: a timeout is reported as exit 124 (timeout(1)'s code), not
        # the raw -15/-9 signal death. Callers (the verifier's verdict prompt,
        # the transcript) reason about "timed out" as a distinct outcome, and
        # the executor's documented timeout contract expects 124 specifically.
        # The real signal stays visible in stderr above.
        # Cap stored output: a verbose suite must not bloat memory or the DB.
        stdout, stderr = _cap_output(stdout or ""), _cap_output(stderr or "")
        return {
            "argv": argv,
            "exit_code": 124 if timed_out else proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    @staticmethod
    def _kill_group(pid: int, sig: int = signal.SIGTERM) -> None:
        """Signal the child's whole process group; fall back to the child alone.

        The child may already be gone when this runs — exited between the timeout
        and the kill, and fully reaped, not merely a zombie (a zombie still
        answers `getpgid`; a reaped pid does not). `ProcessLookupError` from the
        lookup is success, not a problem — but it is not the end of the story:
        whatever the child spawned is still in the group it led, so a vanished
        leader *redirects* the group kill rather than cancelling it. The pid that
        named the leader still names that group, because the only way this class
        spawns is `start_new_session=True` — the child led the group, so the
        group's id is its pid. A group that is gone entirely answers ESRCH below,
        which is the same success it is everywhere else in this function.

        The redirect carries the same refusal the normal path has. A leaderless
        `killpg(pid)` reaches either the dead leader's group or nothing at all —
        a pid that was never a group leader does not name a group — so the one
        way it could land on *our* group is `pid == os.getpgrp()`, and that is
        refused: a group we lead and are alive in is a group we must not signal.

        **The one group this must never signal is its own.** `getpgid` is a
        question about a pid, not a promise about which session that pid is in.
        A child that was never given its own session answers with *this* process's
        group, and `killpg` then delivers SIGTERM — or SIGKILL, on the second
        call — to the engine, the desktop shell that launched it, and everything
        else sharing the group. The call *succeeds*, so nothing in the `except`
        below would notice; a timeout in a sandboxed command would take the app
        down instead of the command. `start_new_session=True` is what makes the
        group the child's, and this asks rather than assumes: a group we lead is
        a group we must not signal, so the child is signalled on its own.

        This is also what makes the function safe to call from a test, where the
        answer is the runner's group essentially every time.
        """
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            # The leader is reaped; the group it led may not be. Redirect the
            # group signal to the leader's pid — which is the group's id, since
            # this class only spawns session leaders — with the same refusal the
            # normal path carries: a group we lead is a group we must not
            # signal, and we are demonstrably alive. There is no `_signal_alone`
            # here — the pid is gone, there is nobody alone to signal.
            if pid != os.getpgrp():
                try:
                    os.killpg(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            return
        if group == os.getpgrp():
            _signal_alone(pid, sig)
            return
        try:
            os.killpg(group, sig)
        except (ProcessLookupError, PermissionError):
            _signal_alone(pid, sig)
