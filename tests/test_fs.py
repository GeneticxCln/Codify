from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import tempfile
import unittest
from pathlib import Path

from engine.fs import MAX_DIFF_BYTES, FileSystemService, PathEscapeError


class TestFileSystemService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.fs = FileSystemService(str(self.root))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_resolve_valid(self):
        resolved = self.fs.resolve("src/main.py")
        self.assertEqual(resolved, self.root / "src/main.py")

    def test_resolve_escapes(self):
        with self.assertRaises(PathEscapeError):
            self.fs.resolve("../secret.txt")

        with self.assertRaises(PathEscapeError):
            self.fs.resolve("/etc/passwd")

        with self.assertRaises(PathEscapeError):
            self.fs.resolve("foo/../../bar")

        with self.assertRaises(PathEscapeError):
            self.fs.resolve("")

    def test_read_text(self):
        # Non-existent returns empty string
        self.assertEqual(self.fs.read_text("missing.txt"), "")

        # Existing returns contents
        test_file = self.root / "hello.txt"
        test_file.write_text("hello world", encoding="utf-8")
        self.assertEqual(self.fs.read_text("hello.txt"), "hello world")

    def test_apply_create_update_delete(self):
        # Create
        files = [{"path": "new_file.txt", "action": "create", "content": "line 1\nline 2\n"}]
        summaries = self.fs.apply(files, dry_run=False)
        self.assertEqual(len(summaries), 1)
        self.assertTrue((self.root / "new_file.txt").exists())
        self.assertIn("+line 1", summaries[0]["unified_diff"])

        # Update
        files = [{"path": "new_file.txt", "action": "update", "content": "line 1\nline 2 modified\n"}]
        summaries = self.fs.apply(files, dry_run=False)
        self.assertEqual(self.fs.read_text("new_file.txt"), "line 1\nline 2 modified\n")
        self.assertIn("-line 2", summaries[0]["unified_diff"])
        self.assertIn("+line 2 modified", summaries[0]["unified_diff"])

        # Delete
        files = [{"path": "new_file.txt", "action": "delete", "content": None}]
        summaries = self.fs.apply(files, dry_run=False)
        self.assertFalse((self.root / "new_file.txt").exists())
        self.assertIn("-line 1", summaries[0]["unified_diff"])

    def test_apply_dry_run(self):
        files = [{"path": "dry_file.txt", "action": "create", "content": "dry content\n"}]
        summaries = self.fs.apply(files, dry_run=True)
        self.assertEqual(len(summaries), 1)
        self.assertFalse((self.root / "dry_file.txt").exists())
        self.assertIn("+dry content", summaries[0]["unified_diff"])


class TestWhatApplyActuallyChanged(unittest.TestCase):
    """`changed` and `diff_note`: the fixer's proposal is not evidence of a change."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.fs = FileSystemService(str(self.root))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_a_proposal_matching_the_current_contents_is_not_a_change(self):
        (self.root / "same.py").write_text("value = 1\n", encoding="utf-8")
        before = (self.root / "same.py").stat().st_mtime_ns

        summaries = self.fs.apply(
            [{"path": "same.py", "action": "update", "content": "value = 1\n"}], dry_run=False
        )

        self.assertFalse(summaries[0]["changed"])
        self.assertEqual(summaries[0]["unified_diff"], "")
        self.assertEqual((self.root / "same.py").stat().st_mtime_ns, before, "not rewritten")

    def test_creating_a_file_that_already_has_that_content_is_not_a_change(self):
        (self.root / "there.py").write_text("x\n", encoding="utf-8")
        summaries = self.fs.apply(
            [{"path": "there.py", "action": "create", "content": "x\n"}], dry_run=False
        )
        self.assertFalse(summaries[0]["changed"])

    def test_deleting_a_missing_file_is_not_a_change(self):
        summaries = self.fs.apply(
            [{"path": "ghost.py", "action": "delete", "content": None}], dry_run=False
        )
        self.assertFalse(summaries[0]["changed"], "nothing disappeared, so nothing changed")

    def test_deleting_an_empty_file_is_a_change(self):
        (self.root / "empty.py").write_text("", encoding="utf-8")
        summaries = self.fs.apply(
            [{"path": "empty.py", "action": "delete", "content": None}], dry_run=False
        )
        self.assertTrue(summaries[0]["changed"], "the file is gone even with no content")
        self.assertFalse((self.root / "empty.py").exists())
        self.assertEqual(summaries[0]["unified_diff"], "", "an empty file has no diff to show")

    def test_a_binary_file_still_gets_written_and_says_why_there_is_no_diff(self):
        (self.root / "logo.bin").write_bytes(b"\x00\x01\x02old")
        summaries = self.fs.apply(
            [{"path": "logo.bin", "action": "update", "content": "\x00\x01\x02new"}],
            dry_run=False,
        )
        self.assertTrue(summaries[0]["changed"])
        self.assertEqual(summaries[0]["unified_diff"], "")
        self.assertEqual(summaries[0]["diff_note"], "file is binary")
        self.assertEqual((self.root / "logo.bin").read_bytes(), b"\x00\x01\x02new")

    def test_an_oversized_file_is_written_without_building_a_huge_diff(self):
        big = "x" * (MAX_DIFF_BYTES + 10)
        (self.root / "big.txt").write_text(big, encoding="utf-8")
        summaries = self.fs.apply(
            [{"path": "big.txt", "action": "update", "content": "small now\n"}], dry_run=False
        )
        self.assertEqual(summaries[0]["unified_diff"], "")
        self.assertIn("too large to diff", summaries[0]["diff_note"])
        self.assertEqual(self.fs.read_text("big.txt"), "small now\n")

    def test_a_file_that_cannot_be_decoded_is_diffed_as_missing_text(self):
        (self.root / "latin.txt").write_bytes(b"caf\xe9 not utf-8")
        summaries = self.fs.apply(
            [{"path": "latin.txt", "action": "update", "content": "now utf-8\n"}], dry_run=False
        )
        self.assertIn(summaries[0]["diff_note"], ("file is not UTF-8 text", "file is binary"))
        self.assertEqual(self.fs.read_text("latin.txt"), "now utf-8\n")

    def test_no_temporary_file_is_left_behind(self):
        self.fs.apply([{"path": "a.txt", "action": "create", "content": "a\n"}], dry_run=False)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.txt"])

    def test_read_text_or_none_is_none_exactly_when_there_is_no_text(self):
        (self.root / "ok.txt").write_text("text\n", encoding="utf-8")
        (self.root / "blob.bin").write_bytes(b"\x00\x01binary")
        (self.root / "dir").mkdir()

        self.assertEqual(self.fs.read_text_or_none("ok.txt"), "text\n")
        self.assertIsNone(self.fs.read_text_or_none("blob.bin"), "a NUL byte is not source code")
        self.assertIsNone(self.fs.read_text_or_none("dir"))
        self.assertIsNone(self.fs.read_text_or_none("missing.py"))
        self.assertIsNone(self.fs.read_text_or_none("../../etc/passwd"))
        self.assertIsNone(self.fs.read_text_or_none("/etc/passwd"))

    def test_a_symlink_out_of_the_workspace_is_still_refused(self):
        outside = Path(self.temp_dir.name).parent / "codify-fs-outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        try:
            (self.root / "link.txt").symlink_to(outside)
            with self.assertRaises(PathEscapeError):
                self.fs.resolve("link.txt")
        finally:
            outside.unlink(missing_ok=True)


class TestEditAction(unittest.TestCase):
    """The fixer's search/replace op: resolved against the file as it exists."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.fs = FileSystemService(str(self.root))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed(self, text: str, name: str = "svc.py") -> None:
        (self.root / name).write_text(text, encoding="utf-8")

    def test_an_exact_match_edit_replaces_and_reports_resolved_content(self):
        self._seed("def greet():\n    return 1\n")
        summaries = self.fs.apply(
            [{"path": "svc.py", "action": "edit",
              "edits": [{"old_text": "return 1", "new_text": "return 42"}]}],
            dry_run=False,
        )
        self.assertEqual(self.fs.read_text("svc.py"), "def greet():\n    return 42\n")
        self.assertTrue(summaries[0]["changed"])
        self.assertIn("-    return 1", summaries[0]["unified_diff"])
        self.assertIn("+    return 42", summaries[0]["unified_diff"])
        # The resolved full content is what a dry-run proposal would store.
        self.assertEqual(summaries[0]["resolved_content"], "def greet():\n    return 42\n")

    def test_a_dry_run_edit_resolves_without_writing(self):
        self._seed("a = 1\n")
        summaries = self.fs.apply(
            [{"path": "svc.py", "action": "edit",
              "edits": [{"old_text": "a = 1", "new_text": "a = 2"}]}],
            dry_run=True,
        )
        # Nothing written, but the proposal is concrete content, ready to store.
        self.assertEqual(self.fs.read_text("svc.py"), "a = 1\n")
        self.assertTrue(summaries[0]["changed"])
        self.assertEqual(summaries[0]["resolved_content"], "a = 2\n")

    def test_a_no_op_edit_is_not_a_change(self):
        self._seed("a = 1\n")
        summaries = self.fs.apply(
            [{"path": "svc.py", "action": "edit",
              "edits": [{"old_text": "a = 1", "new_text": "a = 1"}]}],
            dry_run=False,
        )
        self.assertFalse(summaries[0]["changed"])

    def test_a_count_mismatch_is_refused(self):
        self._seed("x = f()\ny = f()\n")
        with self.assertRaises(ValueError) as ctx:
            self.fs.apply(
                [{"path": "svc.py", "action": "edit",
                  "edits": [{"old_text": "f()", "new_text": "g()"}]}],
                dry_run=False,
            )
        self.assertIn("appears 2 time(s), expected 1", str(ctx.exception))
        # The file is untouched — a refused edit must not write half an edit.
        self.assertEqual(self.fs.read_text("svc.py"), "x = f()\ny = f()\n")

    def test_count_zero_replaces_every_occurrence(self):
        self._seed("x = f()\ny = f()\n")
        self.fs.apply(
            [{"path": "svc.py", "action": "edit",
              "edits": [{"old_text": "f()", "new_text": "g()", "count": 0}]}],
            dry_run=False,
        )
        self.assertEqual(self.fs.read_text("svc.py"), "x = g()\ny = g()\n")

    def test_an_absent_old_text_is_refused(self):
        self._seed("a = 1\n")
        with self.assertRaises(ValueError) as ctx:
            self.fs.apply(
                [{"path": "svc.py", "action": "edit",
                  "edits": [{"old_text": "not there", "new_text": "x"}]}],
                dry_run=False,
            )
        self.assertIn("old_text appears 0 time(s), expected 1", str(ctx.exception))

    def test_edits_apply_in_order_against_the_previous_result(self):
        self._seed("def greet():\n    return 1\n")
        self.fs.apply(
            [{"path": "svc.py", "action": "edit", "edits": [
                {"old_text": "return 1", "new_text": "return 2"},
                {"old_text": "return 2", "new_text": "return 3"},
            ]}],
            dry_run=False,
        )
        self.assertEqual(self.fs.read_text("svc.py"), "def greet():\n    return 3\n")

    def test_an_edit_on_a_missing_file_is_refused(self):
        with self.assertRaises(ValueError):
            self.fs.apply(
                [{"path": "ghost.py", "action": "edit",
                  "edits": [{"old_text": "x", "new_text": "y"}]}],
                dry_run=False,
            )


if __name__ == "__main__":
    unittest.main()
