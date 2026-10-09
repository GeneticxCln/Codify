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
from unittest.mock import patch

from engine.fs import PathEscapeError
from engine.library import (
    MAX_READ_CHARS,
    LibraryService,
    fts5_available,
    format_read,
    format_search,
)
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
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _make_workspace(self.root)
        self.lib = LibraryService(str(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_read_returns_text_and_reports_truncation(self) -> None:
        res = self.lib.read("src/app.py")
        self.assertIn("def greet", res["text"])
        self.assertFalse(res["truncated"])

        big = self.lib.read("big.txt")
        self.assertTrue(big["truncated"], "a truncating read must say so")
        self.assertEqual(len(big["text"]), MAX_READ_CHARS)

    def test_read_cannot_escape_the_workspace(self) -> None:
        with self.assertRaises(PathEscapeError):
            self.lib.read("../../etc/passwd")

    def test_read_of_a_directory_is_refused(self) -> None:
        with self.assertRaises(IsADirectoryError):
            self.lib.read("src")

    def test_search_is_literal_and_case_insensitive(self) -> None:
        res = self.lib.search("greet")
        paths = {m["path"] for m in res["matches"]}
        self.assertIn("src/app.py", paths)
        self.assertIn("src/util.py", paths)
        self.assertNotIn("node_modules/dep.js", paths, "package caches are not scanned")
        self.assertNotIn("binary.dat", res["files_scanned"] and paths, "binary files are skipped")

    def test_search_honours_a_glob(self) -> None:
        res = self.lib.search("greet", glob="*.py")
        self.assertTrue(all(m["path"].endswith(".py") for m in res["matches"]))

    def test_search_reports_its_own_limits(self) -> None:
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

    def test_search_rejects_an_empty_query(self) -> None:
        with self.assertRaises(ValueError):
            self.lib.search("   ")

    def test_regex_search_finds_structural_patterns_literal_cannot(self) -> None:
        """Alternation + escaped metachars: no single substring can ask this."""
        res = self.lib.search(r"greet\(|GREETING", regex=True)
        paths = {m["path"] for m in res["matches"]}
        self.assertIn("src/app.py", paths)
        self.assertIn("src/util.py", paths)
        self.assertTrue(res.get("regex"), "the result must say which mode ran")

    def test_regex_search_rejects_an_invalid_pattern_as_a_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            self.lib.search("([unclosed", regex=True)

    def test_regex_search_rejects_an_oversized_pattern(self) -> None:
        with self.assertRaises(ValueError):
            self.lib.search("a" * 201, regex=True)

    def test_literal_search_is_untouched_by_the_regex_path(self) -> None:
        res = self.lib.search("def greet")
        self.assertTrue(res["matches"])
        self.assertNotIn("regex", res)


class TestKeywordSearch(unittest.TestCase):
    """The second retrieval strategy, and the reason it can be trusted.

    FTS5 is a compile-time option of SQLite, not an import, so the strategy
    degrades rather than refuses: a workspace whose interpreter lacks it gets
    the literal substring answer, labelled as such. Every fallback here is
    exercised by *forcing* the condition — a probe patched away, a connection
    made to raise — never by skipping the test when the box happens to have the
    feature. A test that only passes where FTS5 exists proves nothing about
    the machine that needs the fallback.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "providers.py").write_text(
            "def validate_provider_url(u):\n    '''Providers must be loopback.'''\n",
            encoding="utf-8",
        )
        (self.root / "notes.md").write_text(
            "we validate the base_url of every provider\n",
            encoding="utf-8",
        )
        (self.root / "unrelated.py").write_text("x = 1\n", encoding="utf-8")
        self.lib = LibraryService(str(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_multiword_query_finds_whole_files_substring_misses(self) -> None:
        """The case the strategy exists for: no single line carries both words."""
        self.assertEqual(self.lib.search("validate provider")["matches"], [])
        res = self.lib.search("validate provider", mode="keyword")
        paths = {m["path"] for m in res["matches"]}
        self.assertEqual(paths, {"providers.py", "notes.md"})
        self.assertNotIn("unrelated.py", paths)

    def test_ranking_is_bm25_and_not_the_path_sort(self) -> None:
        """The file containing *both* terms outranks the file with one, and the
        names are chosen so alphabetical order would say the opposite."""
        res = self.lib.search("validate provider", mode="keyword")
        self.assertEqual(res["matches"][0]["path"], "notes.md")

    def test_the_result_labels_its_strategy_and_shape(self) -> None:
        res = self.lib.search("validate provider", mode="keyword")
        self.assertEqual(res["strategy"], "fts5_bm25")
        self.assertTrue(res["fts5"])
        self.assertTrue(all(m["line"] == 0 for m in res["matches"]))
        rendered = format_search(res)
        self.assertIn("ranked by keyword", rendered)
        self.assertIn("whole files, not lines", rendered)

    def test_keyword_mode_still_honours_a_glob(self) -> None:
        res = self.lib.search("validate provider", glob="*.py", mode="keyword")
        self.assertTrue(all(m["path"].endswith(".py") for m in res["matches"]))

    def test_the_fallback_runs_when_fts5_is_missing(self) -> None:
        """A probe patched to False is the box without FTS5, answered the way
        that box must be: the literal answer, labelled as a fallback."""
        with patch("engine.library.fts5_available", return_value=False):
            res = self.lib.search("validate provider", mode="keyword")
        self.assertEqual(res["strategy"], "substring_fallback")
        self.assertFalse(res["fts5"])
        self.assertEqual(res["matches"], self.lib.search("validate provider")["matches"])
        self.assertIn("keyword index unavailable", format_search(res))

    def test_the_fallback_is_forced_through_a_failing_connection_not_a_flag(self) -> None:
        """The probe says yes and the connection then fails anyway: the same
        degradation must hold, because a probe proves one moment, not the call."""
        import sqlite3

        def broken_connect(*args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("unable to open database file")

        with patch("engine.library.fts5_available", return_value=True), patch(
            "sqlite3.connect", side_effect=broken_connect
        ):
            res = self.lib.search("validate provider", mode="keyword")
        self.assertEqual(res["strategy"], "substring_fallback")
        self.assertTrue(res["fts5"], "the probe said yes; the label says what failed")

    def test_a_query_fts5_itself_refuses_falls_back_rather_than_raising(self) -> None:
        """A bare quote is a query-syntax error to FTS5. The caller asked for a
        keyword search and must not learn sqlite's grammar to get an answer."""
        self.assertTrue(fts5_available())
        res = self.lib.search('validate "unclosed', mode="keyword")
        self.assertEqual(res["strategy"], "substring_fallback")
        self.assertEqual(res["matches"], [])

    def test_an_unknown_mode_is_a_valueerror_not_a_silent_substring(self) -> None:
        """A caller asking for "semantic" must be told it does not exist, not
        quietly handed substring and left to trust the wrong answer."""
        with self.assertRaises(ValueError):
            self.lib.search("anything", mode="semantic")

    def test_the_default_search_contract_is_unchanged(self) -> None:
        """Substring stays the default and its shape is load-bearing: the
        evidence checker reads matches[].path and files_scanned to decide which
        cited paths were actually seen."""
        res = self.lib.search("loopback")
        self.assertNotIn("strategy", res)
        self.assertNotIn("fts5", res)
        self.assertEqual(res["files_scanned"], 3)
        self.assertEqual(res["matches"][0]["path"], "providers.py")
        self.assertEqual(res["matches"][0]["line"], 2)


class TestLibrarianReadsMore(unittest.TestCase):
    """The rest of the read surface, on the standard fixture workspace."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _make_workspace(self.root)
        self.lib = LibraryService(str(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_line_range_reaches_past_the_head_cap(self) -> None:
        """The window is taken where the lines are, not from the first 8K chars:
        a range read exists to reach the bottom half of a large file."""
        lines = [f"line {i}" for i in range(1, 1501)]
        (self.root / "long.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

        res = self.lib.read("long.txt", offset=1400, limit=50)

        self.assertTrue(res["text"].startswith("line 1400"), res["text"][:60])
        self.assertIn("line 1449", res["text"])
        self.assertNotIn("line 1399", res["text"])
        self.assertEqual(res["lines"], 50)
        self.assertEqual(res["total_lines"], 1500)
        self.assertTrue(res["truncated"])

    def test_line_window_on_a_small_file_reports_its_slice(self) -> None:
        res = self.lib.read("src/app.py", offset=2, limit=1)
        self.assertIn("hello", res["text"])
        self.assertEqual(res["lines"], 1)
        self.assertEqual(res["offset"], 2)
        self.assertEqual(res["total_lines"], 6)

    def _file_with_separators_that_are_not_newlines(self) -> list[str]:
        """Ten lines by `\\n`, three of which hold a character `str.splitlines` also breaks a line on."""
        lines = [f"line {i}" for i in range(1, 11)]
        lines[2] = "line 3 \x0c still line 3"      # a form feed: the page break old C sources carry
        lines[5] = "line 6   still line 6"    # U+2028, which JSON and JavaScript text can hold
        lines[7] = "line 8 \x85 still line 8"      # NEL
        (self.root / "pages.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return lines

    def test_a_line_window_counts_lines_by_newline_like_the_range_it_was_asked_for(self) -> None:
        """The window starts at the Nth `\\n`, so it must end after `limit` of them.

        It was cut with `str.splitlines`, which also breaks on form feeds, U+2028 and NEL: asking for lines 2-5 of a
        file with a form feed on line 3 returned lines 2, "3", the rest of 3, and 4, so line 5 was lost while the
        header still claimed it was there.
        """
        lines = self._file_with_separators_that_are_not_newlines()

        res = self.lib.read("pages.txt", offset=2, limit=4)

        self.assertEqual(res["lines"], 4)
        self.assertEqual(res["text"].split("\n"), lines[1:5])

        res = self.lib.read("pages.txt", offset=5, limit=3)

        self.assertEqual(res["text"].split("\n"), lines[4:7])
        self.assertEqual(res["total_lines"], 10)

    def test_a_search_hit_and_a_read_agree_about_which_line_it_is(self) -> None:
        """`search_code` says "path:LINE" and the model then calls `read_file` at LINE: the two must count alike."""
        self._file_with_separators_that_are_not_newlines()

        for query, wanted_line in (("still line 3", 3), ("still line 6", 6), ("line 9", 9)):
            with self.subTest(query=query):
                hits = self.lib.search(query, glob="pages.txt")["matches"]
                self.assertEqual([h["line"] for h in hits], [wanted_line])
                read_back = self.lib.read("pages.txt", offset=hits[0]["line"], limit=1)
                self.assertIn(query, read_back["text"])

    def test_the_regex_walk_counts_lines_the_same_way(self) -> None:
        from engine.library import scan_regex

        self._file_with_separators_that_are_not_newlines()

        found = scan_regex(str(self.root), r"still line 6", "pages.txt", 5.0)

        self.assertEqual([m["line"] for m in found["matches"]], [6])

    def test_a_windows_file_reads_without_its_carriage_returns(self) -> None:
        (self.root / "crlf.txt").write_bytes(b"one\r\ntwo\r\nthree")

        res = self.lib.read("crlf.txt", offset=1, limit=3)

        self.assertEqual(res["text"], "one\ntwo\nthree")
        self.assertEqual(res["lines"], 3)
        self.assertEqual(res["total_lines"], 3)
        # `$` still means the end of a line in a file that ends its lines with two characters.
        from engine.library import scan_regex

        found = scan_regex(str(self.root), r"^two$", "crlf.txt", 5.0)
        self.assertEqual([m["line"] for m in found["matches"]], [2])
        self.assertEqual(self.lib.search("two", glob="crlf.txt")["matches"][0]["text"], "two")

    def test_an_empty_window_is_rendered_as_one(self) -> None:
        """format_read must not do range math on an empty window — the old
        offset+lines-1 math produced nonsense like "lines 50-49"."""
        rendered = format_read({
            "path": "x.py", "text": "", "lines": 0, "offset": 50,
            "window": 10, "total_lines": 40, "truncated": True,
        })
        self.assertIn("empty range", rendered)
        self.assertNotIn("lines 50-49", rendered)

    def test_search_rendering_names_the_glob(self) -> None:
        res = self.lib.search("greet", glob="*.py")
        self.assertIn("glob=", format_search(res))

    def test_tree_skips_caches_and_reports_its_limit(self) -> None:
        tree = self.lib.tree()
        self.assertTrue(any(p.endswith("src/app.py") for p in tree["files"]))
        self.assertFalse(any("node_modules" in p for p in tree["files"]))


class TestLibrarianCommands(unittest.TestCase):
    """The read-only mode is the enforcement point: the library delegates to it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _make_workspace(self.root)
        self.lib = LibraryService(str(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_read_only_git_history_is_allowed(self) -> None:
        res = SandboxService().run_command(str(self.root), ["git", "status"], mode="read_only")
        self.assertIn("exit_code", res)

    def test_read_only_mode_refuses_writing_git(self) -> None:
        for argv in (["git", "commit", "-m", "x"], ["git", "add", "-A"], ["git", "checkout", "."]):
            with self.assertRaises(CommandNotAllowed, msg=f"{argv} must be refused"):
                validate_argv(argv, self.lib._fs, mode="read_only")

    def test_read_only_mode_refuses_git_redirects(self) -> None:
        # `-C` would point git at another directory and `--output` would write a file.
        for argv in (["git", "-C", "/tmp", "status"], ["git", "log", "--output=/tmp/out"]):
            with self.assertRaises(CommandNotAllowed):
                validate_argv(argv, self.lib._fs, mode="read_only")

    def test_read_only_mode_refuses_binaries_that_can_write(self) -> None:
        for argv in (["rm", "-rf", "src"], ["pytest"], ["npm", "test"], ["python3", "x.py"]):
            with self.assertRaises(CommandNotAllowed):
                validate_argv(argv, self.lib._fs, mode="read_only")

    def test_read_only_mode_allows_ls_but_not_its_recursive_flag(self) -> None:
        validate_argv(["ls", "-la", "src"], self.lib._fs, mode="read_only")
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["ls", "-R"], self.lib._fs, mode="read_only")

    def test_read_only_mode_still_refuses_paths_outside_the_workspace(self) -> None:
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["ls", "/etc"], self.lib._fs, mode="read_only")

    def test_a_refused_command_reaches_the_caller_as_a_refusal_not_a_crash(self) -> None:
        with self.assertRaises(CommandNotAllowed):
            self.lib.git(["commit", "-m", "nope"])

    def test_read_only_binaries_are_found_on_path(self) -> None:
        """`ls` must exist, or the ability is theoretical on this machine."""
        self.assertTrue(os.path.exists("/bin/ls") or os.path.exists("/usr/bin/ls"))


if __name__ == "__main__":
    unittest.main()
