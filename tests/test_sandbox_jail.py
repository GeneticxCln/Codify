"""`run_command` inside bubblewrap, and the two places the opt-in lives.

The jail is exercised for real, not merely asserted about: a secret under this
run's `CODIFY_HOME` must be absent, `/tmp` outside the workspace must be
read-only, the workspace must still be writable, `.git` metadata must not be,
`/run` must be a fresh tmpfs, and the network must be unreachable unless the
goal or the engine setting opted in. These tests never touch the developer's
`~/.codify` — `tests/hermetic.py` already moved it — and they do not skip when
bubblewrap is missing: a container without `bwrap` cannot prove the containment
this file exists for.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
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
from engine.sandbox import (
    CommandNotAllowed,
    SandboxService,
    bwrap_can_jail,
    clear_bwrap_probe_cache,
)
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


def _probe_script(
    secret: Path, outside: Path, port: int, host_pid: int, host_socket: Path, host_tmp_file: Path,
) -> str:
    """One workspace script: what the jail let it see and do, printed as KEY=yes/no."""

    def quoted(path: Path) -> str:
        return repr(str(path))

    return f"""
import os
import socket
import tempfile
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
check("HOST_PID", lambda: os.kill({host_pid}, 0))
check("TMP_FILE", lambda: Path({quoted(host_tmp_file)}).read_text())
check("TMP_WRITE", lambda: (Path(tempfile.mkdtemp()) / "scratch.txt").write_text("scratch"))

def host_socket():
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    sock.connect({quoted(host_socket)})
    sock.close()

check("TMP_SOCKET", host_socket)

def net():
    sock = socket.create_connection(("127.0.0.1", {port}), timeout=2)
    sock.close()

check("NET", net)
print("HOME=" + os.environ.get("HOME", ""))
print("TMP=" + tempfile.gettempdir())
"""


class SandboxJailTests(unittest.TestCase):
    """A real bwrap invocation around a real workspace command."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.secret = home.codify_home() / "secret.txt"
        self.secret.parent.mkdir(parents=True, exist_ok=True)
        self.secret.write_text("do not read me\n", encoding="utf-8")
        # Outside the workspace and outside the jail's fresh /tmp: a write here must fail.
        self.outside = Path(f"/var/tmp/codify-jail-outside-{os.getpid()}")
        self.outside.unlink(missing_ok=True)

        # A host process, a host file and a host unix socket in /tmp. The jail must
        # not see any of them: a shared pid namespace could signal the process, and
        # /tmp is full of pathname sockets even though /run is hidden.
        self.host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.host_socket_path = Path(f"/tmp/codify-jail-socket-{os.getpid()}.sock")
        self.host_socket_path.unlink(missing_ok=True)
        self.host_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.host_socket.bind(str(self.host_socket_path))
        self.host_socket.listen(1)
        self.host_tmp_file = Path(f"/tmp/codify-jail-file-{os.getpid()}.txt")
        self.host_tmp_file.write_text("host only\n", encoding="utf-8")

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
            _probe_script(
                self.secret, self.outside, self.port, self.host.pid,
                self.host_socket_path, self.host_tmp_file,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.server.close()
        for conn in self.accepted:
            conn.close()
        self.host.terminate()
        try:
            self.host.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.host.kill()
            self.host.wait()
        self.host_socket.close()
        self.host_socket_path.unlink(missing_ok=True)
        self.host_tmp_file.unlink(missing_ok=True)
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
        self.assertIn("HOST_PID=no", output, "the jail shared the host's PID namespace")
        self.assertIn("TMP_FILE=no", output, "the jail read a host file in /tmp")
        self.assertIn("TMP_SOCKET=no", output, "the jail connected to a host unix socket in /tmp")
        self.assertIn("TMP_WRITE=yes", output, "the jail's /tmp was not writable")
        self.assertIn("TMP=/tmp", output, "the jail moved tempfile's directory out of /tmp")
        self.assertEqual("bwrap", result["isolation"])
        self.assertIsNone(result["isolation_warning"])
        self.assertFalse(self.outside.exists())
        self.assertEqual("written", (self.root / "inside.txt").read_text(encoding="utf-8"))
        self.assertEqual(
            {"inside.txt", "probe.py"}, {p.name for p in self.root.iterdir()},
            "a test temp directory was left in the workspace",
        )

    def test_the_jail_cannot_write_git_metadata(self) -> None:
        """A writable workspace must not make its `.git` writable too.

        A test that can write `.git/config` can name a `core.fsmonitor` or install a
        post-commit hook, and the engine's own commit runs outside this jail with the
        person's real HOME. The workspace stays writable; its metadata is bound back
        read-only.
        """
        git_dir = self.root / ".git"
        (git_dir / "hooks").mkdir(parents=True)
        config = git_dir / "config"
        config.write_text("[core]\n", encoding="utf-8")
        (self.root / "git_probe.py").write_text(
            "from pathlib import Path\n"
            "\n"
            "def check(name, action):\n"
            "    try:\n"
            "        action()\n"
            "    except Exception:\n"
            "        print(name + '=no')\n"
            "    else:\n"
            "        print(name + '=yes')\n"
            "\n"
            "check('GIT_CONFIG', lambda: Path('.git/config').open('a', encoding='utf-8').write('[core]\\n'))\n"
            "check('GIT_HOOK', lambda: Path('.git/hooks/post-commit').write_text('#!/bin/sh\\n', encoding='utf-8'))\n",
            encoding="utf-8",
        )

        result = SandboxService(settings=_Settings()).run_command(
            str(self.root), ["python3", "git_probe.py"], timeout_s=30
        )

        self.assertEqual(0, result["exit_code"], result)
        output = str(result["stdout"])
        self.assertIn("GIT_CONFIG=no", output, "the jailed command wrote .git/config")
        self.assertIn("GIT_HOOK=no", output, "the jailed command installed a hook")
        self.assertEqual("[core]\n", config.read_text(encoding="utf-8"))
        self.assertFalse((git_dir / "hooks" / "post-commit").exists())

    def test_the_jail_hides_the_hosts_run_and_tmp_directories(self) -> None:
        sandbox = SandboxService(settings=_Settings())
        jail, warning = sandbox._jail_argv(["python3", "probe.py"], self.root, False)

        assert jail is not None, warning
        pairs = list(zip(jail, jail[1:], strict=False))
        self.assertIn(("--tmpfs", "/run"), pairs)
        self.assertIn(("--tmpfs", "/tmp"), pairs)
        self.assertIn("--unshare-pid", jail, "the jail shared the host's PIDs")
        self.assertIn("--unshare-ipc", jail, "the jail shared the host's IPC namespace")

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

    def test_auto_falls_back_when_bwrap_installed_cannot_build_a_jail(self) -> None:
        """Installed is not usable: a bwrap that cannot make a namespace must not turn
        every harmless command into a failure, and `auto` must say it ran unjailed."""
        stub = self.root / "bwrap"
        stub.write_text(
            "#!/bin/sh\n"
            "echo 'bwrap: No permissions to create new namespace' >&2\n"
            "exit 1\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        clear_bwrap_probe_cache()
        self.addCleanup(clear_bwrap_probe_cache)
        real_which = __import__("shutil").which

        def stub_which(name: str, *args: object, **kwargs: object) -> str | None:
            return str(stub) if name == "bwrap" else real_which(name, *args, **kwargs)

        with mock.patch("engine.sandbox.shutil.which", side_effect=stub_which):
            result = SandboxService(settings=_Settings(isolation="auto")).run_command(
                str(self.root), ["python3", "probe.py"], timeout_s=30
            )
            self.assertEqual(0, result["exit_code"], result)
            self.assertEqual("off", result["isolation"])
            self.assertIn("cannot build a jail", str(result["isolation_warning"]))

            with self.assertRaises(CommandNotAllowed) as caught:
                SandboxService(settings=_Settings(isolation="required")).run_command(
                    str(self.root), ["python3", "probe.py"], timeout_s=30
                )

        self.assertIn("cannot build a jail", str(caught.exception))

    def test_off_is_the_deliberate_unjailed_mode(self) -> None:
        sandbox = SandboxService(settings=_Settings(isolation="off"))
        result = sandbox.run_command(str(self.root), ["python3", "probe.py"], timeout_s=30)

        self.assertEqual(0, result["exit_code"], result)
        self.assertEqual("off", result["isolation"])
        self.assertIsNone(result["isolation_warning"])


class BwrapProbeCacheTests(unittest.TestCase):
    """A timeout is not a fact about the host, and must not be remembered as one."""

    def setUp(self) -> None:
        clear_bwrap_probe_cache()
        self.addCleanup(clear_bwrap_probe_cache)

    def test_a_timed_out_probe_is_not_cached(self) -> None:
        starts: list[int] = []

        class Hanging:
            pid = 999_999
            returncode = None

            def __init__(self, *args: object, **kwargs: object) -> None:
                starts.append(1)

            def wait(self, timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired("bwrap", 5)

        with mock.patch("engine.sandbox.subprocess.Popen", Hanging):
            self.assertFalse(bwrap_can_jail("/nonexistent/bwrap"))
            self.assertFalse(bwrap_can_jail("/nonexistent/bwrap"))

        self.assertEqual(2, len(starts), "a probe timeout was cached as a host fact")

    def test_a_definite_answer_is_cached_per_network_choice(self) -> None:
        starts: list[int] = []

        class Done:
            pid = 999_998
            returncode = 0

            def __init__(self, *args: object, **kwargs: object) -> None:
                starts.append(1)

            def wait(self, timeout: float | None = None) -> int:
                return 0

        with mock.patch("engine.sandbox.subprocess.Popen", Done):
            self.assertTrue(bwrap_can_jail("/fake/bwrap"))
            self.assertTrue(bwrap_can_jail("/fake/bwrap"))
            self.assertEqual(1, len(starts), "a definite answer was probed twice")
            self.assertTrue(bwrap_can_jail("/fake/bwrap", network=True))

        self.assertEqual(2, len(starts), "the probe ignored the network choice")


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
