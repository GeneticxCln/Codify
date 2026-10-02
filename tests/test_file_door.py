"""A person's own file door: read a file to edit it, and save it back (`FileSystemService`).

Everything that writes the workspace on an agent's behalf goes through `apply`, behind the approved-goal gate. The
editor needs one thing `apply` is not: a *person* pressing Save. That is `save_text`, and it is the same careful writer
(containment, `.git`, atomic replace, mode kept) with three differences that are the whole contract:

  * it only ever **replaces a file that exists**, so Save is never a way to create or delete one;
  * it refuses unless the caller names the version it read (`base_version`), so a file an agent or an editor changed in
    the meantime is a conflict, never a silent overwrite;
  * nothing reaches it except one HTTP route (see `test_invariants_at_their_boundary.TestAPersonsSaveIsTheOneOtherDoor`).

What a file *is* for this purpose is stated once: UTF-8 text without a NUL byte, at most `MAX_EDIT_BYTES`. Anything else
is refused in words, because opening a PNG in a text editor and saving it back is how it gets corrupted.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.fs import (
    MAX_EDIT_BYTES,
    FileAccessError,
    FileChangedError,
    FileNotTextError,
    FileSystemService,
    FileTooLargeError,
    GitMetadataError,
    NotAFileError,
    PathEscapeError,
    ProtectedRootError,
)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class DoorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.fs = FileSystemService(str(self.root))

    def seed(self, name: str, raw: bytes | str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
        return path


class TestReadingAFileToEditIt(DoorCase):
    def test_the_text_comes_back_with_its_version_and_size(self) -> None:
        raw = b"def f():\n    return 1\n"
        self.seed("src/a.py", raw)

        got = self.fs.read_editable("src/a.py")

        self.assertEqual("src/a.py", got.path)
        self.assertEqual(raw.decode(), got.content)
        self.assertEqual(sha(raw), got.version)
        self.assertEqual(len(raw), got.size)

    def test_line_endings_and_a_byte_order_mark_are_not_translated(self) -> None:
        # The editor decides what to do with CRLF; the engine must not decide for it, or a save changes every line.
        raw = b"\xef\xbb\xbfone\r\ntwo\r\n"
        self.seed("win.txt", raw)

        got = self.fs.read_editable("win.txt")

        self.assertEqual("﻿one\r\ntwo\r\n", got.content)
        self.assertEqual(sha(raw), got.version, "the version is of the bytes on disk, not of a normalised copy")

    def test_a_missing_file_and_a_directory_are_both_not_a_file(self) -> None:
        (self.root / "dir").mkdir()
        for rel in ("nope.txt", "dir"):
            with self.subTest(rel=rel), self.assertRaises(NotAFileError):
                self.fs.read_editable(rel)

    def test_a_binary_file_is_refused_as_binary(self) -> None:
        self.seed("logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

        with self.assertRaises(FileNotTextError) as caught:
            self.fs.read_editable("logo.png")
        self.assertEqual("binary", caught.exception.reason)

    def test_a_nul_byte_anywhere_makes_it_binary_not_just_in_the_first_block(self) -> None:
        # `looks_binary` sniffs 8 KB, which is right for a prompt. Here a NUL later on would open fine and then be
        # refused on save, so a file the editor opens is always one it can save back.
        self.seed("late.txt", b"a" * 20_000 + b"\x00" + b"b" * 10)

        with self.assertRaises(FileNotTextError) as caught:
            self.fs.read_editable("late.txt")
        self.assertEqual("binary", caught.exception.reason)

    def test_an_operating_system_refusal_is_a_file_access_error_not_a_crash(self) -> None:
        self.seed("a.txt", b"x")

        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(FileAccessError) as caught:
                self.fs.read_editable("a.txt")
        self.assertIn("Permission denied", caught.exception.detail)

    def test_text_that_is_not_utf8_is_refused_as_an_encoding_problem(self) -> None:
        self.seed("latin.txt", "caf\xe9\n".encode("latin-1"))

        with self.assertRaises(FileNotTextError) as caught:
            self.fs.read_editable("latin.txt")
        self.assertEqual("encoding", caught.exception.reason)

    def test_a_file_over_the_limit_is_refused_before_it_is_read_into_memory(self) -> None:
        self.seed("big.txt", b"a" * (MAX_EDIT_BYTES + 1))

        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read a file over the limit")):
            with self.assertRaises(FileTooLargeError) as caught:
                self.fs.read_editable("big.txt")
        self.assertEqual(MAX_EDIT_BYTES + 1, caught.exception.size)

    def test_a_file_that_grows_between_the_size_check_and_the_read_is_still_refused(self) -> None:
        self.seed("a.txt", b"small")

        with mock.patch.object(Path, "read_bytes", return_value=b"a" * (MAX_EDIT_BYTES + 1)):
            with self.assertRaises(FileTooLargeError) as caught:
                self.fs.read_editable("a.txt")
        self.assertEqual(MAX_EDIT_BYTES + 1, caught.exception.size)

    def test_the_size_is_in_bytes_not_characters(self) -> None:
        raw = "caf\u00e9 \u20ac\n".encode()  # 7 characters, 10 bytes
        self.seed("utf.txt", raw)

        got = self.fs.read_editable("utf.txt")

        self.assertEqual(7, len(got.content))
        self.assertEqual(10, got.size)

    def test_a_file_exactly_at_the_limit_opens(self) -> None:
        self.seed("edge.txt", b"a" * MAX_EDIT_BYTES)

        self.assertEqual(MAX_EDIT_BYTES, self.fs.read_editable("edge.txt").size)

    def test_every_way_out_of_the_workspace_is_refused(self) -> None:
        self.seed(".git/config", "[core]\n")
        outside = self.root.parent / "codify-door-outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        (self.root / "link.txt").symlink_to(outside)
        cases = ["../x", "/etc/passwd", "a/../../x", ".git/config", "link.txt", "a\x00b.txt", ""]

        for rel in cases:
            with self.subTest(rel=rel), self.assertRaises(PathEscapeError):
                self.fs.read_editable(rel)


class TestSavingIt(DoorCase):
    def test_the_file_is_replaced_and_the_new_version_is_of_what_was_written(self) -> None:
        before = b"old\n"
        self.seed("a.txt", before)

        saved = self.fs.save_text("a.txt", "new\n", sha(before))

        self.assertEqual(b"new\n", (self.root / "a.txt").read_bytes())
        self.assertEqual(sha(b"new\n"), saved.version)
        self.assertEqual(4, saved.size)
        self.assertEqual(saved.version, self.fs.read_editable("a.txt").version)

    def test_the_size_reported_is_of_the_bytes_written(self) -> None:
        self.seed("a.txt", b"x")

        saved = self.fs.save_text("a.txt", "caf\u00e9 \u20ac\n", sha(b"x"))  # 7 characters, 10 bytes

        self.assertEqual(10, saved.size)
        self.assertEqual(10, (self.root / "a.txt").stat().st_size)

    def test_what_is_written_is_exactly_what_was_sent(self) -> None:
        self.seed("a.txt", b"x\n")

        self.fs.save_text("a.txt", "﻿one\r\ntwo", sha(b"x\n"))

        self.assertEqual("﻿one\r\ntwo".encode(), (self.root / "a.txt").read_bytes())

    def test_a_file_that_changed_since_it_was_read_is_a_conflict_and_is_left_alone(self) -> None:
        read = b"mine\n"
        self.seed("a.txt", read)
        (self.root / "a.txt").write_bytes(b"theirs\n")  # an agent, or another editor, got there first

        with self.assertRaises(FileChangedError) as caught:
            self.fs.save_text("a.txt", "overwrite\n", sha(read))

        self.assertEqual(sha(b"theirs\n"), caught.exception.current_version)
        self.assertEqual(b"theirs\n", (self.root / "a.txt").read_bytes(), "a conflicting save wrote anyway")

    def test_a_save_never_creates_a_file(self) -> None:
        with self.assertRaises(NotAFileError):
            self.fs.save_text("new.txt", "hello\n", sha(b""))

        self.assertEqual([], list(self.root.iterdir()))

    def test_a_save_never_writes_a_directory_or_makes_one(self) -> None:
        (self.root / "d").mkdir()

        with self.assertRaises(NotAFileError):
            self.fs.save_text("d", "x", sha(b""))
        with self.assertRaises(NotAFileError):
            self.fs.save_text("missing-dir/a.txt", "x", sha(b""))

        self.assertEqual(["d"], sorted(p.name for p in self.root.iterdir()))

    def test_every_way_out_of_the_workspace_is_refused_and_nothing_is_written(self) -> None:
        config = self.seed(".git/config", "[core]\n")
        outside = self.root.parent / "codify-door-outside2.txt"
        outside.write_text("secret\n", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        (self.root / "link.txt").symlink_to(outside)

        for rel in ["../codify-door-outside2.txt", "/etc/hostname", ".git/config", "link.txt", "a\x00b", ""]:
            with self.subTest(rel=rel), self.assertRaises(PathEscapeError):
                self.fs.save_text(rel, "pwned\n", sha(b""))

        self.assertEqual(b"[core]\n", config.read_bytes())
        self.assertEqual("secret\n", outside.read_text(encoding="utf-8"))

    def test_git_metadata_is_named_as_such(self) -> None:
        self.seed(".git/hooks/pre-commit", "#!/bin/sh\n")

        with self.assertRaises(GitMetadataError):
            self.fs.save_text(".git/hooks/pre-commit", "#!/bin/sh\nrm -rf ~\n", sha(b""))

    def test_text_a_file_could_not_hold_is_refused_and_the_file_is_untouched(self) -> None:
        raw = b"keep\n"
        path = self.seed("a.txt", raw)
        cases = {
            "a NUL byte (it would reopen as binary)": ("a\x00b", "binary"),
            "a lone surrogate (it cannot be encoded)": ("a\ud800b", "encoding"),
        }

        for label, (content, reason) in cases.items():
            with self.subTest(label), self.assertRaises(FileNotTextError) as caught:
                self.fs.save_text("a.txt", content, sha(raw))
            self.assertEqual(reason, caught.exception.reason)

        self.assertEqual(raw, path.read_bytes())

    def test_content_over_the_limit_is_refused_by_its_encoded_size(self) -> None:
        raw = b"keep\n"
        path = self.seed("a.txt", raw)
        # One character, three bytes: the limit is on bytes, so this is over while its length is not.
        content = "€" * (MAX_EDIT_BYTES // 3 + 1)
        self.assertLess(len(content), MAX_EDIT_BYTES)

        with self.assertRaises(FileTooLargeError):
            self.fs.save_text("a.txt", content, sha(raw))

        self.assertEqual(raw, path.read_bytes())

    def test_a_file_that_grew_past_the_limit_since_it_was_read_is_refused_unread(self) -> None:
        path = self.seed("a.txt", b"x")
        path.write_bytes(b"a" * (MAX_EDIT_BYTES + 1))

        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read a file over the limit")):
            with self.assertRaises(FileTooLargeError):
                self.fs.save_text("a.txt", "small\n", sha(b"x"))

        self.assertEqual(MAX_EDIT_BYTES + 1, path.stat().st_size)

    def test_a_write_the_system_refuses_is_a_file_access_error_and_the_file_is_untouched(self) -> None:
        raw = b"keep\n"
        path = self.seed("a.txt", raw)

        with mock.patch.object(FileSystemService, "_write_atomic", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(FileAccessError):
                self.fs.save_text("a.txt", "new\n", sha(raw))

        self.assertEqual(raw, path.read_bytes())

    @unittest.skipUnless(os.name == "posix", "the executable bit is a POSIX idea")
    def test_a_saved_script_stays_executable(self) -> None:
        raw = b"#!/bin/sh\necho old\n"
        script = self.seed("run.sh", raw)
        script.chmod(0o755)

        self.fs.save_text("run.sh", "#!/bin/sh\necho new\n", sha(raw))

        self.assertEqual(0o755, script.stat().st_mode & 0o777)

    def test_no_temporary_file_is_left_behind(self) -> None:
        self.seed("a.txt", b"x\n")

        self.fs.save_text("a.txt", "y\n", sha(b"x\n"))

        self.assertEqual(["a.txt"], sorted(p.name for p in self.root.iterdir()))

    def test_saving_through_a_link_inside_the_workspace_writes_the_file_it_points_at(self) -> None:
        raw = b"target\n"
        target = self.seed("real.txt", raw)
        (self.root / "alias.txt").symlink_to(target)

        self.fs.save_text("alias.txt", "edited\n", sha(raw))

        self.assertEqual(b"edited\n", target.read_bytes())
        self.assertTrue((self.root / "alias.txt").is_symlink(), "the link was replaced by a regular file")

    def test_a_workspace_rooted_somewhere_protected_is_not_written(self) -> None:
        raw = b"x\n"
        self.seed("a.txt", raw)

        with mock.patch("engine.fs.protected_root_reason", return_value="it is a system directory"):
            with self.assertRaises(ProtectedRootError):
                self.fs.save_text("a.txt", "y\n", sha(raw))

        self.assertEqual(raw, (self.root / "a.txt").read_bytes())


if __name__ == "__main__":
    unittest.main()
