"""Print a tag's release notes, and refuse a tag the version files disagree with.

Three files carry Codify's version by hand and a fourth — the tag — is what a human types:

| File | Who reads it |
|---|---|
| `src-tauri/tauri.conf.json` | the About tab (`package_info`) and the update check |
| `src-tauri/Cargo.toml` | the binary's own name and version |
| `ui/package.json` | npm, and nothing at runtime |

Nothing compared them until this script. A release whose tag says `v1.0.0` while
`tauri.conf.json` still says `0.2.0` is worse than a missing release: every copy of that build
compares its version against the latest tag, so it would offer an update to the thing it
already is, forever, and pressing the button would download the same version again. That is
the failure this exists to make impossible, and it is checked *before* anything is built — the
release workflow runs this first, so a tag that disagrees stops the run in seconds.

The notes are generated rather than written by hand for the same reason they are generated at
all: a file name in a release page that the release does not contain is a broken download for
whoever clicks it, and the one place those names are known is here.

    python3 scripts/release_notes.py v1.0.0                 # notes on stdout
    python3 scripts/release_notes.py v1.0.0 --root /some/repo

Exit status is 0 with the notes on stdout, or 1 with the disagreement on stderr and nothing
on stdout, so a caller can pipe the notes into a file without a failure producing a half-file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = "github.com/GeneticxCln/Codify"

# The three files, in the order a person would look at them.
VERSION_FILES = ("src-tauri/tauri.conf.json", "src-tauri/Cargo.toml", "ui/package.json")

# The `[package]` version in a Cargo.toml: at the start of a line, so a dependency's inline
# `{ version = "2" }` and the `rust-version` key below it are both excluded.
CRATE_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)

# The architecture Tauri names its Linux bundles with, and the only one this repository
# builds: the release workflow is pinned to an `ubuntu-22.04` x86_64 runner (docs/18 §3). A
# build on another architecture needs this line and that workflow changed together.
ARCH = "amd64"


def versions(root: Path) -> list[tuple[str, str]]:
    """The version each of the three files carries, as `(path, version)`.

    A file that is missing, unreadable or has no version is a failure rather than a skipped
    entry: "two of the three agree" is not a state this repository should be able to reach,
    and a script that shrugged at a missing file would call a delete a pass.
    """
    found: list[tuple[str, str]] = []
    for name in VERSION_FILES:
        path = root / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise SystemExit(f"release_notes: cannot read {name} ({error})") from error
        if name.endswith(".json"):
            try:
                version = json.loads(text).get("version")
            except json.JSONDecodeError as error:
                raise SystemExit(f"release_notes: {name} is not JSON ({error})") from error
        else:
            match = CRATE_VERSION.search(text)
            version = match.group(1) if match else None
        if not isinstance(version, str) or not version.strip():
            raise SystemExit(f"release_notes: {name} carries no version")
        found.append((name, version.strip()))
    return found


def tag_version(tag: str) -> str:
    """The version a tag names: `v1.0.0` and `1.0.0` are the same release, so both are accepted."""
    return tag[1:] if tag.startswith("v") else tag


def disagreements(tag: str, found: list[tuple[str, str]]) -> list[str]:
    """Everything about this tag and these versions that does not add up, one sentence each."""
    wanted = tag_version(tag)
    if not re.fullmatch(r"\d+(\.\d+)*", wanted):
        return [
            f"the tag {tag!r} does not name a version, so a package built from it could not be "
            f"compared with any release"
        ]
    return [
        f"{name} says {version}, and the tag says {wanted}: set all three files to {wanted} and "
        f"re-tag, or publish the version the files already carry"
        for name, version in found
        if version != wanted
    ]


def notes(tag: str, version: str) -> str:
    """The release page's body for `tag`, naming exactly the files the workflow publishes."""
    deb = f"Codify_{version}_{ARCH}.deb"
    appimage = f"Codify_{version}_{ARCH}.AppImage"
    return f"""Codify {version} for Linux ({ARCH}), built from `{tag}` on Ubuntu 22.04.

| File | What it is |
|---|---|
| `{deb}` | A Debian/Ubuntu package. Needs a package manager to install it. |
| `{appimage}` | One self-contained file. Needs only `chmod +x`. |
| `SHA256SUMS` | The checksums of the two above. |

```bash
sudo apt install ./{deb}
chmod +x ./{appimage} && ./{appimage}
sha256sum -c SHA256SUMS
```

**Each package carries the engine** and the Python it runs on, so neither needs a checkout or a
Python of yours. Both need `git`, which the deb installs for you. To run a checkout's engine
instead — your own changes, or the Laya gate's SDK, which the packages leave out — start the app
with `CODIFY_ROOT=/path/to/Codify`; `{REPO}` has the three commands that set a checkout up.

**Nothing here updates itself.** The app's About tab asks GitHub for the newest release when a
person presses Check for updates, and can download the package for the copy they are running —
it installs nothing, runs nothing and replaces no binary. The commands above are yours to run.
(`docs/18` §4 in the repository says the same in more words.)
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("tag", help="the release tag, e.g. v1.0.0")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="the checkout to read the version files from (default: the working directory)",
    )
    args = parser.parse_args(argv)

    found = versions(args.root)
    problems = disagreements(args.tag, found)
    if problems:
        for line in problems:
            print(f"release_notes: {line}", file=sys.stderr)
        return 1
    # Every file agrees, so the version is any of them — the first, which is the one the About
    # tab prints and therefore the one a person will see.
    print(notes(args.tag, found[0][1]), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
