"""The webview may reach the engine's loopback port and the dev server's one, and no other local port.

`connect-src` used to allow `http://localhost:*` and `ws://localhost:*`, so a script that ran in the webview
could call whatever listened on any local port by that name. The UI only ever talks to the engine at
`127.0.0.1` (`ui/src/api.ts`, `goalStream.ts`, `engineStream.ts`); `localhost` was there for Vite's hot reload,
which is one fixed port (`strictPort` in `ui/vite.config.ts`). The engine's own port cannot be named here:
it is chosen at launch and changes when the engine restarts, so `127.0.0.1:*` stays, and the boot token is what
protects it (docs/03).
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def directives(field: str = "csp") -> dict[str, list[str]]:
    config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    csp: str = config["app"]["security"][field]
    parsed: dict[str, list[str]] = {}
    for directive in csp.split(";"):
        parts = directive.split()
        if parts:
            parsed[parts[0]] = parts[1:]
    return parsed


def connect_src(field: str = "csp") -> list[str]:
    parsed = directives(field)
    if "connect-src" not in parsed:
        raise AssertionError(f"tauri.conf.json {field} has no connect-src directive")
    return parsed["connect-src"]


class TestTheWebviewsConnectBounds(unittest.TestCase):
    def test_no_local_host_other_than_the_engines_loopback_has_a_wildcard_port(self) -> None:
        for field in ("csp", "devCsp"):
            for source in connect_src(field):
                with self.subTest(field=field, source=source):
                    if re.search(r"^(https?|wss?)://[^/]*:\*$", source):
                        self.assertTrue(
                            source.startswith(("http://127.0.0.1:", "ws://127.0.0.1:")),
                            f"{source} lets the webview reach any port on a host that is not the engine's loopback",
                        )

    def test_production_csp_omits_the_dev_server_and_dev_csp_includes_it(self) -> None:
        prod = connect_src("csp")
        dev = connect_src("devCsp")
        for needed in ("http://127.0.0.1:*", "ws://127.0.0.1:*"):
            self.assertIn(needed, prod)
            self.assertIn(needed, dev)
        for dev_only in ("http://localhost:5173", "ws://localhost:5173"):
            self.assertNotIn(dev_only, prod)
            self.assertIn(dev_only, dev)

    def test_hardening_directives_are_present(self) -> None:
        for field in ("csp", "devCsp"):
            parsed = directives(field)
            with self.subTest(field=field):
                self.assertEqual(parsed.get("form-action"), ["'none'"])
                self.assertEqual(parsed.get("base-uri"), ["'self'"])
                self.assertEqual(parsed.get("object-src"), ["'none'"])
                self.assertEqual(parsed.get("frame-src"), ["'none'"])

    def test_the_dev_servers_port_is_the_one_the_csp_names(self) -> None:
        vite = (ROOT / "ui" / "vite.config.ts").read_text(encoding="utf-8")
        self.assertRegex(vite, r"port:\s*5173")
        self.assertRegex(vite, r"strictPort:\s*true")


if __name__ == "__main__":
    unittest.main()
