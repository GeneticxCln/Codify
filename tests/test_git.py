import tempfile
import unittest
from pathlib import Path

from engine.git import GitService


class TestGitService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.git = GitService()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_git_init_and_detection(self):
        self.assertFalse(self.git.is_git_repo(str(self.root)))
        ok = self.git.init_repo(str(self.root))
        self.assertTrue(ok)
        self.assertTrue(self.git.is_git_repo(str(self.root)))

    def test_status_and_commit(self):
        self.git.init_repo(str(self.root))

        # Empty status
        self.assertEqual(self.git.get_status(str(self.root)).strip(), "")

        # Create file
        (self.root / "hello.py").write_text("print('hello')\n")
        status = self.git.get_status(str(self.root))
        self.assertIn("hello.py", status)

        # Commit
        rev = self.git.commit(str(self.root), "feat: initial commit")
        self.assertIsNotNone(rev)
        self.assertEqual(len(rev), 40)

        # Clean status after commit
        self.assertEqual(self.git.get_status(str(self.root)).strip(), "")

        # Commit when no changes returns None
        rev2 = self.git.commit(str(self.root), "feat: duplicate")
        self.assertIsNone(rev2)


if __name__ == "__main__":
    unittest.main()
