from __future__ import annotations

import difflib
import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A unified diff of a multi-megabyte file is not a diff anybody reads, and building
# one for every step is how a chat panel ends up rendering a 40 MB payload.
MAX_DIFF_BYTES = 1_000_000
# How much of a file to sniff for a NUL byte before calling it binary.
BINARY_SNIFF_BYTES = 8192
# The largest file the editor will open or save. An editor holds the whole text in memory and sends it over HTTP as
# one JSON string, so this is a limit on what a person is asked to carry, not on what the workspace may contain.
MAX_EDIT_BYTES = 1_000_000


class PathEscapeError(Exception):
    def __init__(self, path: str):
        super().__init__(f"path escapes workspace: {path}")
        self.path = path


# The repository's own metadata. It is inside the workspace path-wise and
# outside it in every sense that matters: `.git/config` can carry a credential
# in a remote URL, and writing it is worse than reading it — `core.fsmonitor`
# names a command git then runs on **every later status**, including the
# librarian's, and the setting outlives the goal that wrote it. Nothing in the
# engine reads or writes through here: git's own subprocesses own this
# directory, so refusing all of it costs no capability.
GIT_DIRNAME = ".git"


class GitMetadataError(PathEscapeError):
    """Same handling as an escape, different reason: the path is git's own.

    A subclass on purpose. Every existing `except PathEscapeError` site is
    already the right answer — `read_text_or_none` reports "nothing to read
    here" rather than handing `.git/config` to a model, and `apply` propagates
    so the fixer's step fails loudly instead of writing a hook.
    """

    def __init__(self, path: str):
        Exception.__init__(self, f"path is inside the repository's git metadata: {path}")
        self.path = path


class ProtectedRootError(PathEscapeError):
    """The workspace root is somewhere a goal must never be able to write into.

    A subclass of `PathEscapeError` for the reason `GitMetadataError` is: every site that already
    handles an escape is the right answer (the fixer's step fails loudly, nothing is written).
    """

    def __init__(self, root: str, reason: str):
        Exception.__init__(self, f"workspace root {root} is not a place goals may write: {reason}")
        self.path = root
        self.reason = reason


class NotAFileError(Exception):
    """The path is inside the workspace and is not a file: it is missing, or it is a directory."""

    def __init__(self, path: str):
        super().__init__(f"no file at {path}")
        self.path = path


class FileTooLargeError(Exception):
    def __init__(self, size: int):
        super().__init__(f"{size} bytes is over the {MAX_EDIT_BYTES} an editor will hold")
        self.size = size


class FileNotTextError(Exception):
    """What the file holds is not editable text. `reason` is `binary` (a NUL byte) or `encoding` (not UTF-8)."""

    def __init__(self, reason: str):
        super().__init__(f"not editable text: {reason}")
        self.reason = reason


class FileChangedError(Exception):
    """The file on disk is not the version the caller read. Carries the version it is now."""

    def __init__(self, current_version: str):
        super().__init__("the file changed since it was read")
        self.current_version = current_version


class FileAccessError(Exception):
    """The operating system refused: a file the engine may not read, or a directory it may not write into."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# The directories that hold the machine rather than a project. Exact matches only: a project
# at `/usr/local/src/thing` or `/tmp/scratch` is ordinary; `/usr` and `/tmp` themselves are not.
SYSTEM_ROOTS = frozenset({
    "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/media", "/mnt",
    "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var", "/var/tmp",
    # Where `/bin`, `/sbin` and `/lib*` resolve to on a merged-/usr system, which is most of them.
    "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib32", "/usr/lib64", "/usr/local", "/usr/share",
    "/usr/include",
})
# Directories under the home that hold credentials and nothing anybody would call a project.
CREDENTIAL_DIRS = (".ssh", ".gnupg", ".aws", ".kube")


def protected_root_reason(root: Path) -> str | None:
    """Why `root` may not be a workspace, or None when it may.

    About the *root*, not about file names inside it: a dotfiles repository below `$HOME` owns a
    `.ssh/config` and a `.bashrc` that are just files in a repo. What must be impossible is a root
    that contains the real ones — an approved goal there writes through the same `apply` as any
    source file, and `~/.bashrc` or `~/.ssh/authorized_keys` is a login.
    """
    from engine import home  # here, not at module top: home imports nothing of fs, keep it that way

    resolved = root.resolve()
    real_home = Path.home().resolve()
    if resolved == real_home or real_home.is_relative_to(resolved):
        return (
            "it is your home directory or contains it, so a goal could rewrite ~/.bashrc or "
            "~/.ssh/authorized_keys; choose a folder inside it"
        )
    # Both spellings: `/bin` is a symlink to `/usr/bin` on a merged-/usr system, and either one is
    # the machine, however it was reached.
    for spelled in {str(resolved), os.path.abspath(root)}:
        if spelled in SYSTEM_ROOTS:
            return f"{spelled} is a system directory, not a project"
    for name in CREDENTIAL_DIRS:
        guarded = real_home / name
        if resolved == guarded or guarded in resolved.parents:
            return f"{guarded} holds credentials, not a project"
    # The state directory by the same reach rule as `$HOME`: a goal rooted *in* it, or in something
    # that contains it, can rewrite the database, the token and the stored keys; one rooted in a
    # subfolder cannot get out of that subfolder.
    state = home.codify_home().resolve()
    if resolved == state or resolved in state.parents:
        return "it is Codify's own state directory, or contains it (the database, the token, the stored keys)"
    return None


def looks_binary(raw: bytes) -> bool:
    """A NUL byte in the first few KB means "not text".

    Decoding alone cannot tell: ``b"\\x00\\x01\\x02binary"`` *is* valid UTF-8, so a
    binary file sailed through `decode()` and got injected into a model prompt as if
    it were source. That is how a fixer ends up being asked to edit a PNG.
    """
    return b"\x00" in raw[:BINARY_SNIFF_BYTES]


def version_of(raw: bytes) -> str:
    """The identity of a file's bytes, which is all a save needs to know the file has not moved under it."""
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class TextFile:
    """A file as an editor holds it: its text, and the version of the bytes that text came from."""

    path: str
    content: str
    version: str
    size: int


def _lines_keepends(text: str) -> list[str]:
    """The lines of `text` with their endings kept, cut at `\n` and nothing else, for `difflib`.

    The newline-only counterpart of `engine.library.text_lines`, which drops the endings. It lives here and not
    there because `engine.library` imports this module. `str.splitlines` also breaks on a form feed, a vertical
    tab, the file/group/record separators, NEL, U+2028 and U+2029, which `read_file` and the search tools do not
    count as line ends: a diff cut there put its hunks at line numbers they did not agree with, and `difflib`
    joins its pieces with nothing between them, so a piece without a `\n` ran into the next line. A `\r` before a
    `\n` stays, so a CRLF file diffs as it always did.
    """
    *whole, last = text.split("\n")
    lines = [line + "\n" for line in whole]
    if last:
        lines.append(last)
    return lines


def _require_writable_text(value: str, rel: str, what: str) -> None:
    """Refuse, as a `ValueError` the fixer is asked about once, text that cannot be saved as UTF-8.

    A reply's JSON can spell a lone surrogate (`"\\ud800"`): half of a character pair on its own, which Python
    holds as a string and no file can hold. It used to surface only in phase two, when the bytes were made, so a
    dry run (phase one alone) accepted it and the proposal failed later, in the database, outside any re-ask; and
    the codec's own message named neither the file nor what to do. So it is judged in phase one, for a real run
    and a dry run alike, before anything is written or stored. A pair (`"\\ud83d\\ude00"`) is one character by
    the time `json.loads` returns, so it is not refused. `what` is `"path"` or `"text"`; the file is named with
    `ascii()` so the reason can go back into a prompt without carrying a surrogate with it.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        found = sorted({ch for ch in value if "\ud800" <= ch <= "\udfff"})
        escapes = ", ".join(ascii(ch)[1:-1] for ch in found[:3]) + (", ..." if len(found) > 3 else "")
        raise ValueError(
            f"{ascii(rel)}: the {what} contains {escapes}, a lone surrogate escape. That is half of a character "
            f"and not a character, so it cannot be saved in a file. Write the character itself instead, or leave it out."
        ) from None


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


@dataclass
class _Original:
    """What a path held before a batch touched it, kept as bytes so a binary file survives."""

    data: bytes | None  # None: the path did not exist
    mode: int | None

    @classmethod
    def of(cls, path: Path) -> _Original:
        if not path.is_file():
            return cls(None, None)
        try:
            return cls(path.read_bytes(), path.stat().st_mode)
        except OSError:
            return cls(None, None)


class FileSystemService:
    def __init__(self, root_path: str):
        self.root = Path(root_path).resolve()

    def resolve(self, rel: str) -> Path:
        # A NUL cannot be part of a path on any filesystem, and `Path.resolve` answers one with a bare ValueError,
        # which every caller would have to know to catch. It is a path that does not name anything in the workspace.
        if not rel or "\x00" in rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise PathEscapeError(rel)
        # Anywhere in the path, not just at the root: a submodule or a linked
        # worktree keeps its own `.git` a level or two down.
        if GIT_DIRNAME in Path(rel).parts:
            raise GitMetadataError(rel)
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise PathEscapeError(rel)
        # A symlink inside the workspace points at git's metadata without ever
        # spelling `.git`, so ask where the path *lands*, not only how it reads.
        if GIT_DIRNAME in target.relative_to(self.root).parts:
            raise GitMetadataError(rel)
        return target

    def read_editable(self, rel: str) -> TextFile:
        """A file as the editor opens it: exact text, plus the version a later save must name.

        Not `read_text_or_none`: that cuts a large file short and answers None for anything it cannot show, which is
        right for a prompt and wrong for a file a person is about to save back. Here a file is either exactly representable as text or it is
        refused, in a way that says which, because "open a PNG, save it" is how a file gets corrupted. A NUL byte
        *anywhere* means binary (not just in the first block, as `looks_binary` sniffs), so what opens is always
        something `save_text` will accept back.
        """
        target = self.resolve(rel)
        try:
            if not target.is_file():
                raise NotAFileError(rel)
            size = target.stat().st_size
            if size > MAX_EDIT_BYTES:
                raise FileTooLargeError(size)
            raw = target.read_bytes()
        except OSError as exc:
            raise FileAccessError(exc.strerror or str(exc)) from exc
        if len(raw) > MAX_EDIT_BYTES:  # it grew between the stat and the read
            raise FileTooLargeError(len(raw))
        if b"\x00" in raw:
            raise FileNotTextError("binary")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileNotTextError("encoding") from exc
        return TextFile(rel, content, version_of(raw), len(raw))

    def save_text(self, rel: str, content: str, base_version: str) -> TextFile:
        """A person's Save: replace one existing file with exactly `content`, if it is still the file they opened.

        **This is the one writer besides `apply`, and it is not an agent's.** `apply` runs for the fixer behind the
        approved-goal gate; this runs because a person pressed Save in the editor, and nothing in the engine may call it
        except the one route that carries that press (`docs/00` §6.9, held by `test_invariants_at_their_boundary`).

        It is `apply` with the three things a person's save needs differently:

         - it only **replaces a file that exists**. Save is not a way to create or delete a file or a folder;
         - it names the version it read, and a file that is no longer that version is a `FileChangedError`, never a
           silent overwrite of what an agent, a terminal or another editor wrote in the meantime. (The check and the
           replace are two steps, not one lock; the window is the length of one `os.replace`, and the loser of that race
           is the same as it would be without the check.)
         - the text is written *exactly*: no newline translation, no trailing newline added.

        Containment, `.git`, a protected root and the temp-file-and-rename are the same code `apply` uses.
        """
        reason = protected_root_reason(self.root)
        if reason is not None:
            raise ProtectedRootError(str(self.root), reason)
        target = self.resolve(rel)
        try:
            if not target.is_file():
                raise NotAFileError(rel)
            size = target.stat().st_size
        except OSError as exc:
            raise FileAccessError(exc.strerror or str(exc)) from exc
        if "\x00" in content:
            raise FileNotTextError("binary")
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise FileNotTextError("encoding") from exc
        if len(data) > MAX_EDIT_BYTES:
            raise FileTooLargeError(len(data))
        if size > MAX_EDIT_BYTES:
            # Too big to have been opened here, so it is not the file the caller read.
            raise FileTooLargeError(size)
        try:
            current = version_of(target.read_bytes())
        except OSError as exc:
            raise FileAccessError(exc.strerror or str(exc)) from exc
        if current != base_version:
            raise FileChangedError(current)
        try:
            self._write_atomic(target, data)
        except PathEscapeError:
            raise
        except OSError as exc:
            raise FileAccessError(exc.strerror or str(exc)) from exc
        return TextFile(rel, content, version_of(data), len(data))

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

    def _write_atomic(self, target: Path, data: bytes) -> list[Path]:
        """Write via a temp file + rename, so a crash cannot truncate the target.

        Returns the directories this call had to create, outermost first, so a caller that
        has to undo the write can remove them again.
        """
        # The target itself was resolved through containment, but its parents
        # were not: a symlinked directory inside the workspace would redirect
        # the write outside it. Resolve the parent and re-check containment.
        real_parent = target.parent.resolve()
        if real_parent != self.root and self.root not in real_parent.parents:
            raise PathEscapeError(str(target))
        created: list[Path] = []
        probe = real_parent
        while not probe.exists():
            created.append(probe)
            probe = probe.parent
        created.reverse()
        # The temp file is created, never opened: a fixed name (`<file>.codify-tmp`)
        # is one a repository can pre-plant as a symlink, and `write_text` follows
        # a symlink, so writing `a.txt` overwrote whatever `a.txt.codify-tmp`
        # pointed at, outside the workspace or not. A random name cannot be
        # planted, and `O_EXCL | O_NOFOLLOW` refuses to open anything that already
        # exists — a symlink included, dangling or not. `0o666` lets the umask
        # decide the mode, exactly as `write_text` did.
        tmp = real_parent / f"{target.name}.{secrets.token_hex(6)}.codify-tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        # The directories are made inside the `try` that takes them back, as is the open of the
        # temp file: a name the filesystem refuses (the temp file's name is longer than the target's)
        # or a full disk fails *there*, and the directories this call had just made must not outlive it.
        try:
            real_parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, flags, 0o666)
            # `write_text` mints the temp file with the process default (0644 before
            # umask), and `os.replace` carries *that* mode onto the target. An edit
            # to a script that was executable came back non-executable — and since
            # the fixer then commits the file, git recorded a mode change nobody
            # asked for. Copy the mode the target already had; a file being created
            # keeps the default, since there is no earlier mode to honour.
            try:
                mode: int | None = target.stat().st_mode if target.is_file() else None
            except OSError:
                mode = None
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            if mode is not None:
                os.chmod(tmp, mode)
            os.replace(tmp, real_parent / target.name)
        except BaseException:
            # A directory made for a write that then failed is not left standing — and it is
            # empty only once this call's own temp file is gone, so that goes first.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            for directory in reversed(created):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
        finally:
            # `unlink` and not `exists` first: `Path.exists` raises, before Python 3.12, for a name the
            # filesystem refuses, and that would replace the failure being reported with its own.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return created

    def apply(self, files: list[dict[str, Any]], *, dry_run: bool) -> list[dict[str, Any]]:
        """Write the fixer's file operations, and say what actually changed.

        **All of it or none of it.** Two phases: every operation is resolved and validated
        first — path containment, `.git`, the action, each edit's search text — against a virtual
        view that reflects the operations before it in the same batch, so an edit of a file the
        batch just created sees it; and only then is anything written. A batch that cannot be
        applied changes nothing. If a *write* then fails half-way (a full disk, a permission),
        what had already been touched is put back: content, mode, deleted files and created
        directories. This used to be a single loop that wrote as it validated, so
        `[create a, create b, edit-with-missing-text]` raised on the third op with the first two
        left on disk, uncommitted and never announced (audit of 2026-09-29, M4).

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
        # Before anything is resolved or read. A workspace saved before `protected_root_reason`
        # existed is still in the database, and `WorkspaceService.create` cannot refuse it.
        reason = protected_root_reason(self.root)
        if reason is not None:
            raise ProtectedRootError(str(self.root), reason)

        # ── phase 1: resolve and validate everything, touch nothing ──────────────
        summaries: list[dict[str, Any]] = []
        # Where each path stands after the operations planned so far: its text, or None once deleted.
        virtual: dict[Path, str | None] = {}
        for item in files:
            rel = item["path"]
            action = item["action"]
            content = item.get("content")
            _require_writable_text(rel, rel, "path")
            target = self.resolve(rel)
            if target.is_dir():
                raise ValueError(f"{rel} is a directory, not a file")
            if target in virtual:
                planned = virtual[target]
                before, note = ("" if planned is None else planned), None
                exists = planned is not None
            else:
                before, note = self._read_for_diff(target)
                exists = target.is_file()
            if action == "edit":
                # Resolve the ops against what the file holds right now, batch so far included.
                # The result is the concrete `after` content, so dry-run storage and diff
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
            if action != "delete":
                _require_writable_text(after, rel, "text")
            # A delete changes the tree iff the file is there — an *empty* file
            # still disappears, which `before != after` alone would miss.
            changed = exists if action == "delete" else before != after
            diff = ""
            if before != after and note is None:
                diff = "".join(
                    difflib.unified_diff(
                        _lines_keepends(before),
                        _lines_keepends(after),
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                    )
                )
            virtual[target] = None if action == "delete" else after
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
        if dry_run:
            return summaries

        # ── phase 2: write, and undo what was written if a write fails ───────────
        originals: dict[Path, _Original] = {}
        made: list[Path] = []
        try:
            for target, final in virtual.items():
                on_disk = target.is_file()
                if final is None:
                    if not on_disk:
                        continue
                elif on_disk and _read_bytes(target) == final.encode("utf-8"):
                    continue
                originals[target] = _Original.of(target)
                if final is None:
                    target.unlink(missing_ok=True)
                else:
                    made.extend(self._write_atomic(target, final.encode("utf-8")))
        except BaseException as failure:
            self._restore(originals, made, failure)
            raise
        return summaries

    def _restore(self, originals: dict[Path, _Original], made: list[Path], failure: BaseException) -> None:
        """Put back what a failed batch had touched: bytes, mode, deleted files, new directories.

        Best effort per path, so one file that cannot be restored does not stop the rest from
        being; if any cannot, the failure that surfaces says so, because a tree that is *known*
        to be half-written is a different thing to tell the user from one that is not.
        """
        stuck: list[str] = []
        for target, original in reversed(list(originals.items())):
            try:
                if original.data is None:
                    target.unlink(missing_ok=True)
                else:
                    self._write_atomic(target, original.data)
                    if original.mode is not None:
                        os.chmod(target, original.mode)
            except OSError:
                stuck.append(str(target.relative_to(self.root)))
        for directory in reversed(made):
            try:
                directory.rmdir()
            except OSError:
                pass  # not empty, or already gone: either way there is nothing of ours in it
        if stuck:
            raise OSError(
                f"{failure}; and these files could not be put back as they were: {', '.join(stuck)}"
            ) from failure

    def _resolve_edits(self, before: str, target: Path, edits: list[dict[str, Any]]) -> tuple[str, str | None]:
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
                except (TypeError, ValueError, OverflowError):  # OverflowError: `Infinity`, which JSON as Python reads it holds
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
