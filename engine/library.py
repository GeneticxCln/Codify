"""The librarian's abilities: bounded, read-only access to the workspace.

Why this module exists: the pipeline used to plan blind. The planner received a
title and a description, and the fixer then read only the paths that blind planner
had guessed — so if the guess was wrong, no agent ever saw the right file. The
librarian is the slot that actually looks, and this is what it is allowed to look
with.

Two rules hold everywhere in here:

1. **Nothing here can change the workspace.** Reads and searches are pure Python;
   `git` and `run` go through `SandboxService` in read-only mode, which is the one
   place command allowlisting happens (no second validator to drift).
2. **Every call is bounded and reports its own truncation.** A librarian that
   silently sees one third of a repository produces confident wrong answers, which
   is worse than producing none — so each result carries `truncated` and the counts
   behind it, and the prompt tells the model to say when it could not see enough.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from engine.fs import FileSystemService, PathEscapeError
from engine.sandbox import CommandNotAllowed, SandboxService

# Per-read cap. Big enough for a real source file, small enough that one read
# cannot fill a context window on its own.
MAX_READ_CHARS = 8_000
# How much material one round may hand back, across every read and search. The
# engine runs at most MAX_LIBRARY_ROUNDS of these, so this is the librarian's whole
# budget for a goal.
MAX_ROUND_CHARS = 60_000
MAX_MATCHES = 40
# Searches walk the tree in sorted order and stop at these. Reported, never silent.
MAX_FILES_SCANNED = 800
MAX_SCAN_BYTES = 200_000
MAX_TREE_ENTRIES = 120
READ_ONLY_TIMEOUT_S = 20

# Directories that are never source: version control internals and package caches.
# This is a structural exclusion (they are not the user's code), not a content
# filter — an allowlist of file *suffixes* would be the hardcoded-list mistake.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
    "target", ".next", ".nuxt", ".cache", "vendor", ".idea", ".vscode",
}
MAX_INDEX_BYTES = 2_000_000


def looks_binary(sample: bytes) -> bool:
    """A NUL byte in the first block means this is not text worth quoting."""
    return b"\x00" in sample


class ReadResult(dict):
    """A dict, but named, so callers cannot pass the wrong shape by accident."""


class LibraryService:
    """Read-only reconnaissance for one workspace."""

    def __init__(self, root_path: str):
        self.root = str(Path(root_path).resolve())
        self._fs = FileSystemService(self.root)

    # ── reading ────────────────────────────────────────────────────────────────

    def read(self, path: str) -> dict:
        """Read one workspace file, truncated to MAX_READ_CHARS.

        Path escape is refused by FileSystemService, so `../../etc/passwd` raises
        rather than leaking: the model asks, the engine decides.
        """
        resolved = self._fs.resolve(path)
        if resolved.is_dir():
            raise IsADirectoryError(f"{path} is a directory")
        raw = resolved.read_bytes()
        truncated = len(raw) > MAX_READ_CHARS
        text = raw[: MAX_READ_CHARS * 4].decode("utf-8", errors="replace")[:MAX_READ_CHARS]
        return {
            "path": str(resolved.relative_to(self.root)),
            "text": text,
            "lines": text.count("\n") + 1,
            "bytes": len(raw),
            "truncated": truncated,
        }

    def tree(self, depth: int = 2) -> dict:
        """A shallow listing, so the first librarian call starts from the real tree.

        Cheap orientation instead of a dozen blind reads: which top-level dirs
        exist, where the tests and manifests live.
        """
        entries: list[str] = []
        truncated = False
        base_depth = len(Path(self.root).parts)
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            here = Path(dirpath)
            if len(here.parts) - base_depth >= depth:
                dirnames[:] = []
            for name in sorted(filenames):
                entries.append(str((here / name).relative_to(self.root)))
                if len(entries) >= MAX_TREE_ENTRIES:
                    truncated = True
                    break
            if truncated:
                break
        return {"files": entries, "truncated": truncated, "limit": MAX_TREE_ENTRIES}

    # ── searching ──────────────────────────────────────────────────────────────

    def search(self, query: str, glob: str | None = None) -> dict:
        """Literal, case-insensitive substring search across the workspace.

        Literal rather than regex: a model-supplied regex is a ReDoS waiting to
        happen on a big tree, and every real question ("where is this symbol
        called") is a substring question. `glob` narrows by path pattern.
        """
        needle = (query or "").strip()
        if not needle:
            raise ValueError("empty search query")
        lowered = needle.lower()
        matches: list[dict] = []
        files_scanned = 0
        files_skipped = 0
        truncated = False

        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                rel = str((Path(dirpath) / name).relative_to(self.root))
                if glob and not fnmatch.fnmatch(rel, glob):
                    continue
                if files_scanned >= MAX_FILES_SCANNED:
                    truncated = True
                    break
                try:
                    if (Path(dirpath) / name).stat().st_size > MAX_INDEX_BYTES:
                        files_skipped += 1
                        continue
                    raw = (Path(dirpath) / name).read_bytes()[:MAX_SCAN_BYTES]
                except OSError:
                    files_skipped += 1
                    continue
                if looks_binary(raw[:2048]):
                    files_skipped += 1
                    continue
                files_scanned += 1
                for lineno, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                    if lowered in line.lower():
                        matches.append({"path": rel, "line": lineno, "text": line.strip()[:240]})
                        if len(matches) >= MAX_MATCHES:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break

        return {
            "query": needle,
            "glob": glob,
            "matches": matches,
            "files_scanned": files_scanned,
            "files_skipped": files_skipped,
            "truncated": truncated,
        }

    # ── commands (through the one allowlist) ───────────────────────────────────

    def git(self, args: list[str]) -> dict:
        """Read-only git: history, diffs, blame. Writes are refused by the sandbox."""
        return self._command(["git", *[str(a) for a in args]])

    def run(self, argv: list[str]) -> dict:
        """A read-only inspect command (ls, wc, read-only git)."""
        return self._command([str(a) for a in argv])

    def _command(self, argv: list[str]) -> dict:
        # Raises CommandNotAllowed; the caller records the refusal and moves on
        # rather than failing the goal, exactly like the verifier's refusals.
        return SandboxService().run_command(
            self.root, argv, timeout_s=READ_ONLY_TIMEOUT_S, mode="read_only",
        )


def format_read(result: dict) -> str:
    """Render a read for the model, including the part it must know about."""
    note = f" [truncated at {MAX_READ_CHARS} chars]" if result["truncated"] else ""
    return f"--- {result['path']} ({result['lines']} lines){note}\n{result['text']}"


def format_search(result: dict) -> str:
    head = (
        f"--- search {result['query']!r}"
        f"{f' glob={result['glob']}' if result['glob'] else ''}: "
        f"{len(result['matches'])} matches in {result['files_scanned']} files"
    )
    if result["truncated"]:
        head += " (TRUNCATED — results are partial)"
    if result["files_skipped"]:
        skipped = result["files_skipped"]
        head += f", {skipped} unreadable or binary file{'s' if skipped != 1 else ''} skipped"
    lines = [head]
    lines += [f"{m['path']}:{m['line']}: {m['text']}" for m in result["matches"]]
    return "\n".join(lines)


def format_command(result: dict) -> str:
    argv = " ".join(result.get("argv") or [])
    out = (result.get("stdout") or "").strip()
    err = (result.get("stderr") or "").strip()
    body = out if out else err
    if len(body) > 4_000:
        body = body[:4_000] + "\n… (output truncated)"
    return f"--- $ {argv} (exit {result.get('exit_code')})\n{body}"


__all__ = [
    "LibraryService",
    "CommandNotAllowed",
    "PathEscapeError",
    "format_read",
    "format_search",
    "format_command",
    "MAX_READ_CHARS",
    "MAX_ROUND_CHARS",
]
