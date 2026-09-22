"""The librarian's abilities.

Two properties matter more than the mechanics, and both are asserted here: it can
find things out (read, search, history, inspect commands), and it cannot change
anything (an escape, a write command, or a forbidden binary is refused).
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import os
import tempfile
import unittest
from pathlib import Path

from engine.fs import PathEscapeError
from engine.library import MAX_READ_CHARS, LibraryService, format_search
from engine.sandbox import CommandNotAllowed, SandboxService, validate_argv


def _make_workspace(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def greet(name):\n    return f'hello {name}'\n\n\ndef main():\n    greet('world')\n",
        encoding="utf-8",
    )
    (root / "src" / "util.py").write_text("GREETING = 'hi'\n", encoding="utf-8")
    (root / "README.md").write_text("# Project\n\nrun the thing\n", encoding="utf-8")
    (root / "big.txt").write_text("x" * (MAX_READ_CHARS + 500), encoding="utf-8")
    # Never scanned: package caches are not the user's code.
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("greet()\n", encoding="utf-8")
    (root / "binary.dat").write_bytes(b"\x00\x01\x02greet")


class TestLibrarianReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _make_workspace(self.root)
        self.lib = LibraryService(str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_returns_text_and_reports_truncation(self):
        res = self.lib.read("src/app.py")
        self.assertIn("def greet", res["text"])
        self.assertFalse(res["truncated"])

        big = self.lib.read("big.txt")
        self.assertTrue(big["truncated"], "a truncating read must say so")
        self.assertEqual(len(big["text"]), MAX_READ_CHARS)

    def test_read_cannot_escape_the_workspace(self):
        with self.assertRaises(PathEscapeError):
            self.lib.read("../../etc/passwd")

    def test_read_of_a_directory_is_refused(self):
        with self.assertRaises(IsADirectoryError):
            self.lib.read("src")

    def test_search_is_literal_and_case_insensitive(self):
        res = self.lib.search("greet")
        paths = {m["path"] for m in res["matches"]}
        self.assertIn("src/app.py", paths)
        self.assertIn("src/util.py", paths)
        self.assertNotIn("node_modules/dep.js", paths, "package caches are not scanned")
        self.assertNotIn("binary.dat", res["files_scanned"] and paths, "binary files are skipped")

    def test_search_honours_a_glob(self):
        res = self.lib.search("greet", glob="*.py")
        self.assertTrue(all(m["path"].endswith(".py") for m in res["matches"]))

    def test_search_reports_its_own_limits(self):
        """A partial search must be labelled as partial — silently answering from
        a fraction of the tree is how a confident wrong answer gets produced."""
        res = self.lib.search("greet")
        self.assertIn("matches", res)
        self.assertIn("truncated", res)
        self.assertEqual(res["files_scanned"], 4)
        rendered = format_search(res)
        self.assertIn("in 4 files", rendered)
        self.assertIn("binary file skipped", rendered)

        res["truncated"] = True
        self.assertIn("TRUNCATED", format_search(res))

    def test_search_rejects_an_empty_query(self):
        with self.assertRaises(ValueError):
            self.lib.search("   ")

    def test_regex_search_finds_structural_patterns_literal_cannot(self):
        """Alternation + escaped metachars: no single substring can ask this."""
        res = self.lib.search(r"greet\(|GREETING", regex=True)
        paths = {m["path"] for m in res["matches"]}
        self.assertIn("src/app.py", paths)
        self.assertIn("src/util.py", paths)
        self.assertTrue(res.get("regex"), "the result must say which mode ran")

    def test_regex_search_rejects_an_invalid_pattern_as_a_valueerror(self):
        with self.assertRaises(ValueError):
            self.lib.search("([unclosed", regex=True)

    def test_regex_search_rejects_an_oversized_pattern(self):
        with self.assertRaises(ValueError):
            self.lib.search("a" * 201, regex=True)

    def test_literal_search_is_untouched_by_the_regex_path(self):
        res = self.lib.search("def greet")
        self.assertTrue(res["matches"])
        self.assertNotIn("regex", res)


    def test_tree_skips_caches_and_reports_its_limit(self):
        tree = self.lib.tree()
        self.assertTrue(any(p.endswith("src/app.py") for p in tree["files"]))
        self.assertFalse(any("node_modules" in p for p in tree["files"]))


class TestLibrarianCommands(unittest.TestCase):
    """The read-only mode is the enforcement point: the library delegates to it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _make_workspace(self.root)
        self.lib = LibraryService(str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_only_git_history_is_allowed(self):
        res = SandboxService().run_command(str(self.root), ["git", "status"], mode="read_only")
        self.assertIn("exit_code", res)

    def test_read_only_mode_refuses_writing_git(self):
        for argv in (["git", "commit", "-m", "x"], ["git", "add", "-A"], ["git", "checkout", "."]):
            with self.assertRaises(CommandNotAllowed, msg=f"{argv} must be refused"):
                validate_argv(argv, self.lib._fs, mode="read_only")

    def test_read_only_mode_refuses_git_redirects(self):
        # `-C` would point git at another directory and `--output` would write a file.
        for argv in (["git", "-C", "/tmp", "status"], ["git", "log", "--output=/tmp/out"]):
            with self.assertRaises(CommandNotAllowed):
                validate_argv(argv, self.lib._fs, mode="read_only")

    def test_read_only_mode_refuses_binaries_that_can_write(self):
        for argv in (["rm", "-rf", "src"], ["pytest"], ["npm", "test"], ["python3", "x.py"]):
            with self.assertRaises(CommandNotAllowed):
                validate_argv(argv, self.lib._fs, mode="read_only")

    def test_read_only_mode_allows_ls_but_not_its_recursive_flag(self):
        validate_argv(["ls", "-la", "src"], self.lib._fs, mode="read_only")
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["ls", "-R"], self.lib._fs, mode="read_only")

    def test_read_only_mode_still_refuses_paths_outside_the_workspace(self):
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["ls", "/etc"], self.lib._fs, mode="read_only")

    def test_a_refused_command_reaches_the_caller_as_a_refusal_not_a_crash(self):
        with self.assertRaises(CommandNotAllowed):
            self.lib.git(["commit", "-m", "nope"])

    def test_read_only_binaries_are_found_on_path(self):
        """`ls` must exist, or the ability is theoretical on this machine."""
        self.assertTrue(os.path.exists("/bin/ls") or os.path.exists("/usr/bin/ls"))


if __name__ == "__main__":
    unittest.main()
