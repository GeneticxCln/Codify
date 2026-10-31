"""Check a package you downloaded against the release's own SHA256SUMS.

The About tab downloads a package and hands over the command to install it. What it cannot answer is
whether the file that landed is the file that was published: a download can be truncated by a flaky
connection, a partially-written file looks exactly like a whole one to `apt`, and an asset can be
re-uploaded after the release page was read. The release workflow writes `sha256sum` output into a
`SHA256SUMS` asset beside the packages (`docs/18` §3), and this compares the two.

    python3 scripts/verify_package.py                       # what is in ~/Downloads
    python3 scripts/verify_package.py ~/Downloads/Codify_1.0.0_amd64.deb
    python3 scripts/verify_package.py --sums SHA256SUMS dist/Codify_*.deb
    python3 scripts/verify_package.py --tag v1.0.0

**What a pass claims, and what it does not.** It claims the bytes on disk are the bytes that
release's `SHA256SUMS` lists — so a truncated, corrupted or re-uploaded file is caught here, before
`sudo apt install`. It is not a signature, and it does not say the release is authentic by itself:
the checksums come from the same place the package did, over the same connection, so anyone who could
serve a wrong package could serve matching checksums for it. Authenticity is the release's
build-provenance attestation (`gh attestation verify`, `docs/18` §5).

Exit status is 0 when every file matched, 1 when any file did not (or could not be read), and 2 when
there was nothing to check — no package named and none found to look at.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

BASE = "https://github.com/GeneticxCln/Codify/releases"

# The two shapes a release's checksums are at, as GitHub serves them. `latest` is the same endpoint
# the app's own update check reads: the newest release that is neither a draft nor a pre-release.
LATEST_SUMS = f"{BASE}/latest/download/SHA256SUMS"
TAGGED_SUMS = f"{BASE}/download/{{tag}}/SHA256SUMS"

# A tag this script will put in an address. Written out rather than trusted: the tag is concatenated
# into a URL, and `--tag ../../etc/passwd` is a path a person mistyped, not a version.
TAG = re.compile(r"\Av?[0-9][0-9A-Za-z._-]*\Z")

# One line of `sha256sum` output: a digest, two spaces (or a `*` for binary mode), and a name.
SUMS_LINE = re.compile(r"\A([0-9a-fA-F]{64}) [ *](.+)\Z")

# What this repository publishes: Tauri names the bundles, and nothing else in a downloads folder is
# this app's to check. `PACKAGE_GLOBS` is what "no package named" looks for.
PACKAGE_GLOBS = ("Codify_*.deb", "Codify_*.AppImage")

# The read size for hashing a file. A package is tens of megabytes, and there is no reason to hold
# one in memory to take its digest.
BLOCK = 8 * 1024 * 1024


def parse_sums(text: str) -> dict[str, str]:
    """`{file name: digest}` from the text of a `SHA256SUMS` file, digests lowercased.

    A line that is not `sha256sum` output is refused with its number rather than skipped: a
    checksums file that has been mangled — half pasted, or with a header someone added — would
    otherwise read as "this file is not listed", which sends the reader to re-download a package
    that was fine. An empty answer is refused for the opposite reason: comparing against nothing
    would report every file as unlisted, and that is not a checksum failure.
    """
    sums: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        match = SUMS_LINE.match(line)
        if match is None:
            raise ValueError(
                f"line {number} of the checksums is not `<sha256>  <name>`, so the file is not "
                f"`sha256sum` output: {line!r}"
            )
        sums[match.group(2)] = match.group(1).lower()
    if not sums:
        raise ValueError("the checksums list no files, so there is nothing to compare against")
    return sums


def digest_of(path: Path) -> str:
    """The sha256 of one file, read in blocks, or a sentence saying why it could not be read."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(BLOCK):
                digest.update(block)
    except OSError as error:
        raise ValueError(f"{path} could not be read ({error})") from error
    return digest.hexdigest()


def sums_url(tag: str | None) -> str:
    """Where a release's checksums are, or a sentence refusing a tag that is not one.

    A version without the leading `v` gets one, because that is how this repository's releases are
    tagged (`release.yml` triggers on `tags: ["v*"]`), so `--tag 1.0.0` and `--tag v1.0.0` are one
    release and a person should not have to know which spelling it was published under.
    """
    if tag is None:
        return LATEST_SUMS
    if not TAG.match(tag):
        raise ValueError(
            f"{tag!r} is not a release tag (they look like `v1.0.0`), so no address was built from it"
        )
    return TAGGED_SUMS.format(tag=tag if tag.startswith("v") else f"v{tag}")


def fetch_sums(url: str, timeout: float = 30.0) -> str:
    """The checksums at `url`, as text, or a sentence naming what went wrong."""
    try:
        # `https`, an address this repository's releases are at, and a tag already checked against
        # TAG — which is the whole of the reason this is safe to open from here.
        with urlopen(url, timeout=timeout) as answer:  # noqa: S310
            body: bytes = answer.read()
            return body.decode("utf-8", "replace")
    except (HTTPError, URLError, OSError) as error:
        raise ValueError(f"could not read the checksums from {url} ({error})") from error


def downloads_dir(named: str | None) -> Path:
    """Where to look when no package was named: what was passed, or the person's own downloads."""
    if named is not None:
        return Path(named).expanduser()
    return Path.home() / "Downloads"


def packages_in(directory: Path) -> list[Path]:
    """The Codify packages in `directory`, newest name last, and nothing else in it."""
    found: list[Path] = []
    for pattern in PACKAGE_GLOBS:
        found.extend(sorted(directory.glob(pattern)))
    return found


@dataclass(frozen=True)
class Result:
    """What one file turned out to be. `problem` is `None` exactly when it matched."""

    path: Path
    digest: str | None
    problem: str | None


def verify(files: Sequence[Path], sums: Mapping[str, str]) -> list[Result]:
    """Check each file against the release's own digest, one `Result` each.

    Every failure is a sentence for the person reading it, because the three cases need three
    different actions: a file that is not there, a file the release never listed (a different
    release, or a browser's `(1)` suffix), and a file whose bytes are not the published ones.
    """
    results: list[Result] = []
    for path in files:
        if not path.is_file():
            results.append(
                Result(path, None, f"there is no file at {path}, so there is nothing to check")
            )
            continue
        expected = sums.get(path.name)
        if expected is None:
            results.append(
                Result(
                    path,
                    None,
                    f"the release's SHA256SUMS does not list {path.name}, so this file is not one "
                    f"the release published — check that the name is exactly as it was published",
                )
            )
            continue
        try:
            digest = digest_of(path)
        except ValueError as error:
            results.append(Result(path, None, str(error)))
            continue
        if digest != expected:
            results.append(
                Result(
                    path,
                    digest,
                    f"{path.name} is not the published file: the release's SHA256SUMS has "
                    f"{expected}, and this file is {digest}",
                )
            )
            continue
        results.append(Result(path, digest, None))
    return results


def _fail(message: str, code: int = 1) -> int:
    print(f"verify_package: {message}", file=sys.stderr)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_package.py",
        description="Check downloaded Codify packages against a release's SHA256SUMS.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        metavar="PACKAGE",
        help="the package(s) to check; with none, whatever is in the downloads folder",
    )
    parser.add_argument(
        "--sums",
        metavar="PATH",
        help="a SHA256SUMS file you already have, instead of fetching the release's",
    )
    parser.add_argument(
        "--tag",
        metavar="TAG",
        help="the release to check against (default: the newest one, as the app's own check reads)",
    )
    parser.add_argument(
        "--downloads",
        metavar="DIR",
        help="where to look when no package is named (default: ~/Downloads)",
    )
    args = parser.parse_args(argv)

    try:
        if args.sums:
            source = Path(args.sums).expanduser()
            text = source.read_text(encoding="utf-8")
            where = str(source)
        else:
            url = sums_url(args.tag)
            text = fetch_sums(url)
            where = url
        sums = parse_sums(text)
    except (ValueError, OSError) as error:
        return _fail(str(error))

    if args.files:
        files = [Path(name).expanduser() for name in args.files]
    else:
        directory = downloads_dir(args.downloads)
        files = packages_in(directory)
        if not files:
            return _fail(
                f"no package was named and none matching {' or '.join(PACKAGE_GLOBS)} is in "
                f"{directory}: name one, or pass --downloads DIR",
                code=2,
            )

    print(f"verify_package: {len(files)} package(s) against {where}")
    results = verify(files, sums)
    failed = [result for result in results if result.problem is not None]
    for result in results:
        if result.problem is None:
            print(f"  ok    {result.path.name}  {result.digest}")
        else:
            print(f"  FAIL  {result.problem}")
    if failed:
        return _fail(
            f"{len(failed)} of {len(results)} package(s) did not match the release's own checksums",
            code=1,
        )
    print(
        "verify_package: every file is the bytes that release published. This is not a signature — "
        "the checksums came from the same place the package did — so it catches a truncated, "
        "corrupted or re-uploaded file. For authenticity, check the release's build-provenance "
        "attestation with `gh attestation verify` (docs/18 §5)."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
