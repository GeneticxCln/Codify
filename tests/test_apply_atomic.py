"""`FileSystemService.apply` changes the tree completely or not at all (audit of 2026-09-29, M4).

Its docstring said every operation is resolved before anything is written; it was not.
`[create a, create b, edit-with-missing-text]` raised on the third op with `a.txt` and `b.txt`
already on disk, and so did a batch with a `.git` path, a `../` escape or an unknown action in it.
The step failed, the tree was half-changed and uncommitted, and `_publish_changes` runs only after
`apply` returns, so nothing ever announced the files that had been written.

Two phases now. Everything is resolved and validated first, against a virtual view that reflects the
ops before it in the same batch (an edit of a file the batch just created must see it); then the
writes happen, and a write that fails half-way puts back what it had already touched — content, mode,
deleted files and created directories included.

The assertion in every case is about the tree: a snapshot of every path, its bytes and its mode,
taken before and compared after. Not "an exception was raised".
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.fs import MAX_DIFF_BYTES, FileSystemService, GitMetadataError, PathEscapeError


def snapshot(root: Path) -> dict[str, tuple[str, bytes | None, int | None]]:
    """Every path under `root` (and the directory itself), with its bytes and mode."""
    seen: dict[str, tuple[str, bytes | None, int | None]] = {}
    for path in sorted([root, *root.rglob("*")]):
        info = path.lstat()
        kind = "dir" if stat.S_ISDIR(info.st_mode) else "file"
        seen[str(path.relative_to(root))] = (
            kind, None if kind == "dir" else path.read_bytes(), stat.S_IMODE(info.st_mode),
        )
    return seen


class ApplyCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "ws"
        self.root.mkdir()
        (self.root / "keep.txt").write_text("keep me\n", encoding="utf-8")
        (self.root / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        os.chmod(self.root / "run.sh", 0o700)
        (self.root / "blob.bin").write_bytes(b"\x00\x01\x02binary\xff")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        self.fs = FileSystemService(str(self.root))

    def apply(self, ops: list[dict[str, Any]], *, dry_run: bool = False) -> list[dict[str, Any]]:
        return self.fs.apply(ops, dry_run=dry_run)

    def assertUntouched(self, ops: list[dict[str, Any]], exception: type[BaseException]) -> BaseException:
        before = snapshot(self.root)
        with self.assertRaises(exception) as caught:
            self.apply(ops)
        self.assertEqual(before, snapshot(self.root), "the tree changed although the batch was refused")
        self.assertFalse((self.base / "escape.txt").exists(), "a file was written outside the workspace")
        return caught.exception


def create(path: str, content: str = "new\n") -> dict[str, Any]:
    return {"path": path, "action": "create", "content": content}


class TestABatchThatCannotBeAppliedWritesNothing(ApplyCase):
    """The four reproductions from the audit, each of which used to leave a.txt and b.txt behind."""

    def test_an_edit_whose_text_is_missing(self) -> None:
        error = self.assertUntouched(
            [create("a.txt"), create("b.txt"),
             {"path": "keep.txt", "action": "edit", "edits": [{"old_text": "not there", "new_text": "x"}]}],
            ValueError,
        )
        self.assertIn("keep.txt", str(error))

    def test_a_path_inside_git_metadata(self) -> None:
        self.assertUntouched(
            [create("a.txt"), {"path": ".git/config", "action": "update", "content": "[core]\n\thooksPath=x\n"}],
            GitMetadataError,
        )

    def test_a_path_that_escapes_the_workspace(self) -> None:
        self.assertUntouched([create("a.txt"), create("../escape.txt")], PathEscapeError)

    def test_an_unknown_action(self) -> None:
        self.assertUntouched([create("a.txt"), {"path": "b.txt", "action": "frobnicate"}], ValueError)

    def test_a_malformed_edit_list(self) -> None:
        self.assertUntouched(
            [create("a.txt"), {"path": "keep.txt", "action": "edit", "edits": [{"old_text": "keep"}]}],
            ValueError,
        )


    def test_a_target_that_is_a_directory(self) -> None:
        # Refused up front. It used to fail at the rename, after earlier files were written.
        (self.root / "adir").mkdir()

        error = self.assertUntouched([create("a.txt"), create("adir", "x\n")], ValueError)

        self.assertIn("adir", str(error))


class TestABatchStillMeansWhatItMeantOneOpAtATime(ApplyCase):
    """Validating everything first must not change what a sequence of ops *does*."""

    def test_an_edit_sees_the_file_the_same_batch_just_created(self) -> None:
        self.apply([
            create("fresh.txt", "alpha beta\n"),
            {"path": "fresh.txt", "action": "edit", "edits": [{"old_text": "beta", "new_text": "gamma"}]},
        ])

        self.assertEqual("alpha gamma\n", (self.root / "fresh.txt").read_text(encoding="utf-8"))

    def test_a_file_deleted_and_recreated_in_one_batch_ends_up_recreated(self) -> None:
        summaries = self.apply([
            {"path": "keep.txt", "action": "delete"}, create("keep.txt", "again\n"),
        ])

        self.assertEqual("again\n", (self.root / "keep.txt").read_text(encoding="utf-8"))
        self.assertTrue(all(s["changed"] for s in summaries))

    def test_a_second_edit_of_the_same_file_applies_to_the_first_ones_result(self) -> None:
        self.apply([
            {"path": "keep.txt", "action": "edit", "edits": [{"old_text": "keep", "new_text": "hold"}]},
            {"path": "keep.txt", "action": "edit", "edits": [{"old_text": "hold me", "new_text": "held"}]},
        ])

        self.assertEqual("held\n", (self.root / "keep.txt").read_text(encoding="utf-8"))

    def test_the_diff_reports_the_change_relative_to_the_batch_so_far(self) -> None:
        summaries = self.apply([
            {"path": "keep.txt", "action": "update", "content": "one\n"},
            {"path": "keep.txt", "action": "update", "content": "two\n"},
        ])

        self.assertIn("-one", summaries[1]["unified_diff"])
        self.assertIn("+two", summaries[1]["unified_diff"])

    def test_a_dry_run_writes_nothing_and_reports_the_same_summaries(self) -> None:
        before = snapshot(self.root)
        summaries = self.apply([create("a.txt"), {"path": "keep.txt", "action": "delete"}], dry_run=True)

        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(["a.txt", "keep.txt"], [s["path"] for s in summaries])
        self.assertTrue(all(s["changed"] for s in summaries))

    def test_a_no_op_update_is_not_reported_as_a_change(self) -> None:
        summaries = self.apply([{"path": "keep.txt", "action": "update", "content": "keep me\n"}])

        self.assertFalse(summaries[0]["changed"])


class TestAWriteThatFailsHalfWayPutsEverythingBack(ApplyCase):
    def failing_second_replace(self) -> Any:
        real = os.replace
        calls = {"n": 0}

        def replace(src: Any, dst: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(28, "No space left on device")
            real(src, dst)

        return patch("engine.fs.os.replace", replace)

    def test_the_first_write_is_undone_including_a_directory_it_created(self) -> None:
        before = snapshot(self.root)

        with self.failing_second_replace():
            with self.assertRaises(OSError):
                self.apply([
                    {"path": "keep.txt", "action": "update", "content": "changed\n"},
                    create("deep/er/file.txt"),
                ])

        self.assertEqual(before, snapshot(self.root), "a half-applied batch was left on disk")
        self.assertFalse((self.root / "deep").exists(), "a directory the failed batch created is still there")

    def test_a_directory_made_by_a_write_that_succeeded_is_removed_too(self) -> None:
        # The case the rollback exists for: `_write_atomic` cleans up after *its own* failure, but
        # `deep/` was made by the first write, which went fine, and only the batch knows it is
        # now the batch's to remove.
        before = snapshot(self.root)

        with self.failing_second_replace():
            with self.assertRaises(OSError):
                self.apply([create("deep/er/first.txt"), create("second.txt")])

        self.assertEqual(before, snapshot(self.root))
        self.assertFalse((self.root / "deep").exists())

    def test_an_executable_bit_survives_the_round_trip(self) -> None:
        before = snapshot(self.root)

        with self.failing_second_replace():
            with self.assertRaises(OSError):
                self.apply([
                    {"path": "run.sh", "action": "update", "content": "#!/bin/sh\necho changed\n"},
                    create("second.txt"),
                ])

        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(0o700, stat.S_IMODE((self.root / "run.sh").stat().st_mode))

    def test_a_deleted_file_comes_back_with_its_bytes_and_mode(self) -> None:
        before = snapshot(self.root)

        with self.failing_second_replace():
            with self.assertRaises(OSError):
                self.apply([
                    {"path": "run.sh", "action": "delete"},
                    create("one.txt"),
                    create("two.txt"),
                ])

        self.assertEqual(before, snapshot(self.root))

    def test_a_binary_original_is_restored_byte_for_byte(self) -> None:
        # The diff machinery cannot show a binary file, so it never held its text; the rollback
        # must hold the *bytes*, or restoring it would have rewritten it as something else.
        before = snapshot(self.root)

        with self.failing_second_replace():
            with self.assertRaises(OSError):
                self.apply([
                    {"path": "blob.bin", "action": "update", "content": "now text\n"},
                    create("second.txt"),
                ])

        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(b"\x00\x01\x02binary\xff", (self.root / "blob.bin").read_bytes())

    def test_a_file_replaced_by_a_folder_comes_back_as_the_file(self) -> None:
        # `keep.txt` -> `keep.txt/inner.txt` is one batch that deletes a file and makes a folder of its name. When
        # a later write fails, the folder has to go before the file can come back; the rollback used to restore
        # the file first, could not (a folder stood there), and left neither, naming `keep.txt` as lost.
        before = snapshot(self.root)

        with self.failing_second_replace():
            with self.assertRaises(OSError) as caught:
                self.apply([
                    {"path": "keep.txt", "action": "delete"},
                    create("keep.txt/inner.txt"),
                    create("second.txt"),
                ])

        self.assertEqual(before, snapshot(self.root), "the file the batch deleted did not come back")
        self.assertNotIn("put back", str(caught.exception))

    def test_no_temp_file_is_left_behind(self) -> None:
        with self.failing_second_replace():
            with self.assertRaises(OSError):
                self.apply([create("one.txt"), create("two.txt")])

        self.assertEqual([], [p.name for p in self.root.rglob("*.codify-tmp")])


class TestAFileTheEngineCannotReadIsNotTouched(ApplyCase):
    """An existing file whose bytes the engine cannot read cannot be put back, so a batch may not touch it.

    `_Original.of` recorded such a file as `None`, which the rollback reads as "did not exist": a batch that failed
    after overwriting it unlinked the person's file. (As root nothing is unreadable, so these tests make `read_bytes`
    refuse one path, as a mode-0200 file does for any other user. The real-EACCES proof was a one-off script run
    as another uid, against a mode-0200 file, with a second `os.replace` made to fail; it is not repeated here. These
    tests prove only the code path: that an `OSError` on reading is never taken for "the path did not exist".)
    """

    def unreadable(self, name: str, *, after_reads: int = 0) -> Any:
        real, target, seen = Path.read_bytes, self.root / name, {"n": 0}

        def read_bytes(path: Path) -> bytes:
            if path == target:
                seen["n"] += 1
                if seen["n"] > after_reads:
                    raise PermissionError(13, "Permission denied", str(path))
            return real(path)

        return patch.object(Path, "read_bytes", read_bytes)

    def test_no_action_on_it_gets_past_phase_one(self) -> None:
        ops = {
            "create": create("keep.txt", "new\n"),
            "update": {"path": "keep.txt", "action": "update", "content": "new\n"},
            "edit": {"path": "keep.txt", "action": "edit", "edits": [{"old_text": "keep", "new_text": "kept"}]},
            "delete": {"path": "keep.txt", "action": "delete"},
        }
        for action, op in ops.items():
            for dry_run in (True, False):
                with self.subTest(action=action, dry_run=dry_run):
                    before = snapshot(self.root)
                    with self.unreadable("keep.txt"):
                        with self.assertRaises(ValueError) as caught:
                            self.apply([create("first.txt"), op, create("last.txt")], dry_run=dry_run)
                    self.assertEqual(before, snapshot(self.root))
                    message = str(caught.exception)
                    self.assertIn("keep.txt", message)
                    self.assertIn("cannot be read", message)
                    self.assertNotIn("old_text", message)  # an edit is not told its text was wrong
                    self.assertIn("(Permission denied)", message)
                    self.assertNotIn("Errno", message)  # the reason is prose for a model, not the OS's repr
                    self.assertNotIn(str(self.root), message)  # and carries no absolute path
                    self.assertFalse(message.endswith("."), "the re-ask adds its own full stop")

    def test_a_file_over_the_diff_limit_is_judged_the_same_way(self) -> None:
        # `_read_for_diff` does not read such a file, so it used to pass phase one (and a dry run) and fail in
        # phase two with a bare PermissionError, which nothing asks the fixer about.
        (self.root / "big.log").write_bytes(b"x" * (MAX_DIFF_BYTES + 10))
        real, target = Path.open, self.root / "big.log"

        def refuse(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == target:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *args, **kwargs)

        for action in ("update", "delete"):
            for dry_run in (True, False):
                with self.subTest(action=action, dry_run=dry_run):
                    before = snapshot(self.root)
                    op = {"path": "big.log", "action": action, "content": "new\n"}
                    with patch.object(Path, "open", refuse):
                        with self.assertRaises(ValueError) as caught:
                            self.apply([create("first.txt"), op], dry_run=dry_run)
                    self.assertEqual(before, snapshot(self.root))
                    self.assertIn("big.log exists but cannot be read", str(caught.exception))

    def test_a_failure_of_its_own_operation_does_not_make_the_rollback_report_an_intact_file_as_lost(self) -> None:
        # The directory refuses the unlink; the rollback used to rewrite the untouched file, fail the same way, and
        # tell the person it "could not be put back" although its bytes never changed.
        real_unlink, real_write = Path.unlink, FileSystemService._write_atomic
        target = self.root / "keep.txt"

        def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == target:
                raise PermissionError(13, "Permission denied", str(path))
            real_unlink(path, *args, **kwargs)

        def write_atomic(service: FileSystemService, path: Path, data: bytes) -> list[Path]:
            if path == target:
                raise PermissionError(13, "Permission denied", str(path))
            return real_write(service, path, data)

        before = snapshot(self.root)
        with patch.object(Path, "unlink", unlink), patch.object(FileSystemService, "_write_atomic", write_atomic):
            with self.assertRaises(OSError) as caught:
                self.apply([create("first.txt"), {"path": "keep.txt", "action": "delete"}])

        self.assertEqual(before, snapshot(self.root))
        self.assertNotIn("put back", str(caught.exception))

    def test_a_file_that_stops_being_readable_after_phase_one_is_never_taken_for_absent(self) -> None:
        for op in (
            {"path": "keep.txt", "action": "update", "content": "new\n"},
            {"path": "keep.txt", "action": "delete"},
        ):
            with self.subTest(action=op["action"]):
                before = snapshot(self.root)
                with self.unreadable("keep.txt", after_reads=1):  # phase one reads it once; every later read fails
                    with self.assertRaises(OSError):
                        self.apply([create("first.txt"), op])
                self.assertEqual(before, snapshot(self.root))


class TestAPathThatIsNeitherAFileNorAFolderIsNotTouched(ApplyCase):
    """`is_file()` calls a pipe absent, so it was recorded as "did not exist" and a rollback unlinked it."""

    def test_a_named_pipe_is_refused_and_survives(self) -> None:
        os.mkfifo(self.root / "pipe")
        for action in ("create", "update", "delete"):
            for dry_run in (True, False):
                with self.subTest(action=action, dry_run=dry_run):
                    op = {"path": "pipe", "action": action, "content": "new\n"}
                    with self.assertRaises(ValueError) as caught:
                        self.apply([create("first.txt"), op], dry_run=dry_run)
                    self.assertIn("pipe is not a regular file", str(caught.exception))
                    self.assertTrue(stat.S_ISFIFO((self.root / "pipe").lstat().st_mode))
                    self.assertFalse((self.root / "first.txt").exists())


class TestAPathThatIsBothAFileAndAFolderIsRefusedBeforeAnythingIsWritten(ApplyCase):
    """The filesystem used to say so in phase two, after a dry run had accepted the batch."""

    def test_each_clash_is_refused_in_a_dry_run_and_a_real_run_alike(self) -> None:
        cases = {
            "a file, then a path inside it": ([create("a.py"), create("a.py/b.py")], ["a.py", "a.py/b.py"]),
            "a path inside it, then the file": ([create("a.py/b.py"), create("a.py")], ["a.py", "a.py/b.py"]),
            "a path inside an existing file": ([create("run.sh/deeper/inner.py")], ["run.sh", "run.sh/deeper/inner.py"]),
            "an existing file updated, then a path inside it": (
                [{"path": "keep.txt", "action": "update", "content": "n\n"}, create("keep.txt/inner.py")],
                ["keep.txt", "keep.txt/inner.py"],
            ),
        }
        for name, (ops, named) in cases.items():
            for dry_run in (True, False):
                with self.subTest(case=name, dry_run=dry_run):
                    before = snapshot(self.root)
                    with self.assertRaises(ValueError) as caught:
                        self.apply(ops, dry_run=dry_run)
                    self.assertEqual(before, snapshot(self.root))
                    for path in named:
                        self.assertIn(path, str(caught.exception))

    def test_each_sentence_names_the_file_and_the_path_inside_it_apart(self) -> None:
        # The file's name is a prefix of the path inside it, so a bare assertIn cannot tell a sentence that names
        # both from one that names only the longer. These are anchored on the words around each name.
        with self.assertRaises(ValueError) as both:
            self.apply([create("a.py"), create("a.py/b.py")], dry_run=True)
        self.assertEqual(
            "a.py and a.py/b.py cannot both be written: a.py is a file, and a.py/b.py would be inside it "
            "as if it were a folder. Keep one of them",
            str(both.exception),
        )

        with self.assertRaises(ValueError) as existing:
            self.apply([create("run.sh/deeper/inner.py")], dry_run=True)
        self.assertEqual(
            "run.sh/deeper/inner.py cannot be written: run.sh is an existing file, not a folder. "
            "To replace the file with a folder, delete run.sh in the same batch",
            str(existing.exception),
        )

    def test_a_folder_deleted_in_the_batch_does_not_end_the_search_for_a_file_above_it(self) -> None:
        # Walking up from x/a/c.py, x/a is deleted in the batch (a folder may take its place) and the walk must go on
        # to x, which the same batch makes a file. Ending the walk at the deleted path would let this through to
        # phase two, where it fails on the filesystem after a dry run had accepted it.
        ops = [create("x"), {"path": "x/a", "action": "delete"}, create("x/a/c.py")]
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                before = snapshot(self.root)
                with self.assertRaises(ValueError) as caught:
                    self.apply(ops, dry_run=dry_run)
                self.assertEqual(before, snapshot(self.root))
                self.assertEqual(
                    "x and x/a/c.py cannot both be written: x is a file, and x/a/c.py would be inside it "
                    "as if it were a folder. Keep one of them",
                    str(caught.exception),
                )

    def test_an_ancestor_on_disk_with_a_name_that_is_not_utf8_is_named_in_ascii(self) -> None:
        # A symlink in the workspace to a file whose name is not UTF-8: the ancestor's spelling comes from the
        # disk, carries a lone surrogate, and the sentence is a model's prompt and an event payload.
        with open(os.fsencode(str(self.root)) + b"/\xff.txt", "wb") as handle:
            handle.write(b"x\n")
        os.symlink("\udcff.txt", self.root / "ln")

        with self.assertRaises(ValueError) as caught:
            self.apply([create("ln/x.txt")], dry_run=True)

        message = str(caught.exception)
        message.encode("utf-8")  # a surrogate would raise here
        self.assertIn("'\\udcff.txt' is an existing file", message)

    def test_a_file_replaced_by_a_folder_of_its_name_still_works(self) -> None:
        # `utils.py` -> `utils/__init__.py`. It succeeded before, and must keep doing so.
        ops = [{"path": "keep.txt", "action": "delete"}, create("keep.txt/inner.txt")]

        self.apply(ops, dry_run=True)
        self.apply(ops)

        self.assertEqual("new\n", (self.root / "keep.txt" / "inner.txt").read_text(encoding="utf-8"))

    def test_and_so_does_listing_the_folders_file_before_the_deletion(self) -> None:
        # The same batch in the other order failed in phase two (OSError): the deletions now run first.
        ops = [create("keep.txt/inner.txt"), {"path": "keep.txt", "action": "delete"}]

        self.apply(ops, dry_run=True)
        self.apply(ops)

        self.assertEqual("new\n", (self.root / "keep.txt" / "inner.txt").read_text(encoding="utf-8"))

    def test_a_clash_that_the_batch_itself_undoes_is_not_one(self) -> None:
        # Judged on where each path ends up, as phase two does. These succeeded before and still do.
        before = snapshot(self.root)
        self.apply([create("keep.txt/x.txt"), {"path": "keep.txt/x.txt", "action": "delete"}])
        self.assertEqual(before, snapshot(self.root))

        self.apply([create("a.py/b.py"), {"path": "a.py/b.py", "action": "delete"}, create("a.py")])
        self.assertEqual("new\n", (self.root / "a.py").read_text(encoding="utf-8"))


class TestASuccessfulBatchIsUnchanged(ApplyCase):
    def test_ordinary_writes_keep_the_mode_of_the_file_they_replace(self) -> None:
        self.apply([{"path": "run.sh", "action": "update", "content": "#!/bin/sh\necho two\n"}])

        self.assertEqual(0o700, stat.S_IMODE((self.root / "run.sh").stat().st_mode))
        self.assertEqual("#!/bin/sh\necho two\n", (self.root / "run.sh").read_text(encoding="utf-8"))

    def test_a_new_nested_file_is_created_with_its_directories(self) -> None:
        self.apply([create("a/b/c.txt", "deep\n")])

        self.assertEqual("deep\n", (self.root / "a" / "b" / "c.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
