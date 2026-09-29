"""The state directory and everything in it belong to one user (audit M11).

`~/.codify` was created `0755` and `codify.db`, `codify.db-wal` and `codify.db-shm` were `0644`,
while `boot_token` and `secrets.json` were `0600`. The database holds every prompt, every diff
the fixer proposed and every recalled event, and `docs/03` treats it as protected as the token.
Ubuntu homes are `0750`, so on Ubuntu the exposure was hidden; Debian and Arch homes are not.

The properties asserted here are ones a process umask must not be able to break, so every test
runs under a deliberately permissive umask (`022`), which is the usual one and the one that
produced the finding. And an install that already has the wide modes is repaired on the next
start, because that is most of the machines this will ever run on.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine import home
from engine.db import connect
from engine.providers import Keychain


@contextmanager
def permissive_umask() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class StateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.state = self.base / "codify-home"
        for name in ("CODIFY_DB", "CODIFY_SECRETS", "CODIFY_BOOT_TOKEN"):
            self.addCleanup(os.environ.pop, name, None)
        patcher = patch.dict(os.environ, {"CODIFY_HOME": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        umask = permissive_umask()
        umask.__enter__()
        self.addCleanup(umask.__exit__, None, None, None)

    def everything_under_state(self) -> list[Path]:
        return sorted(self.state.rglob("*"))


class TestAFreshStateDirectory(StateCase):
    def test_the_directory_and_every_file_in_it_are_private(self) -> None:
        conn = connect()
        self.addCleanup(conn.close)
        # Make SQLite create its -wal and -shm files, which it does lazily on first write.
        conn.execute("CREATE TABLE IF NOT EXISTS scratch (x INTEGER)")
        conn.execute("INSERT INTO scratch VALUES (1)")
        conn.commit()
        home.boot_token()
        Keychain(secrets_path=home.secrets_path())._store("providers/openai", "sk-test")

        self.assertEqual(0o700, mode_of(self.state), "the state directory is not private")
        names = {p.name for p in self.everything_under_state()}
        self.assertTrue({"codify.db", "boot_token", "secrets.json"} <= names, names)
        self.assertTrue(any(n.endswith("-wal") for n in names), f"the test never produced a WAL file: {names}")
        for path in self.everything_under_state():
            if path.is_file():
                self.assertEqual(0o600, mode_of(path), f"{path.name} is readable by other users")
            else:
                self.assertEqual(0o700, mode_of(path), f"{path.name}/ is open to other users")


class TestAnInstallThatAlreadyHasTheWideModes(StateCase):
    def test_starting_the_engine_tightens_what_an_older_build_left_open(self) -> None:
        self.state.mkdir()
        os.chmod(self.state, 0o755)
        first = connect()
        first.execute("CREATE TABLE IF NOT EXISTS scratch (x INTEGER)")
        first.execute("INSERT INTO scratch VALUES (1)")
        first.commit()
        # Simulate the older build: every file it made was 0644 (the umask's doing).
        for path in self.state.iterdir():
            os.chmod(path, 0o644)
        os.chmod(self.state, 0o755)
        # Keep the connection open so its -wal/-shm exist while the next start runs.
        self.addCleanup(first.close)

        second = connect()
        self.addCleanup(second.close)

        self.assertEqual(0o700, mode_of(self.state))
        for path in self.state.iterdir():
            self.assertEqual(0o600, mode_of(path), f"{path.name} was left readable by other users")


class TestADatabaseOutsideTheStateDirectory(StateCase):
    def test_only_the_file_is_made_private_never_a_directory_the_user_chose(self) -> None:
        # `CODIFY_DB` may point anywhere, into a directory that holds other things. The
        # database file is ours to protect; the directory is not ours to chmod.
        chosen = self.base / "shared-projects"
        chosen.mkdir()
        os.chmod(chosen, 0o755)
        db = chosen / "my.db"

        with patch.dict(os.environ, {"CODIFY_DB": str(db)}):
            conn = connect()
        self.addCleanup(conn.close)

        self.assertEqual(0o600, mode_of(db))
        self.assertEqual(0o755, mode_of(chosen), "a directory the user chose was chmod'ed")

    def test_a_path_handed_straight_to_connect_is_treated_the_same_way(self) -> None:
        chosen = self.base / "elsewhere"
        chosen.mkdir()
        os.chmod(chosen, 0o755)

        conn = connect(chosen / "direct.db")
        self.addCleanup(conn.close)

        self.assertEqual(0o600, mode_of(chosen / "direct.db"))
        self.assertEqual(0o755, mode_of(chosen))


if __name__ == "__main__":
    unittest.main()
