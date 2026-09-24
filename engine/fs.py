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
        size = path.stat().st_size
        if size > MAX_DIFF_BYTES:
            return path.read_bytes()[:MAX_DIFF_BYTES].decode("utf-8", errors="replace")
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
            size = path.stat().st_size
            if size > MAX_DIFF_BYTES:
                raw = path.read_bytes()[:MAX_DIFF_BYTES]
            else:
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
        # The target itself was resolved through containment, but its parents
        # were not: a symlinked directory inside the workspace would redirect
        # the write outside it. Resolve the parent and re-check containment.
        real_parent = target.parent.resolve()
        if real_parent != self.root and self.root not in real_parent.parents:
            raise PathEscapeError(str(target))
        real_parent.mkdir(parents=True, exist_ok=True)
        tmp = real_parent / (target.name + ".codify-tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, real_parent / target.name)
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

        The `edit` action is a search/replace against the file as it exists now:
        `old_text` must appear exactly `count` times (default 1; 0 means every
        occurrence). Each op is resolved to the resulting full content before
        anything is written, so a dry-run's stored proposal is the final bytes —
        `apply` replays content, never re-runs edits against a moved file.
        An `edit` that cannot be applied raises ValueError with the reason.
        """
        summaries: list[dict] = []
        for item in files:
            rel = item["path"]
            action = item["action"]
            content = item.get("content")
            target = self.resolve(rel)
            before, note = self._read_for_diff(target)
            if action == "edit":
                # Resolve the ops against what is on disk right now. The result
                # is the concrete `after` content, so dry-run storage and diff
                # generation treat an edit exactly like a whole-file write.
                after, edit_note = self._resolve_edits(before, target, item.get("edits") or [])
                if edit_note:
                    # An inapplicable edit is a fixer mistake, not an I/O error:
                    # it must fail the step loudly rather than write half an edit.
                    raise ValueError(f"edit failed for {rel}: {edit_note}")
                note = None
            else:
                if action not in ("create", "update", "delete"):
                    raise ValueError(f"unknown file action for {rel}: {action!r}")
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
                    # The resolved full content, so a stored dry-run proposal for
                    # an edit replays byte-identically without re-matching. Only
                    # set where it differs from what the fixer itself sent.
                    "resolved_content": after if action == "edit" else None,
                    # Why a diff is absent when something *did* change.
                    "diff_note": note if changed and not diff else None,
                }
            )
        return summaries

    def _resolve_edits(self, before: str, target: Path, edits: list[dict]) -> tuple[str, str | None]:
        """Apply search/replace ops in order; (result, error-reason-or-None).

        Each op: {old_text, new_text, count}. `old_text` must exist exactly
        `count` times (default 1 — the safest contract for a model, since an
        ambiguous match silently editing the wrong occurrence is worse than an
        error; count=0 means every occurrence). Ops apply to the result of the
        previous one, in order, like a model would expect.
        """
        text = before
        for i, op in enumerate(edits, 1):
            if not isinstance(op, dict) or not isinstance(op.get("old_text"), str):
                return text, f"edit #{i} is not {{old_text, new_text}}"
            old = op["old_text"]
            new = op.get("new_text")
            if not isinstance(new, str):
                return text, f"edit #{i} has no new_text"
            raw_count = op.get("count")
            if raw_count is None:
                count = 1
            else:
                try:
                    count = int(raw_count)
                except (TypeError, ValueError):
                    return text, f"edit #{i} has a non-integer count"
                if count < 0:
                    return text, f"edit #{i}: count must be >= 0"
            if old == "":
                # An empty old_text is never a legal edit: str.replace("") would
                # interleave `new` between every character, and the occurrence
                # math above is meaningless for it. Name the mistake instead of
                # confusing the model with a misleading "not found".
                return text, f"edit #{i}: old_text must not be empty"
            occurrences = text.count(old)
            if count == 0:
                if occurrences == 0:
                    return text, f"edit #{i}: old_text not found"
                text = text.replace(old, new)
                continue
            if occurrences != count:
                return text, (
                    f"edit #{i}: old_text appears {occurrences} time(s), expected {count}"
                )
            text = text.replace(old, new, count)
        return text, None
