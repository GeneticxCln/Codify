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


def connect_src() -> list[str]:
    config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    csp: str = config["app"]["security"]["csp"]
    for directive in csp.split(";"):
        parts = directive.split()
        if parts and parts[0] == "connect-src":
            return parts[1:]
    raise AssertionError("tauri.conf.json has no connect-src directive")


class TestTheWebviewsConnectBounds(unittest.TestCase):
    def test_no_local_host_other_than_the_engines_loopback_has_a_wildcard_port(self) -> None:
        for source in connect_src():
            with self.subTest(source=source):
                if re.search(r"^(https?|wss?)://[^/]*:\*$", source):
                    self.assertTrue(
                        source.startswith(("http://127.0.0.1:", "ws://127.0.0.1:")),
                        f"{source} lets the webview reach any port on a host that is not the engine's loopback",
                    )

    def test_the_engine_and_the_dev_servers_hot_reload_are_still_reachable(self) -> None:
        sources = connect_src()
        for needed in ("http://127.0.0.1:*", "ws://127.0.0.1:*", "http://localhost:5173", "ws://localhost:5173"):
            self.assertIn(needed, sources)

    def test_the_dev_servers_port_is_the_one_the_csp_names(self) -> None:
        vite = (ROOT / "ui" / "vite.config.ts").read_text(encoding="utf-8")
        self.assertRegex(vite, r"port:\s*5173")
        self.assertRegex(vite, r"strictPort:\s*true")


if __name__ == "__main__":
    unittest.main()
