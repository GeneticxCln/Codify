"""Name the dependencies `pyproject.toml` declares that this interpreter has not installed.

`make doctor` runs this with the checkout's own `.venv/bin/python3`, because "is the machine
ready" and "is this checkout's environment current" are different questions: a `.venv` made
before a dev dependency was added keeps working, and keeps warning, and nothing says why.
`make setup` re-installs into the existing `.venv`, which is the fix this points at.

Presence only, not versions. Reading the version specifiers needs `packaging`, which is not
in the standard library, and a doctor that imports a third-party package to say a third-party
package is missing has picked the wrong tool. Missing is the failure that actually happened.

Plain text rather than `tomllib`: the declared floor is 3.10 and `tomllib` starts at 3.11
(the same reason `tests/test_dependency_parity.py` parses by hand). `declared` raises when it
finds nothing, so a renamed key fails loudly instead of reporting an empty, healthy list.

    python3 scripts/venv_gaps.py pyproject.toml     # exit 0 nothing missing, 1 something is, 2 unreadable
"""
from __future__ import annotations

import re
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

_COMMENT = re.compile(r"#.*")
# The two arrays that install something: `[project] dependencies` and the `dev` extra.
_ARRAYS = (r"^dependencies\s*=\s*\[(.*?)^\]", r"^dev\s*=\s*\[(.*?)^\]")
_NAME_END = re.compile(r"[<>=!~\[;@ ]")


def declared(text: str) -> list[str]:
    """Distribution names from the runtime dependencies and the `dev` extra, in file order."""
    names: list[str] = []
    for pattern in _ARRAYS:
        match = re.search(pattern, text, re.S | re.M)
        if not match:
            continue
        for spec in re.findall(r'"([^"]+)"', _COMMENT.sub("", match.group(1))):
            names.append(_NAME_END.split(spec.strip(), 1)[0])
    if not names:
        raise ValueError("no `dependencies` or `dev` array found")
    return names


def is_installed(name: str) -> bool:
    try:
        metadata.version(name)
    except metadata.PackageNotFoundError:
        return False
    return True


def missing(names: list[str], *, installed: Callable[[str], bool] = is_installed) -> list[str]:
    return [name for name in names if not installed(name)]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: venv_gaps.py PYPROJECT_TOML\n")
        return 2
    try:
        names = declared(Path(args[0]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"venv_gaps: {args[0]} declares no dependencies I can read ({exc})\n")
        return 2
    gaps = missing(names)
    for name in gaps:
        sys.stdout.write(f"{name}\n")
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
