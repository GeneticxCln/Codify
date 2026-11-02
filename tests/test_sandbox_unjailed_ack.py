"""`auto` does not run a workspace's code unjailed until a person has said it may.

`sandbox_isolation=auto` used to mean "use bubblewrap if it works, otherwise run the command anyway and say so".
The commands the allowlist admits (`pytest`, `npm run`, `make`, `cargo test`, `python script.py`) execute code that
lives in the workspace, so on a machine with no working jail — every macOS and Windows machine, and a Linux one
without user namespaces — approving a goal in a cloned repository meant running that repository's code as the
person, with a warning in the log. The jail is the second boundary (docs/03 §1.4); `auto` without it was none.

Now `auto` with no jail refuses unless `sandbox_unjailed_ok` is 1, a switch only `PUT /settings/engine` writes
(docs/00 §6.2). `required` and `off` are unchanged, and a `SandboxService` with no settings at all stays the
unjailed one it has always been, which is what the librarian path and most of the suite construct.

Nothing here needs bubblewrap installed: its absence is simulated by `shutil.which`, so the proof does not depend
on the machine running it.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import _clean_engine_string  # noqa: F401 — imported so the settings tables are loaded
from engine.db import connect
from engine.profile import PORTABLE_INT_SETTINGS
from engine.sandbox import CommandNotAllowed, SandboxService, clear_bwrap_probe_cache
from engine.services import SettingsService

_REAL_WHICH: Any = shutil.which


def _without_bwrap(name: str, *args: object, **kwargs: object) -> str | None:
    return None if name == "bwrap" else _REAL_WHICH(name, *args, **kwargs)


class _Settings:
    """The three settings the sandbox reads, without a database behind them."""

    def __init__(self, isolation: str = "auto", network: int = 0, unjailed_ok: int | None = None) -> None:
        self.isolation = isolation
        self.network = network
        self.unjailed_ok = unjailed_ok

    def get_str(self, key: str) -> str:
        return self.isolation if key == "sandbox_isolation" else ""

    def get_int(self, key: str) -> int:
        if key == "sandbox_network":
            return self.network
        if key == "sandbox_unjailed_ok":
            if self.unjailed_ok is None:
                raise KeyError(key)  # a settings object that predates the switch: it must read as "no"
            return self.unjailed_ok
        return 0


class UnjailedRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / "probe.py").write_text("print('ran')\n", encoding="utf-8")
        clear_bwrap_probe_cache()
        self.addCleanup(clear_bwrap_probe_cache)

    def _run(self, settings: object | None) -> dict[str, object]:
        sandbox = SandboxService(settings=settings)
        with mock.patch("engine.sandbox.shutil.which", side_effect=_without_bwrap):
            return sandbox.run_command(str(self.root), ["python3", "probe.py"], timeout_s=30)

    def test_auto_without_a_jail_refuses_by_default(self) -> None:
        for settings in (_Settings("auto"), _Settings("auto", unjailed_ok=0), _Settings("auto", unjailed_ok=None)):
            with self.subTest(unjailed_ok=settings.unjailed_ok):
                with self.assertRaises(CommandNotAllowed) as caught:
                    self._run(settings)
                message = str(caught.exception)
                self.assertIn("sandbox_unjailed_ok", message, "the refusal names the setting that lifts it")
                self.assertIn("Settings", message, "and says where a person finds it")

    def test_auto_refuses_when_bwrap_is_installed_but_cannot_build_a_jail(self) -> None:
        stub = self.root / "bwrap"
        stub.write_text("#!/bin/sh\necho 'No permissions to create new namespace' >&2\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)

        def stub_which(name: str, *args: object, **kwargs: object) -> str | None:
            return str(stub) if name == "bwrap" else _REAL_WHICH(name, *args, **kwargs)

        with mock.patch("engine.sandbox.shutil.which", side_effect=stub_which):
            with self.assertRaises(CommandNotAllowed) as caught:
                SandboxService(settings=_Settings("auto")).run_command(
                    str(self.root), ["python3", "probe.py"], timeout_s=30,
                )
        self.assertIn("sandbox_unjailed_ok", str(caught.exception))
        self.assertIn("cannot build a jail", str(caught.exception))

    def test_the_switch_restores_the_old_run_and_still_says_it_was_unjailed(self) -> None:
        result = self._run(_Settings("auto", unjailed_ok=1))
        self.assertEqual(0, result["exit_code"], result)
        self.assertEqual("off", result["isolation"])
        self.assertIn("ran unjailed", str(result["isolation_warning"]))

    def test_required_and_off_do_not_read_the_switch(self) -> None:
        with self.assertRaises(CommandNotAllowed) as caught:
            self._run(_Settings("required", unjailed_ok=1))
        self.assertIn("required", str(caught.exception))

        result = self._run(_Settings("off", unjailed_ok=0))
        self.assertEqual(0, result["exit_code"], result)
        self.assertIsNone(result["isolation_warning"])

    def test_a_service_with_no_settings_is_still_the_unjailed_one(self) -> None:
        result = self._run(None)
        self.assertEqual(0, result["exit_code"], result)
        self.assertEqual("off", result["isolation"])

    def test_a_read_only_command_is_not_held_to_it_either_way(self) -> None:
        """Read-only mode runs `ls`, `wc` and a hardened `git`; the refusal is about running the project's code."""
        sandbox = SandboxService(settings=_Settings("auto"))
        with mock.patch("engine.sandbox.shutil.which", side_effect=_without_bwrap):
            result = sandbox.run_command(str(self.root), ["ls"], timeout_s=30, mode="read_only")
        self.assertEqual(0, result["exit_code"], result)


class SwitchSettingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = connect(Path(self._tmp.name) / "codify.db")
        self.addCleanup(self.conn.close)
        self.settings = SettingsService(self.conn)

    def test_it_is_off_on_a_fresh_install_and_clamps_to_a_switch(self) -> None:
        self.assertEqual(0, self.settings.get_int("sandbox_unjailed_ok"))
        self.assertEqual(1, self.settings.set_int("sandbox_unjailed_ok", 7))
        self.assertEqual(1, self.settings.get_int("sandbox_unjailed_ok"))
        self.assertEqual(0, self.settings.set_int("sandbox_unjailed_ok", 0))

    def test_a_profile_cannot_carry_it(self) -> None:
        self.assertNotIn("sandbox_unjailed_ok", PORTABLE_INT_SETTINGS)


if __name__ == "__main__":
    unittest.main()
