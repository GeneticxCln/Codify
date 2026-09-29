"""The preview launcher hands over what it promised, or fails loudly.

`scripts/preview_engine.py` starts the engine and waits for the boot
handshake. The failure modes are the subject here, because they are the part a
developer cannot see from inside the script: a boot that never hands over must
end the script (not hang it), an engine that exits cleanly without booting is
a failure even though its exit code says otherwise, and the env file — which
holds the credential — must not land wider than 0600.

The engine is faked with a throwaway script that prints the same handshake
line the real one prints, so these run in seconds and never touch the real
`~/.codify`. One end-to-end case does boot the real `engine.app.serve` under a
scratch home — the pattern `tests/test_home.py` uses — because the
handshake's *format* is a contract between this script, the desktop shell
(`engine_protocol.rs`), and `engine/app.py`, and a fake would let the three
drift apart while every test here stayed green.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.request
from pathlib import Path

from scripts.preview_engine import paste_lines, read_handshake

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class HandshakeParsing(unittest.TestCase):
    def test_the_boot_line_yields_token_and_port(self) -> None:
        self.assertEqual(
            read_handshake("CODIFY_ENGINE token=9f2c port=7430"),
            ("9f2c", 7430),
        )

    def test_the_real_handshake_line_is_parsed(self) -> None:
        # The exact shape engine/app.py prints, trailing newline included —
        # the relay reads lines, not records.
        line = (
            "CODIFY_ENGINE token=a98663628194e78254e4ee182e530d3567de09342ae85d96f21b9a99404cc936 "
            "port=7430 hard_exit_s=6\n"
        )
        handshake = read_handshake(line)
        assert handshake is not None
        token, port = handshake
        self.assertEqual(port, 7430)
        self.assertTrue(token.startswith("a9866362"))

    def test_other_output_is_not_a_handshake(self) -> None:
        for line in [
            "state dir /home/x/.codify: database /home/x/.codify/codify.db\n",
            "INFO: Uvicorn running on http://127.0.0.1:7430\n",
            "CODIFY_ENGINE port=7430\n",  # token missing
            "CODIFY_ENGINE token=NOTHEX port=7430\n",  # not the minted shape
            "",
        ]:
            self.assertIsNone(read_handshake(line), f"{line!r} parsed as a handshake")

    def test_port_must_be_a_number(self) -> None:
        self.assertIsNone(read_handshake("CODIFY_ENGINE token=9f2c port=abc"))


class PasteOutput(unittest.TestCase):
    def test_two_assignments_matching_what_the_ui_reads(self) -> None:
        # ui/src/api.ts reads exactly these keys. A renamed key here is a
        # paste instruction that silently does nothing.
        self.assertEqual(
            paste_lines("9f2c", 7431),
            [
                'localStorage.setItem("CODIFY_PORT", "7431");',
                'localStorage.setItem("CODIFY_TOKEN", "9f2c");',
            ],
        )


class EnvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._original = os.environ.get("CODIFY_PREVIEW_ENV_FILE")
        os.environ["CODIFY_PREVIEW_ENV_FILE"] = str(Path(self.tmp.name) / "engine.env")

    def tearDown(self) -> None:
        if self._original is None:
            os.environ.pop("CODIFY_PREVIEW_ENV_FILE", None)
        else:
            os.environ["CODIFY_PREVIEW_ENV_FILE"] = self._original
        self.tmp.cleanup()

    def test_the_env_file_is_written_0600(self) -> None:
        from scripts.preview_engine import write_env_file

        path = Path(os.environ["CODIFY_PREVIEW_ENV_FILE"])
        write_env_file("9f2c", 7431)
        body = path.read_text()
        self.assertIn("CODIFY_PORT=7431", body)
        self.assertIn("CODIFY_TOKEN=9f2c", body)
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"the env file landed at {oct(mode)}; it holds a token")

    def test_an_existing_wider_file_is_tightened(self) -> None:
        from scripts.preview_engine import write_env_file

        path = Path(os.environ["CODIFY_PREVIEW_ENV_FILE"])
        path.write_text("CODIFY_PORT=0\nCODIFY_TOKEN=stale\n")
        path.chmod(0o644)
        write_env_file("9f2c", 7431)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, "a pre-existing 0644 file stayed wide")

    def test_an_invalid_timeout_override_is_a_loud_error(self) -> None:
        from scripts.preview_engine import boot_timeout_s

        original = os.environ.get("CODIFY_PREVIEW_TIMEOUT_S")
        os.environ["CODIFY_PREVIEW_TIMEOUT_S"] = "soon"
        try:
            with self.assertRaisesRegex(RuntimeError, "CODIFY_PREVIEW_TIMEOUT_S"):
                boot_timeout_s()
        finally:
            if original is None:
                os.environ.pop("CODIFY_PREVIEW_TIMEOUT_S", None)
            else:
                os.environ["CODIFY_PREVIEW_TIMEOUT_S"] = original


class EndToEnd(unittest.TestCase):
    """Drive the real script against a fake engine, and the real engine once."""

    # The driver calls `run()` with the caller's argv, so a fake engine can
    # stand where `-m engine` would be: `main()` prepends that module and the
    # fake would never see its own mode argument. Everything else — relay,
    # deadline, env file, exit normalization — is the same code path the real
    # invocation takes. The scripts directory is baked in at write time because
    # the driver itself lives in a temp dir, where `__file__` knows nothing.
    DRIVER = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, %(scripts)r)
        from preview_engine import run
        raise SystemExit(run(sys.argv[1:], dict(__import__("os").environ)))
        """
    ) % {"scripts": str(PROJECT_ROOT / "scripts")}

    FAKE_ENGINE = textwrap.dedent(
        """
        import sys, time
        mode = sys.argv[1]
        if mode == "boot":
            print("state dir /tmp/fake: database /tmp/fake/codify.db", file=sys.stderr, flush=True)
            print("CODIFY_ENGINE token=deadbeef port=7499 hard_exit_s=6", flush=True)
            print("INFO: Uvicorn running on http://127.0.0.1:7499", file=sys.stderr, flush=True)
            time.sleep(1)  # long enough to relay; the run ends when the engine does
        elif mode == "never":
            print("starting up", file=sys.stderr, flush=True)
            time.sleep(30)
        elif mode == "die-clean":
            print("exiting cleanly, having said nothing useful", file=sys.stderr, flush=True)
        elif mode == "die-loud":
            print("traceback: the port was taken", file=sys.stderr, flush=True)
            raise SystemExit(1)
        """
    )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        fake = Path(self.tmp.name) / "fake_engine.py"
        fake.write_text(self.FAKE_ENGINE)
        self.fake = str(fake)
        os.environ["CODIFY_PREVIEW_ENV_FILE"] = str(Path(self.tmp.name) / "engine.env")

    def tearDown(self) -> None:
        os.environ.pop("CODIFY_PREVIEW_ENV_FILE", None)
        self.tmp.cleanup()

    def _run(self, mode: str, timeout_s: float = 2.0) -> subprocess.CompletedProcess[str]:
        driver = Path(self.tmp.name) / "driver.py"
        driver.write_text(self.DRIVER)
        return subprocess.run(
            [sys.executable, str(driver), sys.executable, self.fake, mode],
            capture_output=True,
            text=True,
            timeout=timeout_s + 20,
            env={**os.environ, "CODIFY_PREVIEW_TIMEOUT_S": str(timeout_s)},
        )

    def test_a_booting_engine_is_relayed_and_handed_over(self) -> None:
        result = self._run("boot")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn('localStorage.setItem("CODIFY_PORT", "7499");', result.stdout)
        self.assertIn('localStorage.setItem("CODIFY_TOKEN", "deadbeef");', result.stdout)
        # The relay, not a quieter engine: stderr reached the developer too.
        self.assertIn("state dir /tmp/fake", result.stderr)
        body = Path(os.environ["CODIFY_PREVIEW_ENV_FILE"]).read_text()
        self.assertIn("CODIFY_TOKEN=deadbeef", body)
        self.assertEqual(
            stat.S_IMODE(Path(os.environ["CODIFY_PREVIEW_ENV_FILE"]).stat().st_mode),
            0o600,
        )

    def test_a_wedged_boot_is_killed_within_the_timeout(self) -> None:
        # The whole point of the deadline: this command must END, not hang.
        result = self._run("never", timeout_s=1.0)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no boot line within 1s", result.stderr)

    def test_a_clean_exit_without_a_handshake_is_a_failure(self) -> None:
        # Exit status 0 from an engine that never booted is the lie this
        # script exists to refuse.
        result = self._run("die-clean")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no boot handshake", result.stderr)
        self.assertIn("exited cleanly", result.stderr)

    def test_a_loud_failure_keeps_the_engine_s_status(self) -> None:
        result = self._run("die-loud")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the port was taken", result.stderr)

    def test_the_real_engine_boots_and_hands_over(self) -> None:
        # The contract end to end: the real serve() under a scratch home, the
        # real handshake, and a paste block that is *true* — the port in the
        # env file answers /health, authenticated, before the script says so.
        #
        # This cannot be a `subprocess.run`: after handover the script stays
        # in the foreground on purpose — it is the engine's lease on life — so
        # waiting for its exit would wait forever. The flow is the developer's
        # own: start it, read until the paste block, use what it printed, then
        # stop it and require a clean exit.
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        script = PROJECT_ROOT / "scripts" / "preview_engine.py"
        process = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "CODIFY_HOME": self.tmp.name,
                "CODIFY_DB": str(Path(self.tmp.name) / "codify.db"),
                "CODIFY_SECRETS": str(Path(self.tmp.name) / "secrets.json"),
                "CODIFY_PORT": str(free_port),
            },
        )
        assert process.stdout is not None
        paste_block: list[str] = []
        relayed: list[str] = []
        try:
            for line in process.stdout:
                if line.startswith("CODIFY_ENGINE"):
                    relayed.append(line)
                if "localStorage.setItem" in line:
                    paste_block.append(line.strip())
                if len(paste_block) == 2:
                    break
        finally:
            self.assertEqual(len(paste_block), 2, "no paste block within the read")
            self.assertEqual(len(relayed), 1, "the boot line itself must have been relayed")

        body = Path(os.environ["CODIFY_PREVIEW_ENV_FILE"]).read_text()
        port = int(
            next(
                line.split("=", 1)[1]
                for line in body.splitlines()
                if line.startswith("CODIFY_PORT=")
            )
        )
        bearer = next(
            line.split("=", 1)[1]
            for line in body.splitlines()
            if line.startswith("CODIFY_TOKEN=")
        )
        self.assertEqual(port, free_port, "the env file's port is not the one the engine bound")
        self.assertIn(f'"{free_port}"', paste_block[0], "the paste block names a different port")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.load(response), {"ok": True, "authenticated": True})

        # Stopping the script stops the engine: SIGTERM raises the handler's
        # KeyboardInterrupt, run() terminates the engine, and both leave
        # cleanly — the exit the foreground contract promises.
        process.terminate()
        self.assertEqual(process.wait(timeout=30), 0)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


if __name__ == "__main__":
    unittest.main()
