import tempfile
import unittest
from pathlib import Path

from engine.fs import FileSystemService, PathEscapeError


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


if __name__ == "__main__":
    unittest.main()
