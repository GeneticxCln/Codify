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
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from engine import home
from engine.db import connect
from engine.providers import Keychain


def _stub_module(name: str, **attrs: Any) -> types.ModuleType:
    """A throwaway module for `sys.modules`, carrying arbitrary attributes.

    `types.ModuleType` declares none of the attributes a faked package needs,
    so the stubs are written through `__dict__` rather than by assignment.
    """
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def _free_port() -> int:
    """An ephemeral port, released for the boot path to bind.

    `main()` really does bind and listen, so a test that names a port needs that
    port to be free. Hard-coding the engine's first choice made the suite
    unrunnable for anyone with the app open — it failed with EADDRINUSE before it
    could assert anything. So the port is asked of the OS, and the tests compare
    the announced port against the bound one instead of a constant.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _FakeKeyring:
    """A keyring that *works*, and remembers everything it was asked to do."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str]] = []
        self.reads: list[tuple[str, str]] = []
        self.store: dict[tuple[str, str], str] = {}

    def install(self, usable: bool = True) -> None:
        """Install the stub. `usable=False` mimics a machine whose keyring is a stub."""
        backends = types.ModuleType("keyring.backends")

        class FailKeyring:  # the class the engine probes for and refuses
            pass

        fail = _stub_module("keyring.backends.fail", Keyring=FailKeyring)

        pkg = _stub_module(
            "keyring",
            __path__=[],
            # Anything that is not the fail backend counts as usable.
            get_keyring=(lambda: self) if usable else (lambda: FailKeyring()),
            set_password=self.set_password,
            get_password=self.get_password,
        )

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

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake_home = self.root / "real-home"
        self.fake_home.mkdir()
        self.scratch = self.root / "scratch"
        self.notices: list[str] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @contextlib.contextmanager
    def env(self, **values: str) -> Any:
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

    def assert_real_home_untouched(self) -> None:
        """Nothing was created under the (fake) real home directory. Ever."""
        leftovers = sorted(p.name for p in self.fake_home.rglob("*"))
        self.assertEqual(
            leftovers, [], f"an isolated run wrote into the real store: {leftovers}"
        )


class TestHomeResolution(_EnvCase):
    def test_the_default_store_is_under_the_home_directory(self) -> None:
        with self.env():
            self.assertEqual(home.codify_home(), self.fake_home / ".codify")
            self.assertEqual(home.db_path(), self.fake_home / ".codify" / "codify.db")
            self.assertEqual(
                home.secrets_path(), self.fake_home / ".codify" / "secrets.json"
            )
            self.assertFalse(home.is_isolated())

    def test_one_home_override_moves_both_stores(self) -> None:
        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            self.assertEqual(home.db_path(), self.scratch / "codify.db")
            self.assertEqual(home.secrets_path(), self.scratch / "secrets.json")
            self.assertTrue(home.is_isolated())
            self.assertEqual(home.codify_home(), self.scratch)

    def test_a_store_specific_override_beats_the_home(self) -> None:
        with self.env(
            **{
                home.ENV_HOME: str(self.scratch),
                home.ENV_DB: str(self.root / "elsewhere.db"),
            }
        ):
            self.assertEqual(home.db_path(), self.root / "elsewhere.db")
            self.assertEqual(home.secrets_path(), self.scratch / "secrets.json")

    def test_a_database_override_alone_is_not_isolation_but_is_stated(self) -> None:
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

    def test_an_isolated_run_says_so_at_boot(self) -> None:
        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            notice = home.startup_notice()
            self.assertIn(str(self.scratch), notice)
            self.assertIn("isolated", notice)
            self.assertNotIn("warning", notice)


class TestTheBootTokenOutlivesTheProcess(_EnvCase):
    """A restart must not lock out a client that already holds the token.

    The engine used to mint a new token every boot. The desktop shell noticed —
    it re-reads the handshake from the live process over IPC — so nobody saw a
    401 there, but a browser tab pointed at a dev engine has no shell to ask and
    stayed rejected until a human reloaded it. The token is state now, and these
    are the claims that make it state.
    """

    def setUp(self) -> None:
        super().setUp()
        # `_EnvCase` leaves the scratch directory to the code under test, which
        # creates it as a side effect; these tests put files there themselves.
        self.scratch.mkdir(parents=True, exist_ok=True)

    def token(self) -> str:
        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            return home.boot_token()

    def test_the_first_boot_mints_one_and_the_next_reuses_it(self) -> None:
        first = self.token()
        self.assertTrue(first)
        self.assertEqual(self.token(), first, "a restart rotated the token")

    def test_the_token_lives_beside_the_other_state(self) -> None:
        self.token()
        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            self.assertEqual(home.boot_token_path(), self.scratch / "boot_token")
        self.assertTrue((self.scratch / "boot_token").exists())
        self.assert_real_home_untouched()

    def test_the_token_file_is_owner_only(self) -> None:
        # A bearer credential any local account can read is a different security
        # claim from one they cannot, and the secrets file already draws that
        # line — see `Keychain._write_file`.
        self.token()
        mode = (self.scratch / "boot_token").stat().st_mode & 0o777
        self.assertEqual(oct(mode), oct(0o600))

    def test_a_token_left_by_an_earlier_boot_is_adopted(self) -> None:
        # The state a second engine finds, and the state a racing engine finds:
        # the file exists, so it has to present that token rather than replace
        # it, or the two processes disagree about who is the engine.
        path = self.scratch / "boot_token"
        path.write_text("earlier-boot-token\n")
        self.assertEqual(self.token(), "earlier-boot-token")
        self.assertEqual(path.read_text(), "earlier-boot-token\n")

    def test_a_racing_boot_loses_to_the_token_already_on_disk(self) -> None:
        # The interleaving a second engine finds: the file appears between our
        # read and our create. The ordinary "file is already there" case returns
        # from the read above and never reaches the create, so this is the only
        # way to cover it — and the one that would otherwise leave two engines
        # each holding a token the other does not accept.
        path = self.scratch / "boot_token"
        path.write_text("the-winners-token")
        real_read_text = Path.read_text
        answered_once: list[bool] = []

        def blind_first_read(target: Path, *args: Any, **kwargs: Any) -> str:
            # Report the file as missing, once, so the create is attempted.
            if target == path and not answered_once:
                answered_once.append(True)
                raise FileNotFoundError(target)
            return real_read_text(target, *args, **kwargs)

        with self.env(**{home.ENV_HOME: str(self.scratch)}), patch.object(
            Path, "read_text", blind_first_read
        ):
            self.assertEqual(home.boot_token(), "the-winners-token")
        self.assertEqual(path.read_text(), "the-winners-token")

    def test_an_empty_token_file_is_repaired_rather_than_ignored(self) -> None:
        # A boot that died between creating the file and filling it. Handing out
        # a fresh token without repairing the file would leave every later boot
        # ephemeral too — the old behaviour, permanently and invisibly.
        path = self.scratch / "boot_token"
        path.write_text("")
        token = self.token()
        self.assertTrue(token)
        self.assertEqual(path.read_text(), token)
        self.assertEqual(self.token(), token)

    def test_an_unwritable_state_directory_still_yields_a_token(self) -> None:
        # The engine has to boot even when it cannot keep state. A state
        # directory that is really a file fails the same way for every user,
        # including root, which a permission-based fixture would not.
        blocked = self.scratch / "not-a-directory"
        blocked.write_text("")
        with self.env(**{home.ENV_HOME: str(blocked)}):
            self.assertTrue(home.boot_token())

    def test_two_state_directories_do_not_share_a_token(self) -> None:
        # Otherwise anyone with an install on this machine could authenticate to
        # a scratch run of it.
        other = self.root / "other-scratch"
        with self.env(**{home.ENV_HOME: str(other)}):
            other_token = home.boot_token()
        self.assertNotEqual(other_token, self.token())
        self.assertEqual(len(other_token), 64, "32 bytes, as the old per-boot token was")

    def test_the_engine_boots_with_the_persisted_token(self) -> None:
        # The wiring, not just the helper: this process's `BOOT_TOKEN` was
        # minted from the suite's own throwaway state directory at import.
        import engine.app as app_module

        self.assertEqual(app_module.BOOT_TOKEN, home.boot_token())
        self.assertTrue(home.boot_token_path().exists())

    def test_the_environment_token_wins_over_the_persisted_one(self) -> None:
        # `CODIFY_BOOT_TOKEN` stays the escape hatch for a caller that wants a
        # token scoped to one process. Subprocess because the token is read at
        # import, which is exactly the thing under test.
        (self.scratch / "boot_token").write_text("from-the-file")
        result = subprocess.run(
            [sys.executable, "-c", "import engine.app; print(engine.app.BOOT_TOKEN)"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
            env={
                **os.environ,
                home.ENV_HOME: str(self.scratch),
                home.ENV_BOOT_TOKEN: "from-the-environment",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "from-the-environment")
        # The persisted token is still untouched by being overridden.
        self.assertEqual((self.scratch / "boot_token").read_text(), "from-the-file")


class TestTheSuiteItselfIsHermetic(unittest.TestCase):
    """The invariant `tests/hermetic.py` exists to hold, asserted in the suite.

    Not inside `_EnvCase.env()`: these run against the ambient environment, which is
    the point. If someone deletes the bootstrap (or a new test module forgets its
    one import line while the package form is not in play), this fails loudly instead
    of quietly rewriting a real store on the next `make test`.
    """

    def test_the_suite_runs_against_a_throwaway_store(self) -> None:
        self.assertTrue(home.is_isolated(), "tests/hermetic.py did not activate")
        real = Path.home() / ".codify"
        self.assertNotEqual(home.codify_home(), real)
        self.assertNotEqual(home.db_path(), real / "codify.db")
        self.assertNotEqual(home.secrets_path(), real / "secrets.json")
        self.assertFalse(
            home.keyring_allowed(), "the suite must not be able to reach a real keychain"
        )

    def test_a_key_saved_by_a_test_lands_in_the_throwaway_store(self) -> None:
        kc = Keychain()
        kc.set_provider_key("ollama", "sk-suite-probe")
        self.assertEqual(kc.backend, "file")
        self.assertEqual(
            kc.get_provider_key("ollama"), "sk-suite-probe", "read back from its own store"
        )
        kc.forget("providers/ollama")


class TestIsolatedRunsCannotTouchTheRealStore(_EnvCase):
    def setUp(self) -> None:
        super().setUp()
        self.keyring = _FakeKeyring()
        self.keyring.install()
        self.addCleanup(self.keyring.uninstall)

    def test_a_normal_run_uses_the_keychain_it_was_not_told_to_avoid(self) -> None:
        """Sanity: the stub really is a usable keyring, so the tests below mean something."""
        with self.env():
            kc = Keychain()
            self.assertEqual(kc.backend, "keyring")
            kc.set_provider_key("openai", "sk-real")
            self.assertEqual(len(self.keyring.writes), 1)

    def test_a_redirected_home_keeps_the_keychain_out_of_it(self) -> None:
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

    def test_a_redirected_secrets_file_keeps_the_keychain_out_of_it(self) -> None:
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

    def test_an_injected_store_is_the_store(self) -> None:
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

    def test_the_machine_readable_reason_distinguishes_the_three_causes(self) -> None:
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

    def test_where_and_why_are_two_statements(self) -> None:
        """`storage_detail` says where, `storage_reason` says why.

        If the destination also explained itself, the settings screen would state the
        same thing twice and the two copies could drift apart.
        """
        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            kc = Keychain()
            self.assertIn(str(self.scratch / "secrets.json"), kc.describes_backend())
            self.assertNotIn("deliberately", kc.describes_backend())
            self.assertEqual(kc.storage_reason(), "isolated_run")

    def test_the_database_lands_in_the_scratch_home_too(self) -> None:
        with self.env(**{home.ENV_HOME: str(self.scratch)}):
            conn = connect()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
            self.assertTrue((self.scratch / "codify.db").exists())
            self.assert_real_home_untouched()

    def _boot_with_stubbed_uvicorn(
        self, port: int
    ) -> tuple[io.StringIO, io.StringIO, list[tuple[()]], list[int]]:
        """Run the real boot path, with only uvicorn replaced, and capture it.

        `pick_port` is the one substitution: the port is the caller's choice.
        Everything downstream is production — the socket is created, bound and
        listened on for real, and the handshake is whatever actually got printed.
        `started` is the sockets uvicorn was handed; `bound` is the port each one
        was on, read before the stub closes them.
        """
        import engine.app as app_module

        started: list[tuple[()]] = []
        bound: list[int] = []

        class FakeServer:
            def __init__(self, config: Any) -> None:
                self.config = config

            def run(self, sockets: Any = None) -> None:
                started.append(tuple(sockets or ()))
                for s in sockets or ():
                    bound.append(int(s.getsockname()[1]))
                    s.close()

        fake_uvicorn = _stub_module(
            "uvicorn", Config=lambda *a, **k: types.SimpleNamespace(), Server=FakeServer
        )

        out, err = io.StringIO(), io.StringIO()
        with self.env(**{home.ENV_HOME: str(self.scratch)}), patch.dict(
            sys.modules, {"uvicorn": fake_uvicorn}
        ), patch.object(app_module, "pick_port", return_value=port), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            app_module.main()
        return out, err, started, bound

    def test_the_boot_notice_is_a_stderr_line_and_the_handshake_stays_on_stdout(self) -> None:
        """The Tauri shell scans stdout for `CODIFY_ENGINE token=… port=…`.

        So the state notice must not share that channel: a second stdout line is a
        line the shell has to tolerate, and one of them is a path.
        """
        import engine.app as app_module

        port = _free_port()
        out, err, started, bound = self._boot_with_stubbed_uvicorn(port)

        self.assertIn(
            f"CODIFY_ENGINE token={app_module.BOOT_TOKEN} port={port}", out.getvalue()
        )
        self.assertNotIn("isolated", out.getvalue(), "the handshake channel stays clean")
        self.assertIn("isolated", err.getvalue())
        self.assertIn(str(self.scratch), err.getvalue())
        # Bind-then-announce: the handshake may only print once the socket is
        # listening, so the server receives a pre-bound socket instead of a
        # port to bind later (the old announce-then-bind left a
        # connection-refused window right after "ready").
        self.assertEqual(bound, [port], "the announced port is the port that was bound")
        self.assertEqual(len(started), 1, "the server is still started")
        self.assertEqual(len(started[0]), 1, "uvicorn is handed the pre-bound socket")

    def test_the_boot_notice_survives_a_live_engine_on_the_default_port(self) -> None:
        """`make check` has to work with the app open.

        The boot path binds for real, so any port a test names becomes a port the
        suite needs free. Naming the engine's own default meant a developer with
        the engine running could not run the suite at all. Nothing in the
        handshake needs that port, so this stands in for a live engine on it and
        boots regardless.
        """
        default_port = 7430  # the first port `pick_port` tries

        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        try:
            blocker.bind(("127.0.0.1", default_port))
            blocker.listen(1)
        except OSError:
            # A real engine already holds it — the exact state under test, so
            # there is nothing left to stand in for.
            pass

        import engine.app as app_module

        port = _free_port()
        out, _err, started, bound = self._boot_with_stubbed_uvicorn(port)

        self.assertIn(
            f"CODIFY_ENGINE token={app_module.BOOT_TOKEN} port={port}", out.getvalue()
        )
        self.assertEqual(bound, [port], "the boot path bound a port of its own")
        self.assertEqual(len(started), 1, "the server is still started")


if __name__ == "__main__":
    unittest.main()
