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
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from engine.fs import FileSystemService, PathEscapeError
from engine.sandbox import CommandNotAllowed, SandboxService
from engine.spawn_guard import guarded_argv, guarded_env
from collections.abc import Iterator
from typing import Any

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
# The regex search's two clocks. The soft one is checked between files, inside the worker, and
# ends a search that is merely large. The hard one is the only thing that can end a search whose
# *single match* never returns: CPython's `re` cannot be interrupted and holds the GIL, so no
# thread and no deadline in the same process bounds it — the worker is killed instead. The
# difference is the worker's start-up and the time to ship the answer back.
REGEX_BUDGET_S = PER_LINE_REGEX_SECONDS * 4
REGEX_HARD_LIMIT_S = REGEX_BUDGET_S + 0.5
# By absolute path, never `-m engine.regex_worker`: same reason as the spawn guard's own.
REGEX_WORKER = str(Path(__file__).resolve().parent / "regex_worker.py")
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


def fts5_available() -> bool:
    """Can this interpreter's sqlite rank a MATCH query at all?

    Asked through a probe, never assumed: FTS5 is a compile-time option of
    SQLite, and the answer on this box proves nothing about the next one. The
    index itself is held in memory and discarded, so availability is the only
    durable fact the module needs.
    """
    import sqlite3 as _sqlite3

    try:
        conn = _sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
            return True
        except _sqlite3.Error:
            return False
        finally:
            conn.close()
    except _sqlite3.Error:
        return False


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


class ReadResult(dict[str, Any]):
    """A dict, but named, so callers cannot pass the wrong shape by accident."""


_TOO_EXPENSIVE = "regex took over {seconds:.1f}s — pattern too expensive"


def scan_regex(root_path: str, pattern: str, glob: str | None, budget_s: float) -> dict[str, Any]:
    """The regex walk itself: every file under `root_path`, every line, the model's pattern.

    This runs in `engine/regex_worker.py` and nowhere else — see `LibraryService._search_regex`
    for why it must not run in the engine's own process. Raises `TimeoutError` when the soft
    budget is spent between files; a single match that never returns is the caller's kill.
    """
    rx = re.compile(pattern, re.IGNORECASE)
    deadline = time.monotonic() + budget_s
    matches: list[dict[str, Any]] = []
    files_scanned = 0
    files_skipped = 0
    truncated = False
    root = Path(root_path).resolve()
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        dirnames[:] = [d for d in dirnames if not _is_outside_symlink(root, Path(dirpath) / d)]
        for name in sorted(filenames):
            if time.monotonic() > deadline:
                raise TimeoutError()
            full = Path(dirpath) / name
            if _is_outside_symlink(root, full):
                files_skipped += 1
                continue
            rel = str(full.relative_to(root_path))
            if glob and not fnmatch.fnmatch(rel, glob):
                continue
            if files_scanned >= MAX_FILES_SCANNED:
                truncated = True
                break
            try:
                if full.stat().st_size > MAX_INDEX_BYTES:
                    files_skipped += 1
                    continue
                raw = full.read_bytes()[:MAX_SCAN_BYTES]
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
    return {
        "query": pattern,
        "glob": glob,
        "regex": True,
        "matches": matches,
        "files_scanned": files_scanned,
        "files_skipped": files_skipped,
        "truncated": truncated,
    }


def _run_regex_worker(request: dict[str, Any]) -> dict[str, Any]:
    """Run one regex scan in a worker process and return its reply, or raise ValueError.

    Under the spawn guard like every process this engine starts, in a session of its own so
    the kill takes the whole group, and with a hard clock: `SIGKILL`, not `SIGTERM` — a
    process stuck inside `re` has nothing to clean up and does not run a handler anyway.
    """
    proc = subprocess.Popen(  # noqa: S603 — argv is sys.executable and a script path computed from __file__; the model's pattern travels as JSON on stdin, never in an argv
        guarded_argv([sys.executable, REGEX_WORKER]),
        cwd=request["root"],
        # What a stdlib script needs to start, and nothing of the engine's environment.
        env=guarded_env({k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(json.dumps(request), timeout=REGEX_HARD_LIMIT_S)
    except subprocess.TimeoutExpired:
        SandboxService._kill_group(proc.pid, sig=signal.SIGKILL)
        proc.communicate()
        raise ValueError(_TOO_EXPENSIVE.format(seconds=request["budget_s"])) from None
    except BaseException:
        SandboxService._kill_group(proc.pid, sig=signal.SIGKILL)
        proc.communicate()
        raise
    try:
        reply = json.loads(out)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        detail = (err or out or "").strip()[-300:] or f"exit {proc.returncode}"
        raise ValueError(f"regex search failed: {detail}")
    return reply


class LibraryService:
    """Read-only reconnaissance for one workspace."""

    def __init__(self, root_path: str):
        self.root = str(Path(root_path).resolve())
        self._fs = FileSystemService(self.root)

    # ── reading ────────────────────────────────────────────────────────────────

    def read(self, path: str, offset: int | None = None, limit: int | None = None) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
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

    def _walk_files(self, depth: int | None) -> Iterator[str]:
        """Every file under the root as a relative path, directory by directory, skipping what is not source.

        `depth` stops the descent that many directories down (None: all the way). Never descends through a symlink that
        points outside the workspace, and never into `SKIP_DIRS`.
        """
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
            if depth is not None and len(here.parts) - base_depth >= depth:
                dirnames[:] = []
            for name in sorted(filenames):
                yield str((here / name).relative_to(self.root))

    def tree(self, depth: int = 2) -> dict[str, Any]:
        """A shallow listing, so the first librarian call starts from the real tree.

        Cheap orientation instead of a dozen blind reads: which top-level dirs
        exist, where the tests and manifests live.
        """
        entries: list[str] = []
        truncated = False
        for rel in self._walk_files(depth):
            entries.append(rel)
            if len(entries) >= MAX_TREE_ENTRIES:
                truncated = True
                break
        return {"files": entries, "truncated": truncated, "limit": MAX_TREE_ENTRIES}

    def list_files(self, limit: int) -> dict[str, Any]:
        """Every file path, to the bottom of the tree, for the editor's quick-open.

        Not `tree`: that is orientation for a model (two levels, 120 names). A person looking for `src/a/b/c/handler.py`
        needs all of them, in an order that does not depend on where the walk stopped, so what is kept is sorted. A tree
        bigger than `limit` is cut where the walk reaches it and says so.
        """
        found: list[str] = []
        for rel in self._walk_files(None):
            found.append(rel)
            if len(found) > limit:
                break
        truncated = len(found) > limit
        return {"files": sorted(found[:limit]), "truncated": truncated, "limit": limit}

    # ── searching ──────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        glob: str | None = None,
        regex: bool = False,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Search across the workspace: literal substring, ranked keyword, or bounded regex.

        Literal is the default and stays: every real question ("where is this
        symbol called") is a substring question. `regex=True` opens pattern
        power for structural questions ("find every `except.*pass`"), under hard
        caps — see MAX_REGEX_PATTERN / PER_LINE_REGEX_SECONDS — because a
        model-supplied pattern is still untrusted input. An invalid or oversized
        pattern raises ValueError (refusal-information, not a crash).

        `mode="keyword"` is the second strategy: the same walk, indexed in
        memory and ranked by BM25, for multi-word questions substring answers
        badly ("how do we validate provider urls") — a line carrying only one of
        the words never matches a substring, and the ranking tells the model
        which hits are about the whole question rather than the commonest word.
        """
        needle = (query or "").strip()
        if not needle:
            raise ValueError("empty search query")
        if regex:
            return self._search_regex(needle, glob)
        if mode == "keyword":
            return self._search_keyword(needle, glob)
        if mode is not None:
            raise ValueError(f"unknown search mode: {mode!r}")
        return self._search_substring(needle, glob)

    def _search_substring(self, needle: str, glob: str | None) -> dict[str, Any]:
        """Literal case-insensitive substring: the original search, unchanged.

        Its result shape is load-bearing — the evidence checker reads
        `matches[].path` and `files_scanned` to decide which cited paths were
        actually seen — so nothing about it moved when the keyword strategy
        arrived beside it.
        """
        lowered = needle.lower()
        matches: list[dict[str, Any]] = []
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

    def _search_keyword(self, query: str, glob: str | None) -> dict[str, Any]:
        """BM25-ranked keyword search over an in-memory FTS5 index.

        One walk under the same caps as the substring strategy, each scanned
        file's lines indexed as one FTS5 row so ranking happens per file rather
        than per line. The index lives only for this call and inside this
        process — there is no second artifact for a workspace watcher to go
        stale, and no store outside the workspace for stored text to leak into.

        Two honest limits. Multi-line constructs (a function split across ten
        lines) rank as scattered single-line mentions, so the line-anchored
        shape both other strategies return is lost to a per-file hit; and the
        fallback is real, not a flag: when FTS5 is absent the probe fails and
        the *literal substring* answer is computed and labelled as such, so an
        uninstallable index degrades the answer rather than refusing it.
        """
        import sqlite3 as _sqlite3

        if not fts5_available():
            result = self._search_substring(query, glob)
            result["strategy"] = "substring_fallback"
            result["fts5"] = False
            return result

        rows: list[tuple[str, str]] = []  # (path, whole file as one document)
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
                    if full.stat().st_size > MAX_INDEX_BYTES:
                        files_skipped += 1
                        continue
                    raw = full.read_bytes()[:MAX_SCAN_BYTES]
                except OSError:
                    files_skipped += 1
                    continue
                if looks_binary(raw[:2048]):
                    files_skipped += 1
                    continue
                files_scanned += 1
                rows.append((rel, raw.decode("utf-8", errors="replace")))
            if truncated:
                break

        conn: Any = None
        try:
            conn = _sqlite3.connect(":memory:")
            conn.execute("CREATE VIRTUAL TABLE docs USING fts5(path UNINDEXED, body)")
            conn.executemany("INSERT INTO docs VALUES (?, ?)", rows)
            # An explicit ORDER BY bm25: FTS5's default order for MATCH is
            # unspecified, and the ranking *is* this strategy's point.
            hits = conn.execute(
                "SELECT path, bm25(docs) FROM docs WHERE docs MATCH ? "
                "ORDER BY bm25(docs) LIMIT ?",
                (query, MAX_MATCHES),
            ).fetchall()
        except _sqlite3.Error:
            # A query FTS5 itself refuses (a stray operator in the terms, say)
            # — or the connection failing to open at all — falls back the same
            # way a missing module would: the answer is computed the plain way
            # and labelled. The probe proved availability a moment ago; a
            # probe is not a guarantee about this call.
            result = self._search_substring(query, glob)
            result["strategy"] = "substring_fallback"
            result["fts5"] = True
            return result
        finally:
            if conn is not None:
                conn.close()

        return {
            "query": query,
            "glob": glob,
            "matches": [
                {"path": path, "line": 0, "text": "(file matched; pass to read_file)"}
                for path, _rank in hits
            ],
            "files_scanned": files_scanned,
            "files_skipped": files_skipped,
            "truncated": truncated,
            "strategy": "fts5_bm25",
            "fts5": True,
        }

    def _search_regex(self, pattern: str, glob: str | None) -> dict[str, Any]:
        """Bounded regex search: the literal walker with pattern guards.

        Same walker shape as `search` — SKIP_DIRS, binary sniff, byte caps — so the two
        report identically. A pattern that runs over the time budget aborts the whole search
        with ValueError (reported to the model as a refusal, the same way an invalid pattern
        is) rather than hanging the engine or silently returning partial results.

        The match itself runs in `engine/regex_worker.py`, a process of its own that this
        method kills at `REGEX_HARD_LIMIT_S`. It cannot run here: a match holds the GIL and
        cannot be interrupted, so a thread — which is what this used to be called on — freezes
        the event loop, the health probe and Cancel for as long as the pattern takes.
        """
        if len(pattern) > MAX_REGEX_PATTERN:
            raise ValueError(f"regex pattern too long (>{MAX_REGEX_PATTERN} chars)")
        try:
            # Compiling is safe and fast; *matching* is the part that is not. Checking the
            # syntax here spares a process for the commonest mistake a model makes.
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc

        reply = _run_regex_worker({
            "root": str(Path(self.root).resolve()),
            "pattern": pattern,
            "glob": glob,
            "budget_s": REGEX_BUDGET_S,
        })
        if reply.get("ok"):
            result: dict[str, Any] = reply["result"]
            return result
        if reply.get("error") == "timeout":
            raise ValueError(_TOO_EXPENSIVE.format(seconds=REGEX_BUDGET_S))
        raise ValueError(f"invalid regex: {reply.get('error')}")

    # ── commands (through the one allowlist) ──────────────────────────────

    def git(self, args: list[str]) -> dict[str, Any]:
        """Read-only git: history, diffs, blame. Writes are refused by the sandbox."""
        return self._command(["git", *[str(a) for a in args]])

    def run(self, argv: list[str]) -> dict[str, Any]:
        """A read-only inspect command (ls, wc, read-only git)."""
        return self._command([str(a) for a in argv])

    def _command(self, argv: list[str]) -> dict[str, Any]:
        # Raises CommandNotAllowed; the caller records the refusal and moves on
        # rather than failing the goal, exactly like the verifier's refusals.
        return SandboxService().run_command(
            self.root, argv, timeout_s=READ_ONLY_TIMEOUT_S, mode="read_only",
        )


def format_read(result: dict[str, Any]) -> str:
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


def format_search(result: dict[str, Any]) -> str:
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
    # The keyword strategy says what it did, because the shape of its answer is
    # different and a model that assumes line hits will misread a file hit.
    strategy = result.get("strategy")
    if strategy == "fts5_bm25":
        head += " — ranked by keyword (BM25), whole files, not lines"
    elif strategy == "substring_fallback":
        head += " — keyword index unavailable; literal substring results instead"
    if result["truncated"]:
        head += " (TRUNCATED — results are partial)"
    if result["files_skipped"]:
        skipped = result["files_skipped"]
        head += f", {skipped} unreadable or binary file{'s' if skipped != 1 else ''} skipped"
    lines = [head]
    lines += [f"{m['path']}:{m['line']}: {m['text']}" for m in result["matches"]]
    return "\n".join(lines)


def format_command(result: dict[str, Any]) -> str:
    argv = " ".join(result.get("argv") or [])
    out = (result.get("stdout") or "").strip()
    err = (result.get("stderr") or "").strip()
    if out and err:
        # Both, labelled. This used to print stdout *or* stderr, so a run that printed a banner on stdout and
        # its traceback on stderr showed the banner and hid the reason it failed.
        body = f"stdout:\n{_head(out, 2_500)}\nstderr:\n{_head(err, 1_500)}"
    else:
        body = _head(out or err, 4_000)
    return f"--- $ {argv} (exit {result.get('exit_code')})\n{body}"


def _head(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… (output truncated)"


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def command_tail(result: dict[str, Any], limit: int = 2_000) -> str:
    """The end of what a command printed, for whoever has to act on a failure. Empty when it printed nothing.

    The *end*, not the start: a test runner puts its reason (the failed assertion, the traceback's last
    frame, the summary line) last, and the head of a long run is progress dots. Stderr gets up to half the
    budget and stdout the rest, so a command that is noisy on one stream cannot push the other out.
    """
    out = (result.get("stdout") or "").strip()
    err = (result.get("stderr") or "").strip()
    if not out and not err:
        return ""
    err_budget = min(len(err), limit // 2)
    out_budget = min(len(out), limit - err_budget)
    parts = []
    if out:
        parts.append(f"stdout:\n{_tail(out, out_budget)}")
    if err:
        parts.append(f"stderr:\n{_tail(err, err_budget)}")
    return "\n".join(parts)


__all__ = [
    "LibraryService",
    "CommandNotAllowed",
    "PathEscapeError",
    "format_read",
    "format_search",
    "format_command",
    "command_tail",
    "fts5_available",
    "MAX_READ_CHARS",
    "MAX_ROUND_CHARS",
]


# ── workspace knowledge ──────────────────────────────────────────────────────

# The file a workspace writes down what a previous run learned, and the only
# name it is read under. Deliberately not a dotted config, not a cache, and not
# in `~/.codify`: this is knowledge *about this repository*, it belongs in the
# repository, it is reviewed in a diff, and it dies with the clone.
ROOT_KNOWLEDGE_MD = "CODIFY.md"

# A prior, not an input. Large enough for a real architecture note, small enough
# that it cannot crowd out the workspace the librarian is about to read — and it
# is capped here rather than by the model's cooperation, because the whole risk
# of trusting a file is that it grows.
MAX_KNOWLEDGE_CHARS = 8_000

# A path-shaped token in a backtick pair is how prose names a file. Only
# backticked ones count: a bare "src/app.py" in running text is too weak a
# signal to call a knowledge file stale over.
_PATH_TOKEN = re.compile(r"`([^`\n]{1,200})`")


def _cited_paths(text: str) -> list[str]:
    """Path-shaped tokens in `text`, deduplicated, in first-seen order."""
    found: list[str] = []
    for raw in _PATH_TOKEN.findall(text):
        token = raw.strip()
        # A path needs a separator to be a path here, and a trailing period or
        # comma is prose punctuation rather than part of the name.
        token = token.rstrip(".,;:")
        if "/" not in token or token.startswith(("http://", "https://")):
            continue
        if token not in found:
            found.append(token)
    return found


def read_knowledge(
    root: str, tree_files: set[str] | None = None,
) -> dict[str, Any] | None:
    """The workspace's own knowledge, or None when it has none.

    Returns a dict rather than a bare string because every consumer needs the
    same three facts alongside the text: how much of it was read, whether it was
    cut short, and whether it names files this workspace no longer has.

    That last one is the whole reason this is not just "read the file". A
    knowledge file is written once and read for months; the repository moves
    under it. Paths it names that are gone from the tree are reported as stale
    so the librarian is told which parts of the prior to distrust, instead of
    being handed a confident description of a module that was deleted in March.

    `tree_files` is the librarian's own depth-limited listing, so "stale" here
    means "not in what we were shown" — a path deeper than the listing is
    reported, which is a mild false positive the prompt is careful about. Pass
    `None` for a caller with no listing: that is *no opinion*, and it must not
    degrade into an empty one, because "this file names nothing that exists" is
    a finding about a repository, and a caller that never looked would be
    reporting it anyway.
    """
    text = FileSystemService(root).read_text_or_none(ROOT_KNOWLEDGE_MD)
    if text is None or not text.strip():
        return None

    truncated = len(text) > MAX_KNOWLEDGE_CHARS
    if truncated:
        text = text[:MAX_KNOWLEDGE_CHARS]

    named = _cited_paths(text)
    stale = [p for p in named if p not in tree_files] if tree_files is not None else []
    return {
        "path": ROOT_KNOWLEDGE_MD,
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "paths": named[:20],
        "stale_paths": stale[:10],
    }


def format_knowledge(knowledge: dict[str, Any] | None) -> str:
    """Render the prior for a prompt, labelled so it cannot be read as fact.

    The wording is the load-bearing part. A prior that arrives without its
    caveats is a hallucination with a citation, and the librarian's evidence
    pack is built on the opposite premise — that a path in it was actually seen.
    """
    if not knowledge:
        return "(none — this workspace has written down nothing yet)"
    lines = [
        f"--- {knowledge['path']} — what a PREVIOUS run of Codify concluded ---",
        "This is a prior, NOT evidence. It was written by an earlier run and the "
        "repository may have moved since. Nothing in it is verified: a path named "
        "below is a claim about a file, and you have not opened it.",
    ]
    stale = knowledge.get("stale_paths") or []
    if stale:
        shown = ", ".join(stale[:5])
        lines += [
            "",
            f"STALE — do not rely on these: {shown}",
            f"The knowledge file names {len(stale)} path(s) that were NOT in the tree "
            "listing you were given. Either they were deleted, renamed or moved, or "
            "they are deeper than the listing reaches. Treat every claim about them "
            "as wrong until you open the file yourself and see it.",
        ]
    lines += ["", knowledge["text"], f"--- end {knowledge['path']} ---"]
    if knowledge.get("truncated"):
        lines.append(
            f"(truncated at {MAX_KNOWLEDGE_CHARS} of {len(knowledge['text'])}+ chars — "
            "the rest of that file was not read)"
        )
    lines.append(
        "Use it to aim your reads — a prior naming the right file saves a round. "
        "Cite nothing from it as evidence: every path in your pack must be one you "
        "opened, matched or saw listed."
    )
    return "\n".join(lines)
