"""Fetch a third-party repository for a benchmark task, deliberately and with a paper trail.

Nothing in this module runs unless a human types the command. That is the whole
licensing position: vendoring a repository puts someone else's code into this
repository's history, which is a decision a build script must not make on its
own. So the default is absence — `manifest.json` ships with `repos: []`, and a
task that needs a repository that has not been fetched fails with a message
saying exactly that.

Three things make a fetch acceptable, and each one is enforced here rather than
promised in a document:

* **A permissive licence, named by the operator.** The licence cannot be reliably
  detected from a tarball — it may live in a file the prune step drops, or be
  spelled four ways — so this tool does not guess. The operator states it, and a
  licence outside `PERMISSIVE_LICENSES` is refused outright. A copyleft or
  source-available licence is a decision for a human to make in the open, not a
  flag on a command line.
* **A pin.** A commit SHA, never a branch. A benchmark that re-measures a moving
  target is not a benchmark.
* **A reason.** `--why` is required, and it is written into the repository's
  NOTICE.md beside the upstream URL, the SHA, the licence and the date. A
  vendored snapshot with no recorded reason is a mystery blob in a diff.

`benchmarks/repos/` is git-ignored, so a fetched tree is present on the machine
that fetched it and nowhere else unless someone deliberately commits it. The
manifest entry is the reproducible half: upstream, SHA, subpath, licence and
reason are enough for anyone to recreate the snapshot with one command.

    python3 -m benchmarks.vendor --repo ows/algorithms@1a2b3c4 \
        --license MIT --subpath algorithms/sorting --why "cross-file rename task"

Pass `--source` to vendor from a local tarball or directory instead of the
network, which is what the tests use and what an air-gapped checkout needs.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = BENCH_DIR / "manifest.json"
REPOS_DIR = BENCH_DIR / "repos"
NOTICE_NAME = "NOTICE.md"

# SPDX ids only. `MIT`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC` and
# `0BSD` are the licences whose obligations a vendored read-only snapshot can
# meet with a NOTICE and an upstream link. Everything else — GPL, AGPL, LGPL,
# MPL, BUSL, "source available" — is out, because the cost of getting that
# judgement wrong is a licence obligation attached to this project's own
# distribution, and that judgement belongs to a person.
PERMISSIVE_LICENSES = frozenset({
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD",
})

# Dropped from every snapshot. Build output and editor noise inflate the diff
# and prove nothing; the second group is dropped because a vendored tree is
# read-only here, and a checked-in test suite that the benchmark never runs is
# an invitation to believe it did.
PRUNED_DIRS = frozenset({
    ".git", ".github", ".hg", ".svn", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    "target", "vendor", ".idea", ".vscode", "coverage",
})
PRUNED_SUFFIXES = (
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".class", ".jar",
    ".min.js", ".map", ".DS_Store",
)
# A single megabyte: a benchmark task reads and edits source, and a repository
# that cannot be vendored under this cap is a repository whose relevant part
# should be a committed fixture instead.
MAX_FILE_BYTES = 1_000_000
MAX_MEMBERS = 20_000


class VendorError(RuntimeError):
    """Anything that should stop a fetch with a reason rather than a traceback."""


def _reject_unsafe(name: str) -> None:
    """Refuse a member that would write outside the destination.

    `tarfile` has had path-traversal members in the wild, and a fetch that
    trusts its input is a fetch that can write into `~/.ssh`. The check is
    lexical and absolute-path based because that is all the member carries.
    """
    if name.startswith("/") or name.startswith("~"):
        raise VendorError(f"refusing absolute path in archive: {name!r}")
    parts = Path(name).parts
    if ".." in parts:
        raise VendorError(f"refusing parent-directory path in archive: {name!r}")
    if len(parts) > 1 and ":" in parts[0]:
        raise VendorError(f"refusing drive-qualified path in archive: {name!r}")


def _pruned(name: str) -> bool:
    parts = Path(name).parts
    if any(p in PRUNED_DIRS for p in parts):
        return True
    return name.endswith(PRUNED_SUFFIXES)


def _download(url: str, timeout_s: int) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310
            return bytes(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise VendorError(f"could not fetch {url}: {exc}") from exc


def extract_snapshot(
    data: bytes, dest: Path, *, subpath: str, sha: str
) -> list[str]:
    """Write the kept members of a GitHub-style tarball into `dest`.

    The archive's single top-level directory (`repo-sha`) is stripped, so a
    snapshot is the repository rather than a wrapper around it. Returns the
    repo-relative paths written, for the report.
    """
    written: list[str] = []
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError as exc:
        raise VendorError(f"not a readable .tar.gz: {exc}") from exc

    with archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise VendorError(
                f"archive has {len(members)} entries, over the {MAX_MEMBERS} cap — "
                "vendor a subtree with --subpath instead"
            )
        # The whole archive is vetted before a single byte is written. A member
        # that escapes, or one that is a symlink, is a reason to refuse the
        # fetch — not a member to skip past while writing the rest.
        for member in members:
            _reject_unsafe(member.name)
            if not member.isdir() and not member.isfile():
                raise VendorError(
                    f"refusing non-regular archive member: {member.name!r} "
                    "(symlinks and devices have no place in a read-only snapshot)"
                )

        roots = {Path(m.name).parts[0] for m in members if Path(m.name).parts}
        if len(roots) != 1:
            raise VendorError(
                f"expected one top-level directory in the archive, found {sorted(roots)[:4]}"
            )
        # One wrapper directory, and the loop below strips it: a snapshot is the
        # repository, not a directory named after it.
        roots.pop()

        for member in members:
            if member.isdir():
                continue
            parts = Path(member.name).parts[1:]
            rel = Path(*parts) if parts else Path(member.name)
            if subpath:
                if parts[:1] != (subpath,):
                    continue
                rel = Path(*parts[1:])
            rel_str = str(rel)
            if not rel_str or rel_str == "." or _pruned(rel_str):
                continue
            if member.size > MAX_FILE_BYTES:
                raise VendorError(
                    f"{rel_str} is {member.size} bytes, over the "
                    f"{MAX_FILE_BYTES} cap — vendor a subtree instead"
                )
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise VendorError(f"could not read {rel_str} from the archive")
            with source, out.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            written.append(rel_str)

    if not written:
        where = f" under {subpath!r}" if subpath else ""
        raise VendorError(
            f"the snapshot{where} kept no files — check --subpath against the "
            f"repository at {sha[:12]}"
        )
    return written


def write_notice(dest: Path, entry: dict[str, Any]) -> Path:
    """The provenance record, written beside the code it explains."""
    subpath = entry.get("subpath") or "(whole tree)"
    body = f"""# {entry["name"]} — vendored snapshot

This directory is **not** Codify source. It is a read-only slice of somebody
else's repository, kept here so a benchmark task has something real to work on.

| | |
|---|---|
| Upstream | <{entry["upstream"]}> |
| Commit | `{entry["sha"]}` |
| Licence | {entry["license"]} |
| Subtree | `{subpath}` |
| Fetched | {entry["fetched"]} (UTC) |
| Why | {entry["why"]} |

## How to recreate it

```
python3 -m benchmarks.vendor --repo {entry["name"]}@{entry["sha"]} \\
    --license {entry["license"]} --subpath {subpath} --why "<the reason above>"
```

## What was removed

Everything outside `{subpath}`, plus build output, VCS metadata, editor state and
`__pycache__` (`benchmarks/vendor.py`, `PRUNED_DIRS` / `PRUNED_SUFFIXES`), and
any file over {MAX_FILE_BYTES} bytes. Symlinks, devices and other non-regular
entries are refused rather than skipped: a snapshot that quietly dropped them
would not be the repository it claims to be.

## What this snapshot may not do

`benchmarks/repos/` is git-ignored, so this tree is present only on the machine
that fetched it. It is read-only input: the runner copies it into a scratch
directory (`benchmarks/runner.py`, `materialize`) and every task works there.
Nothing in the pipeline writes back into this directory.
"""
    path = dest / NOTICE_NAME
    path.write_text(body, encoding="utf-8")
    return path


def record_in_manifest(manifest_path: Path, entry: dict[str, Any]) -> None:
    """Add or replace the repo's manifest entry, keeping the list sorted."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repos = [
        r for r in manifest.get("repos", [])
        if r.get("name") != entry["name"]
    ]
    repos.append(entry)
    manifest["repos"] = sorted(repos, key=lambda r: str(r.get("name")))
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def parse_spec(spec: str) -> tuple[str, str]:
    """`owner/repo@sha` -> (`owner/repo`, `sha`). A branch is not a pin."""
    if "@" not in spec:
        raise VendorError(
            f"{spec!r} has no @sha — a benchmark must pin a commit, not a branch"
        )
    upstream, _, sha = spec.partition("@")
    sha = sha.strip()
    if len(sha) < 7:
        raise VendorError(f"{spec!r} has no usable commit SHA")
    if not upstream or upstream.count("/") != 1:
        raise VendorError(
            f"{upstream!r} is not an owner/repo pair — e.g. pallets/click"
        )
    return upstream, sha


def vendor(
    spec: str,
    *,
    license_id: str,
    why: str,
    subpath: str = "",
    source: Path | None = None,
    root: Path = BENCH_DIR,
    manifest_path: Path | None = None,
    timeout_s: int = 60,
    now: str | None = None,
) -> dict[str, Any]:
    """Fetch, prune, notice and record one repository. Returns the manifest entry."""
    if license_id not in PERMISSIVE_LICENSES:
        raise VendorError(
            f"licence {license_id!r} is not on the permissive list "
            f"({', '.join(sorted(PERMISSIVE_LICENSES))}). Vendoring a copyleft or "
            "source-available project attaches obligations to Codify's own "
            "distribution; that is a decision for a person, made in the open."
        )
    why = why.strip()
    if not why:
        raise VendorError("--why is required: a vendored tree with no recorded reason "
                          "is an unexplainable blob in the next diff")
    upstream, sha = parse_spec(spec)
    name = upstream.rpartition("/")[2]

    if source is not None:
        if source.is_dir():
            source = _tar_dir(source)
        if not source.is_file():
            raise VendorError(f"--source {source} is not a file")
        data = source.read_bytes()
        origin = f"local archive {source}"
    else:
        url = f"https://codeload.github.com/{upstream}/tar.gz/{sha}"
        data = _download(url, timeout_s)
        origin = url

    dest = root / "repos" / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    try:
        written = extract_snapshot(data, dest, subpath=subpath, sha=sha)
    except VendorError:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    entry = {
        "name": name,
        "upstream": f"https://github.com/{upstream}",
        "sha": sha,
        "license": license_id,
        "subpath": subpath,
        "why": why,
        "fetched": now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
        "files": len(written),
        "source": origin,
    }
    write_notice(dest, entry)
    record_in_manifest(manifest_path or root / "manifest.json", entry)
    return entry


def _tar_dir(directory: Path) -> Path:
    """Pack a local directory in the shape `extract_snapshot` expects."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=f"{directory.name}/{path.relative_to(directory)}")
    packed = directory.parent / f"{directory.name}.tar.gz"
    packed.write_bytes(buffer.getvalue())
    return packed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Vendor a permissively licensed repository for a benchmark task.",
    )
    parser.add_argument("--repo", required=True, help="owner/repo@<commit-sha>")
    parser.add_argument(
        "--license", required=True, dest="license_id",
        help="SPDX id of the upstream licence; must be permissive (see docs/08)",
    )
    parser.add_argument("--why", required=True, help="why this task needs this repository")
    parser.add_argument(
        "--subpath", default="", help="subtree to keep, e.g. src/thing (default: whole tree)"
    )
    parser.add_argument(
        "--source", type=Path, default=None,
        help="vendor from a local .tar.gz or directory instead of the network",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)

    try:
        entry = vendor(
            args.repo,
            license_id=args.license_id,
            why=args.why,
            subpath=args.subpath,
            source=args.source,
            manifest_path=args.manifest,
        )
    except VendorError as exc:
        print(f"vendor: {exc}", file=sys.stderr)
        return 2

    print(f"vendored  {entry['name']}  {entry['sha'][:12]}  {entry['license']}  "
          f"{entry['files']} files")
    print(f"into      {REPOS_DIR / entry['name']} (git-ignored; notice written)")
    print(f"recorded  {args.manifest}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
