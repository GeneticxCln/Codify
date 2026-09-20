from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from engine.fs import FileSystemService, PathEscapeError

PYTEST_FLAGS = {"-q", "-v", "--tb=short", "--no-header"}
MAXFAIL_RE = re.compile(r"^--maxfail=\d+$")
FLAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
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


def validate_argv(argv: list[str], fs: FileSystemService) -> None:
    if not argv:
        raise CommandNotAllowed("empty argv")
    if "/" in argv[0] or "\\" in argv[0]:
        raise CommandNotAllowed("argv[0] must be a basename")
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "pytest":
        for tok in rest:
            if tok.startswith("-"):
                if tok not in PYTEST_FLAGS and not MAXFAIL_RE.match(tok):
                    raise CommandNotAllowed(f"pytest flag not allowed: {tok}")
                continue
            if not _is_workspace_path(fs, tok):
                raise CommandNotAllowed(f"pytest path not allowed: {tok}")
        return
    if cmd == "python":
        if rest[:2] == ["-m", "pytest"]:
            validate_argv(["pytest", *rest[2:]], fs)
            return
        if len(rest) == 1 and rest[0].endswith(".py") and _is_workspace_path(fs, rest[0]):
            return
        raise CommandNotAllowed("python only allows -m pytest or one workspace .py script")
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
            if tok != "./..." and not _is_workspace_path(fs, tok):
                raise CommandNotAllowed(f"go arg not allowed: {tok}")
        return
    if cmd == "git":
        if rest == ["status"] or rest == ["diff"] or rest == ["log", "-1"]:
            return
        raise CommandNotAllowed("git write commands are forbidden")
    raise CommandNotAllowed(f"binary not allowed: {cmd}")


class SandboxService:
    def run_command(self, root_path: str, argv: list[str], timeout_s: int = 120) -> dict:
        fs = FileSystemService(root_path)
        validate_argv(argv, fs)
        resolved = shutil.which(argv[0])
        if not resolved:
            raise CommandNotAllowed(f"{argv[0]} not found on PATH")
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TERM") if k in os.environ}
        proc = subprocess.run(
            [resolved, *argv[1:]],
            cwd=str(Path(root_path).resolve()),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=False,
            check=False,
        )
        return {
            "argv": argv,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
