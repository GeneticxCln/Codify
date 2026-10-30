"""Regenerate `packaging/engine.lock`: the engine's dependencies, pinned and hashed, for the bundle.

    make lock-engine                   # same as: python3 scripts/lock_engine.py

The lock is what `scripts/bundle_engine.py` installs into the CPython a package carries, with
`--require-hashes`. It is resolved by *that* interpreter's own pip — the pinned build from
`packaging/python.json`, downloaded and checked the same way the bundle does — for the same
platform tags the bundle installs with, from `engine/requirements.txt` (which
`tests/test_dependency_parity.py` keeps equal to `pyproject.toml`). So a version, a wheel and an
environment marker all mean here exactly what they will mean in the package, and a lock made on
a Mac does not quietly drop keyring's Linux-only dependencies.

Each line records the licence its wheel declares, because the packages *redistribute* these
files: `docs/18` §2 lists what that means, and a dependency whose licence is not one of
`ALLOWED` stops the lock instead of reaching a release. A wheel that declares no usable licence
needs an entry in `LICENCE_OVERRIDES` saying what it is and where that was read.

Run by a person when a dependency moves; never by the gate, which only checks that the committed
lock still covers `engine/requirements.txt` (`tests/test_bundle_engine.py`).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # noqa: S404 — argv lists only, never a shell
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import bundle_engine  # noqa: E402 — run as a file, the checkout has to be on the path first

REQUIREMENTS = bundle_engine.ROOT / "engine" / "requirements.txt"
RESOLVE_TIMEOUT_S = 600

# Licences a package may carry to be shipped inside Codify's packages. Permissive ones, plus the
# MPL's file-level copyleft (certifi, orjson, tld), which a package meets by shipping those
# files unmodified with their notice — the dist-info directories the bundle keeps.
ALLOWED = frozenset({
    "MIT", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0", "MPL-2.0", "MPL-1.1",
})

# Wheels whose metadata states a licence only as prose or not at all. Each says what it is.
LICENCE_OVERRIDES = {
    # The `License` field is the whole BSD 3-Clause text rather than an identifier.
    "scrapling": "BSD-3-Clause",
    # No License-Expression; the only statement is the `License :: OSI Approved :: MIT License` classifier.
    "jaraco.classes": "MIT",
}

_SPDX = re.compile(r"[A-Za-z0-9.+-]+")


class LockError(RuntimeError):
    """A lock that must not be written, with the sentence that says why."""


@dataclass(frozen=True)
class Locked:
    name: str
    version: str
    sha256: str
    licence: str

    def line(self) -> str:
        return f"{self.name}=={self.version} --hash=sha256:{self.sha256}  # {self.licence}"


def licence_allowed(expression: str) -> bool:
    """Whether an SPDX expression can ship: every `AND` part allowed, any one `OR` alternative.

    `MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later` passes on its first alternative; an
    expression this cannot read (parentheses inside an `OR`, `WITH` exceptions) does not pass —
    a licence nobody can parse is a licence nobody has checked.
    """
    expression = expression.strip()
    if not expression:
        return False
    for part in (p.strip() for p in expression.split(" AND ")):
        part = part.removeprefix("(").removesuffix(")").strip()
        options = [o.strip() for o in part.split(" OR ")]
        if not all(_SPDX.fullmatch(o) for o in options):
            return False
        if not any(o in ALLOWED for o in options):
            return False
    return True


def licence_of(name: str, metadata: dict[str, Any]) -> str:
    """The licence a wheel declares, as an SPDX expression, or a LockError saying it has none."""
    if name in LICENCE_OVERRIDES:
        return LICENCE_OVERRIDES[name]
    expression = str(metadata.get("license_expression") or "").strip()
    if expression:
        return expression
    field = str(metadata.get("license") or "").strip()
    if field and "\n" not in field and len(field) <= 60 and all(_SPDX.fullmatch(t) for t in field.split() if t not in ("AND", "OR")):
        return field
    raise LockError(
        f"{name} declares no licence this can read; add it to LICENCE_OVERRIDES in "
        "scripts/lock_engine.py with where the licence was read"
    )


def from_report(report: dict[str, Any]) -> list[Locked]:
    """The lock's entries from pip's `--report`, sorted by name; refuses anything unhashed."""
    entries: list[Locked] = []
    for item in report.get("install", []):
        metadata = item["metadata"]
        name = str(metadata["name"])
        hashes = item.get("download_info", {}).get("archive_info", {}).get("hashes", {})
        sha256 = hashes.get("sha256")
        if not sha256:
            raise LockError(f"pip resolved {name} without a sha256, so it cannot be installed with --require-hashes")
        entries.append(Locked(name, str(metadata["version"]), str(sha256), licence_of(name, metadata)))
    refused = [e for e in entries if not licence_allowed(e.licence)]
    if refused:
        raise LockError(
            "these licences are not in ALLOWED: "
            + ", ".join(f"{e.name} ({e.licence})" for e in refused)
        )
    return sorted(entries, key=lambda e: e.name.lower())


def render(entries: Iterable[Locked], pin: bundle_engine.Pin) -> str:
    floor = ".".join(str(n) for n in bundle_engine.GLIBC_FLOOR)
    header = [
        f"# The engine's dependencies as the bundled CPython {pin.version} installs them, on glibc {floor} and newer.",
        "# Generated by `make lock-engine` (scripts/lock_engine.py) from engine/requirements.txt. Do not edit:",
        "# scripts/bundle_engine.py installs exactly these files, with --require-hashes.",
    ]
    return "\n".join([*header, *(e.line() for e in entries)]) + "\n"


def resolve(python: Path, pin: bundle_engine.Pin, report: Path) -> dict[str, Any]:
    argv = [
        str(python), "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet",
        "--disable-pip-version-check", "--no-input", "--report", str(report),
        *bundle_engine.target_flags(pin),
        "-r", str(REQUIREMENTS),
    ]
    done = subprocess.run(argv, timeout=RESOLVE_TIMEOUT_S, check=False)  # noqa: S603 — the pinned interpreter's own pip resolving engine/requirements.txt with --dry-run; no shell, nothing installed
    if done.returncode != 0:
        raise LockError(f"pip could not resolve engine/requirements.txt (exit {done.returncode})")
    data: dict[str, Any] = json.loads(report.read_text(encoding="utf-8"))
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--cache", type=Path, default=bundle_engine.DEFAULT_CACHE)
    parser.add_argument("--python-tarball", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        pin = bundle_engine.load_pin()
        archive = bundle_engine.fetch_python(pin, args.cache.resolve(), tarball=args.python_tarball)
        with tempfile.TemporaryDirectory(prefix="lock-engine-") as scratch:
            root = bundle_engine.extract_python(archive, Path(scratch))
            entries = from_report(resolve(root / "bin" / "python3", pin, Path(scratch) / "report.json"))
    except (bundle_engine.BundleError, LockError) as exc:
        print(f"lock-engine: {exc}", file=sys.stderr)
        return 1
    bundle_engine.LOCK_PATH.write_text(render(entries, pin), encoding="utf-8")
    print(f"lock-engine: {bundle_engine.LOCK_PATH} — {len(entries)} packages for CPython {pin.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
