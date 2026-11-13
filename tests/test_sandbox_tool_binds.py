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

from engine.sandbox import GO_PROXY_DEST, SandboxService, _go_proxy_value, _jail_env


def _bound(args: list[str]) -> list[Path]:
    """The read-only bind sources in a `_tool_binds` argument list."""
    return [Path(args[i + 1]) for i, word in enumerate(args) if word == "--ro-bind"]


def _dest_of(args: list[str], source: Path) -> str | None:
    """Where the jail shows `source`, or None when it is not bound."""
    for i, word in enumerate(args):
        if word == "--ro-bind" and Path(args[i + 1]) == source:
            return args[i + 2]
    return None


class TestToolBinds(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "home"
        self.home.mkdir()

    def covers(self, bound: list[Path], path: Path) -> bool:
        return any(path == root or root in path.parents for root in bound)

    def binds_for(self, executable: Path) -> list[Path]:
        return _bound(self.args_for(executable))

    def args_for(self, executable: Path) -> list[str]:
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            for name in ("VIRTUAL_ENV", "PYTHONHOME", "RUSTUP_HOME", "CARGO_HOME", "GOMODCACHE", "GOPATH", "GOPROXY"):
                os.environ.pop(name, None)
            return SandboxService._tool_binds(executable, self.home)

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

    def test_cargo_sees_the_downloaded_crates_but_not_the_sources_or_the_credentials(self) -> None:
        proxy = self.home / ".cargo" / "bin" / "cargo"
        proxy.parent.mkdir(parents=True)
        proxy.write_text("#!/bin/sh\n", encoding="utf-8")
        for part in ("index", "cache", "src"):
            (self.home / ".cargo" / "registry" / part / "index.crates.io-x").mkdir(parents=True)
        (self.home / ".rustup").mkdir()
        (self.home / ".cargo" / "credentials.toml").write_text("token", encoding="utf-8")
        (self.home / ".cargo" / "config.toml").write_text("[registries]", encoding="utf-8")
        bound = self.binds_for(proxy)
        registry = self.home / ".cargo" / "registry"
        self.assertTrue(self.covers(bound, registry / "index" / "index.crates.io-x"))
        self.assertTrue(self.covers(bound, registry / "cache" / "index.crates.io-x"))
        # `src` is what `cargo` unpacks into, so it must stay writable (not bound): a read-only one failed
        # "failed to unpack package" for any crate the person had downloaded but not yet built.
        self.assertFalse(self.covers(bound, registry / "src" / "index.crates.io-x"))
        self.assertFalse(self.covers(bound, self.home / ".cargo" / "credentials.toml"))
        self.assertFalse(self.covers(bound, self.home / ".cargo" / "config.toml"))

    def test_go_gets_its_download_cache_as_a_proxy_and_not_at_its_own_path(self) -> None:
        go = self.home / "sdk" / "go" / "bin" / "go"
        go.parent.mkdir(parents=True)
        go.write_text("", encoding="utf-8")
        download = self.home / "go" / "pkg" / "mod" / "cache" / "download"
        download.mkdir(parents=True)
        extracted = self.home / "go" / "pkg" / "mod" / "github.com" / "google" / "uuid@v1.6.0"
        extracted.mkdir(parents=True)
        args = self.args_for(go)
        # At its own path `go` would take a lock file in it, and a read-only mount refuses that.
        self.assertEqual(GO_PROXY_DEST, _dest_of(args, download))
        self.assertFalse(self.covers(_bound(args), extracted))

    def test_a_jailed_go_asks_the_cache_first_and_then_the_persons_own_proxy(self) -> None:
        with mock.patch.dict(os.environ, {"GOPROXY": "off"}):
            self.assertEqual(f"file://{GO_PROXY_DEST},off", _go_proxy_value())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOPROXY", None)
            self.assertEqual(f"file://{GO_PROXY_DEST},https://proxy.golang.org,direct", _go_proxy_value())
        self.assertEqual({}, _jail_env(["bwrap", "--", "pytest"]))
        self.assertIn("GOPROXY", _jail_env(["bwrap", "--ro-bind", "/x", GO_PROXY_DEST, "--", "go"]))

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
