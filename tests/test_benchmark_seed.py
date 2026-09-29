"""`benchmarks.seed_endpoint` makes a real-model baseline reproducible without the settings screen.

A number from a real model is only worth recording if someone else can get the same setup: which endpoint,
which model, which role config. Doing that by hand in Settings → Agents is eight roles of clicking with no
record of what was chosen, so this writes them in one command, into a state directory of its own — never the
developer's real one, and never with a key in the database.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from benchmarks.runner import configured_models
from benchmarks.seed_endpoint import main
from engine.models import ROLES

URL = "http://127.0.0.1:8089/v1"
MODEL = "qwen2.5-1.5b-instruct-q4_k_m"


def seed(home: Path, *extra: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(["--home", str(home), "--base-url", URL, "--model", MODEL, *extra])
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class TestSeeding(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "bench-home"

    def test_every_role_gets_the_model_and_the_endpoint(self) -> None:
        code, _, err = seed(self.home)

        self.assertEqual(0, code, err)
        conn = sqlite3.connect(self.home / "codify.db")
        try:
            rows = conn.execute("SELECT role, provider, protocol, model_name, base_url FROM agent_configs").fetchall()
        finally:
            conn.close()
        self.assertEqual(set(ROLES), {r[0] for r in rows})
        for role, provider, protocol, model, base_url in rows:
            with self.subTest(role=role):
                self.assertEqual(("openai_compat", MODEL, URL), (protocol, model, base_url))
                self.assertEqual("localserver", provider)

    def test_the_runner_finds_every_role_ready(self) -> None:
        seed(self.home)

        self.assertEqual(set(ROLES), set(configured_models(self.home / "codify.db")))

    def test_a_key_the_caller_names_is_stored_in_the_private_file_and_never_in_the_database(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"BENCH_KEY": "sk-not-a-real-key-9f8e7d6c"}):
            code, out, err = seed(self.home, "--api-key-env", "BENCH_KEY")

        self.assertEqual(0, code, err)
        secrets = self.home / "secrets.json"
        self.assertIn("sk-not-a-real-key-9f8e7d6c", secrets.read_text(encoding="utf-8"))
        self.assertEqual(0o600, stat.S_IMODE(secrets.stat().st_mode))
        self.assertNotIn(b"sk-not-a-real-key-9f8e7d6c", (self.home / "codify.db").read_bytes())
        self.assertNotIn("sk-not-a-real-key-9f8e7d6c", out + err, "the key was printed")

    def test_without_a_key_a_placeholder_is_stored_and_said_to_be_one(self) -> None:
        code, out, _ = seed(self.home)

        self.assertEqual(0, code)
        self.assertIn("placeholder", out)
        self.assertTrue(json.loads((self.home / "secrets.json").read_text(encoding="utf-8")))

    def test_it_says_how_to_run_the_benchmark_against_what_it_wrote(self) -> None:
        _, out, _ = seed(self.home)

        self.assertIn(f"CODIFY_HOME={self.home}", out)
        self.assertIn("benchmarks.runner --tier repo_scale", out)
        self.assertIn(str(self.home / "codify.db"), out)

    def test_it_refuses_the_real_state_directory(self) -> None:
        # "Real" is a throwaway here: were the guard to regress, this test must not be what edits a
        # developer's actual roles.
        from unittest import mock

        fake_home = self.home.parent / "fake-home"
        with mock.patch("pathlib.Path.home", return_value=fake_home):
            code, _, err = seed(fake_home / ".codify")

        self.assertNotEqual(0, code)
        self.assertIn("real", err)
        self.assertFalse((fake_home / ".codify" / "codify.db").exists(), "the guard let the write through")

    def test_a_base_url_that_could_send_a_key_somewhere_else_is_refused(self) -> None:
        # The same rule as anywhere else a key is stored (docs/03 §1.2): plain http to a non-loopback host.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(["--home", str(self.home), "--base-url", "http://10.0.0.5:8080/v1", "--model", MODEL])
            except SystemExit as exc:
                code = int(exc.code) if isinstance(exc.code, int) else 1

        self.assertNotEqual(0, code)


if __name__ == "__main__":
    unittest.main()
