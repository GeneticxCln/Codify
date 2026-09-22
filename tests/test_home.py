"""Where engine state lands, and the guarantee that a redirected run is isolated.

The bug this suite exists for is concrete: a verification run with `CODIFY_DB`
pointed at a temp directory saved a fake provider key into the developer's *real*
`~/.codify/secrets.json`. `CODIFY_DB` moved the database; nothing moved the
credentials, and the keychain was tried first on top of that. So the tests below
install a fake `keyring` module and assert it records **zero** reads and writes
whenever the run was pointed at its own store — including the case of a store
handed straight to `Keychain`, which is how the API tests build one.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import contextlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import home
from engine.db import connect
from engine.providers import Keychain


class _FakeKeyring:
    """A keyring that *works*, and remembers everything it was asked to do."""

    def __init__(self):
        self.writes: list[tuple[str, str, str]] = []
        self.reads: list[tuple[str, str]] = []
        self.store: dict[tuple[str, str], str] = {}

    def install(self, usable: bool = True) -> None:
        """Install the stub. `usable=False` mimics a machine whose keyring is a stub."""
        backends = types.ModuleType("keyring.backends")
        fail = types.ModuleType("keyring.backends.fail")

        class FailKeyring:  # the class the engine probes for and refuses
            pass

        fail.Keyring = FailKeyring  # type: ignore[attr-defined]

        pkg = types.ModuleType("keyring")
        pkg.__path__ = []  # type: ignore[attr-defined]
        # Anything that is not the fail backend counts as usable.
        pkg.get_keyring = (lambda: self) if usable else (lambda: FailKeyring())
        pkg.set_password = self.set_password
        pkg.get_password = self.get_password

        self._modules = {
            "keyring": pkg,
            "keyring.backends": backends,
            "keyring.backends.fail": fail,
        }
        sys.modules.update(self._modules)

    def uninstall(self) -> None:
        for name in self._modules:
            sys.modules.pop(name, None)

    def set_password(self, service: str, ref: str, value: str) -> None:
        self.writes.append((service, ref, value))
        self.store[(service, ref)] = value

    def get_password(self, service: str, ref: str) -> str | None:
        self.reads.append((service, ref))
        return self.store.get((service, ref))


class _EnvCase(unittest.TestCase):
    """An environment where the real home is a temp directory, and nothing leaks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake_home = self.root / "real-home"
        self.fake_home.mkdir()
        self.scratch = self.root / "scratch"
        self.notices: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    @contextlib.contextmanager
    def env(self, **values):
        """Set CODIFY_* / HOME for a `with` block, and nothing else's opinion in it.

        The CODIFY_* variables are *removed* first — including the suite-wide
        `CODIFY_HOME` that `tests/hermetic.py` installs, which would otherwise decide
        what these tests assert — and restored afterwards.
        """
        names = (home.ENV_HOME, home.ENV_DB, home.ENV_SECRETS)
        saved = {name: os.environ.pop(name, None) for name in names}
        base = {"HOME": str(self.fake_home), **values}
        try:
            with patch.dict(os.environ, base, clear=False):
                yield
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def assert_real_home_untouched(self):
        """Nothing was created under the (fake) real home directory. Ever."""
        leftovers = sorted(p.name for p in self.fake_home.rglob("*"))
        self.assertEqual(
            leftovers, [], f"an isolated run wrote into the real store: {leftovers}"
        )


class TestHomeResolution(_EnvCase):
    def test_the_default_store_is_under_the_home_directory(self):
        with self.env():
            self.assertEqual(home.codify_home(), self.fake_home / ".codify")
            self.assertEqual(home.db_path(), self.fake_home / ".codify" / "codify.db")
            self.assertEqual(
                home.secrets_path(), self.fake_home / ".codify" / "secrets.json"
            )
            self.assertFalse(home.is_isolated())

    def test_one_home_override_moves_both_stores(self):
        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            self.assertEqual(home.db_path(), self.scratch / "codify.db")
            self.assertEqual(home.secrets_path(), self.scratch / "secrets.json")
            self.assertTrue(home.is_isolated())
            self.assertEqual(home.codify_home(), self.scratch)

    def test_a_store_specific_override_beats_the_home(self):
        with self.env(
            **{
                home.ENV_HOME: str(self.scratch),
                home.ENV_DB: str(self.root / "elsewhere.db"),
            }
        ):
            self.assertEqual(home.db_path(), self.root / "elsewhere.db")
            self.assertEqual(home.secrets_path(), self.scratch / "secrets.json")

    def test_a_database_override_alone_is_not_isolation_but_is_stated(self):
        """Moving the database must not silently relocate a user's credentials.

        It also must not silently *look* isolated, which is why the notice for this
        case is a warning naming both stores rather than a plain boot line.
        """
        with self.env(**{home.ENV_DB: str(self.root / "only.db")}):
            self.assertFalse(home.is_isolated())
            self.assertTrue(home.keyring_allowed())
            notice = home.startup_notice()
            self.assertIn("warning", notice)
            self.assertIn(home.ENV_HOME, notice)

    def test_an_isolated_run_says_so_at_boot(self):
        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            notice = home.startup_notice()
            self.assertIn(str(self.scratch), notice)
            self.assertIn("isolated", notice)
            self.assertNotIn("warning", notice)


class TestTheSuiteItselfIsHermetic(unittest.TestCase):
    """The invariant `tests/hermetic.py` exists to hold, asserted in the suite.

    Not inside `_EnvCase.env()`: these run against the ambient environment, which is
    the point. If someone deletes the bootstrap (or a new test module forgets its
    one import line while the package form is not in play), this fails loudly instead
    of quietly rewriting a real store on the next `make test`.
    """

    def test_the_suite_runs_against_a_throwaway_store(self):
        self.assertTrue(home.is_isolated(), "tests/hermetic.py did not activate")
        real = Path.home() / ".codify"
        self.assertNotEqual(home.codify_home(), real)
        self.assertNotEqual(home.db_path(), real / "codify.db")
        self.assertNotEqual(home.secrets_path(), real / "secrets.json")
        self.assertFalse(
            home.keyring_allowed(), "the suite must not be able to reach a real keychain"
        )

    def test_a_key_saved_by_a_test_lands_in_the_throwaway_store(self):
        kc = Keychain()
        kc.set_provider_key("ollama", "sk-suite-probe")
        self.assertEqual(kc.backend, "file")
        self.assertEqual(
            kc.get_provider_key("ollama"), "sk-suite-probe", "read back from its own store"
        )
        kc.forget("providers/ollama")


class TestIsolatedRunsCannotTouchTheRealStore(_EnvCase):
    def setUp(self):
        super().setUp()
        self.keyring = _FakeKeyring()
        self.keyring.install()
        self.addCleanup(self.keyring.uninstall)

    def test_a_normal_run_uses_the_keychain_it_was_not_told_to_avoid(self):
        """Sanity: the stub really is a usable keyring, so the tests below mean something."""
        with self.env():
            kc = Keychain()
            self.assertEqual(kc.backend, "keyring")
            kc.set_provider_key("openai", "sk-real")
            self.assertEqual(len(self.keyring.writes), 1)

    def test_a_redirected_home_keeps_the_keychain_out_of_it(self):
        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            kc = Keychain()
            self.assertEqual(kc.backend, "file")
            kc.set_provider_key("google", "fake-google-key")

            self.assertEqual(self.keyring.writes, [], "the real keychain was written")
            self.assertEqual(self.keyring.reads, [], "the real keychain was read")
            stored = json.loads((self.scratch / "secrets.json").read_text())
            self.assertEqual(stored["providers/google"], "fake-google-key")
            # ...and the key comes back from that store, not from anywhere else.
            self.assertEqual(kc.get_provider_key("google"), "fake-google-key")
            self.assert_real_home_untouched()

    def test_a_redirected_secrets_file_keeps_the_keychain_out_of_it(self):
        with self.env(**{home.ENV_SECRETS: str(self.scratch / "keys.json")}):
            kc = Keychain()
            self.assertEqual(kc.backend, "file")
            kc.set_provider_key("openai", "sk-scratch")
            self.assertEqual(self.keyring.writes, [])
            self.assertEqual(
                json.loads((self.scratch / "keys.json").read_text())["providers/openai"],
                "sk-scratch",
            )
            self.assert_real_home_untouched()

    def test_an_injected_store_is_the_store(self):
        """What the API tests do — and what they were not getting before.

        `Keychain(secrets_path=...)` means \"use this file\". It used to still try the
        OS keychain first, so a test could write into the real one (and read a real
        credential back out).
        """
        target = self.scratch / "injected.json"
        with self.env():
            kc = Keychain(secrets_path=target)
            self.assertEqual(kc.backend, "file")
            kc.set_provider_key("anthropic", "sk-injected")
            self.assertEqual(self.keyring.writes, [])
            self.assertEqual(
                kc.get_provider_key("anthropic"), "sk-injected", "must read its own store"
            )
            self.assertEqual(
                json.loads(target.read_text())["providers/anthropic"], "sk-injected"
            )
            self.assert_real_home_untouched()

    def test_the_machine_readable_reason_distinguishes_the_three_causes(self):
        """`file` has three causes; only one of them is "this box has no keyring"."""
        with self.env():
            self.assertEqual(Keychain().storage_reason(), "keyring")
            self.assertEqual(Keychain(secrets_path=self.scratch / "x.json").storage_reason(), "isolated_run")

        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            self.assertEqual(Keychain().storage_reason(), "isolated_run")

        # A keyring that exists but is the fail backend: not isolation, a real absence.
        self.keyring.uninstall()
        self.keyring.install(usable=False)
        with self.env():
            self.assertEqual(Keychain().backend, "file")
            self.assertEqual(Keychain().storage_reason(), "no_keyring")
            self.assertTrue(home.keyring_allowed(), "this is not an isolation case")

    def test_where_and_why_are_two_statements(self):
        """`storage_detail` says where, `storage_reason` says why.

        If the destination also explained itself, the settings screen would state the
        same thing twice and the two copies could drift apart.
        """
        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            kc = Keychain()
            self.assertIn(str(self.scratch / "secrets.json"), kc.describes_backend())
            self.assertNotIn("deliberately", kc.describes_backend())
            self.assertEqual(kc.storage_reason(), "isolated_run")

    def test_the_database_lands_in_the_scratch_home_too(self):
        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            conn = connect()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
            self.assertTrue((self.scratch / "codify.db").exists())
            self.assert_real_home_untouched()

    def test_the_boot_notice_is_a_stderr_line_and_the_handshake_stays_on_stdout(self):
        """The Tauri shell scans stdout for `CODIFY_ENGINE token=… port=…`.

        So the state notice must not share that channel: a second stdout line is a
        line the shell has to tolerate, and one of them is a path.
        """
        import contextlib
        import io

        import engine.app as app_module

        started: list[tuple] = []
        fake_uvicorn = types.ModuleType("uvicorn")
        fake_uvicorn.run = lambda *a, **k: started.append((a, k))

        out, err = io.StringIO(), io.StringIO()
        with self.env(**{home.ENV_HOME: str(self.scratch)}), patch.dict(
            sys.modules, {"uvicorn": fake_uvicorn}
        ), patch.object(app_module, "pick_port", return_value=7430), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            app_module.main()

        self.assertIn(f"CODIFY_ENGINE token={app_module.BOOT_TOKEN} port=7430", out.getvalue())
        self.assertNotIn("isolated", out.getvalue(), "the handshake channel stays clean")
        self.assertIn("isolated", err.getvalue())
        self.assertIn(str(self.scratch), err.getvalue())
        self.assertEqual(len(started), 1, "the server is still started")


if __name__ == "__main__":
    unittest.main()
