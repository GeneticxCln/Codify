"""`scripts/smoke_bundle.py` passes a bundle the shell can use, and fails each way one cannot be used.

The release runs the smoke on both packages before publishing them, so what it calls a pass is
what decides whether a broken engine reaches people. These tests give it fake bundles whose
`engine/__main__.py` is a small stub server and whose `python/bin/python3` hands over to the
interpreter running the suite, and check each verdict: a good boot passes; no handshake, a
health check that is not 200, a write into the bundle and a bundle built for another version
each fail, and say which.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts import smoke_bundle
from scripts.smoke_bundle import SmokeError

SERVER = textwrap.dedent(
    """
    import http.server, os, secrets, sys
    token = secrets.token_hex(32)
    STATUS = {status}
    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == "/health" and self.headers.get("Authorization") == f"Bearer {{token}}"
            self.send_response(STATUS if ok else 401)
            self.end_headers()
            self.wfile.write(b"{{}}")
        def log_message(self, *args):
            pass
    {before}
    server = http.server.HTTPServer(("127.0.0.1", 0), Health)
    print(f"CODIFY_ENGINE token={{token}} port={{server.server_address[1]}}", flush=True)
    server.serve_forever()
    """
)


def _bundle(root: Path, main: str, version: str = "1.3.0") -> Path:
    bundle = root / "engine-bundle"
    (bundle / "engine").mkdir(parents=True)
    (bundle / "engine" / "__init__.py").write_text("", encoding="utf-8")
    (bundle / "engine" / "__main__.py").write_text(main, encoding="utf-8")
    python = bundle / "python" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    (bundle / "BUNDLE.json").write_text(json.dumps({"app_version": version}), encoding="utf-8")
    return bundle


class TestTheSmokesVerdicts(unittest.TestCase):
    def test_an_engine_that_boots_and_answers_passes(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            bundle = _bundle(Path(scratch), SERVER.format(status=200, before=""))
            account = smoke_bundle.smoke(bundle, version="1.3.0", boot_timeout_s=20)
            self.assertIn("/health 200", account)

    def test_an_engine_that_exits_before_its_handshake_fails_with_its_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            bundle = _bundle(Path(scratch), "import sys\nsys.stderr.write('No module named fastapi\\n')\nsys.exit(3)\n")
            with self.assertRaises(SmokeError) as caught:
                smoke_bundle.smoke(bundle, boot_timeout_s=20)
            self.assertIn("exited with 3 before its handshake", str(caught.exception))
            self.assertIn("No module named fastapi", str(caught.exception))

    def test_an_engine_that_never_says_where_it_is_fails_on_the_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            bundle = _bundle(Path(scratch), "import time\nprint('booting', flush=True)\ntime.sleep(60)\n")
            with self.assertRaises(SmokeError) as caught:
                smoke_bundle.smoke(bundle, boot_timeout_s=1.5)
            self.assertIn("no handshake within", str(caught.exception))

    def test_a_health_check_that_is_not_200_fails(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            bundle = _bundle(Path(scratch), SERVER.format(status=500, before=""))
            with self.assertRaises(SmokeError) as caught:
                smoke_bundle.smoke(bundle, boot_timeout_s=20)
            self.assertIn("answered 500", str(caught.exception))

    def test_an_engine_that_writes_beside_itself_fails(self) -> None:
        # Root can write anywhere, so a read-only mode would prove nothing when CI runs as root;
        # the smoke compares the tree before and after instead.
        write = "open(os.path.join(os.path.dirname(__file__), 'cache.db'), 'w').close()"
        with tempfile.TemporaryDirectory() as scratch:
            bundle = _bundle(Path(scratch), SERVER.format(status=200, before=write))
            with self.assertRaises(SmokeError) as caught:
                smoke_bundle.smoke(bundle, boot_timeout_s=20)
            self.assertIn("created engine/cache.db", str(caught.exception))

    def test_a_bundle_built_for_another_release_fails_before_it_boots(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            bundle = _bundle(Path(scratch), "raise SystemExit('must not run')\n", version="1.2.0")
            with self.assertRaises(SmokeError) as caught:
                smoke_bundle.smoke(bundle, version="1.3.0")
            self.assertIn("built for '1.2.0'", str(caught.exception))

    def test_a_directory_that_is_not_a_bundle_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(SmokeError):
                smoke_bundle.smoke(Path(scratch))


class TestFindingTheBundleInAPackage(unittest.TestCase):
    def test_the_one_bundle_is_found_wherever_the_package_put_it(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            bundle = _bundle(Path(scratch) / "usr" / "lib" / "Codify", "")
            self.assertEqual(smoke_bundle.find_bundle(Path(scratch)), bundle)

    def test_no_bundle_or_two_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(SmokeError):
                smoke_bundle.find_bundle(Path(scratch))
            _bundle(Path(scratch) / "a", "")
            _bundle(Path(scratch) / "b", "")
            with self.assertRaises(SmokeError):
                smoke_bundle.find_bundle(Path(scratch))

    def test_the_engine_gets_the_shells_isolation_and_a_throwaway_home(self) -> None:
        env = smoke_bundle.engine_env(Path("/b"), Path("/h"))
        self.assertEqual(env["PYTHONPATH"], "/b")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(env["CODIFY_HOME"], "/h")
        self.assertNotIn("PYTHONHOME", env)


if __name__ == "__main__":
    unittest.main()
