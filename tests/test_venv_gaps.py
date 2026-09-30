"""`scripts/venv_gaps.py` says which declared dependencies an interpreter has not installed.

It exists because `make doctor` used to check the machine and never the checkout's own
virtual environment. A `.venv` made before `httpx2` joined the `dev` extra kept working, and
kept warning (Starlette's `TestClient` falls back to `httpx` and says so) on every run of the
suite, with nothing anywhere saying "run `make setup`". Found by a `make ci-report` on a
developer's machine, 2026-09-30.

The parsing is plain text, not `tomllib`, for the reason `test_dependency_parity` gives: the
floor leg runs the suite on 3.10, which has no `tomllib`. So the first thing checked here is
that the extractor finds the real file's names — a scan that matches nothing proves nothing.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from scripts import venv_gaps

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


class TestWhatIsDeclared(unittest.TestCase):
    def test_the_real_file_yields_the_runtime_dependencies_and_the_dev_extra(self) -> None:
        names = venv_gaps.declared(PYPROJECT.read_text(encoding="utf-8"))

        # One from each list, and the one that started this.
        for expected in ("fastapi", "httpx", "keyring", "pytest", "ruff", "mypy", "httpx2"):
            self.assertIn(expected, names)

    def test_a_version_specifier_is_not_part_of_the_name(self) -> None:
        text = 'dependencies = [\n    "fastapi>=0.115,<1",\n    "pkg[extra]>=2; python_version>\'3\'",\n]\n'

        self.assertEqual(["fastapi", "pkg"], venv_gaps.declared(text))

    def test_a_comment_is_not_a_dependency(self) -> None:
        text = 'dependencies = [\n    # "not-this-one"\n    "real",  # "nor-this"\n]\n'

        self.assertEqual(["real"], venv_gaps.declared(text))

    def test_a_file_with_no_dependency_list_is_an_error_not_an_empty_list(self) -> None:
        with self.assertRaises(ValueError):
            venv_gaps.declared("[project]\nname = 'x'\n")


class TestWhatIsMissing(unittest.TestCase):
    def test_only_what_the_interpreter_lacks_is_named_and_the_order_is_kept(self) -> None:
        have = {"a", "c"}

        self.assertEqual(["b", "d"], venv_gaps.missing(["a", "b", "c", "d"], installed=have.__contains__))

    def test_nothing_is_missing_when_everything_is_there(self) -> None:
        self.assertEqual([], venv_gaps.missing(["a"], installed=lambda _name: True))

    def test_the_real_interpreter_is_asked_by_distribution_name(self) -> None:
        # `pip` and the standard library's own metadata machinery agree on what "installed" means.
        self.assertTrue(venv_gaps.is_installed("httpx"))
        self.assertFalse(venv_gaps.is_installed("no-such-distribution-anywhere-xyz"))


class TestTheCommand(unittest.TestCase):
    def test_exit_status_and_output_say_what_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.toml"
            good.write_text('dependencies = [\n    "httpx",\n]\n', encoding="utf-8")
            bad = Path(tmp) / "bad.toml"
            bad.write_text('dependencies = [\n    "httpx",\n    "no-such-distribution-anywhere-xyz",\n]\n', encoding="utf-8")
            empty = Path(tmp) / "empty.toml"
            empty.write_text("[project]\nname = 'x'\n", encoding="utf-8")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, venv_gaps.main([str(good)]))
                self.assertEqual("", out.getvalue())
                self.assertEqual(1, venv_gaps.main([str(bad)]))
            self.assertEqual("no-such-distribution-anywhere-xyz\n", out.getvalue())

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(2, venv_gaps.main([str(empty)]))
            self.assertIn("declares no dependencies", err.getvalue())


if __name__ == "__main__":
    unittest.main()
