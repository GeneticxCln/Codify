from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import subprocess
import tempfile
import unittest
from pathlib import Path

from engine.git import GitService


class GitTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.git = GitService()
        self.git.init_repo(str(self.root))

    def tearDown(self):
        self.temp_dir.cleanup()

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text)

    def git_out(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=False
        ).stdout

    def committed_paths(self, rev: str = "HEAD") -> set[str]:
        return {
            line
            for line in self.git_out("ls-tree", "-r", "--name-only", rev).splitlines()
            if line
        }

    def porcelain(self) -> str:
        return self.git_out("status", "--porcelain")


class TestGitService(GitTestBase):
    def test_git_init_and_detection(self):
        self.assertTrue(self.git.is_git_repo(str(self.root)))
        self.assertFalse(self.git.is_git_repo(str(self.root / "nope")))

    def test_status_and_commit(self):
        self.assertEqual(self.git.get_status(str(self.root)).strip(), "")

        self.write("hello.py", "print('hello')\n")
        self.assertIn("hello.py", self.git.get_status(str(self.root)))

        rev = self.git.commit(str(self.root), "feat: initial commit", ["hello.py"])
        self.assertIsNotNone(rev)
        self.assertEqual(len(rev), 40)
        self.assertEqual(self.git.get_status(str(self.root)).strip(), "")

        # Nothing left to record for that path.
        self.assertIsNone(self.git.commit(str(self.root), "feat: duplicate", ["hello.py"]))

    def test_a_commit_contains_only_the_paths_it_names(self):
        """The regression: `git add -A` swept the user's work into our commit.

        A workspace is usually a working tree with somebody's unfinished business in
        it. A step that wrote one file must not commit the others — not the ones
        sitting untracked, and not the ones the user had already staged.
        """
        self.write("ours.py", "codify wrote this\n")
        self.write("user_wip.py", "half-finished by hand\n")
        self.write("user_staged.py", "deliberately staged by hand\n")
        subprocess.run(
            ["git", "add", "--", "user_staged.py"], cwd=self.root, check=False
        )

        rev = self.git.commit(str(self.root), "feat: our change", ["ours.py"])
        self.assertIsNotNone(rev)
        self.assertEqual(self.committed_paths(), {"ours.py"})

        status = self.porcelain()
        self.assertIn("?? user_wip.py", status, "untracked work stays untracked")
        self.assertIn("A  user_staged.py", status, "the user's staged file stays staged")

    def test_an_empty_path_list_commits_nothing_at_all(self):
        """A step that changed nothing must not commit what the user had staged."""
        self.write("user_staged.py", "staged before we ran\n")
        subprocess.run(["git", "add", "--", "user_staged.py"], cwd=self.root, check=False)

        self.assertIsNone(self.git.commit(str(self.root), "feat: nothing", []))
        # No commit was made at all: the repository still has an unborn HEAD.
        self.assertEqual(self.git_out("log", "--oneline").strip(), "")
        self.assertIn("A  user_staged.py", self.porcelain())

    def test_a_deletion_is_part_of_the_commit(self):
        self.write("gone.py", "temporary\n")
        self.git.commit(str(self.root), "feat: add gone", ["gone.py"])
        (self.root / "gone.py").unlink()

        rev = self.git.commit(str(self.root), "chore: remove gone", ["gone.py"])
        self.assertIsNotNone(rev)
        self.assertEqual(self.committed_paths(), set())
        self.assertEqual(self.git.get_status(str(self.root)).strip(), "")

    def test_a_path_that_does_not_exist_is_not_an_error_per_path(self):
        """A mixed proposal (one real file, one phantom) still commits the real one."""
        self.write("real.py", "content\n")
        rev = self.git.commit(str(self.root), "feat: add real", ["real.py", "phantom.py"])
        self.assertIsNotNone(rev)
        self.assertEqual(self.committed_paths(), {"real.py"})


if __name__ == "__main__":
    unittest.main()
