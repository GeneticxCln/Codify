"""A symbolic-link loop in a workspace is a refused path, not a crash, on every supported Python.

`Path.resolve()` raises `RuntimeError` for `loop -> loop` before Python 3.13 and returns the path from
3.13 on. The declared floor is 3.10, and every caller of `FileSystemService.resolve` catches
`PathEscapeError` and `ValueError` only, so on 3.10-3.12 a cloned repository holding one link crashed
the argv check, the file readers and the fixer's writer with an uncaught `RuntimeError`. `make
ci-python-floor` is the leg that runs this on the interpreter where it matters.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.fs import FileNotTextError, FileSystemService, NotAFileError, PathEscapeError
from engine.sandbox import CommandNotAllowed, validate_argv


class TestSymlinkLoops(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        os.symlink("loop", self.root / "loop")
        os.symlink("b", self.root / "a")
        os.symlink("a", self.root / "b")
        self.fs = FileSystemService(str(self.root))

    def test_resolve_never_raises_a_runtime_error(self) -> None:
        for name in ("loop", "a", "b", "loop/x", "a/b/c"):
            with self.subTest(name=name):
                try:
                    self.fs.resolve(name)
                except PathEscapeError:
                    pass  # refused: the 3.10-3.12 answer
                # a returned path is the 3.13 answer; either way no RuntimeError

    def test_the_readers_answer_nothing_to_read(self) -> None:
        for name in ("loop", "a"):
            with self.subTest(name=name):
                self.assertIsNone(self.fs.read_text_or_none(name))
                with self.assertRaises((PathEscapeError, NotAFileError, FileNotTextError)):
                    self.fs.read_editable(name)

    def test_the_argv_check_refuses_or_admits_a_loop_without_crashing(self) -> None:
        for mode, argv in (
            ("read_only", ["ls", "loop"]), ("test", ["pytest", "a"]), ("read_only", ["git", "log", "--", "loop"]),
        ):
            with self.subTest(argv=argv):
                try:
                    validate_argv(argv, self.fs, mode=mode)
                except CommandNotAllowed:
                    pass

    def test_the_writer_refuses_a_loop_as_a_value_error_the_fixer_is_asked_about(self) -> None:
        with self.assertRaises((ValueError, PathEscapeError)):
            self.fs.apply([{"path": "loop", "action": "update", "content": "x"}], dry_run=True)


if __name__ == "__main__":
    unittest.main()
