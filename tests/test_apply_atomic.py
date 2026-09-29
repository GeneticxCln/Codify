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

from engine.fs import FileSystemService, GitMetadataError, PathEscapeError


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

    def test_no_temp_file_is_left_behind(self) -> None:
        with self.failing_second_replace():
            with self.assertRaises(OSError):
                self.apply([create("one.txt"), create("two.txt")])

        self.assertEqual([], [p.name for p in self.root.rglob("*.codify-tmp")])


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
