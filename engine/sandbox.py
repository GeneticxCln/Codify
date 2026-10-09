from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from engine import git_readonly
from engine.fs import FileSystemService, PathEscapeError
# The guard that makes "this command dies with the engine" true (see
# engine/spawn_guard.py), including how it is launched: the argv shape and the pid
# handover are the guard's own contract, so they are written down once, next to it.
from engine.spawn_guard import guarded_argv, guarded_env
from typing import IO, Any

PYTEST_FLAGS = {"-q", "-v", "--tb=short", "--no-header"}
MAXFAIL_RE = re.compile(r"^--maxfail=\d+$")
NPM_SCRIPT_RE = re.compile(r"^[A-Za-z0-9_:-]+$")
CARGO_FLAGS = {"--", "--lib", "--bins", "--quiet"}
# Linters and type-checkers (docs/04 section 5). `test` mode only, so they inherit the same approval
# gate as `pytest`: mypy plugins, `build.rs`, a `go vet` cgo build and `make` run the repository's own
# code, exactly as a test suite does (docs/03 section 1.4). What keeps them from *writing* is what
# the engine adds in `hardened_args`, never anything the model supplies.
MYPY_FLAGS = {"--strict", "--ignore-missing-imports"}
# No `--`: the engine appends `-- -D warnings` to clippy, and a second `--` would override it.
CARGO_LINT_FLAGS = {"--lib", "--bins", "--all-targets", "--quiet"}
MAKE_TARGETS = ("lint", "typecheck")


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
    except (PathEscapeError, ValueError):
        # `ValueError` is what `Path.resolve` raises for an embedded NUL. `validate_argv` refuses
        # control characters before it gets here; this is the second line for any caller that
        # reaches a path check some other way, so a hostile path is "not a workspace path" and
        # never a crash.
        return False


# ── read-only mode ────────────────────────────────────────────────────────────
# The librarian inspects the workspace and must never change it. This is the
# allowlist for that mode: three binaries that cannot write, and — for git — the table
# and parser in `engine/git_readonly.py`, which owns what a model may ask git to read
# and is also what the conductor's `git_history` door calls. One place decides what a
# sub-agent may execute.
READ_ONLY_BINARIES = {"ls", "wc", "git"}
# Re-exported: this is the name the rest of the engine and its tests know it by, and the
# value is derived from the table, never a second literal.
READ_ONLY_GIT_SUBCOMMANDS = git_readonly.READ_ONLY_GIT_SUBCOMMANDS
LS_FLAGS = {"-l", "-a", "-h", "-1", "-la", "-al", "-lh", "-lah", "-alh", "-s", "-t"}
WC_FLAGS = {"-l", "-w", "-c", "-m", "-L"}
MAX_READ_ONLY_ARGS = 8
# Stored command output is capped so a verbose suite cannot bloat memory/DB.
MAX_COMMAND_OUTPUT_CHARS = 200_000
# A NUL cannot be passed to a process at all (`subprocess` raises ValueError, which is a
# crash where a refusal belongs) and any other control character is a newline or escape
# no allowlisted command has a reason to receive.
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# What is kept of each stream *while the command runs*. Applied after `communicate()`, the cap above trimmed what was
# stored and nothing else: a test stuck printing in a loop was buffered whole in the engine until its timeout (50 MiB
# of output held 150 MiB). A UTF-8 character is at most four bytes, so this fills the character cap whatever the text.
MAX_KEPT_BYTES = MAX_COMMAND_OUTPUT_CHARS * 4
_READ_CHUNK = 65536


def _cap_output(text: str, truncated: bool = False) -> str:
    if truncated or len(text) > MAX_COMMAND_OUTPUT_CHARS:
        return text[:MAX_COMMAND_OUTPUT_CHARS] + "\n… (output truncated)"
    return text


class _Drain:
    """One pipe, read to its end on a thread of its own, keeping at most `MAX_KEPT_BYTES` of it.

    Read to the end, not just to the cap: a command blocked on a full pipe would never exit, and a timeout is not
    what a long but finished test run should report.
    """

    def __init__(self, pipe: IO[bytes] | None) -> None:
        self.kept = bytearray()
        self.truncated = False
        self._pipe = pipe
        self.thread = threading.Thread(target=self._run, name="sandbox-drain", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        if self._pipe is None:
            return
        try:
            fd = self._pipe.fileno()
            while chunk := os.read(fd, _READ_CHUNK):
                room = MAX_KEPT_BYTES - len(self.kept)
                if room > 0:
                    self.kept += chunk[:room]
                if len(chunk) > room:
                    self.truncated = True
        except (OSError, ValueError):
            pass  # the pipe went away under us: what was read is what there is
        finally:
            self._pipe.close()

    def text(self) -> str:
        """What a model reads: never an exception for a byte that is not UTF-8.

        Decoded strictly, one byte of Latin-1 (`git show` of an old source file, a test printing a blob) raised
        `UnicodeDecodeError` out of `run_command`, past every caller. Line endings are translated the way the text
        mode this replaced translated them, so what a command printed reads as it always did.
        """
        decoded = bytes(self.kept).decode("utf-8", errors="replace")
        return _cap_output(decoded.replace("\r\n", "\n").replace("\r", "\n"), self.truncated)


def _settle(proc: subprocess.Popen[bytes], drains: tuple[_Drain, _Drain], deadline: float) -> bool:
    """Both pipes at their end and the process exited, by `deadline`: what `communicate(timeout=…)` waited for."""
    for drain in drains:
        drain.thread.join(max(0.0, deadline - time.monotonic()))
        if drain.thread.is_alive():
            return False
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    return True


def _signal_alone(pid: int, sig: int) -> None:
    """Signal one pid, escalating nothing. A pid that is already gone is success."""
    try:
        os.kill(pid, signal.SIGKILL if sig == signal.SIGKILL else sig)
    except ProcessLookupError:
        pass


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
        try:
            git_readonly.validate(rest, lambda word: _is_workspace_path(fs, word))
        except git_readonly.GitRefusal as refusal:
            raise CommandNotAllowed(str(refusal)) from None
        return
    flags = LS_FLAGS if cmd == "ls" else WC_FLAGS
    for tok in rest:
        if tok.startswith("-"):
            if tok not in flags:
                raise CommandNotAllowed(f"{cmd} flag not allowed: {tok}")
            continue
        if not _is_workspace_path(fs, tok):
            raise CommandNotAllowed(f"{cmd} path not allowed: {tok}")


def _validate_tsc(rest: list[str], fs: FileSystemService) -> None:
    """`tsc --noEmit [-p PATH]`: a type-check that cannot emit, build or keep incremental state."""
    saw_no_emit = False
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--noEmit":
            saw_no_emit = True
        elif tok in {"-p", "--project"}:
            i += 1
            if i >= len(rest) or not _is_workspace_path(fs, rest[i]):
                raise CommandNotAllowed(f"tsc {tok} needs a path inside the workspace")
        else:
            raise CommandNotAllowed(f"tsc arg not allowed: {tok}")
        i += 1
    if not saw_no_emit:
        raise CommandNotAllowed("tsc only runs with --noEmit (a type-check, never a build)")


def hardened_args(argv: list[str]) -> list[str]:
    """What the child receives after `argv[0]`: the validated tokens plus the flags the engine adds.

    A linter is safe to admit because of this function and not because of what the model asked for.
    The flags are the engine's, appended after validation, so no spelling the model chooses can
    remove them: ruff and mypy keep no cache in the workspace, tsc prints plain text, and clippy
    fails on a warning (a lint that exits 0 on a warning is a lint nobody reads).

    `argv` must already have passed `validate_argv`; for any other command this is `argv[1:]`.
    """
    cmd, rest = argv[0], argv[1:]
    if cmd == "ruff":
        return ["check", "--no-cache", "--output-format=concise", *rest[1:]]
    if cmd == "mypy":
        return ["--cache-dir=/dev/null", *rest]
    if cmd == "tsc":
        return ["--pretty", "false", *rest]
    if cmd == "cargo" and rest[:1] == ["clippy"]:
        return ["clippy", *rest[1:], "--", "-D", "warnings"]
    return rest


def validate_argv(argv: list[str], fs: FileSystemService, mode: str = "test") -> None:
    if not argv:
        raise CommandNotAllowed("empty argv")
    if not all(isinstance(token, str) for token in argv):
        raise CommandNotAllowed("argv must be a list of strings")
    if any(CONTROL_CHARS.search(token) for token in argv):
        raise CommandNotAllowed("argv contains a control character")
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
        if rest and rest[0] in {"check", "clippy"}:
            for tok in rest[1:]:
                if tok not in CARGO_LINT_FLAGS:
                    raise CommandNotAllowed(f"cargo {rest[0]} arg not allowed: {tok}")
            return
        if not rest or rest[0] != "test":
            raise CommandNotAllowed("cargo only allows test, check and clippy")
        for tok in rest[1:]:
            if tok not in CARGO_FLAGS:
                raise CommandNotAllowed(f"cargo arg not allowed: {tok}")
        return
    if cmd == "ruff":
        # No flags at all: `--fix`, `--add-noqa`, `format` and `--config` each write or read what the
        # model chose. The engine adds the one flag set it wants (`hardened_args`).
        if not rest or rest[0] != "check":
            raise CommandNotAllowed("ruff only allows check, with workspace paths and no flags")
        for tok in rest[1:]:
            if not _is_workspace_path(fs, tok):
                raise CommandNotAllowed(f"ruff arg not allowed: {tok}")
        return
    if cmd == "mypy":
        # Exact spellings only: argparse accepts any unambiguous prefix of a long option, so a
        # prefix is not in the set and is refused (`--install-t` is `--install-types`).
        for tok in rest:
            if tok.startswith("-"):
                if tok not in MYPY_FLAGS:
                    raise CommandNotAllowed(f"mypy flag not allowed: {tok}")
                continue
            if not _is_workspace_path(fs, tok):
                raise CommandNotAllowed(f"mypy path not allowed: {tok}")
        return
    if cmd == "tsc":
        _validate_tsc(rest, fs)
        return
    if cmd == "make":
        if len(rest) == 1 and rest[0] in MAKE_TARGETS:
            return
        raise CommandNotAllowed(
            "make only allows `make lint` or `make typecheck`, with no flags or variables"
        )
    if cmd == "go":
        if not rest or rest[0] not in {"test", "vet"}:
            raise CommandNotAllowed("go only allows test and vet")
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
        if argv[0] == "git":
            # Every git the sandbox starts, in either mode, is the hardened one: no pager,
            # no configured diff driver, no user config, no credentials in its environment,
            # and repository discovery that stops at the workspace.
            child_argv = [resolved, *git_readonly.runner_args(argv[1:])]
            env = guarded_env(git_readonly.runner_env(os.environ, root_path))
        else:
            child_argv = [resolved, *hardened_args(argv)]
            env = guarded_env({k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TERM", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME") if k in os.environ})
        try:
            proc = subprocess.Popen(  # noqa: S603 — the argv passed `validate_argv` above; guarded, own session, no shell
                # One process deeper than the command itself: the guard leads the new
                # session (below) and is what removes the command's *whole tree* when
                # this engine is gone. A timeout reads exactly as it did before — the
                # guard is in the group being signalled and reproduces the command's
                # own exit status.
                guarded_argv(child_argv),
                cwd=str(Path(root_path).resolve()),
                env=env,
                # Nothing an allowlisted command runs has anyone to answer it. Inherited,
                # the engine's own stdin would be theirs: `git shortlog` with no revision
                # reads it, and a test that calls `input()` would wait out the timeout.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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
        drains = (_Drain(proc.stdout), _Drain(proc.stderr))
        timed_out = not _settle(proc, drains, time.monotonic() + timeout_s)
        if timed_out:
            # The direct child is not enough — kill the entire group. A gentle
            # TERM first, then KILL for anything still alive a moment later:
            # a test runner that handles TERM to shut its workers down cleanly
            # gets the chance to.
            self._kill_group(proc.pid)
            if not _settle(proc, drains, time.monotonic() + 5):
                self._kill_group(proc.pid, sig=signal.SIGKILL)
                proc.wait()
                # Bounded, unlike the `communicate()` this replaced: a process that escaped the group and still
                # holds a pipe would have kept the engine here for as long as it lived.
                _settle(proc, drains, time.monotonic() + 5)
        # Contract: a timeout is reported as exit 124 (timeout(1)'s code), not
        # the raw -15/-9 signal death. Callers (the verifier's verdict prompt,
        # the transcript) reason about "timed out" as a distinct outcome, and
        # the executor's documented timeout contract expects 124 specifically.
        # The real signal stays visible in stderr above.
        # Capped as it was read (`_Drain`), so a verbose suite bloats neither memory nor the DB. The timeout's own
        # sentence goes after the cap, where a long stderr cannot cut it off.
        stdout, stderr = drains[0].text(), drains[1].text()
        if timed_out:
            stderr += f"\n[timed out after {timeout_s}s — the whole process group was killed]"
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
