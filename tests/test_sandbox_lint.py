from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.fs import FileSystemService
from engine.sandbox import CommandNotAllowed, SandboxService, hardened_args, validate_argv

# Linters and type-checkers on the test allowlist (docs/04 section 5, docs/03 section 1.4).
#
# `ruff check`, `mypy`, `tsc --noEmit`, `cargo check|clippy`, `go vet` and `make lint|typecheck` are
# admitted so the conductor can ask the one question a repository's own tooling answers best: does
# what was just written pass? They are admitted in `test` mode only, so the librarian's `read_only`
# mode and the conductor's pre-approval `run_command` (which maps to `read_only`) cannot run them.
#
# What makes a linter safe to admit is what the engine adds, not what the model asks for: the model
# supplies no flags to ruff or tsc, a closed few to mypy and cargo, and the engine appends the ones
# that keep the run from writing (`--no-cache`, `--cache-dir=/dev/null`, `--noEmit`) or from
# succeeding on a warning (`-D warnings`). Each refusal below is a way a linter writes or runs code
# the model chose, and each real run below asserts on the workspace afterwards.

ACCEPTED: tuple[list[str], ...] = (
    ["ruff", "check"],
    ["ruff", "check", "src"],
    ["ruff", "check", "a.py", "b.py"],
    ["mypy", "pkg"],
    ["mypy", "--strict", "pkg"],
    ["mypy", "--ignore-missing-imports", "a.py"],
    ["tsc", "--noEmit"],
    ["tsc", "--noEmit", "-p", "ui"],
    ["tsc", "-p", "ui/tsconfig.json", "--noEmit"],
    ["tsc", "--noEmit", "--project", "ui"],
    ["cargo", "check"],
    ["cargo", "check", "--lib", "--quiet"],
    ["cargo", "clippy"],
    ["cargo", "clippy", "--all-targets"],
    ["go", "vet", "./..."],
    ["go", "vet", "./pkg"],
    ["make", "lint"],
    ["make", "typecheck"],
)

REFUSED: tuple[list[str], ...] = (
    # ruff: it rewrites files (--fix, format, --add-noqa) and reads a config the model names.
    ["ruff"],
    ["ruff", "format"],
    ["ruff", "check", "--fix"],
    ["ruff", "check", "--fix", "x.py"],
    ["ruff", "check", "--unsafe-fixes"],
    ["ruff", "check", "--add-noqa"],
    ["ruff", "check", "--config", "x.toml"],
    ["ruff", "check", "--select=E"],
    ["ruff", "check", "/etc"],
    ["ruff", "check", "../x"],
    ["ruff", "server"],
    # mypy: install-types runs pip, the others name an interpreter, a config or a writable path.
    ["mypy", "--install-types"],
    ["mypy", "--install-t"],
    ["mypy", "--non-interactive", "--install-types"],
    ["mypy", "--python-executable", "x"],
    ["mypy", "--config-file", "x.ini"],
    ["mypy", "--config-file=x.ini"],
    ["mypy", "--cache-dir=x"],
    ["mypy", "--junit-xml=x"],
    ["mypy", "--html-report", "x"],
    ["mypy", "-c", "code"],
    ["mypy", "/etc"],
    ["mypy", "../x"],
    # tsc: it must not emit, build, or leave incremental state behind.
    ["tsc"],
    ["tsc", "--build"],
    ["tsc", "-b"],
    ["tsc", "-p", "ui"],
    ["tsc", "--noEmit", "--incremental"],
    ["tsc", "--noEmit", "--tsBuildInfoFile", "x"],
    ["tsc", "--noEmit", "--init"],
    ["tsc", "--noEmit", "--outDir", "x"],
    ["tsc", "--noEmit", "-p"],
    ["tsc", "--noEmit", "-p", "/etc"],
    ["tsc", "--noEmit", "-p", "../x"],
    ["tsc", "--noEmit", "a.ts"],
    # cargo: --fix writes source, a second `--` would override -D warnings, a manifest path escapes.
    ["cargo"],
    ["cargo", "build"],
    ["cargo", "install", "x"],
    ["cargo", "clippy", "--fix"],
    ["cargo", "clippy", "--", "-A", "warnings"],
    ["cargo", "check", "--", "x"],
    ["cargo", "check", "--manifest-path", "x"],
    ["cargo", "check", "--target-dir", "x"],
    # go: -vettool runs a program the model named.
    ["go", "build"],
    ["go", "vet", "-vettool=x"],
    ["go", "vet", "-vettool", "x", "./..."],
    ["go", "vet", "/etc"],
    ["go", "vet", "../x"],
    # make: exactly `make lint` or `make typecheck`. Anything else is a different program's worth of
    # surface (-f names a makefile, -C a directory, VAR=x an override, a target is whatever the repo says).
    ["make"],
    ["make", "install"],
    ["make", "check"],
    ["make", "-f", "x.mk", "lint"],
    ["make", "-C", "..", "lint"],
    ["make", "-n", "lint"],
    ["make", "lint", "FOO=1"],
    ["make", "lint", "typecheck"],
    ["make", "lint", "-j4"],
    ["make", "--eval=x", "lint"],
)


class TestTheValidator(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fs = FileSystemService(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_each_linter_the_conductor_needs_is_admitted_in_test_mode(self) -> None:
        for argv in ACCEPTED:
            with self.subTest(argv=argv):
                validate_argv(argv, self.fs, mode="test")

    def test_every_way_a_linter_writes_or_runs_what_the_model_chose_is_refused(self) -> None:
        for argv in REFUSED:
            with self.subTest(argv=argv):
                with self.assertRaises(CommandNotAllowed):
                    validate_argv(argv, self.fs, mode="test")

    def test_none_of_them_is_admitted_in_read_only_mode(self) -> None:
        # The librarian reads; a linter loads project config and, for mypy, project plugins.
        for argv in ACCEPTED:
            with self.subTest(argv=argv):
                with self.assertRaises(CommandNotAllowed):
                    validate_argv(argv, self.fs, mode="read_only")

    def test_the_refusal_says_what_is_allowed(self) -> None:
        with self.assertRaises(CommandNotAllowed) as caught:
            validate_argv(["make", "install"], self.fs)
        self.assertIn("lint", str(caught.exception))
        with self.assertRaises(CommandNotAllowed) as caught:
            validate_argv(["cargo", "build"], self.fs)
        self.assertIn("check", str(caught.exception))


class TestTheEngineAddsTheSafetyFlags(unittest.TestCase):
    def test_what_the_child_is_handed(self) -> None:
        for argv, expected in (
            (["ruff", "check", "src"], ["check", "--no-cache", "--output-format=concise", "src"]),
            (["ruff", "check"], ["check", "--no-cache", "--output-format=concise"]),
            (["mypy", "--strict", "pkg"], ["--cache-dir=/dev/null", "--strict", "pkg"]),
            (["tsc", "--noEmit", "-p", "ui"], ["--pretty", "false", "--noEmit", "-p", "ui"]),
            (["cargo", "clippy", "--lib"], ["clippy", "--lib", "--", "-D", "warnings"]),
            (["cargo", "check", "--lib"], ["check", "--lib"]),
            (["go", "vet", "./..."], ["vet", "./..."]),
            (["make", "lint"], ["lint"]),
            (["pytest", "-q"], ["-q"]),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(hardened_args(argv), expected)


class TestARealRun(unittest.TestCase):
    """The flags are real: run the tools and look at the workspace afterwards."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.sandbox = SandboxService()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _need(self, binary: str) -> None:
        # Declared in pyproject's dev extras: absent is a broken checkout, not a reason to skip.
        self.assertIsNotNone(shutil.which(binary), f"{binary} is a dev dependency and is not on PATH")

    def _names(self) -> set[str]:
        return {p.name for p in self.root.iterdir()}

    def test_ruff_reports_the_finding_and_leaves_no_cache(self) -> None:
        self._need("ruff")
        (self.root / "bad.py").write_text("import os\n", encoding="utf-8")
        result = self.sandbox.run_command(str(self.root), ["ruff", "check"])
        self.assertEqual(1, result["exit_code"], result)
        self.assertIn("bad.py:1:", result["stdout"])
        self.assertIn("F401", result["stdout"])
        self.assertNotIn(".ruff_cache", self._names())

    def test_ruff_cannot_be_asked_to_fix_and_the_file_is_unchanged(self) -> None:
        self._need("ruff")
        (self.root / "bad.py").write_text("import os\n", encoding="utf-8")
        with self.assertRaises(CommandNotAllowed):
            self.sandbox.run_command(str(self.root), ["ruff", "check", "--fix", "bad.py"])
        self.assertEqual("import os\n", (self.root / "bad.py").read_text(encoding="utf-8"))

    def test_ruff_passes_a_clean_file(self) -> None:
        self._need("ruff")
        (self.root / "ok.py").write_text("x = 1\n", encoding="utf-8")
        result = self.sandbox.run_command(str(self.root), ["ruff", "check", "ok.py"])
        self.assertEqual(0, result["exit_code"], result)

    def test_mypy_reports_the_error_and_leaves_no_cache(self) -> None:
        self._need("mypy")
        (self.root / "typed.py").write_text("x: int = 'a'\n", encoding="utf-8")
        result = self.sandbox.run_command(str(self.root), ["mypy", "typed.py"])
        self.assertEqual(1, result["exit_code"], result)
        self.assertIn("typed.py:1", result["stdout"])
        self.assertNotIn(".mypy_cache", self._names())

    def test_make_lint_runs_the_target_and_nothing_else(self) -> None:
        self._need("make")
        (self.root / "Makefile").write_text(
            "lint:\n\t@echo linted\n\t@touch lint-ran\n\ninstall:\n\t@touch install-ran\n",
            encoding="utf-8",
        )
        result = self.sandbox.run_command(str(self.root), ["make", "lint"])
        self.assertEqual(0, result["exit_code"], result)
        self.assertIn("linted", result["stdout"])
        self.assertIn("lint-ran", self._names())
        with self.assertRaises(CommandNotAllowed):
            self.sandbox.run_command(str(self.root), ["make", "install"])
        self.assertNotIn("install-ran", self._names())


if __name__ == "__main__":
    unittest.main()
