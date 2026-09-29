"""Codify starts from the applications menu, with no installer (audit of 2026-09-29, Phase 4).

The shell runs `python3 -m engine` from the checkout and knew the checkout only as its working
directory. Opened from a menu entry — the working directory is `$HOME` — it failed with "No engine to
run" before it started anything, and there was no launcher script and no menu entry anywhere. The
Linux answer to "no installer" is two small files in the user's own XDG directories, and these tests
pin what they promise:

* `scripts/codify` finds the checkout from *its own location*, however it was reached (a symlink on
  PATH is how it is normally reached), hands it to the shell as `CODIFY_ROOT`, and starts the
  **release** build only — a dev build opens a window that says "connection refused", which is
  indistinguishable from a broken app;
* `scripts/install-local.sh` writes exactly a symlink, a `.desktop` entry and an icon, refuses to
  write anything when there is nothing to launch, is idempotent, and `uninstall` removes exactly
  those three and nothing beside them.

The "app" here is a two-line shell stub that prints what it was given; a real window would open on a
developer's screen. `HOME` and every XDG variable point into a temporary directory, so a run cannot
touch the developer's real menu.
"""

from __future__ import annotations

import configparser
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

REPO = Path(__file__).resolve().parent.parent
STUB = '#!/bin/sh\necho "root=$CODIFY_ROOT"\necho "argv=$*"\necho "cwd=$(pwd)"\n'


class LauncherCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        # A checkout of our own: the scripts resolve the checkout from where they *are*, so they
        # are copied in rather than run from the real repository.
        self.root = self.base / "checkout"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "src-tauri" / "icons").mkdir(parents=True)
        for name in ("codify", "install-local.sh"):
            target = self.root / "scripts" / name
            shutil.copy2(REPO / "scripts" / name, target)
        shutil.copy2(REPO / "src-tauri" / "icons" / "128x128.png", self.root / "src-tauri" / "icons" / "128x128.png")
        self.home = self.base / "home"
        self.home.mkdir()
        self.elsewhere = self.base / "elsewhere"
        self.elsewhere.mkdir()

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_BIN_HOME": str(self.home / ".local" / "bin"),
        }
        env.update(extra)
        return env

    def build(self, profile: str = "release") -> Path:
        app = self.root / "src-tauri" / "target" / profile / "codify-desktop"
        app.parent.mkdir(parents=True, exist_ok=True)
        app.write_text(STUB, encoding="utf-8")
        app.chmod(app.stat().st_mode | stat.S_IXUSR)
        return app

    def run_script(
        self, argv: list[str], *, cwd: Path | None = None, extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — fixed argv, a stub app, a temp HOME
            argv, cwd=cwd or self.elsewhere, env=self.env(**(extra or {})),
            capture_output=True, text=True, timeout=30, check=False,
        )

    def install(self, action: str = "install", **extra: str) -> subprocess.CompletedProcess[str]:
        return self.run_script(["sh", str(self.root / "scripts" / "install-local.sh"), action], extra=extra)

    @property
    def link(self) -> Path:
        return self.home / ".local" / "bin" / "codify"

    @property
    def entry(self) -> Path:
        return self.home / ".local" / "share" / "applications" / "codify.desktop"

    @property
    def icon(self) -> Path:
        return self.home / ".local" / "share" / "icons" / "hicolor" / "128x128" / "apps" / "codify.png"


class TestTheLauncher(LauncherCase):
    def test_it_starts_the_release_build_with_the_checkout_named_from_any_directory(self) -> None:
        self.build()

        out = self.run_script([str(self.root / "scripts" / "codify"), "--flag", "value"], cwd=self.elsewhere)

        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn(f"root={self.root}", out.stdout)
        self.assertIn("argv=--flag value", out.stdout, "arguments were not passed on")
        self.assertIn(f"cwd={self.elsewhere}", out.stdout, "the launcher moved the app out of the caller's directory")

    def test_it_finds_the_checkout_through_a_symlink_which_is_how_it_is_normally_reached(self) -> None:
        self.build()
        (self.home / ".local" / "bin").mkdir(parents=True)
        os.symlink(self.root / "scripts" / "codify", self.link)

        out = self.run_script([str(self.link)], cwd=self.home)

        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn(f"root={self.root}", out.stdout)

    def test_a_dev_build_is_never_started_in_place_of_a_release_one(self) -> None:
        self.build("debug")

        out = self.run_script([str(self.root / "scripts" / "codify")])

        self.assertEqual(127, out.returncode)
        self.assertNotIn("root=", out.stdout, "a dev build was started; its window would only show a connection error")

    def test_with_nothing_built_it_says_what_to_run(self) -> None:
        out = self.run_script([str(self.root / "scripts" / "codify")])

        self.assertEqual(127, out.returncode)
        self.assertIn("make build-app", out.stderr)
        self.assertIn(str(self.root), out.stderr)

    def test_an_explicit_codify_root_is_kept(self) -> None:
        other = self.base / "another-checkout"
        (other / "src-tauri" / "target" / "release").mkdir(parents=True)
        app = other / "src-tauri" / "target" / "release" / "codify-desktop"
        app.write_text(STUB, encoding="utf-8")
        app.chmod(0o755)

        out = self.run_script([str(self.root / "scripts" / "codify")], extra={"CODIFY_ROOT": str(other)})

        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn(f"root={other}", out.stdout)


class TestInstallLocal(LauncherCase):
    def test_install_writes_a_link_an_entry_and_an_icon_that_agree_with_each_other(self) -> None:
        self.build()

        out = self.install()

        self.assertEqual(0, out.returncode, out.stderr)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.root / "scripts" / "codify", Path(os.readlink(self.link)))
        self.assertEqual((self.root / "src-tauri" / "icons" / "128x128.png").read_bytes(), self.icon.read_bytes())
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # type: ignore[assignment,method-assign] # desktop keys are case-sensitive
        parser.read(self.entry, encoding="utf-8")
        entry = parser["Desktop Entry"]
        self.assertEqual("Application", entry["Type"])
        self.assertEqual("Codify", entry["Name"])
        self.assertEqual(str(self.link), entry["Exec"], "the menu entry must start the command install just made")
        self.assertEqual("codify", entry["Icon"])
        self.assertEqual(self.icon.stem, entry["Icon"], "the entry names an icon that was not installed")
        self.assertEqual("false", entry["Terminal"])
        self.assertTrue(entry["Categories"].endswith(";"), "the spec wants a trailing semicolon")

    def test_the_installed_command_runs_the_app(self) -> None:
        self.build()
        self.install()

        out = self.run_script([str(self.link)], cwd=self.home)

        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn(f"root={self.root}", out.stdout)

    def test_it_refuses_to_write_anything_when_there_is_nothing_to_launch(self) -> None:
        out = self.install()

        self.assertNotEqual(0, out.returncode)
        self.assertIn("make build-app", out.stderr)
        self.assertFalse(self.link.exists() or self.link.is_symlink())
        self.assertFalse(self.entry.exists())
        self.assertFalse(self.icon.exists())

    def test_installing_twice_is_the_same_as_once(self) -> None:
        self.build()
        self.install()
        first = (self.entry.read_bytes(), os.readlink(self.link), self.icon.read_bytes())

        out = self.install()

        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual(first, (self.entry.read_bytes(), os.readlink(self.link), self.icon.read_bytes()))

    def test_uninstall_removes_those_three_and_nothing_beside_them(self) -> None:
        self.build()
        self.install()
        neighbours = [
            self.home / ".local" / "bin" / "other-tool",
            self.home / ".local" / "share" / "applications" / "other.desktop",
            self.home / ".local" / "share" / "icons" / "hicolor" / "128x128" / "apps" / "other.png",
        ]
        for path in neighbours:
            path.write_text("keep", encoding="utf-8")

        out = self.install("uninstall")

        self.assertEqual(0, out.returncode, out.stderr)
        self.assertFalse(self.link.is_symlink() or self.link.exists())
        self.assertFalse(self.entry.exists())
        self.assertFalse(self.icon.exists())
        for path in neighbours:
            self.assertEqual("keep", path.read_text(encoding="utf-8"), f"uninstall touched {path}")

    def test_uninstall_with_nothing_installed_is_not_an_error(self) -> None:
        self.assertEqual(0, self.install("uninstall").returncode)

    def test_a_path_the_desktop_entry_cannot_carry_is_refused_before_anything_is_written(self) -> None:
        self.build()
        bad = self.home / "we$ird"

        out = self.install(XDG_BIN_HOME=str(bad))

        self.assertNotEqual(0, out.returncode)
        self.assertFalse(bad.exists(), "a refused install still created its directory")
        self.assertFalse(self.entry.exists())

    def test_a_path_with_a_space_is_quoted_in_the_exec_line(self) -> None:
        self.build()
        spaced = self.home / "my bin"

        out = self.install(XDG_BIN_HOME=str(spaced))

        self.assertEqual(0, out.returncode, out.stderr)
        text = self.entry.read_text(encoding="utf-8")
        self.assertIn(f'Exec="{spaced}/codify"', text)

    def test_an_unknown_action_is_a_usage_error(self) -> None:
        out = self.install("frobnicate")

        self.assertEqual(2, out.returncode)
        self.assertIn("usage", out.stderr)


class TestTheScriptsAreExecutableInTheRepository(unittest.TestCase):
    def test_both_carry_the_executable_bit(self) -> None:
        # A launcher that a fresh clone cannot execute is the failure this exists to prevent; git
        # records the bit and a clone gets it back, so the mode on disk is the thing to assert.
        for name in ("codify", "install-local.sh"):
            mode = (REPO / "scripts" / name).stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, f"scripts/{name} is not executable")


if __name__ == "__main__":
    unittest.main()
