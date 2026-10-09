"""Fail when the Rust version Cargo.toml declares is lower than one a dependency needs.

`rust-version` in `src-tauri/Cargo.toml` is a promise: this crate builds on that compiler. It said 1.77.2 for as
long as nobody built on 1.77.2, and the locked dependency graph needed 1.88 (`darling`, `plist`). Nothing could
notice, because `cargo` only compares a dependency's `rust-version` to the compiler in use, never to ours.

This reads `cargo metadata --format-version 1` on stdin and compares the root package's declared version with the
highest `rust_version` among the packages it resolved. A declared version *above* that is fine (a floor you chose);
below it is a claim the graph contradicts. It reads stdin rather than running cargo, so it starts no process of its
own (docs/07) and a test can feed it any graph it likes.

    cd src-tauri && cargo metadata --format-version 1 --locked | python3 ../scripts/check_msrv.py
"""

from __future__ import annotations

import json
import sys
from typing import Any


def parse_version(text: str) -> tuple[int, ...]:
    """`1.88` or `1.88.0` as a tuple that sorts the way versions do; a missing patch is 0."""
    parts = [int(p) for p in text.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def problems(metadata: dict[str, Any]) -> list[str]:
    """What is wrong with the declared `rust-version`, one sentence each; empty when it holds."""
    packages: list[dict[str, Any]] = metadata.get("packages") or []
    root_id = (metadata.get("resolve") or {}).get("root")
    root = next((p for p in packages if p.get("id") == root_id), None)
    if root is None:
        return ["cargo metadata named no root package, so there is no declared rust-version to check"]
    declared = root.get("rust_version")
    if not declared:
        return [f"{root['name']} declares no rust-version, so nothing says which compiler it builds on"]
    needed = [
        (parse_version(p["rust_version"]), p["name"], p["version"])
        for p in packages
        if p.get("rust_version") and p.get("id") != root_id
    ]
    if not needed:
        return []
    highest, name, version = max(needed)
    if parse_version(declared) < highest:
        wanted = ".".join(str(n) for n in highest)
        return [
            f"{root['name']} declares rust-version {declared}, but {name} {version} needs {wanted}: "
            f"set rust-version to at least {wanted}, or pin an older {name} in Cargo.lock"
        ]
    return []


def main() -> int:
    found = problems(json.load(sys.stdin))
    for line in found:
        print(f"check_msrv: {line}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
