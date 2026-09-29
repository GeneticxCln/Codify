"""The floor leg's one destructive act deletes only what is the script's to delete.

`scripts/ci-python-floor.sh` rebuilds a venv that does not hold a working
interpreter of the requested version, and rebuilding means `rm -rf`. The path
ending in `-<version>` was once the only guard on that deletion, which is no
guard at all: `ci-python-floor.sh 3.10 ~/proj-backup-3.10` satisfies it on the
way to deleting the backup, because a directory that is not a venv never has a
working interpreter in it — the guard fires precisely on the wrong targets.

So the script now asks for provenance before deleting: a directory carrying
this script's stamp (written only after a build succeeds), or a directory under
the default CI cache root the Makefile provisions from (covering a build
interrupted before any stamp existed). These tests drive the real script —
bash, not a reimplementation — with a stubbed `uv` so the rebuild completes
without a network or a real interpreter, which is also what keeps them hermetic:
the deletion either happened to a sentinel-bearing directory or it did not,
and the sentinel is what decides every assertion below.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci-python-floor.sh"

# The stub answers every `uv` subcommand with success and, for `uv venv`,
# creates just enough of a venv for the script to consider the build done: a
# bin/python whose `-c` reports the floor version. Nothing is downloaded.
UV_STUB = """\
#!/bin/sh
if [ "$1" = "venv" ]; then
  for arg do venv_dir="$arg"; done
  mkdir -p "$venv_dir/bin"
  cat > "$venv_dir/bin/python" <<'PY'
#!/bin/sh
case "$1" in
  -V*) echo "Python 3.10.0" ;;
  -c*) echo "3.10" ;;
  *) exit 0 ;;
esac
PY
  chmod +x "$venv_dir/bin/python"
fi
exit 0
"""

# A stand-in interpreter that reports the *wrong* version, so `have_version`
# fails and the rebuild branch — the only branch that deletes — is the one
# that runs.
WRONG_PYTHON = """\
#!/bin/sh
case "$1" in
  -V*) echo "Python 2.7.18" ;;
  -c*) echo "2.7" ;;
  *) exit 0 ;;
esac
"""


class TestRebuildDeletionIsOwned(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.bin = Path(self.tmp.name) / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        (self.bin / "uv").write_text(UV_STUB, encoding="utf-8")
        (self.bin / "uv").chmod(0o755)
        # /usr/bin and /bin stay on PATH behind the stub so the script's own
        # externals (cat, cksum, dirname) resolve, while no real uv or
        # python3.10 can be found to turn the rebuild into a network install.
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
        }
        self.env.pop("XDG_CACHE_HOME", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self, version: str, venv: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), version, str(venv)],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )

    def write_foreign_dir(self, name: str) -> Path:
        """A directory that matches the suffix rule and nothing else."""
        target = Path(self.tmp.name) / name
        target.mkdir()
        (target / "keepme.txt").write_text("not a venv\n", encoding="utf-8")
        return target

    def install_wrong_python(self, target: Path) -> None:
        bin_dir = target / "bin"
        bin_dir.mkdir()
        (bin_dir / "python").write_text(WRONG_PYTHON, encoding="utf-8")
        (bin_dir / "python").chmod(0o755)

    def test_a_foreign_directory_ending_in_the_version_is_refused_not_deleted(self) -> None:
        target = self.write_foreign_dir("proj-backup-3.10")

        result = self.run_script("3.10", target)

        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("refusing to replace", result.stderr)
        # The whole point: the directory, and everything in it, survived.
        self.assertTrue(target.is_dir(), "a foreign directory was deleted")
        self.assertTrue((target / "keepme.txt").exists(), "a foreign directory's contents were deleted")

    def test_a_stamped_directory_elsewhere_is_replaced(self) -> None:
        # The stamp is the ownership proof, and it is deliberately honoured
        # away from the default cache root: CI_CACHE is overridable (the
        # script's own help says so), and a venv the script built at an
        # overridden location has the stamp and nothing else to show for it.
        target = self.write_foreign_dir("venv-3.10")
        self.install_wrong_python(target)
        (target / ".ci-python-floor-stamp").write_text("x\n", encoding="utf-8")

        result = self.run_script("3.10", target)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("replacing", result.stdout)
        self.assertIn("ready", result.stdout)
        self.assertNotIn("reusing", result.stdout)
        self.assertFalse((target / "keepme.txt").exists(), "the replaced venv kept its old contents")
        self.assertTrue((target / ".ci-python-floor-stamp").exists())

    def test_a_directory_at_the_default_cache_root_is_replaced_even_without_a_stamp(self) -> None:
        # No stamp: the shape of a build interrupted before the script could
        # write one. The default cache root is the second proof, so this is
        # still replaceable — and the wrong-version interpreter is what sends
        # it down the rebuild path.
        target = self.home / ".cache" / "codify" / "venv-3.10"
        target.mkdir(parents=True)
        (target / "keepme.txt").write_text("half a build\n", encoding="utf-8")
        self.install_wrong_python(target)

        result = self.run_script("3.10", target)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("replacing", result.stdout)
        self.assertIn("ready", result.stdout)
        self.assertNotIn("reusing", result.stdout)
        self.assertFalse((target / "keepme.txt").exists(), "the rebuilt venv kept its old contents")

    def test_a_built_venv_is_reused_untouched(self) -> None:
        # Idempotency survives the ownership gate: a second run on a venv the
        # script just built must say so and delete nothing.
        target = self.write_foreign_dir("venv-3.10")
        self.install_wrong_python(target)
        (target / ".ci-python-floor-stamp").write_text("x\n", encoding="utf-8")
        first = self.run_script("3.10", target)
        self.assertEqual(0, first.returncode, first.stderr)

        second = self.run_script("3.10", target)

        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("reusing", second.stdout)
        # The reuse path's whole output: it exited before any build or delete.
        self.assertNotIn("replacing", second.stdout)
        self.assertNotIn("ready", second.stdout)


if __name__ == "__main__":
    unittest.main()
