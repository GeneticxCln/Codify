"""Which parts of the person's home a jailed command's tool may see.

The jail replaces `$HOME` with an empty tmpfs and binds back, read-only, only what the command's own
program needs. Two audit findings sat in that choice:

* a user-installed script (`pip install --user pytest` puts it in `~/.local/bin`) made the jail bind
  the whole of `~/.local`, whose `share/` holds keyrings and application data, so a jailed test could
  read `~/.local/share/keyrings/login.keyring`;
* `cargo` in `~/.cargo/bin` is a rustup proxy, and with `~/.rustup` hidden every `cargo test` failed
  with "rustup could not choose a version of cargo to run".

These call the bind builder with a real directory layout and assert on the paths it hands to bwrap.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.sandbox import SandboxService


def _bound(args: list[str]) -> list[Path]:
    """The read-only bind sources in a `_tool_binds` argument list."""
    return [Path(args[i + 1]) for i, word in enumerate(args) if word == "--ro-bind"]


class TestToolBinds(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "home"
        self.home.mkdir()

    def covers(self, bound: list[Path], path: Path) -> bool:
        return any(path == root or root in path.parents for root in bound)

    def binds_for(self, executable: Path) -> list[Path]:
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            for name in ("VIRTUAL_ENV", "PYTHONHOME", "RUSTUP_HOME"):
                os.environ.pop(name, None)
            return _bound(SandboxService._tool_binds(executable, self.home))

    def test_a_script_in_dot_local_bin_does_not_expose_dot_local_share(self) -> None:
        script = self.home / ".local" / "bin" / "pytest"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        (self.home / ".local" / "lib" / "python3" / "site-packages").mkdir(parents=True)
        keyring = self.home / ".local" / "share" / "keyrings" / "login.keyring"
        keyring.parent.mkdir(parents=True)
        keyring.write_text("secret", encoding="utf-8")
        bound = self.binds_for(script)
        self.assertFalse(self.covers(bound, keyring), f"the jail binds {bound}, which shows the keyring")
        self.assertNotIn(self.home / ".local", bound)
        # What the script does need is still there.
        self.assertTrue(self.covers(bound, script))
        self.assertTrue(self.covers(bound, self.home / ".local" / "lib" / "python3" / "site-packages"))

    def test_the_rustup_toolchains_are_visible_and_the_cargo_credentials_are_not(self) -> None:
        proxy = self.home / ".cargo" / "bin" / "cargo"
        proxy.parent.mkdir(parents=True)
        proxy.write_text("#!/bin/sh\n", encoding="utf-8")
        toolchain = self.home / ".rustup" / "toolchains" / "stable" / "bin"
        toolchain.mkdir(parents=True)
        token = self.home / ".cargo" / "credentials.toml"
        token.write_text('[registry]\ntoken = "secret"\n', encoding="utf-8")
        bound = self.binds_for(proxy)
        self.assertTrue(self.covers(bound, toolchain), "rustup cannot find a toolchain inside the jail")
        self.assertFalse(self.covers(bound, token), "the crates.io token is visible inside the jail")

    def test_an_interpreter_prefix_still_gets_its_libraries(self) -> None:
        interpreter = self.home / "pyenv" / "3.10" / "bin" / "python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("", encoding="utf-8")
        stdlib = self.home / "pyenv" / "3.10" / "lib" / "python3.10" / "os.py"
        stdlib.parent.mkdir(parents=True)
        stdlib.write_text("", encoding="utf-8")
        (self.home / "pyenv" / "3.10" / "share").mkdir()
        bound = self.binds_for(interpreter)
        self.assertTrue(self.covers(bound, stdlib))


if __name__ == "__main__":
    unittest.main()
