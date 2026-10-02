"""`scripts/drive_a_turn.py --approve`: Start -> conductor -> steps, through a real process.

The script is the one tool that runs the engine against a model on a real socket. With `--approve` it presses
Start on the plan a turn made and reports how the run ended, which is the part of the pipeline nothing could
exercise without a real conductor. These run the *script itself* as a subprocess (it builds its own throwaway
`CODIFY_HOME`, points every role at the given endpoint, and uses the real routes) against the fake server's
scripted conductor, so what is checked is the command a person types.

A run that does not complete exits non-zero and says why, in the engine's own words, so a baseline script can
tell a conductor that finished from one that was paused.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from tests.test_fake_ollama import load_fake

ROOT = Path(__file__).resolve().parent.parent


class DriveCase(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {"FAKE_CONDUCTOR": "1"})
        env.start()
        self.addCleanup(env.stop)
        fake = load_fake()
        self.server = HTTPServer(("127.0.0.1", 0), fake.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def drive(self, *prompt: str, approve: bool = True) -> tuple[int, str, Path]:
        argv = [
            sys.executable, "-m", "scripts.drive_a_turn", "--provider", "ollama", "--model", "qwen2.5-coder:7b",
            "--base-url", self.url, "--timeout", "90", *(["--approve"] if approve else []), *prompt,
        ]
        done = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
        out = done.stdout + done.stderr
        found = re.search(r"^workspace: (.+)$", out, re.M)
        self.assertIsNotNone(found, out)
        assert found is not None
        workspace = Path(found.group(1))
        self.addCleanup(shutil.rmtree, workspace.parent, True)
        return done.returncode, out, workspace


class TestApprove(DriveCase):
    def test_start_is_pressed_on_the_plan_and_the_run_completes(self) -> None:
        code, out, workspace = self.drive("add a banner file")

        self.assertEqual(0, code, out)
        self.assertRegex(out, r"status: COMPLETED")
        self.assertEqual("hello from codify\n", (workspace / "banner.txt").read_text(encoding="utf-8"))
        self.assertRegex(out, r"\[run\] .*'outcome': 'completed'")
        for move in ("write", "verify", "review", "summarize"):
            self.assertIn(move, out)

    def test_without_the_flag_it_stops_at_the_plan_as_it_always_did(self) -> None:
        code, out, workspace = self.drive("add a banner file", approve=False)

        self.assertEqual(0, code, out)
        self.assertFalse((workspace / "banner.txt").exists(), "a file was written with nobody's approval")
        self.assertNotIn("[run]", out)

    def test_a_conductor_that_stops_early_exits_non_zero_and_says_why(self) -> None:
        code, out, workspace = self.drive("add a banner file [stall]")

        self.assertNotEqual(0, code, out)
        self.assertIn("conductor_stopped", out)
        self.assertRegex(out, r"\[run\] .*'outcome': 'paused:conductor_stopped'")

    def test_a_turn_that_made_no_plan_is_not_started(self) -> None:
        code, out, _ = self.drive("what does greet do?")

        self.assertEqual(0, code, out)
        self.assertNotIn("[run]", out)
        self.assertIn("nothing to start", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
