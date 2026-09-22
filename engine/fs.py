from __future__ import annotations

import difflib
import os
from pathlib import Path

# A unified diff of a multi-megabyte file is not a diff anybody reads, and building
# one for every step is how a chat panel ends up rendering a 40 MB payload.
MAX_DIFF_BYTES = 1_000_000
# How much of a file to sniff for a NUL byte before calling it binary.
BINARY_SNIFF_BYTES = 8192


class PathEscapeError(Exception):
    def __init__(self, path: str):
        super().__init__(f"path escapes workspace: {path}")
        self.path = path


def looks_binary(raw: bytes) -> bool:
    """A NUL byte in the first few KB means "not text".

    Decoding alone cannot tell: ``b"\\x00\\x01\\x02binary"`` *is* valid UTF-8, so a
    binary file sailed through `decode()` and got injected into a model prompt as if
    it were source. That is how a fixer ends up being asked to edit a PNG.
    """
    return b"\x00" in raw[:BINARY_SNIFF_BYTES]


class FileSystemService:
    def __init__(self, root_path: str):
        self.root = Path(root_path).resolve()

    def resolve(self, rel: str) -> Path:
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise PathEscapeError(rel)
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise PathEscapeError(rel)
        return target

    def read_text(self, rel: str) -> str:
        path = self.resolve(rel)
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def read_text_or_none(self, rel: str) -> str | None:
        """The file's text, or None when it is not text at all.

        Context builders feed model prompts, and a prompt must not be able to fail
        the step: a suggested path that escapes the workspace, is a directory, is a
        binary blob, or is simply unreadable is information ("there is nothing to
        read here"), not an exception for a goal to die of.
        """
        try:
            path = self.resolve(rel)
            if not path.is_file():
                return None
            raw = path.read_bytes()
            if looks_binary(raw):
                return None
            return raw.decode("utf-8")
        except (PathEscapeError, OSError, UnicodeDecodeError):
            return None

    def _read_for_diff(self, path: Path) -> tuple[str, str | None]:
        """(text, reason there is no text). Never raises.

        `errors="replace"` would let a binary file be diffed into nonsense, so
        binary and oversized files are reported with the reason instead — and the
        write still happens, which is what the user asked for.
        """
        try:
            if not path.is_file():
                return "", None
            size = path.stat().st_size
            if size > MAX_DIFF_BYTES:
                return "", f"file is {size} bytes — too large to diff"
            raw = path.read_bytes()
            if looks_binary(raw):
                return "", "file is binary"
            return raw.decode("utf-8"), None
        except OSError as exc:
            return "", f"unreadable: {exc}"
        except UnicodeDecodeError:
            return "", "file is not UTF-8 text"

    def _write_atomic(self, target: Path, text: str) -> None:
        """Write via a temp file + rename, so a crash cannot truncate the target."""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".codify-tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def apply(self, files: list[dict], *, dry_run: bool) -> list[dict]:
        """Write the fixer's file operations, and say what actually changed.

        `changed` matters: a fixer that proposes a file whose content is already
        exactly what it proposes produces an empty diff, and reporting that as a
        touched file (in the chat's change summary, and in the commit) is a claim
        the engine would be making without evidence.
        """
        summaries: list[dict] = []
        for item in files:
            rel = item["path"]
            action = item["action"]
            content = item.get("content")
            target = self.resolve(rel)
            before, note = self._read_for_diff(target)
            after = "" if action == "delete" else (content or "")
            # A delete changes the tree iff the file is there — an *empty* file
            # still disappears, which `before != after` alone would miss.
            changed = target.is_file() if action == "delete" else before != after
            diff = ""
            if before != after and note is None:
                diff = "".join(
                    difflib.unified_diff(
                        before.splitlines(keepends=True),
                        after.splitlines(keepends=True),
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                    )
                )
            if not dry_run and changed:
                if action == "delete":
                    target.unlink(missing_ok=True)
                else:
                    self._write_atomic(target, after)
            summaries.append(
                {
                    "path": rel,
                    "action": action,
                    "unified_diff": diff,
                    "changed": changed,
                    # Why a diff is absent when something *did* change.
                    "diff_note": note if changed and not diff else None,
                }
            )
        return summaries
