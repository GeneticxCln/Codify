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
# The read window a line-range request may span. A model asking for "lines
# 1000-4000" wants the rest of the file, not a denial; a model asking for the
# whole 20k-line file gets told to page. Bounded like every other tool here.
MAX_READ_LINES = 400
# Regex search guards: a model-supplied pattern is untrusted input, and a
# catastrophic backtracking pattern (nested quantifiers over a long line) can
# hang the whole engine. Three layers: a pattern-length ceiling, a per-file-line
# match deadline, and the existing file/byte/match caps.
MAX_REGEX_PATTERN = 200
PER_LINE_REGEX_SECONDS = 0.5
MAX_ROUND_CHARS = 60_000
MAX_MATCHES = 40
# How far a line-range read will scan a file to place its window (and to count
# the file's true line total). A cap keeps a multi-GB log from being read in
# full just to answer "lines 4500-4600"; past it the count is a floor and the
# result is already marked truncated.
MAX_RANGE_SCAN_BYTES = 32_000_000
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


def _is_outside_symlink(root: Path, path: Path) -> bool:
    """True when path is a symlink escaping the workspace root.

    os.walk does not follow dir symlinks by default, but a symlinked *file*
    is still opened and read — leaking /etc/passwd into match events. Skip
    those; read() already refuses them via fs.resolve().
    """
    try:
        if not path.is_symlink():
            return False
        real = path.resolve()
        return real != root and root not in real.parents
    except OSError:
        return True


class ReadResult(dict):
    """A dict, but named, so callers cannot pass the wrong shape by accident."""


class LibraryService:
    """Read-only reconnaissance for one workspace."""

    def __init__(self, root_path: str):
        self.root = str(Path(root_path).resolve())
        self._fs = FileSystemService(self.root)

    # ── reading ────────────────────────────────────────────────────────────────

    def read(self, path: str, offset: int | None = None, limit: int | None = None) -> dict:
        """Read one workspace file, truncated to MAX_READ_CHARS.

        Line ranges: `offset` is the 1-based first line, `limit` the number of
        lines (capped at MAX_READ_LINES). Ranges are how a model reaches the
        bottom half of a large file — before this, a head-truncated read made
        everything past the cap *unreachable*, and the model had to pretend the
        rest did not exist.

        Path escape is refused by FileSystemService, so `../../etc/passwd` raises
        rather than leaking: the model asks, the engine decides.
        """
        resolved = self._fs.resolve(path)
        if resolved.is_dir():
            raise IsADirectoryError(f"{path} is a directory")
        total_size = resolved.stat().st_size
        rel = str(resolved.relative_to(self.root))
        if offset is not None or limit is not None:
            # Normalize: 1-based, bounded window, reported back so the model can
            # tell (and say) when it is seeing a slice rather than the file.
            return self._read_line_window(
                resolved, rel, total_size,
                max(1, offset or 1), min(limit or MAX_READ_LINES, MAX_READ_LINES),
            )
        # Read only the prefix the cap can use — a multi-GB log file is a real
        # workspace resident, and read_bytes() on it is an OOM for a window we
        # throw 3/4 of away. total_size answers "was it truncated" without
        # loading the rest.
        with resolved.open("rb") as fh:
            raw = fh.read(MAX_READ_CHARS * 4)
        text = raw.decode("utf-8", errors="replace")[:MAX_READ_CHARS]
        return {
            "path": rel,
            "text": text,
            "lines": text.count("\n") + 1,
            "bytes": total_size,
            "truncated": total_size > MAX_READ_CHARS,
        }

    def _read_line_window(
        self, resolved: Path, rel: str, total_size: int, start: int, window: int,
    ) -> dict:
        """A line window taken from where the lines actually are.

        Seeks to the byte where line `start` begins instead of slicing the
        head-capped text: windowing the first MAX_READ_CHARS characters made
        any range past the cap come back empty — defeating the one thing line
        ranges exist for, reaching the bottom half of a file the head hides.
        Scanning streams in bounded chunks, so placing a window in a multi-GB
        file costs no more than the chunks actually read.
        """
        start_byte: int | None = None
        newlines_before = 0
        newlines_after_start = 0
        consumed = 0
        eof_seen = False
        last_bytes = b""
        window_raw = b""
        window_lines: list[str] = []

        with resolved.open("rb") as fh:
            # Pass 1: find the byte offset where line `start` begins.
            while True:
                chunk = fh.read(MAX_SCAN_BYTES)
                if chunk:
                    last_bytes = chunk[-1:]
                count = chunk.count(b"\n")
                if newlines_before + count >= start - 1:
                    # Walk to the (start-1)th newline inside this chunk; the
                    # window starts on the byte after it.
                    idx = -1
                    for _ in range((start - 1) - newlines_before):
                        idx = chunk.index(b"\n", idx + 1)
                    start_byte = consumed + idx + 1
                    break
                newlines_before += count
                consumed += len(chunk)
                if not chunk:
                    eof_seen = True
                    break
                if consumed > MAX_RANGE_SCAN_BYTES:
                    break

            if start_byte is None:
                # Line `start` is beyond EOF (or the scan cap stopped us):
                # an empty window either way, with the exact line count when
                # EOF was reached and a floor when the cap was.
                total = newlines_before
                if eof_seen and total_size > 0 and last_bytes != b"\n":
                    total += 1
                return {
                    "path": rel, "text": "", "lines": 0, "offset": start,
                    "window": window, "total_lines": total, "truncated": True,
                }

            # Pass 2: the window itself, from the real line start.
            fh.seek(start_byte)
            window_raw = fh.read(MAX_READ_CHARS * 4)
            if window_raw:
                last_bytes = window_raw[-1:]
            window_lines = window_raw.decode("utf-8", errors="replace").splitlines()
            newlines_after_start = window_raw.count(b"\n")

            # Pass 3 (bounded): finish counting to EOF so the "of N" label is
            # the file's real line total, not a guess from the head. Past the
            # cap it stays a floor; `truncated` already marks the view partial.
            scan_after = 0
            while True:
                chunk = fh.read(MAX_SCAN_BYTES)
                if not chunk:
                    eof_seen = True
                    break
                if chunk:
                    last_bytes = chunk[-1:]
                newlines_after_start += chunk.count(b"\n")
                scan_after += len(chunk)
                if scan_after > MAX_RANGE_SCAN_BYTES:
                    break

        total = (start - 1) + newlines_after_start
        if eof_seen and total_size > 0 and last_bytes != b"\n":
            total += 1
        # A window that hits the char cap still stops cleanly: slice, then
        # re-trim to MAX_READ_CHARS so one read cannot balloon the prompt.
        text = "\n".join(window_lines[:window])[:MAX_READ_CHARS]
        # Count the lines actually sliced, not newlines in the joined text:
        # an empty window is 0 lines (the old +1 turned an empty file read
        # into "1 line" and produced ranges like "lines 100-99").
        shown = len(window_lines[:window])
        return {
            "path": rel,
            "text": text,
            "lines": shown,
            "offset": start,
            "window": window,
            "total_lines": total,
            # Range reads never claim to be the whole file.
            "truncated": True,
        }

    def tree(self, depth: int = 2) -> dict:
        """A shallow listing, so the first librarian call starts from the real tree.

        Cheap orientation instead of a dozen blind reads: which top-level dirs
        exist, where the tests and manifests live.
        """
        entries: list[str] = []
        truncated = False
        base_depth = len(Path(self.root).parts)
        root = Path(self.root).resolve()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            # Never descend through a symlink pointing outside the workspace.
            kept = []
            for d in dirnames:
                full = Path(dirpath) / d
                try:
                    if full.is_symlink() and full.resolve() != root and root not in full.resolve().parents:
                        continue
                except OSError:
                    continue
                kept.append(d)
            dirnames[:] = kept
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

    def search(self, query: str, glob: str | None = None, regex: bool = False) -> dict:
        """Search across the workspace: literal substring, or bounded regex.

        Literal is the default and stays: every real question ("where is this
        symbol called") is a substring question. `regex=True` opens pattern
        power for structural questions ("find every `except.*pass`"), under hard
        caps — see MAX_REGEX_PATTERN / PER_LINE_REGEX_SECONDS — because a
        model-supplied pattern is still untrusted input. An invalid or oversized
        pattern raises ValueError (refusal-information, not a crash).
        """
        needle = (query or "").strip()
        if not needle:
            raise ValueError("empty search query")
        if regex:
            return self._search_regex(needle, glob)
        lowered = needle.lower()
        matches: list[dict] = []
        files_scanned = 0
        files_skipped = 0
        truncated = False
        root = Path(self.root).resolve()

        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            dirnames[:] = [d for d in dirnames if not _is_outside_symlink(root, Path(dirpath) / d)]
            for name in sorted(filenames):
                full = Path(dirpath) / name
                if _is_outside_symlink(root, full):
                    files_skipped += 1
                    continue
                rel = str(full.relative_to(self.root))
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

    def _search_regex(self, pattern: str, glob: str | None) -> dict:
        """Bounded regex search: the literal walker with pattern guards.

        Same walker shape as `search` — SKIP_DIRS, binary sniff, byte caps — so
        the two report identically. A pattern that runs over the time budget
        aborts the whole search with ValueError (reported to the model
        as a refusal, the same way an invalid pattern is) rather than hanging
        the engine or silently returning partial results.
        """
        import re as _re
        import time as _time

        if len(pattern) > MAX_REGEX_PATTERN:
            raise ValueError(f"regex pattern too long (>{MAX_REGEX_PATTERN} chars)")
        try:
            rx = _re.compile(pattern, _re.IGNORECASE)
            rx.search("timeout probe line")
        except _re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc

        budget_s = PER_LINE_REGEX_SECONDS * 4
        deadline = _time.monotonic() + budget_s
        try:
            matches: list[dict] = []
            files_scanned = 0
            files_skipped = 0
            truncated = False
            root = Path(self.root).resolve()
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                dirnames[:] = [d for d in dirnames if not _is_outside_symlink(root, Path(dirpath) / d)]
                for name in sorted(filenames):
                    if _time.monotonic() > deadline:
                        raise TimeoutError()
                    full = Path(dirpath) / name
                    if _is_outside_symlink(root, full):
                        files_skipped += 1
                        continue
                    rel = str(full.relative_to(self.root))
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
                        if rx.search(line):
                            matches.append({"path": rel, "line": lineno, "text": line.strip()[:240]})
                            if len(matches) >= MAX_MATCHES:
                                truncated = True
                                break
                    if truncated:
                        break
                if truncated:
                    break
        except TimeoutError:
            raise ValueError(
                f"regex took over {PER_LINE_REGEX_SECONDS * 4:.1f}s — pattern too expensive"
            ) from None

        return {
            "query": pattern,
            "glob": glob,
            "regex": True,
            "matches": matches,
            "files_scanned": files_scanned,
            "files_skipped": files_skipped,
            "truncated": truncated,
        }

    # ── commands (through the one allowlist) ──────────────────────────────

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
    if "offset" in result:
        # A line-range read announces its slice: what it saw, of how many lines.
        # An empty window must not do the range math — lines-1 with lines=0
        # produced nonsense like "lines 50-49".
        if result.get("lines", 0) == 0:
            head = (
                f"--- {result['path']} (line {result['offset']} requested — empty range)"
            )
            return f"{head} of {result['total_lines']}"
        head = (
            f"--- {result['path']} lines {result['offset']}-{result['offset'] + result['lines'] - 1} "
            f"of {result['total_lines']}"
        )
        body = result["text"]
        return f"{head}\n{body}" if body else f"{head} (empty range)"
    note = f" [truncated at {MAX_READ_CHARS} chars]" if result["truncated"] else ""
    return f"--- {result['path']} ({result['lines']} lines){note}\n{result['text']}"


def format_search(result: dict) -> str:
    kind = "regex" if result.get("regex") else "search"
    # No nested same-quote f-string: the inner expression needs Python 3.12 to
    # parse, while pyproject declares >=3.10 — the engine failed to import at
    # all on 3.10/3.11. Plain concatenation parses everywhere.
    glob_note = (" glob=" + str(result["glob"])) if result["glob"] else ""
    head = (
        f"--- {kind} {result['query']!r}"
        f"{glob_note}: "
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
