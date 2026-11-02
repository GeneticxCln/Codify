"""`run_command` inside bubblewrap, and the two places the opt-in lives.

The jail is exercised for real, not asserted about: a secret under this run's
`CODIFY_HOME` must be absent, `/tmp` outside the workspace must be read-only,
the workspace must still be writable, and the network must be unreachable
unless the goal or the engine setting opted in. These tests never touch the
developer's `~/.codify` — `tests/hermetic.py` already moved it — and they do
not skip when bubblewrap is missing: a container without `bwrap` cannot prove
the containment this file exists for.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine import home
from engine.app import _clean_engine_string
from engine.db import connect
from engine.models import GoalCreate, WorkspaceCreate
from engine.sandbox import CommandNotAllowed, SandboxService
from engine.services import GoalService, WorkspaceService


class _Settings:
    """The two settings the sandbox reads, without a database behind them."""

    def __init__(self, isolation: str = "auto", network: int = 0) -> None:
        self.isolation = isolation
        self.network = network

    def get_str(self, key: str) -> str:
        return self.isolation if key == "sandbox_isolation" else ""

    def get_int(self, key: str) -> int:
        return self.network if key == "sandbox_network" else 0


def _probe_script(secret: Path, outside: Path, port: int) -> str:
    """One workspace script: what the jail let it see and do, printed as KEY=yes/no."""

    def quoted(path: Path) -> str:
        return repr(str(path))

    return f"""
import os
import socket
from pathlib import Path

def check(name, action):
    try:
        action()
    except Exception:
        print(name + "=no")
    else:
        print(name + "=yes")

check("SECRET", lambda: Path({quoted(secret)}).read_text())
check("OUTSIDE", lambda: Path({quoted(outside)}).write_text("written"))
check("INSIDE", lambda: Path("inside.txt").write_text("written"))
check("HOME_WRITE", lambda: Path(os.environ["HOME"], "home.txt").write_text("written"))

def net():
    sock = socket.create_connection(("127.0.0.1", {port}), timeout=2)
    sock.close()

check("NET", net)
print("HOME=" + os.environ.get("HOME", ""))
"""


class SandboxJailTests(unittest.TestCase):
    """A real bwrap invocation around a real workspace command."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.secret = home.codify_home() / "secret.txt"
        self.secret.parent.mkdir(parents=True, exist_ok=True)
        self.secret.write_text("do not read me\n", encoding="utf-8")
        self.outside = Path(f"/tmp/codify-jail-outside-{os.getpid()}")
        self.outside.unlink(missing_ok=True)

        # One loopback listener: unreachable with `--unshare-net`, reachable when the goal opts in.
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.server.settimeout(10)
        self.port = self.server.getsockname()[1]
        self.accepted: list[socket.socket] = []

        def accept() -> None:
            try:
                conn, _ = self.server.accept()
                self.accepted.append(conn)
            except OSError:
                pass

        self.accept_thread = threading.Thread(target=accept, daemon=True)
        self.accept_thread.start()
        (self.root / "probe.py").write_text(
            _probe_script(self.secret, self.outside, self.port), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.server.close()
        for conn in self.accepted:
            conn.close()
        self.secret.unlink(missing_ok=True)
        self.outside.unlink(missing_ok=True)
        self.temp_dir.cleanup()

    def _run(self, network: bool) -> dict[str, object]:
        sandbox = SandboxService(settings=_Settings(network=int(network)))
        return sandbox.run_command(str(self.root), ["python3", "probe.py"], timeout_s=30, network=network)

    def test_the_jail_hides_state_and_writes_only_the_workspace(self) -> None:
        result = self._run(network=False)

        self.assertEqual(0, result["exit_code"], result)
        output = str(result["stdout"])
        self.assertIn("SECRET=no", output, "the jailed command read this run's state directory")
        self.assertIn("OUTSIDE=no", output, "the jailed command wrote outside the workspace")
        self.assertIn("INSIDE=yes", output, "the jailed command could not write its own workspace")
        self.assertIn("HOME_WRITE=yes", output, "the fresh HOME was not writable")
        self.assertIn(f"HOME={Path.home().resolve()}", output, "the jail changed a path the build may print")
        self.assertEqual("bwrap", result["isolation"])
        self.assertIsNone(result["isolation_warning"])
        self.assertFalse(self.outside.exists())
        self.assertEqual("written", (self.root / "inside.txt").read_text(encoding="utf-8"))

    def test_network_is_unreachable_until_the_goal_opts_in(self) -> None:
        blocked = self._run(network=False)
        self.assertEqual(0, blocked["exit_code"], blocked)
        self.assertIn("NET=no", str(blocked["stdout"]))

        allowed = self._run(network=True)
        self.assertEqual(0, allowed["exit_code"], allowed)
        self.assertIn("NET=yes", str(allowed["stdout"]))

    def test_required_refuses_to_run_without_bubblewrap(self) -> None:
        real_which = __import__("shutil").which

        def no_bwrap(name: str, *args: object, **kwargs: object) -> str | None:
            return None if name == "bwrap" else real_which(name, *args, **kwargs)

        sandbox = SandboxService(settings=_Settings(isolation="required"))
        with mock.patch("engine.sandbox.shutil.which", side_effect=no_bwrap):
            with self.assertRaises(CommandNotAllowed) as caught:
                sandbox.run_command(str(self.root), ["python3", "probe.py"], timeout_s=30)

        self.assertIn("bubblewrap", str(caught.exception))

    def test_auto_runs_and_says_it_is_unjailed_without_bubblewrap(self) -> None:
        real_which = __import__("shutil").which

        def no_bwrap(name: str, *args: object, **kwargs: object) -> str | None:
            return None if name == "bwrap" else real_which(name, *args, **kwargs)

        sandbox = SandboxService(settings=_Settings(isolation="auto"))
        with mock.patch("engine.sandbox.shutil.which", side_effect=no_bwrap):
            result = sandbox.run_command(str(self.root), ["python3", "probe.py"], timeout_s=30)

        self.assertEqual(0, result["exit_code"], result)
        self.assertEqual("off", result["isolation"])
        self.assertIn("ran unjailed", str(result["isolation_warning"]))

    def test_off_is_the_deliberate_unjailed_mode(self) -> None:
        sandbox = SandboxService(settings=_Settings(isolation="off"))
        result = sandbox.run_command(str(self.root), ["python3", "probe.py"], timeout_s=30)

        self.assertEqual(0, result["exit_code"], result)
        self.assertEqual("off", result["isolation"])
        self.assertIsNone(result["isolation_warning"])


class IsolationSettingTests(unittest.TestCase):
    """The setting accepts its three words and refuses every other one."""

    def test_only_required_auto_and_off_are_stored(self) -> None:
        self.assertEqual("auto", _clean_engine_string("sandbox_isolation", " auto "))
        self.assertEqual("required", _clean_engine_string("sandbox_isolation", "required"))
        self.assertEqual("off", _clean_engine_string("sandbox_isolation", "off"))

        with self.assertRaises(Exception) as caught:
            _clean_engine_string("sandbox_isolation", "disabled")
        self.assertIn("required, auto or off", str(caught.exception))


class GoalNetworkOptInTests(unittest.TestCase):
    """The per-goal half of the opt-in is stored, not accepted and dropped."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "test.db")
        self.workspace = WorkspaceService(self.conn).create(
            WorkspaceCreate(name="jail", root_path=str(self.root))
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_allow_network_is_persisted_and_defaults_off(self) -> None:
        goals = GoalService(self.conn)
        opted_in = goals.create(
            GoalCreate(workspace_id=self.workspace.id, title="needs npm", allow_network=True)
        )
        default = goals.create(
            GoalCreate(workspace_id=self.workspace.id, title="offline")
        )

        self.assertTrue(goals.get(opted_in.id).allow_network)
        self.assertFalse(goals.get(default.id).allow_network)


if __name__ == "__main__":
    unittest.main()
