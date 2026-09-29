"""What the engine says about itself, and the one warning worth a warning.

The failure this module guards is not a crash — it is a capability installed into an
environment the engine is not running in. `pip install laya` into `.venv` makes the
SDK available to `make test` and leaves the desktop shell, which resolves `python3`
from the login shell's PATH, running an engine that cannot import it. Nothing raises.
The gate just quietly uses the fallback model on every goal, at ~20 s and ~1.4k tokens
against ~31 ms and none, and the only evidence is a latency nobody connected to a
missing package.

So the tests below assert three things: the facts are the interpreter's real facts,
the SDK probe reports a broken install as broken rather than absent, and the warning
appears exactly when the checkout's own interpreter is being bypassed — and not on a
healthy install, because a warning that is always present is not read.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import importlib
import importlib.metadata
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from engine import capabilities
from engine.laya import SDK_DISABLE_ENV


class InterpreterReportTests(unittest.TestCase):
    def test_the_interpreter_is_named_as_the_running_one(self) -> None:
        report = capabilities.interpreter_report()
        self.assertEqual(report["executable"], sys.executable)
        self.assertEqual(report["prefix"], sys.prefix)
        self.assertEqual(report["base_prefix"], sys.base_prefix)
        self.assertEqual(report["version_info"], [sys.version_info[0], sys.version_info[1]])
        self.assertTrue(report["implementation"])

    def test_being_in_a_virtualenv_is_derived_not_assumed(self) -> None:
        """`sys.prefix != sys.base_prefix` is the definition; a path substring is not.

        A checkout whose path contains ".venv" in a directory name would satisfy a
        substring test while running the system interpreter, which is the exact
        mistake this report exists to avoid making.
        """
        with patch.object(sys, "prefix", "/opt/venv-thing"):
            with patch.object(sys, "base_prefix", "/opt/venv-thing"):
                self.assertFalse(capabilities.interpreter_report()["in_virtualenv"])
            with patch.object(sys, "base_prefix", "/usr"):
                self.assertTrue(capabilities.interpreter_report()["in_virtualenv"])


class LayaSdkReportTests(unittest.TestCase):
    def test_an_install_that_cannot_be_imported_says_why(self) -> None:
        """Presence is not the question; importability is.

        A package that is on the path and raises on import is the case a presence
        check reports as fine. The reason has to survive into the report, because
        "not importable" alone sends the reader to `pip install` — which is not the
        fix for a half-installed one.
        """
        with patch.object(importlib, "import_module", side_effect=ImportError("boom")):
            report = capabilities.laya_sdk_report()
        self.assertFalse(report["importable"])
        self.assertIn("ImportError", report["import_error"] or "")
        self.assertIn("boom", report["import_error"] or "")

    def test_an_importable_sdk_reports_no_error_and_a_version(self) -> None:
        # `patch.object` on the module this test imported, not `patch("...")` on a
        # dotted string: the string form resolves the target through
        # `pkgutil.resolve_name`, which on the system interpreter patches a
        # different object than `engine.capabilities` looks the attribute up on,
        # so the patch applies and the real function still runs. That surfaced as
        # a version of `None` on `python3` and the right answer under the venv —
        # a test whose result depends on which interpreter ran it is not a test.
        with patch.object(importlib, "import_module"):
            with patch.object(importlib.metadata, "version", return_value="0.3.21"):
                report = capabilities.laya_sdk_report()
        self.assertTrue(report["importable"])
        self.assertIsNone(report["import_error"])
        self.assertEqual(report["version"], "0.3.21")

    def test_being_disabled_by_the_environment_is_not_being_uninstalled(self) -> None:
        """The two are different fields because they need different fixes.

        `importable` is a property of the interpreter; `disabled_by_env` is a
        decision someone made. Collapsing them is how a UI tells someone to install
        a package they already have.
        """
        with patch.dict(os.environ, {SDK_DISABLE_ENV: "0"}):
            self.assertTrue(capabilities.laya_sdk_report()["disabled_by_env"])
        with patch.dict(os.environ, {SDK_DISABLE_ENV: "1"}):
            self.assertFalse(capabilities.laya_sdk_report()["disabled_by_env"])
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(capabilities.laya_sdk_report()["disabled_by_env"])


class MismatchWarningTests(unittest.TestCase):
    """The warning that earns its place: a checkout's venv the engine is not using."""

    @staticmethod
    def _interpreter(**overrides: object) -> dict[str, Any]:
        """A system interpreter, or one with the named facts changed.

        Built as a dict rather than by patching `sys` because `_warnings` is handed
        the interpreter it should reason about — the report is a value, and testing
        a value does not mean mutating the process to produce it.
        """
        report: dict[str, Any] = {
            "executable": "/usr/bin/python3", "version": "3.12.0", "version_info": [3, 12],
            "implementation": "CPython", "in_virtualenv": False,
            "prefix": "/usr", "base_prefix": "/usr",
        }
        report.update(overrides)
        return report

    @staticmethod
    def _sdk(importable: bool) -> dict[str, Any]:
        return {
            "importable": importable,
            "import_error": None if importable else "ModuleNotFoundError: No module named 'laya'",
            "version": "0.3.21" if importable else None,
        }

    def test_a_bypassed_checkout_interpreter_is_warned_about(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            interpreter_path = root / ".venv" / "bin" / "python3"
            interpreter_path.parent.mkdir(parents=True)
            interpreter_path.write_text("#!/bin/sh\n")

            warnings = capabilities._warnings(self._interpreter(), self._sdk(False), root)

        self.assertEqual(len(warnings), 1, warnings)
        # Both halves of the fix have to be in the message: the interpreter that is
        # running, and the one the checkout would have used.
        self.assertIn("/usr/bin/python3", warnings[0])
        self.assertIn("python3", warnings[0])

    def test_a_healthy_install_warns_about_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            interpreter_path = root / ".venv" / "bin" / "python3"
            interpreter_path.parent.mkdir(parents=True)
            interpreter_path.write_text("#!/bin/sh\n")
            # Running inside a venv, with the SDK importable: nothing to say.
            in_venv = self._interpreter(
                executable=str(interpreter_path), in_virtualenv=True,
                prefix=str(root / ".venv"),
            )
            self.assertEqual(capabilities._warnings(in_venv, self._sdk(True), root), [])

    def test_no_sdk_and_no_venv_is_the_documented_default_not_a_warning(self) -> None:
        """A fresh checkout has neither. That is the state the docs describe."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertEqual(capabilities._warnings(self._interpreter(), self._sdk(False), root), [])

    def test_an_interpreter_below_the_declared_floor_is_warned_about(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            old = self._interpreter(
                executable="/usr/bin/python3.9", version="3.9.18", version_info=[3, 9],
            )
            warnings = capabilities._warnings(old, self._sdk(True), root)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("3.9.18", warnings[0])


class StartupLinesTests(unittest.TestCase):
    def test_the_boot_log_names_the_interpreter_and_the_sdk_state(self) -> None:
        lines = capabilities.startup_lines()
        self.assertTrue(lines)
        self.assertIn(sys.executable, lines[0])
        self.assertTrue(any("laya SDK" in line for line in lines))

    def test_a_warning_reaches_the_log_and_not_only_the_api(self) -> None:
        """The report has to exist in `codify.log` before anyone opens Settings."""
        fake = {
            "interpreter": {"executable": "/usr/bin/python3", "version": "3.9.1",
                            "implementation": "CPython", "in_virtualenv": False,
                            "prefix": "/usr", "base_prefix": "/usr"},
            "project_root": "/tmp/x", "checkout_interpreter": "/tmp/x/.venv/bin/python3",
            "laya_sdk": {"importable": False, "import_error": "ModuleNotFoundError",
                         "version": None, "disabled_by_env": False},
            "warnings": ["something is wrong"],
        }
        with patch.object(capabilities, "capability_report", return_value=fake):
            lines = capabilities.startup_lines()
        self.assertIn("warning: something is wrong", lines)
        self.assertIn("laya SDK not importable — ModuleNotFoundError", lines)


class RuntimeRouteTests(unittest.IsolatedAsyncioTestCase):
    """The route the Settings screen reads. Separate from `/settings/laya`."""

    async def asyncSetUp(self) -> None:
        import httpx

        from engine.app import app, BOOT_TOKEN

        # The route reads no app state — it is a property of the process — so the
        # only thing the auth middleware needs is a token to compare against.
        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_it_reports_the_interpreter_and_the_sdk(self) -> None:
        response = await self.client.get("/settings/runtime", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertEqual(report["interpreter"]["executable"], sys.executable)
        self.assertIn("importable", report["laya_sdk"])
        self.assertIn("version", report["laya_sdk"])
        self.assertIsInstance(report["warnings"], list)
        self.assertIn("checkout_interpreter", report)

    async def test_it_answers_a_probe_that_carries_no_token(self) -> None:
        """It names filesystem paths, so the boot token is the whole of the guard."""
        response = await self.client.get("/settings/runtime")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
