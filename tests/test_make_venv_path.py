"""The Makefile prefers a checkout's `.venv`, and never at the floor leg's expense.

`make setup` creates `./.venv`, and the Makefile puts its `bin` first on PATH so that
`make setup && make check` works with nothing activated. That is a convenience with a
trap in it: the declared-minimum leg is a nested `make` whose recipe puts *its own*
3.10 interpreter first on PATH, and a Makefile that then prepended `.venv/bin` again
would displace it. The leg would still run, still pass, and be a second run of the host
Python — the gate dropping its oldest leg without saying so, which is the exact failure
that leg exists to prevent (a PEP 701 f-string shipped from a 3.14 laptop and was a
SyntaxError on 3.10).

So the guard (`CODIFY_FLOOR_LEG`) is tested where it matters: the real Makefile, run by
real make, asked what PATH it would hand a recipe.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

MAKEFILE = Path(__file__).resolve().parent.parent / "Makefile"

# A second makefile, so the production one needs no target that exists only for a test.
SHOW_PATH = (
    'show-path:\n\t@printf "%s" "$$PATH"\n'
    'show-mypy:\n\t@printf "%s" "$(MYPY)"\n'
)


class MakePathCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "Makefile").write_text(MAKEFILE.read_text(encoding="utf-8"), encoding="utf-8")
        (self.dir / "extra.mk").write_text(SHOW_PATH, encoding="utf-8")
        # The Makefile reads the declared minimum from here at parse time.
        (self.dir / "pyproject.toml").write_text('requires-python = ">=3.10"\n', encoding="utf-8")

    def make_venv(self) -> Path:
        binary = self.dir / ".venv" / "bin"
        binary.mkdir(parents=True)
        python = binary / "python3"
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(0o755)
        return binary

    def path_seen_by_a_recipe(self, **env: str) -> list[str]:
        base = {k: v for k, v in os.environ.items() if k != "CODIFY_FLOOR_LEG"}
        base.update(env)
        done = subprocess.run(
            ["make", "-s", "-f", "Makefile", "-f", "extra.mk", "show-path"],
            cwd=self.dir, env=base, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout.split(os.pathsep)


class TestTheVenvIsPreferred(MakePathCase):
    def test_a_checkout_venv_goes_first_on_path(self) -> None:
        venv_bin = self.make_venv()

        entries = self.path_seen_by_a_recipe()

        self.assertEqual(str(venv_bin), entries[0])

    def test_no_venv_leaves_path_alone(self) -> None:
        entries = self.path_seen_by_a_recipe(PATH="/usr/bin:/bin")

        self.assertEqual(["/usr/bin", "/bin"], entries)

    def test_a_directory_named_venv_that_holds_no_python_is_not_used(self) -> None:
        # A stray `.venv` folder is not an interpreter; putting it first would make
        # `python3` resolve to nothing at all.
        (self.dir / ".venv" / "bin").mkdir(parents=True)

        entries = self.path_seen_by_a_recipe(PATH="/usr/bin:/bin")

        self.assertEqual(["/usr/bin", "/bin"], entries)


class TestTheFloorLegKeepsItsOwnInterpreter(MakePathCase):
    def test_the_floor_marker_stops_the_venv_displacing_the_floor_python(self) -> None:
        self.make_venv()
        floor_bin = str(self.dir / "cache" / "venv-3.10" / "bin")

        # What the floor recipe hands its nested make: its own bin first, the marker set.
        entries = self.path_seen_by_a_recipe(
            PATH=f"{floor_bin}{os.pathsep}/usr/bin", CODIFY_FLOOR_LEG="1",
        )

        self.assertEqual(floor_bin, entries[0], "the .venv displaced the floor interpreter")
        self.assertNotIn(str(self.dir / ".venv" / "bin"), entries)

    def test_without_the_marker_the_same_setup_would_have_been_displaced(self) -> None:
        # The control: proves the marker is what protects the floor, so the test above
        # cannot pass merely because the venv logic is broken.
        self.make_venv()
        floor_bin = str(self.dir / "cache" / "venv-3.10" / "bin")

        entries = self.path_seen_by_a_recipe(PATH=f"{floor_bin}{os.pathsep}/usr/bin")

        self.assertEqual(str(self.dir / ".venv" / "bin"), entries[0])

    def test_the_floor_recipe_actually_sets_the_marker(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        recipe = [line for line in text.splitlines() if "PATH=$(PY_MIN_VENV)/bin" in line]
        self.assertEqual(1, len(recipe), "the floor recipe moved; update this test with it")
        self.assertIn("CODIFY_FLOOR_LEG=1", recipe[0])


class TestMypyComesFromTheRightPlace(MakePathCase):
    """`MYPY` is resolved by `$(shell)` at parse time, before the exported PATH applies.

    GNU make 4.3 runs `$(shell)` with the environment make *started* in, so a `mypy` earlier
    on the caller's PATH (a `uv tool` or pipx install, which has no pydantic for the plugin)
    won over the venv's and failed `make typecheck` on a clean clone — the fresh-clone run of
    `make ci` found it after everything else had passed.
    """

    def stub_mypy(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        binary = directory / "mypy"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        return binary

    def mypy_chosen(self, **env: str) -> str:
        base = {k: v for k, v in os.environ.items() if k not in ("CODIFY_FLOOR_LEG", "MYPY")}
        base.update(env)
        done = subprocess.run(
            ["make", "-s", "-f", "Makefile", "-f", "extra.mk", "show-mypy"],
            cwd=self.dir, env=base, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout

    def test_the_venvs_mypy_beats_a_global_one_earlier_on_path(self) -> None:
        venv_mypy = self.stub_mypy(self.make_venv())
        global_mypy = self.stub_mypy(self.dir / "global-tools")

        chosen = self.mypy_chosen(PATH=f"{global_mypy.parent}{os.pathsep}/usr/bin{os.pathsep}/bin")

        self.assertEqual(str(venv_mypy), chosen, "a global mypy shadowed the checkout's venv")

    def test_the_floor_leg_keeps_the_mypy_of_its_own_interpreter(self) -> None:
        self.stub_mypy(self.make_venv())
        floor_mypy = self.stub_mypy(self.dir / "cache" / "venv-3.10" / "bin")

        chosen = self.mypy_chosen(
            PATH=f"{floor_mypy.parent}{os.pathsep}/usr/bin{os.pathsep}/bin", CODIFY_FLOOR_LEG="1",
        )

        self.assertEqual(str(floor_mypy), chosen, "the floor leg type-checked with the host's mypy")

    def test_without_a_venv_the_mypy_on_path_is_used(self) -> None:
        on_path = self.stub_mypy(self.dir / "tools")

        chosen = self.mypy_chosen(PATH=f"{on_path.parent}{os.pathsep}/usr/bin{os.pathsep}/bin")

        self.assertEqual(str(on_path), chosen)


class TestSetup(unittest.TestCase):
    def test_setup_makes_a_venv_installs_the_dev_extra_and_the_ui(self) -> None:
        done = subprocess.run(
            ["make", "-n", "setup"], cwd=MAKEFILE.parent, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("python3 -m venv .venv", done.stdout)
        self.assertIn('pip install -e ".[dev]"', done.stdout)
        self.assertIn("npm ci", done.stdout)


if __name__ == "__main__":
    unittest.main()
