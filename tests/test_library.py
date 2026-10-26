"""The librarian's abilities.

Two properties matter more than the mechanics, and both are asserted here: it can
find things out (read, search, history, inspect commands), and it cannot change
anything (an escape, a write command, or a forbidden binary is refused).
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import os
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from engine.fs import GitMetadataError, PathEscapeError
from engine.library import (
    MAX_MATCHES,
    MAX_READ_CHARS,
    MAX_SCAN_BYTES,
    SHOWN_SKIPPED_DIRS,
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


class TestTheWalkersOpenOnlyWhatIsSafeToOpen(unittest.TestCase):
    """A symlink into git's metadata, and a pipe, are two ways a file in the workspace is not a file to read."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _make_workspace(self.root)
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text(
            '[remote "origin"]\n\turl = https://user:SECRETTOKEN@example.test/r.git\n', encoding="utf-8"
        )
        self.lib = LibraryService(str(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_with_deadline(self, fn: Callable[..., object], *args: Any, **kwargs: Any) -> dict[str, object]:
        """Run `fn` on a thread and fail, rather than hang, when it does not come back."""
        box: dict[str, object] = {}

        def target() -> None:
            try:
                box["value"] = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — the test reports whatever it was
                box["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "the call is stuck opening a pipe")
        return box

    def test_a_link_into_git_metadata_is_not_searched(self) -> None:
        os.symlink(".git/config", self.root / "cfg.txt")
        for mode in (None, "keyword"):
            with self.subTest(mode=mode):
                res = self.lib.search("SECRETTOKEN", mode=mode)
                self.assertEqual([], res["matches"], res)
        res = self.lib.search("SECRETTOKEN", regex=True)
        self.assertEqual([], res["matches"], res)

    def test_a_link_into_git_metadata_is_not_read(self) -> None:
        os.symlink(".git/config", self.root / "cfg.txt")
        with self.assertRaises(GitMetadataError):
            self.lib.read("cfg.txt")

    def test_a_pipe_does_not_hang_a_search(self) -> None:
        os.mkfifo(self.root / "pipe")
        for mode in (None, "keyword"):
            with self.subTest(mode=mode):
                box = self._run_with_deadline(self.lib.search, "greet", mode=mode)
                self.assertNotIn("error", box, box)

    def test_a_pipe_is_refused_by_read_instead_of_opened(self) -> None:
        os.mkfifo(self.root / "pipe")
        box = self._run_with_deadline(self.lib.read, "pipe")
        self.assertIn("error", box)
        self.assertIn("regular file", str(box["error"]))


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

    def test_regex_search_rejects_a_pattern_the_compiler_overflows_on_as_a_valueerror(self) -> None:
        """`OverflowError` is not a `re.error`; the librarian's caller catches only `ValueError`."""
        with self.assertRaises(ValueError):
            self.lib.search("a{99999999999}", regex=True)

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

    def test_hitting_the_match_cap_is_reported_as_truncated(self) -> None:
        """A cut answer must not be described as the whole one.

        Substring and regex searches set `truncated` when they stop at
        `MAX_MATCHES`; the keyword strategy asked for exactly the cap and kept
        `truncated: False`, so its header read "40 matches" with no sign that
        anything after them had been left out.
        """
        for number in range(MAX_MATCHES + 5):
            (self.root / f"many{number:02d}.py").write_text("needleword = 1\n", encoding="utf-8")
        res = self.lib.search("needleword", mode="keyword")
        self.assertEqual(res["strategy"], "fts5_bm25")
        self.assertEqual(len(res["matches"]), MAX_MATCHES)
        self.assertTrue(res["truncated"])
        self.assertIn("TRUNCATED", format_search(res))

    def test_exactly_the_cap_is_complete_and_not_called_truncated(self) -> None:
        """The extra row is what makes the claim exact, in both directions."""
        for number in range(MAX_MATCHES):
            (self.root / f"many{number:02d}.py").write_text("needleword = 1\n", encoding="utf-8")
        res = self.lib.search("needleword", mode="keyword")
        self.assertEqual(len(res["matches"]), MAX_MATCHES)
        self.assertFalse(res["truncated"])
        self.assertNotIn("TRUNCATED", format_search(res))


class TestTheSearchesNameWhatTheWalkRefused(unittest.TestCase):
    """"0 matches in 3 files" is an answer about the files that were opened, not about the workspace.

    `SKIP_DIRS` is a deliberate structural exclusion — build output, dependency trees, caches — and it used to be a
    silent one. A needle that lived only under `dist/` came back as an absence with `files_skipped: 0` beside it,
    saying nothing had been skipped, because those files were never in the walker's count of what it saw. Every
    strategy now names the directories it met and refused, and says what that means to the reader of the header,
    who is a model deciding whether the answer it has is complete.

    The regex strategy goes through `engine/regex_worker.py`, so its case also proves the account survives the
    worker's one JSON reply.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("greet = 1\n", encoding="utf-8")
        (self.root / "dist").mkdir()
        (self.root / "dist" / "bundle.py").write_text("ONLY_IN_DIST = 1\n", encoding="utf-8")
        (self.root / "node_modules" / "pkg").mkdir(parents=True)
        (self.root / "node_modules" / "pkg" / "index.js").write_text("greet();\n", encoding="utf-8")
        (self.root / ".cache").mkdir()
        (self.root / ".cache" / "notes.md").write_text("greet\n", encoding="utf-8")
        self.lib = LibraryService(str(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_every_strategy_names_the_directories_it_did_not_walk(self) -> None:
        for kwargs in ({}, {"regex": True}, {"mode": "keyword"}):
            with self.subTest(**kwargs):
                result = self.lib.search("greet", **kwargs)
                self.assertEqual([".cache", "dist", "node_modules"], result["skipped_dirs"])
                self.assertEqual(["src/app.py"], [m["path"] for m in result["matches"]])
                rendered = format_search(result)
                self.assertIn("not searched: .cache, dist, node_modules", rendered)

    def test_a_needle_inside_an_excluded_directory_is_not_reported_as_absent(self) -> None:
        """The failure this exists for: the string is in the workspace, and the answer must not read as absence."""
        result = self.lib.search("ONLY_IN_DIST")
        self.assertEqual([], result["matches"])
        self.assertEqual(1, result["files_scanned"], "only src/app.py was ever opened")
        rendered = format_search(result)
        self.assertIn("0 matches", rendered)
        self.assertIn("not searched: .cache, dist, node_modules", rendered)
        self.assertIn("nothing inside them is in this result", rendered)

    def test_only_the_excluded_names_are_reported(self) -> None:
        """The clause lists this module's own vocabulary: an unusual directory is unusual, not excluded."""
        (self.root / "generated").mkdir()
        (self.root / "generated" / "kept.py").write_text("greet = 2\n", encoding="utf-8")
        result = self.lib.search("greet")
        self.assertIn("generated/kept.py", [m["path"] for m in result["matches"]])
        self.assertNotIn("generated", result["skipped_dirs"])

    def test_the_list_is_capped_and_counts_what_it_held_back(self) -> None:
        for name in ("build", "target", "vendor", ".next", ".nuxt", ".ruff_cache", "venv"):
            (self.root / name).mkdir()
        result = self.lib.search("greet")
        self.assertEqual(10, len(result["skipped_dirs"]))
        rendered = format_search(result)
        self.assertIn(f"and {10 - SHOWN_SKIPPED_DIRS} more", rendered)

    def test_a_workspace_without_one_gets_the_header_it_always_got(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            (Path(plain) / "a.txt").write_text("needle\n", encoding="utf-8")
            lib = LibraryService(plain)
            for kwargs in ({}, {"regex": True}, {"mode": "keyword"}):
                with self.subTest(**kwargs):
                    result = lib.search("needle", **kwargs)
                    self.assertNotIn("skipped_dirs", result)
                    self.assertNotIn("not searched", format_search(result))


class TestTheSearchesNameTheirOtherStructuralSilences(unittest.TestCase):
    """Two more ways a search is partial without saying so: the file cap, and a file read only in part.

    `MAX_FILES_SCANNED` ended a walk, and `MAX_SCAN_BYTES` meant every file was read only to its first slice of
    bytes — so a needle in the tail of a large file, or in a file the walk never reached, came back as no match at
    all. "TRUNCATED — results are partial" also collapsed two different facts into one word: listing every match
    there was, and never opening the later files. The reader's next move differs (narrow the walk, or list what is
    there), so the header now says which cap ended it.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.lib = LibraryService(str(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_walk_cut_at_the_file_cap_says_which_cap_and_what_to_do(self) -> None:
        for n in range(5):
            (self.root / f"f{n}.py").write_text("needle\n", encoding="utf-8")
        with patch("engine.library.MAX_FILES_SCANNED", 3):
            result = self.lib.search("needle")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["stopped"], "files")
        self.assertEqual(result["files_scanned"], 3)
        rendered = format_search(result)
        # The sentence names the cap the walk was given. The number is read from the constant the walk used, which
        # the patch above lowered only for the walk, so the assertion is about the sentence, not about 800.
        self.assertIn("the walk stopped at its limit of", rendered)
        self.assertIn("later files were not searched", rendered)
        self.assertIn("narrow it with `glob`", rendered)

    def _one_needle_a_file(self, count: int) -> None:
        """`count` files, one line each: one match per file, so all three strategies count the same thing."""
        for number in range(count):
            (self.root / f"many{number:03d}.py").write_text("needle\n", encoding="utf-8")

    def test_a_walk_cut_at_the_match_cap_says_that_instead(self) -> None:
        self._one_needle_a_file(MAX_MATCHES + 5)
        for kwargs in ({}, {"regex": True}, {"mode": "keyword"}):
            with self.subTest(**kwargs):
                result = self.lib.search("needle", **kwargs)
                self.assertEqual(result["stopped"], "matches")
                self.assertEqual(len(result["matches"]), MAX_MATCHES)
                rendered = format_search(result)
                self.assertIn(f"the limit of {MAX_MATCHES} matches was reached", rendered)
                self.assertNotIn("later files were not searched", rendered)

    def test_a_list_that_exactly_fills_the_cap_is_not_called_cut(self) -> None:
        """The claim has to be exact in both directions: naming the cap made a wrong one louder."""
        self._one_needle_a_file(MAX_MATCHES)
        for kwargs in ({}, {"regex": True}, {"mode": "keyword"}):
            with self.subTest(**kwargs):
                result = self.lib.search("needle", **kwargs)
                self.assertEqual(len(result["matches"]), MAX_MATCHES)
                self.assertFalse(result["truncated"])
                self.assertNotIn("TRUNCATED", format_search(result))

    def test_a_file_read_only_in_part_is_counted_and_said(self) -> None:
        """The needle is past `MAX_SCAN_BYTES` in a file that is otherwise searched: no match, and now a reason."""
        (self.root / "big.py").write_text(
            "x = 1\n" + "# pad\n" * (MAX_SCAN_BYTES // 6 + 100) + "NEEDLE_PAST_THE_CAP = 1\n", encoding="utf-8"
        )
        result = self.lib.search("NEEDLE_PAST_THE_CAP")
        self.assertEqual([], result["matches"])
        self.assertEqual(1, result["byte_capped"])
        self.assertEqual(1, result["files_scanned"], "the file is still one the walk read")
        self.assertIn(
            f"1 file read only in part (the first {MAX_SCAN_BYTES:,} bytes of each were searched)",
            format_search(result),
        )

    def test_a_whole_small_tree_says_none_of_it(self) -> None:
        (self.root / "a.py").write_text("needle\n", encoding="utf-8")
        for kwargs in ({}, {"regex": True}, {"mode": "keyword"}):
            with self.subTest(**kwargs):
                result = self.lib.search("needle", **kwargs)
                self.assertNotIn("stopped", result)
                self.assertNotIn("byte_capped", result)
                rendered = format_search(result)
                self.assertNotIn("TRUNCATED", rendered)
                self.assertNotIn("read only in part", rendered)


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
